"""Finite metadata-only playlist expansion for the owned worker boundary."""

import json
import math
import re
from urllib.parse import urlsplit

from backend.sanitizer import sanitize_text

MAX_CHILDREN = 128
MAX_POSITION = 4096
MAX_PLAN_BYTES = 49152


class PlaylistError(ValueError):
    """A permanent selection or metadata rejection; never retry automatically."""


def enforce_single_item(ydl):
    original = ydl.process_ie_result
    def process(info, *args, **kwargs):
        if isinstance(info, dict) and (info.get("_type") in ("playlist", "multi_video") or "entries" in info):
            raise PlaylistError("Media jobs cannot download nested playlists; enqueue a playlist parent")
        return original(info, *args, **kwargs)
    ydl.process_ie_result = process


def parse_selection(selection):
    if selection is None:
        return None
    if not isinstance(selection, str) or not selection or len(selection) > 4096:
        raise PlaylistError("Playlist selection must be a bounded positive item list")
    positions = set()
    for part in selection.split(","):
        if re.fullmatch(r"[1-9][0-9]*(?:-[1-9][0-9]*)?", part) is None:
            raise PlaylistError("Use positive playlist items or ascending ranges")
        bounds = [int(value) for value in part.split("-")]
        start, end = bounds[0], bounds[-1]
        if not 1 <= start <= end <= MAX_POSITION or end - start >= MAX_CHILDREN:
            raise PlaylistError("Playlist positions or selected item count exceed their limits")
        positions.update(range(start, end + 1))
        if len(positions) > MAX_CHILDREN:
            raise PlaylistError("Select at most 128 playlist items")
    return tuple(sorted(positions))


def media_url(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2048 or any(ord(char) <= 32 for char in value):
        raise PlaylistError("Playlist child URL is invalid or too large")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        parsed.port
    except ValueError as error:
        raise PlaylistError("Playlist children require explicit HTTP(S) URLs without user information") from error
    return value


def validate_playlist_result(result):
    if not isinstance(result, dict) or set(result) != {"playlist"}:
        raise PlaylistError("Playlist metadata cannot contain media outputs")
    plan = result["playlist"]
    if not isinstance(plan, dict) or set(plan) != {"entries"} or not isinstance(plan["entries"], list):
        raise PlaylistError("Invalid playlist metadata envelope")
    if not 1 <= len(plan["entries"]) <= MAX_CHILDREN:
        raise PlaylistError("A playlist must select between 1 and 128 media items")
    normalized = []
    previous = 0
    for entry in plan["entries"]:
        if not isinstance(entry, dict) or not set(entry) <= {"position", "url", "title", "thumbnail_url", "duration", "referer"}:
            raise PlaylistError("Nested or unsupported playlist metadata")
        position = entry.get("position")
        if type(position) is not int or not previous < position <= MAX_POSITION:
            raise PlaylistError("Playlist positions must be unique, ordered and bounded")
        previous = position
        title = entry.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 512):
            raise PlaylistError("Playlist title exceeds its limit")
        duration = entry.get("duration")
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
            raise PlaylistError("Invalid playlist duration")
        thumbnail = entry.get("thumbnail_url")
        normalized.append({"position": position, "url": media_url(entry.get("url")),
                           "title": sanitize_text(title), "thumbnail_url": media_url(thumbnail) if thumbnail else None,
                           "duration": duration, "referer": media_url(entry["referer"]) if entry.get("referer") else None})
    canonical = {"playlist": {"entries": normalized}}
    if len(json.dumps(canonical, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_PLAN_BYTES:
        raise PlaylistError("Playlist metadata exceeds its 48 KiB limit")
    return canonical


def extract_playlist(ydl, url, selection, is_cancelled):
    selected = parse_selection(selection)
    info = ydl.extract_info(url, download=False, process=False)
    if not isinstance(info, dict) or info.get("_type") != "playlist" or "entries" not in info:
        raise PlaylistError("Expected a finite playlist with explicit media entries")
    entries = []
    last = max(selected) if selected else MAX_POSITION
    iterator = iter(info["entries"])
    for position in range(1, last + 1):
        if is_cancelled():
            raise PlaylistError("Playlist expansion stopped")
        try:
            entry = next(iterator)
        except StopIteration:
            break
        if selected is not None and position not in selected:
            continue
        if not entry:
            if selected is not None:
                raise PlaylistError("A selected playlist entry is unavailable")
            continue
        if not isinstance(entry, dict) or entry.get("_type") in ("playlist", "multi_video") or "entries" in entry:
            raise PlaylistError("Nested playlists are unsupported")
        child_url = entry.get("url")
        referer = None
        if not child_url and entry.get("formats"):
            formats = entry["formats"]
            if not isinstance(formats, list) or len(formats) != 1 or not isinstance(formats[0], dict):
                raise PlaylistError("Playlist entry needs an independently resolvable URL or one explicit source")
            child_url = formats[0].get("url")
            headers = formats[0].get("http_headers") or {}
            if not isinstance(headers, dict):
                raise PlaylistError("Invalid playlist source headers")
            referer = headers.get("Referer")
        child_url = child_url or entry.get("webpage_url")
        entries.append({"position": position, "url": child_url, "referer": referer,
                        "title": entry.get("title"), "thumbnail_url": entry.get("thumbnail"), "duration": entry.get("duration")})
        # Validate incrementally so lazy extractors cannot build an unbounded plan.
        validate_playlist_result({"playlist": {"entries": entries}})
    if selected is not None and tuple(entry["position"] for entry in entries) != selected:
        raise PlaylistError("Selected playlist entries are missing")
    if selected is None:
        try:
            next(iterator)
        except StopIteration:
            pass
        else:
            raise PlaylistError("Playlist scanning exceeds 4096 positions; select explicit items")
    return validate_playlist_result({"playlist": {"entries": entries}})
