"""Opt-in headless .deb extraction checks; never install or start a runtime.

This exercises the compiled package resolver and registration CLI in a temporary
home. AppImage relocation is simulated against the extracted executable; a real
AppImage, clean target installation, GUI and browser handoff remain separate gates.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import time

HOST = "com.vidgrab.companion"
ORIGIN = "chrome-extension://ikjpekhbbaehdhigakbahlhafaojbjpm/"


def run(command, environment, *, success=True, input=None):
    result = subprocess.run(command, env=environment, input=input, capture_output=True, timeout=30)
    if success and result.returncode:
        raise AssertionError(f"{command[0]} failed: {result.stderr.decode(errors='replace')[-3000:]}")
    if not success:
        assert result.returncode, f"Expected refusal from {command}"
    return result


def native_response(binary, caller, message, environment):
    result = run([str(binary), caller], environment, input=message)
    assert len(result.stdout) >= 4, "Native response must contain a complete length prefix"
    size = struct.unpack("=I", result.stdout[:4])[0]
    assert 0 < size <= 65536 and len(result.stdout) == size + 4, "Native stdout must contain exactly one bounded response"
    return json.loads(result.stdout[4:])


def check(artifact):
    artifact = artifact.resolve(strict=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vidgrab-deb-check-") as scratch:
        root = Path(scratch)
        prefix = root / "package"
        home = root / "home with বাংলা"
        home.mkdir()
        environment = {**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config")}
        for key in ("APPIMAGE", "APPDIR", "PYTHONPATH", "PYTHONHOME", "VIDGRAB_DATA_DIR", "VIDGRAB_DOWNLOAD_DIR", "VIDGRAB_LOG_DIR", "VIDGRAB_TEMP_DIR"):
            environment.pop(key, None)
        run(["dpkg-deb", "--extract", str(artifact), str(prefix)], environment)
        extraction_seconds = time.monotonic() - started
        binary = prefix / "usr/bin/vidgrab-desktop"
        assert binary.is_file(), "The package must contain the compiled desktop executable"
        discovery = json.loads(run([str(binary), "--check-package"], environment).stdout)
        engine = Path(discovery["engine"])
        assert engine.is_relative_to(prefix / "usr/lib"), "Tauri must discover package resources independently of the executable directory"
        assert not (home / "data").exists(), "Package inspection must not create state or registration"
        components = json.loads(run([str(engine), "--self-check"], {**environment, "PATH": ""}).stdout)

        saved = json.loads(run([str(binary), "--register-browser-host"], environment).stdout)
        bridge = home / "data/vidgrab-p02/native"
        host = bridge / "vidgrab-native-host"
        config_path = bridge / "host-config.json"
        assert bridge.stat().st_mode & 0o777 == 0o700 and host.stat().st_mode & 0o777 == 0o700
        assert config_path.stat().st_mode & 0o777 == 0o600
        chrome = home / f"config/google-chrome/NativeMessagingHosts/{HOST}.json"
        firefox = home / f".mozilla/native-messaging-hosts/{HOST}.json"
        for path in (chrome, firefox):
            manifest = json.loads(path.read_text())
            assert manifest["path"] == str(host) and manifest["type"] == "stdio"
        assert "engine" not in json.loads(config_path.read_text()), "Mounted resource paths must not be persisted in browser activation configuration"
        assert not Path(saved["data_dir"]).exists(), "Registration must not start an engine or create its database"

        request = json.dumps({"spike_version": 1, "id": "guard", "op": "status", "params": {}}).encode()
        frame = struct.pack("=I", len(request)) + request
        response = native_response(host, "chrome-extension://untrusted/", frame, environment)
        assert response["error"]["code"] == "ORIGIN_REJECTED"
        response = native_response(host, ORIGIN, struct.pack("=I", 65537), environment)
        assert response["error"]["code"] == "BAD_FRAME"
        assert not Path(saved["data_dir"]).exists(), "Rejected native input must not activate an engine"

        foreign = b'{"name":"com.vidgrab.companion","path":"/another/installation"}'
        original_firefox = firefox.read_bytes()
        original_config = config_path.read_bytes()
        firefox.write_bytes(foreign)
        run([str(binary), "--register-browser-host"], environment, success=False)
        assert firefox.read_bytes() == foreign and config_path.read_bytes() == original_config
        firefox.write_bytes(original_firefox)

        state = root / "selected state"
        media = root / "selected media বাংলা"
        state.mkdir()
        media.mkdir()
        (state / "vidgrab.db").write_bytes(b"preserved state fixture")
        (media / "finished.mp3").write_bytes(b"preserved media fixture")
        saved.update(data_dir=str(state), download_dir=str(media))
        config_path.write_text(json.dumps(saved))
        original_image = root / "VidGrab original.AppImage"
        moved_image = root / "VidGrab moved বাংলা.AppImage"
        # Executable placeholders prove launch-path repair, not AppImage packaging.
        for image in (original_image, moved_image):
            image.write_bytes(b"simulated AppImage launch target")
            image.chmod(0o755)
            repaired = json.loads(run([str(binary), "--register-browser-host"], {**environment, "APPIMAGE": str(image)}).stdout)
            assert repaired["app"] == str(image)
            assert repaired["data_dir"] == str(state) and repaired["download_dir"] == str(media)
            assert json.loads(chrome.read_text())["path"] == str(host)
        removed = json.loads(run([str(binary), "--unregister-browser-host"], environment).stdout)
        assert removed["removed"] == 2 and not chrome.exists() and not firefox.exists()
        assert (state / "vidgrab.db").read_bytes() == b"preserved state fixture"
        assert (media / "finished.mp3").read_bytes() == b"preserved media fixture"
        assert config_path.exists(), "Disconnecting browsers must preserve selected paths"

        maximum = (0,)
        elf_count = 0
        seen = set()
        for path in prefix.rglob("*"):
            if not path.is_file() or path.resolve() in seen:
                continue
            seen.add(path.resolve())
            with path.open("rb") as file:
                if file.read(4) != b"\x7fELF":
                    continue
            elf_count += 1
            symbols = run(["readelf", "--version-info", str(path)], environment).stdout.decode()
            for version in re.findall(r"Name: GLIBC_([0-9.]+)", symbols):
                maximum = max(maximum, tuple(map(int, version.split("."))))
        depends = run(["dpkg-deb", "--field", str(artifact), "Depends"], environment).stdout.decode().strip()
        declared = re.search(r"libc6 \(>= ([0-9.]+)\)", depends)
        assert declared and tuple(map(int, declared[1].split("."))) >= maximum, f"The package's glibc dependency must cover every shipped ELF requirement: required={maximum}, Depends={depends}"
        return {"artifact": str(artifact), "sha256": hashlib.file_digest(artifact.open("rb"), "sha256").hexdigest(),
            "artifact_bytes": artifact.stat().st_size, "extraction_seconds": round(extraction_seconds, 3),
            "headless_check_seconds": round(time.monotonic() - started, 3), "elf_files": elf_count,
            "required_glibc": ".".join(map(str, maximum)), "depends": depends, "components": components,
            "checks": ["compiled Tauri resource discovery", "frozen self-check with empty PATH", "private native registration",
                "native origin/frame rejection", "foreign manifest refusal", "simulated AppImage relocation preserving paths",
                "registration removal preserving fixture state/media", "declared glibc covers shipped ELF files"],
            "limits": ["extracted .deb, not an install/uninstall", "Fedora host, not a clean target baseline",
                "simulated relocation, not an actual AppImage", "no GUI, tray, Wayland, live browser or fixture-download check"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    for tool in ("dpkg-deb", "readelf"):
        if shutil.which(tool) is None:
            parser.error(f"Missing development check tool: {tool}")
    receipt = check(arguments.deb)
    output = json.dumps(receipt, indent=2) + "\n"
    if arguments.report:
        with arguments.report.open("x") as file:
            file.write(output)
    print(output, end="")


if __name__ == "__main__":
    main()
