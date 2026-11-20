"""Redact request material at diagnostic sinks, preserving operational values."""
from importlib import metadata
from pathlib import Path
import re
import shutil
import sys
import tempfile
import traceback

_HEADERS = re.compile(r"\b(?:authorization|proxy-authorization|cookies?|set-cookie|referer|user[-_]agent|"
                      r"(?:http[-_]|request[-_])?headers|auth[-_]ref|probe[-_]id|credential[-_]url)"
                      r"['\"]?\s*[:=]", re.IGNORECASE)
# Punctuation is legal inside URLs; consume the whole non-whitespace token.
_URLS = re.compile(r"\bhttps?(?:://|%3a%2f%2f)\S+", re.IGNORECASE)
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_TOKEN_PARAM = re.compile(r"\b(?:access_)?token['\"]?\s*[:=]\s*\S+", re.IGNORECASE)


def redact_diagnostic(value):
    text = str(value)
    if _HEADERS.search(text):
        return "Request details omitted from diagnostics"
    redacted = _URLS.sub("[redacted URL]", text)
    redacted = _BEARER.sub("Bearer [redacted]", redacted)
    redacted = _TOKEN_PARAM.sub("token=[redacted]", redacted)
    return redacted


def redact_exception(error):
    return redact_diagnostic("".join(traceback.format_exception(error)))


def check_download_dir(download_dir):
    path = Path(download_dir).expanduser()
    writable = False
    if path.is_dir():
        try:
            with tempfile.TemporaryFile(dir=path):
                writable = True
        except OSError:
            writable = False
    target = path
    while target != target.parent and not target.exists():
        target = target.parent
    disk_free_bytes = None
    if target.exists():
        try:
            disk_free_bytes = int(shutil.disk_usage(target).free)
        except OSError:
            disk_free_bytes = None
    return {"writable": writable, "disk_free_bytes": disk_free_bytes}


def inspect_components(components, component_versions):
    checks = []
    versions = component_versions or {}
    comp_map = components or {}
    for name in ("ffmpeg", "ffprobe", "deno"):
        version_value = versions.get(name)
        if name in comp_map and comp_map[name]:
            binary = Path(comp_map[name])
            available = binary.is_file()
            error = None if available else f"{name} executable was not found"
        elif version_value:
            available = True
            error = None
        else:
            found = shutil.which(name)
            available = found is not None
            error = None if available else f"{name} executable was not found"
        checks.append({
            "name": name,
            "available": available,
            "version": redact_diagnostic(version_value) if version_value else None,
            "error": redact_diagnostic(error) if error else None,
        })
    for name, pkg in (("yt_dlp", "yt-dlp"), ("ejs", "yt-dlp-ejs")):
        ver = versions.get(name)
        error = None
        if not ver:
            try:
                ver = metadata.version(pkg)
            except metadata.PackageNotFoundError:
                ver = None
                error = f"{pkg} package is not installed"
        checks.append({
            "name": name,
            "available": ver is not None,
            "version": redact_diagnostic(ver) if ver else None,
            "error": redact_diagnostic(error) if error else None,
        })
    py_ver = versions.get("python") or sys.version.split()[0]
    checks.append({
        "name": "python",
        "available": True,
        "version": redact_diagnostic(py_ver),
        "error": None,
    })
    return checks
