"""Locate and verify bundled tools without relying on the user's PATH."""

import hashlib
from importlib import metadata
from pathlib import Path
import json
import subprocess
import sys


def component_directory():
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Use the frozen executable for packaged component checks")
    return Path(sys._MEIPASS) / "tools"


def check_components(directory=None):
    directory = Path(directory) if directory is not None else component_directory()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    paths = {}
    versions = {}
    for name in ("ffmpeg", "ffprobe", "deno"):
        executable = directory / (name + (".exe" if sys.platform == "win32" else ""))
        if hashlib.sha256(executable.read_bytes()).hexdigest() != manifest[name]["sha256"]:
            raise RuntimeError(f"Bundled {name} checksum mismatch")
        result = subprocess.run(
            [str(executable), "--version" if name == "deno" else "-version"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        versions[name] = result.stdout.splitlines()[0]
        paths[name] = str(executable)
    import yt_dlp_ejs
    scripts = list(Path(yt_dlp_ejs.__file__).parent.rglob("*.js"))
    if not scripts or metadata.version("yt-dlp-ejs") != "0.8.0":
        raise RuntimeError("The frozen yt-dlp requires its matching EJS 0.8.0 scripts")
    versions.update({"python": sys.version.split()[0], "yt_dlp": metadata.version("yt-dlp"), "ejs": metadata.version("yt-dlp-ejs")})
    return paths, versions
