"""Opt-in headless .rpm extraction and metadata checks; never install or start a runtime.

Extracts a built VidGrab .rpm into a temporary root via rpm2cpio/cpio and verifies
RPM Requires capabilities, AppStream metainfo, desktop launcher, license notices,
Tauri resource discovery, empty-PATH frozen self-check, native-host registration
and origin/frame guards, and maximum shipped ELF GLIBC requirements.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

HOST = "com.vidgrab.companion"
ORIGIN = "chrome-extension://ikjpekhbbaehdhigakbahlhafaojbjpm/"
REQUIRED_CAPABILITIES = (
    "libwebkit2gtk-4.1.so.0()(64bit)",
    "libgtk-3.so.0()(64bit)",
    "libayatana-appindicator3.so.1()(64bit)",
    "libsoup-3.0.so.0()(64bit)",
    "libcrypto.so.3()(64bit)",
)


def run(command, environment, *, success=True, input=None, cwd=None):
    result = subprocess.run(command, env=environment, input=input, cwd=cwd, capture_output=True, timeout=60)
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


def parse_version(text):
    return tuple(int(part) for part in text.split("."))


def check(artifact, *, max_glibc=None):
    artifact = artifact.resolve(strict=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vidgrab-rpm-check-") as scratch:
        root = Path(scratch)
        prefix = root / "package"
        prefix.mkdir()
        home = root / "home with বাংলা"
        home.mkdir()
        environment = {
            **os.environ,
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
        }
        for key in (
            "APPIMAGE",
            "APPDIR",
            "PYTHONPATH",
            "PYTHONHOME",
            "VIDGRAB_DATA_DIR",
            "VIDGRAB_DOWNLOAD_DIR",
            "VIDGRAB_LOG_DIR",
            "VIDGRAB_TEMP_DIR",
        ):
            environment.pop(key, None)

        meta_lines = run(
            ["rpm", "-qp", "--qf", "%{NAME}\n%{VERSION}\n%{LICENSE}\n%{ARCH}\n", str(artifact)],
            environment,
        ).stdout.decode().splitlines()
        assert len(meta_lines) == 4, f"Unexpected RPM metadata output: {meta_lines}"
        pkg_name, pkg_version, pkg_license, pkg_arch = meta_lines
        assert pkg_license == "MIT", f"Expected MIT RPM license, got {pkg_license!r}"
        assert pkg_arch == "x86_64", f"Expected x86_64 RPM arch, got {pkg_arch!r}"

        requires = [
            line.strip()
            for line in run(["rpm", "-qp", "--requires", str(artifact)], environment).stdout.decode().splitlines()
            if line.strip()
        ]
        for cap in REQUIRED_CAPABILITIES:
            assert cap in requires, f"Missing required RPM capability {cap!r} in {requires}"

        cpio_bytes = run(["rpm2cpio", str(artifact)], environment).stdout
        run(["cpio", "-idmu", "--quiet"], environment, input=cpio_bytes, cwd=prefix)
        extraction_seconds = time.monotonic() - started

        binary = prefix / "usr/bin/vidgrab-desktop"
        assert binary.is_file(), "The RPM must contain /usr/bin/vidgrab-desktop"
        assert (prefix / "usr/bin/vidgrab-native-host").is_file(), "The RPM must contain /usr/bin/vidgrab-native-host"

        metainfo = prefix / "usr/share/metainfo/com.vidgrab.desktop.metainfo.xml"
        assert metainfo.is_file(), "The RPM must ship AppStream metainfo XML"
        component = ET.parse(metainfo).getroot()
        assert component.tag == "component" and component.get("type") == "desktop-application"
        assert component.findtext("id") == "com.vidgrab.desktop"
        assert component.findtext("name") == "VidGrab"
        assert component.findtext("project_license") == "MIT"
        launchable = component.find("launchable[@type='desktop-id']")
        assert launchable is not None and (prefix / "usr/share/applications" / launchable.text).is_file()

        discovery = json.loads(run([str(binary), "--check-package"], environment).stdout)
        engine = Path(discovery["engine"])
        assert engine.is_relative_to(prefix / "usr/lib"), "Tauri must discover RPM resources under /usr/lib"
        for notice in ("LICENSE", "THIRD_PARTY.md", "GPLv3.ffmpeg.txt", "FONTS_LICENSE.txt"):
            assert (prefix / "usr/share/doc/vidgrab" / notice).is_file(), f"Missing RPM doc notice: {notice}"
            assert (engine.parent / "_internal/notices" / notice).is_file(), f"Missing engine notice: {notice}"
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
        assert "engine" not in json.loads(config_path.read_text()), "Mounted resource paths must not be persisted"
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

        removed = json.loads(run([str(binary), "--unregister-browser-host"], environment).stdout)
        assert removed["removed"] == 2 and not chrome.exists() and not firefox.exists()
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
                maximum = max(maximum, parse_version(version))

        declared_glibc = [
            parse_version(match.group(1))
            for req in requires
            for match in [re.fullmatch(r"libc\.so\.6\(GLIBC_([0-9.]+)\)\(64bit\)", req)]
            if match
        ]
        assert declared_glibc and max(declared_glibc) >= maximum, (
            f"Declared RPM glibc capability must cover every shipped ELF requirement: "
            f"required={maximum}, requires={requires}"
        )
        if max_glibc is not None:
            limit = parse_version(max_glibc)
            assert maximum <= limit, f"Shipped RPM ELFs require GLIBC_{maximum} exceeding --max-glibc {max_glibc}"

        return {
            "artifact": str(artifact),
            "package": pkg_name,
            "version": pkg_version,
            "license": pkg_license,
            "arch": pkg_arch,
            "sha256": hashlib.file_digest(artifact.open("rb"), "sha256").hexdigest(),
            "artifact_bytes": artifact.stat().st_size,
            "extraction_seconds": round(extraction_seconds, 3),
            "headless_check_seconds": round(time.monotonic() - started, 3),
            "elf_files": elf_count,
            "required_glibc": ".".join(map(str, maximum)),
            "requires": requires,
            "components": components,
            "os_release": platform.freedesktop_os_release(),
            "checks": [
                "RPM header metadata & capability Requires",
                "AppStream metainfo XML & desktop launcher",
                "RPM doc notices & engine notices",
                "compiled Tauri resource discovery under /usr/lib",
                "frozen self-check with empty PATH",
                "private native registration & unregistration",
                "native origin/frame rejection & foreign manifest refusal",
                "declared glibc capability covers shipped ELF files",
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpm", type=Path, required=True)
    parser.add_argument("--max-glibc", help="Maximum permitted GLIBC symbol version (e.g. 2.39)")
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    for tool in ("rpm", "rpm2cpio", "cpio", "readelf"):
        if shutil.which(tool) is None:
            parser.error(f"Missing development check tool: {tool}")
    receipt = check(arguments.rpm, max_glibc=arguments.max_glibc)
    output = json.dumps(receipt, indent=2) + "\n"
    if arguments.report:
        with arguments.report.open("x") as file:
            file.write(output)
    print(output, end="")


if __name__ == "__main__":
    main()
