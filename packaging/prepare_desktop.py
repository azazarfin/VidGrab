"""Copy a staged native spike into Tauri's ignored resource directories."""

import argparse
from pathlib import Path
import shutil
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-unknown-linux-gnu"
WINDOWS_TARGET = "x86_64-pc-windows-msvc"


def prepare(bundle, tauri_dir, *, target=TARGET):
    if target not in (TARGET, WINDOWS_TARGET):
        raise ValueError("Unsupported native bundle target")
    suffix = ".exe" if target == WINDOWS_TARGET else ""
    bundle = Path(bundle).resolve()
    tauri_dir = Path(tauri_dir).resolve()
    if bundle.is_relative_to(tauri_dir) or tauri_dir.is_relative_to(bundle):
        raise ValueError("The bundle and Tauri resource paths must not overlap")
    engine = bundle / "engine"
    native = bundle / ("vidgrab-native-host" + suffix)
    required = (engine / ("vidgrab-engine" + suffix), engine / "_internal" / "tools" / "manifest.json", native)
    if not all(path.is_file() for path in required):
        raise ValueError("Use a complete staged native bundle with its frozen engine and native host")
    resources = tauri_dir / "resources"
    destination = resources / "engine"
    if destination.is_symlink():
        raise ValueError("The Tauri engine resource directory must not be a symlink")
    resources.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".engine-", dir=resources) as scratch:
        staged = Path(scratch) / "engine"
        shutil.copytree(engine, staged)
        if destination.exists():
            shutil.rmtree(destination)
        staged.rename(destination)
    binaries = tauri_dir / "binaries"
    binaries.mkdir(parents=True, exist_ok=True)
    shutil.copy2(native, binaries / f"vidgrab-native-host-{target}{suffix}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--target", choices=(TARGET, WINDOWS_TARGET), default=WINDOWS_TARGET if sys.platform == "win32" else TARGET)
    arguments = parser.parse_args()
    try:
        prepare(arguments.bundle, ROOT / "desktop" / "src-tauri", target=arguments.target)
    except ValueError as error:
        parser.error(str(error))
    print(f"Prepared the complete frozen engine and {arguments.target} native host for Tauri")


if __name__ == "__main__":
    main()
