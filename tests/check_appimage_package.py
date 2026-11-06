"""Opt-in actual AppImage extraction, integrity and relocation checks.

Uses extract-and-run and a temporary home. No GUI, engine server, live browser,
FUSE mount or installer is started. Run on the selected target baseline.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import time


def check(artifact, max_glibc):
    source = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("vidgrab_abi", source / "packaging/check_abi.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    artifact = artifact.resolve(strict=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vidgrab-appimage-check-") as scratch:
        folder = Path(scratch)
        image = folder / "VidGrab original.AppImage"
        shutil.copy2(artifact, image)
        image.chmod(0o755)
        runtime_abi = audit.audit_tree(folder, max_glibc=max_glibc)
        home = folder / "home with বাংলা"
        home.mkdir()
        environment = {**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
            "APPIMAGE_EXTRACT_AND_RUN": "1"}
        for key in ("APPIMAGE", "APPDIR", "PYTHONHOME", "PYTHONPATH", "VIDGRAB_DATA_DIR",
                    "VIDGRAB_DOWNLOAD_DIR", "VIDGRAB_LOG_DIR", "VIDGRAB_TEMP_DIR"):
            environment.pop(key, None)

        def run(command, *, input=None, env=environment):
            result = subprocess.run(list(map(str, command)), env=env, cwd=folder, input=input,
                capture_output=True, timeout=120)
            assert result.returncode == 0, result.stderr.decode(errors="replace")[-5000:]
            return result.stdout

        run([image, "--appimage-extract"])
        extracted = folder / "squashfs-root"
        abi = audit.audit_tree(extracted, max_glibc=max_glibc)
        required = max(tuple(map(int, value.split("."))) for value in
            (runtime_abi["required_glibc"], abi["required_glibc"]))
        abi = {"elf_files": abi["elf_files"] + runtime_abi["elf_files"],
            "required_glibc": ".".join(map(str, required)),
            "requirements": {"AppImage runtime": runtime_abi["required_glibc"],
                **{f"AppDir/{path}": value for path, value in abi["requirements"].items()}}}
        engines = list(extracted.rglob("vidgrab-engine"))
        assert len(engines) == 1, "The complete frozen engine must be shipped once"
        components = json.loads(run([engines[0], "--self-check"], env={**environment, "PATH": ""}))
        discovery = json.loads(run([image, "--check-package"]))
        assert discovery["app"] == str(image) and not (home / "data").exists()
        saved = json.loads(run([image, "--register-browser-host"]))
        config = home / "data/vidgrab-p02/native/host-config.json"
        host = config.parent / "vidgrab-native-host"
        assert "engine" not in json.loads(config.read_text()) and not Path(saved["data_dir"]).exists()
        response = run([host, "chrome-extension://untrusted/"], input=struct.pack("=I", 2) + b"{}")
        size = struct.unpack("=I", response[:4])[0]
        assert 0 < size <= 65536 and len(response) == size + 4
        assert json.loads(response[4:])["error"]["code"] == "ORIGIN_REJECTED"
        state = folder / "selected state"
        media = folder / "selected media বাংলা"
        state.mkdir()
        media.mkdir()
        (state / "vidgrab.db").write_bytes(b"fixture state")
        (media / "finished.mp3").write_bytes(b"fixture media")
        saved.update(data_dir=str(state), download_dir=str(media))
        config.write_text(json.dumps(saved))
        moved = folder / "VidGrab moved বাংলা.AppImage"
        image.rename(moved)
        repaired = json.loads(run([moved, "--register-browser-host"]))
        assert repaired["app"] == str(moved)
        assert repaired["data_dir"] == str(state) and repaired["download_dir"] == str(media)
        assert json.loads(run([moved, "--unregister-browser-host"]))["removed"] == 2
        assert config.exists() and (state / "vidgrab.db").read_bytes() == b"fixture state"
        assert (media / "finished.mp3").read_bytes() == b"fixture media"
    with artifact.open("rb") as file:
        checksum = hashlib.file_digest(file, "sha256").hexdigest()
    return {"artifact": str(artifact), "sha256": checksum, "artifact_bytes": artifact.stat().st_size,
        "seconds": round(time.monotonic() - started, 3), "abi": abi, "components": components,
        "os_release": platform.freedesktop_os_release(),
        "checks": ["actual AppImage extraction", "whole AppDir glibc ceiling",
            "frozen self-check with empty PATH", "actual image resource discovery",
            "private browser registration and origin rejection", "actual image relocation preserving paths",
            "registration removal preserving fixture files"],
        "limits": ["extract-and-run, not a FUSE mount", "headless commands, no GUI/browser/tray/Wayland lifecycle"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appimage", type=Path, required=True)
    parser.add_argument("--max-glibc", default="2.39")
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    ceiling = tuple(map(int, arguments.max_glibc.split(".")))
    receipt = check(arguments.appimage, ceiling)
    if arguments.report:
        with arguments.report.open("x") as file:
            file.write(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: value for key, value in receipt.items() if key != "abi"}, indent=2))


if __name__ == "__main__":
    main()
