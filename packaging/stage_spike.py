"""Build the native spike into a clean, explicitly selected staging directory."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Build/stage the P02 native lifecycle spike")
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--desktop", type=Path, help="Optional already-built Tauri executable")
    parser.add_argument("--chrome-origin", default=os.environ.get("VIDGRAB_CHROME_ORIGIN"))
    parser.add_argument("--firefox-id", default=os.environ.get("VIDGRAB_FIREFOX_ID"))
    arguments = parser.parse_args()
    if sys.platform not in ("linux", "win32"):
        parser.error("The spike supports native Linux and Windows builds")
    suffix = ".exe" if sys.platform == "win32" else ""
    target = "x86_64-pc-windows-msvc" if sys.platform == "win32" else "x86_64-unknown-linux-gnu"
    output = arguments.output.resolve()
    if output.exists():
        parser.error("Choose a fresh output directory; staging never deletes an existing tree")
    if output.is_relative_to(ROOT) and not output.is_relative_to(ROOT / "packaging" / "out"):
        parser.error("Staging inside source is limited to the ignored packaging/out directory")
    for runtime in (arguments.data_dir.resolve(), arguments.download_dir.resolve()):
        if runtime.is_relative_to(ROOT) or runtime.is_relative_to(output):
            parser.error("Runtime and media paths must be outside source and the staged installation")
    started = time.monotonic()
    subprocess.run(["cargo", "build", "--locked", "--offline", "--release", "-p", "vidgrab-supervisor", "-p", "vidgrab-native-host"], cwd=ROOT, check=True)
    output.mkdir(parents=True)
    cargo_target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target"))
    if not cargo_target.is_absolute():
        cargo_target = ROOT / cargo_target
    with tempfile.TemporaryDirectory(prefix="vidgrab-freeze-") as scratch:
        subprocess.run([
            sys.executable, "-B", "-m", "PyInstaller", "--noconfirm",
            "--distpath", str(Path(scratch) / "dist"), "--workpath", str(Path(scratch) / "work"),
            str(ROOT / "packaging" / "engine.spec"),
        ], cwd=ROOT, env={**os.environ, "VIDGRAB_COMPONENT_DIR": str(arguments.components.resolve())}, check=True)
        shutil.copytree(Path(scratch) / "dist" / "vidgrab-engine", output / "engine")
    for name in ("vidgrab-supervisor", "vidgrab-native-host"):
        shutil.copy2(cargo_target / "release" / (name + suffix), output / (name + suffix))
    app = output / ("vidgrab-supervisor" + suffix)
    if arguments.desktop:
        app = output / ("vidgrab-desktop" + suffix)
        shutil.copy2(arguments.desktop, app)
    chrome = json.loads((ROOT / "extension" / "manifest.json").read_text())
    public = base64.b64decode(chrome["key"])
    identifier = "".join(chr(ord("a") + int(letter, 16)) for letter in hashlib.sha256(public).hexdigest()[:32])
    chrome_origin = (arguments.chrome_origin or f"chrome-extension://{identifier}/").strip()
    firefox = json.loads((ROOT / "extension" / "manifest.firefox.json").read_text())
    default_firefox_id = firefox.get("browser_specific_settings", {}).get("gecko", {}).get("id", "vidgrab@azaz.local")
    firefox_id = (arguments.firefox_id or default_firefox_id).strip()
    config = {
        "app": str(app), "engine": str(output / "engine" / ("vidgrab-engine" + suffix)),
        "data_dir": str(arguments.data_dir.resolve()), "download_dir": str(arguments.download_dir.resolve()),
        "chrome_origin": chrome_origin, "firefox_id": firefox_id,
    }
    (output / "host-config.json").write_text(json.dumps(config, indent=2) + "\n")
    # Manifests are reviewable artifacts; installing them is a separate command.
    native = output / "native"
    native.mkdir()
    for browser, allowlist in (("chrome", {"allowed_origins": [config["chrome_origin"]]}), ("firefox", {"allowed_extensions": [config["firefox_id"]]})):
        manifest = {"name": "com.vidgrab.companion", "description": "VidGrab desktop companion lifecycle spike", "path": str(output / ("vidgrab-native-host" + suffix)), "type": "stdio", **allowlist}
        (native / f"{browser}.json").write_text(json.dumps(manifest, indent=2) + "\n")
    check_started = time.monotonic()
    result = subprocess.run([str(output / "engine" / ("vidgrab-engine" + suffix)), "--self-check"],
        env={**os.environ, "PATH": "", "PYTHONPATH": "", "PYTHONHOME": ""}, capture_output=True, text=True, check=True)
    receipt = {"target": target, "python": sys.version.split()[0],
        "components": json.loads(result.stdout)["components"], "self_check_seconds": round(time.monotonic() - check_started, 3),
        "build_seconds": round(time.monotonic() - started, 3),
        "unpacked_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file())}
    (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
