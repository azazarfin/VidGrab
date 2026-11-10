"""Explicit build-time downloads; engine startup never fetches components."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import stat
import tarfile
import urllib.request
import zipfile


def cached_asset(item, cache, *, offline=False):
    path = cache / item["file"]
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"Missing cached component: {path.name}")
        cache.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(item["url"], headers={"User-Agent": "VidGrab-P02-build"})
        with urllib.request.urlopen(request, timeout=60) as response, path.open("xb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise ValueError(f"Component checksum mismatch: {path.name}")
    return path


def extract_zip_selection(archive, directory, selection):
    with zipfile.ZipFile(archive) as source:
        selected = []
        for name, destination in selection:
            matches = [entry for entry in source.infolist() if not entry.is_dir()
                       and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG)
                       and PurePosixPath(entry.filename).name == name]
            if len(matches) != 1:
                raise ValueError(f"Expected one regular {name} in the pinned archive")
            selected.append((matches[0], destination))
        for entry, destination in selected:
            (directory / destination).write_bytes(source.read(entry))


def extract_ffmpeg(archive, directory, *, target="x86_64-unknown-linux-gnu"):
    if target == "x86_64-pc-windows-msvc":
        extract_zip_selection(archive, directory, (("ffmpeg.exe", "ffmpeg.exe"), ("ffprobe.exe", "ffprobe.exe"), ("LICENSE", "GPLv3.ffmpeg.txt")))
        return
    if target != "x86_64-unknown-linux-gnu":
        raise ValueError("Unsupported FFmpeg target")
    # Select regular files by exact basename, rather than extracting tar paths.
    with tarfile.open(archive, "r:xz") as source:
        for name, destination in (("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe"), ("GPLv3.txt", "GPLv3.ffmpeg.txt")):
            matches = [entry for entry in source.getmembers() if entry.isfile() and Path(entry.name).name == name]
            if len(matches) != 1:
                raise ValueError(f"Expected one regular {name} in the pinned archive")
            with source.extractfile(matches[0]) as contents:
                (directory / destination).write_bytes(contents.read())
            if name in ("ffmpeg", "ffprobe"):
                (directory / destination).chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description="Stage the pinned native media tools")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--lock", type=Path)
    arguments = parser.parse_args()
    targets = {"Linux": "x86_64-unknown-linux-gnu", "Windows": "x86_64-pc-windows-msvc"}
    target = targets.get(platform.system())
    if target is None or platform.machine().lower() not in ("x86_64", "amd64"):
        parser.error("Pinned media tools support Linux and Windows x86_64")
    selected_lock = arguments.lock or Path(__file__).with_name("windows.components.lock.json" if platform.system() == "Windows" else "components.lock.json")
    lock = json.loads(selected_lock.read_text())
    if lock.get("schema") != 1 or lock.get("target") != target:
        parser.error("The component lock must match this native build target")
    assets = {name: cached_asset(item, arguments.cache, offline=arguments.offline) for name, item in lock["assets"].items()}
    arguments.output.mkdir(parents=True, exist_ok=True)
    extract_ffmpeg(assets["ffmpeg"], arguments.output, target=target)
    suffix = ".exe" if platform.system() == "Windows" else ""
    extract_zip_selection(assets["deno"], arguments.output, (("deno" + suffix, "deno" + suffix),))
    (arguments.output / ("deno" + suffix)).chmod(0o755)
    manifest = {
        name: {"sha256": hashlib.sha256((arguments.output / (name + suffix)).read_bytes()).hexdigest(),
               "version": lock["assets"]["ffmpeg" if name == "ffprobe" else name]["version"]}
        for name in ("ffmpeg", "ffprobe", "deno")
    }
    (arguments.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
