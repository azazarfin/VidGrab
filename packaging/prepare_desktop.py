"""Copy a staged Linux spike into Tauri's ignored resource directories."""

import argparse
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-unknown-linux-gnu"


def prepare(bundle, tauri_dir):
    bundle = Path(bundle).resolve()
    tauri_dir = Path(tauri_dir).resolve()
    if bundle.is_relative_to(tauri_dir) or tauri_dir.is_relative_to(bundle):
        raise ValueError("The bundle and Tauri resource paths must not overlap")
    engine = bundle / "engine"
    native = bundle / "vidgrab-native-host"
    required = (engine / "vidgrab-engine", engine / "_internal" / "tools" / "manifest.json", native)
    if not all(path.is_file() for path in required):
        raise ValueError("Use a complete staged Linux bundle with its frozen engine and native host")
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
    shutil.copy2(native, binaries / f"vidgrab-native-host-{TARGET}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        prepare(arguments.bundle, ROOT / "desktop" / "src-tauri")
    except ValueError as error:
        parser.error(str(error))
    print(f"Prepared the complete frozen engine and {TARGET} native host for Tauri")


if __name__ == "__main__":
    main()
