"""Explicit build-time downloads; engine startup never fetches components."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
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


def extract_ffmpeg(archive, directory):
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
    parser = argparse.ArgumentParser(description="Stage the pinned Linux media tools")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    arguments = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("The component lock currently describes Linux x86_64 only")
    lock = json.loads(Path(__file__).with_name("components.lock.json").read_text())
    assets = {name: cached_asset(item, arguments.cache, offline=arguments.offline) for name, item in lock["assets"].items()}
    arguments.output.mkdir(parents=True, exist_ok=True)
    extract_ffmpeg(assets["ffmpeg"], arguments.output)
    with zipfile.ZipFile(assets["deno"]) as source:
        (arguments.output / "deno").write_bytes(source.read("deno"))
    (arguments.output / "deno").chmod(0o755)
    manifest = {
        name: {"sha256": hashlib.sha256((arguments.output / name).read_bytes()).hexdigest(),
               "version": lock["assets"]["ffmpeg" if name == "ffprobe" else name]["version"]}
        for name in ("ffmpeg", "ffprobe", "deno")
    }
    (arguments.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
