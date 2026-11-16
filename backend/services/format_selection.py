"""Volatile probe witnesses and one selection repair within owned fresh extraction."""
from collections import OrderedDict
from dataclasses import dataclass, field
import re
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.services.control_errors import ControlError
from backend.services.retry_policy import RequestFailure


class FormatIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format_id: str = Field(min_length=1, max_length=1024)
    ext: str = Field(min_length=1, max_length=32)
    height: int | None = Field(default=None, ge=1, le=16384)
    width: int | None = Field(default=None, ge=1, le=16384)
    fps: float | None = Field(default=None, ge=0, le=1000, allow_inf_nan=False)
    video: bool
    audio: bool
    separate_audio: bool = False


@dataclass
class ProbeWitness:
    url: str = field(repr=False)
    identity: str | None = field(repr=False)
    formats: list = field(repr=False)
    expires_at: float


class ProbeWitnesses:
    def __init__(self, clock):
        self.clock = clock
        self.records = OrderedDict()

    def _prune(self):
        for identity, witness in list(self.records.items()):
            if witness.expires_at <= self.clock():
                del self.records[identity]

    def create(self, url, identity, formats, *, expires_at):
        self._prune()
        permitted = {"format_id", "ext", "height", "width", "resolution", "fps", "vcodec", "acodec"}
        rows = [{key: value for key, value in row.items() if key in permitted} for row in formats[:100]]
        reference = str(uuid4())
        self.records[reference] = ProbeWitness(url, identity, rows, expires_at)
        while len(self.records) > 128:
            self.records.popitem(last=False)
        return reference

    def resolve(self, reference, url, identity, selector):
        self._prune()
        witness = self.records.get(reference)
        if witness is None:
            raise ControlError("PROBE_EXPIRED", 410, "The format list expired. Refresh formats before downloading")
        if witness.url != url or witness.identity != identity:
            raise ControlError("PROBE_CONTEXT_SCOPE", 403, "Probe and download must use the same media and browser context")
        row = next((row for row in witness.formats if selector in (row["format_id"], row["format_id"] + "+bestaudio/best")), None)
        if row is None:
            raise ControlError("FORMAT_UNAVAILABLE", 422, "Choose a format from the current media probe")
        height, width = row.get("height"), row.get("width")
        resolution = str(row.get("resolution") or "")
        dimensions = re.fullmatch(r"(\d+)x(\d+)", resolution)
        vertical = re.fullmatch(r"(\d+)(?:p)?", resolution)
        if height is None and dimensions:
            width, height = map(int, dimensions.groups())
        elif height is None and vertical:
            height = int(vertical.group(1))
        video = row.get("vcodec") not in (None, "none") or bool(height)
        audio = row.get("acodec") not in (None, "none")
        if (video and height is None) or (not video and not audio):
            # Unknown quality never authorizes a best-effort substitution.
            raise ControlError("FORMAT_UNAVAILABLE", 422, "This format's quality cannot be verified. Choose a supported quality or app defaults")
        try:
            return FormatIntent(format_id=row["format_id"], ext=row["ext"], height=height, width=width,
                                fps=row.get("fps"), video=video, audio=audio,
                                separate_audio=selector.endswith("+bestaudio/best")).model_dump()
        except ValueError as error:
            raise ControlError("FORMAT_UNAVAILABLE", 422, "This format's quality exceeds the supported limits") from error

    def close(self):
        self.records.clear()


class FormatUnavailable(RequestFailure):
    def __init__(self):
        super().__init__("FORMAT_UNAVAILABLE")


@dataclass
class FormatRefresh:
    count: int = 0


def _equivalent(row, intent):
    video = row.get("vcodec") not in (None, "none") or bool(row.get("height"))
    audio = row.get("acodec") not in (None, "none")
    return (not row.get("has_drm") and row.get("ext") == intent.ext
            and video == intent.video and audio == intent.audio
            and (intent.height is None or row.get("height") == intent.height)
            and (intent.width is None or row.get("width") == intent.width)
            and (intent.fps is None or row.get("fps") == intent.fps))


def _selection_matches(selected, intent):
    if len(selected) != 1:
        return False
    parts = selected[0].get("requested_formats") or selected
    if not any(_equivalent(row, intent) for row in parts):
        return False
    return not intent.separate_audio or any(row.get("vcodec") == "none" and row.get("acodec") not in (None, "none") for row in parts)


def install_format_refresh(ydl, values):
    if values is None:
        return None
    intent = FormatIntent.model_validate(values)
    original = ydl._select_formats
    refresh = FormatRefresh()

    def select(formats, selector):
        rows = formats[:4096]
        if any(row.get("format_id") == intent.format_id and _equivalent(row, intent) for row in rows):
            selected = original(formats, selector)
            if _selection_matches(selected, intent):
                return selected
        if refresh.count:
            raise FormatUnavailable()
        refresh.count += 1
        replacement = next((row for row in reversed(rows) if _equivalent(row, intent)), None)
        if replacement is None:
            raise FormatUnavailable()
        expression = replacement["format_id"] + ("+bestaudio" if intent.separate_audio else "")
        selected = original(formats, ydl.build_format_selector(expression))
        if not _selection_matches(selected, intent):
            raise FormatUnavailable()
        return selected

    ydl._select_formats = select
    return refresh
