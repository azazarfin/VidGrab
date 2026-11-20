"""Opt-in GUI and browser lifecycle checks against an actual Linux package.

Use an installed desktop executable or an AppImage, with the existing isolated
Linux driver setup. Browser activation uses the package's copied native bridge
and canonical descriptor; no staged --config launch is used.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import check_installed_browsers as browsers
from tests.native_support import WebDriver, close_window, fixture_server, isolate, local_call, stop_owner, virtual_display, wait_for

HOST = "com.vidgrab.companion.json"


def launch_diagnostics(home):
    processes = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            command = (directory / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            name = (directory / "comm").read_text().strip()
            if not any(value in name for value in ("vidgrab", "WebKit", "bwrap", "Xvfb", "xkbcomp")):
                continue
            environment = dict(item.split("=", 1) for item in (directory / "environ").read_text().split("\0") if "=" in item)
            processes.append({"pid": directory.name, "name": name, "command": command,
                "status": [line for line in (directory / "status").read_text().splitlines()
                    if line.startswith(("State:", "PPid:", "Threads:"))],
                "stdio": {str(fd): os.readlink(directory / "fd" / str(fd)) for fd in range(3)
                    if (directory / "fd" / str(fd)).exists()},
                "wait": (directory / "wchan").read_text(),
                "threads": [{"id": task.name, "wait": (task / "wchan").read_text()} for task in (directory / "task").iterdir()],
                "environment": {key: environment[key] for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "APPDIR", "APPIMAGE",
                    "PYTHONHOME", "PYTHONPATH", "DISPLAY", "GDK_BACKEND", "DBUS_SESSION_BUS_ADDRESS", "HOME",
                    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "MOZ_HEADLESS", "MOZ_ENABLE_WAYLAND") if key in environment}})
        except (OSError, ValueError):
            continue
    logs = {str(path.relative_to(home)): path.read_text(errors="replace")[-5000:]
        for path in home.rglob("*.log") if path.is_file() and path.name in ("driver.log", "engine.log")}
    return {"processes": processes, "logs": logs}


def configure(app, root, environment, browser=None):
    root.mkdir()
    result = subprocess.run([str(app), "--register-browser-host"], env=environment,
        capture_output=True, text=True, check=True, timeout=30)
    config = json.loads(result.stdout)
    config.update(data_dir=str(root / "state"), download_dir=str(root / "media"))
    descriptor = Path(environment["XDG_DATA_HOME"]) / "vidgrab-p02/native/host-config.json"
    descriptor.write_text(json.dumps(config))
    if browser is None:
        return config
    generated = (Path(environment["XDG_CONFIG_HOME"]) / "google-chrome/NativeMessagingHosts" / HOST
        if browser == "chrome" else Path(environment["HOME"]) / ".mozilla/native-messaging-hosts" / HOST)
    manifest = json.loads(generated.read_text())
    if browser == "chrome":
        # CDP requires a custom profile; use the package-generated host manifest.
        registration = root / "profile/NativeMessagingHosts" / HOST
        registration.parent.mkdir(parents=True)
    else:
        registration = generated
        registration.unlink()
    return config, registration, manifest


def check_ui(app, environment, url, screenshots, *, fuse=False):
    root = Path(environment["HOME"]) / "package desktop বাংলা"
    config = configure(app, root, environment)
    driver = WebDriver("/usr/bin/WebKitWebDriver", {**environment, "TAURI_WEBVIEW_AUTOMATION": "true"}, root / "driver.log")
    try:
        started = time.monotonic()
        capabilities = driver.start({"webkitgtk:browserOptions": {"binary": str(app), "args": []}})
        wait_for(lambda: driver.script("return document.querySelector('.connection')?.textContent === 'Connected'"), label="packaged connected UI")
        startup_seconds = round(time.monotonic() - started, 3)
        body = driver.script("return document.body.innerText")
        assert "Add URL or batch" in body and "This desktop has no tray" in body, body
        first = local_call(config)
        fuse_mount = None
        if fuse:
            executable = Path(f'/proc/{first["supervisor_pid"]}/exe').resolve(strict=True)
            fuse_entries = [line for line in Path("/proc/self/mountinfo").read_text().splitlines()
                if line.split(" - ")[1].split()[0].startswith("fuse.")]
            mounts = [line for line in fuse_entries
                if executable.is_relative_to(Path(line.split()[4]))]
            assert len(mounts) == 1, {"executable": str(executable), "test_mounts": fuse_entries,
                "owner_mounts": [line for line in Path(f'/proc/{first["supervisor_pid"]}/mountinfo').read_text().splitlines()
                    if line.split(" - ")[1].split()[0].startswith("fuse.")]}
            fuse_mount = {"executable": str(executable), "mountpoint": mounts[0].split()[4], "mountinfo": mounts[0]}
        driver.screenshot(screenshots / "package-empty.png")
        close_window("VidGrab")
        assert driver.script("return document.querySelector('footer .secondary').textContent") == "Quit VidGrab"
        repeated = subprocess.run([str(app)], env=environment, capture_output=True, timeout=30)
        assert repeated.returncode == 0, repeated.stderr.decode(errors="replace")
        assert local_call(config)["supervisor_pid"] == first["supervisor_pid"]
        assert local_call(config)["engine_pid"] == first["engine_pid"]
        driver.fill("#media-url", "file:///fixture.wav")
        driver.click(".download-form button.primary")
        wait_for(lambda: driver.script("return document.querySelector('[role=alert]')?.textContent.includes('HTTP or HTTPS')"), label="packaged fixture rejection")
        driver.click("#toggle-media-options")
        driver.click("#option-audio-only")
        driver.fill("#media-url", url)
        driver.click(".download-form button.primary")
        wait_for(lambda: (snapshot := local_call(config)).get("jobs") and snapshot["jobs"][0]["status"] == "complete", timeout=30, label="packaged frozen MP3 completion")
        driver.click('[data-desktop-tab="history"]')
        wait_for(lambda: driver.script("return document.querySelector('.history-view .job-status')?.textContent === 'Finished'"), timeout=15, label="packaged frozen MP3")
        completed = local_call(config)
        media = Path(completed["jobs"][0]["file_path"])
        assert media.is_file() and media.suffix == ".mp3"
        checksum = hashlib.sha256(media.read_bytes()).hexdigest()
        driver.screenshot(screenshots / "package-complete.png")
        os.kill(completed["engine_pid"], signal.SIGKILL)
        recovered = wait_for(lambda: (snapshot if (snapshot := local_call(config)).get("engine_generation") == 2 and snapshot.get("state") == "ready" else None), label="packaged engine recovery")
        assert recovered["supervisor_pid"] == first["supervisor_pid"]
        assert recovered["jobs"][0]["status"] == "complete"
        assert hashlib.sha256(media.read_bytes()).hexdigest() == checksum
        wait_for(lambda: driver.script("return document.querySelector('.connection')?.textContent === 'Connected' && document.querySelectorAll('[role=alert]').length === 0"), label="packaged UI recovery")
        driver.click("footer .secondary")
        wait_for(lambda: not (Path(config["data_dir"]) / "ipc/control.sock").exists()
            and not Path(f'/proc/{first["supervisor_pid"]}').exists()
            and not Path(f'/proc/{recovered["engine_pid"]}').exists(), timeout=8, label="packaged explicit Quit")
        result = {"result": "passed", "webview": capabilities, "startup_seconds": startup_seconds,
            "media_sha256": checksum, "checks": ["package UI", "no-tray Close", "repeated package activation",
                "external URL rejection", "frozen WAV to MP3", "engine recovery", "explicit Quit"]}
        if fuse_mount:
            wait_for(lambda: not any(line.split()[4] == fuse_mount["mountpoint"]
                for line in Path("/proc/self/mountinfo").read_text().splitlines()), timeout=8, label="AppImage FUSE unmount after Quit")
            result["fuse_mount"] = {**fuse_mount, "removed_after_quit": True}
            result["checks"].append("FUSE resource mount and unmount")
        return result
    except BaseException:
        diagnostics = {"launch": launch_diagnostics(Path(environment["HOME"]))}
        for name, probe in (("page", lambda: driver.script("return {url:location.href, body:document.body.innerText}")),
                            ("owner", lambda: local_call(config)),
                            ("driver", lambda: (root / "driver.log").read_text(errors="replace")[-6000:])):
            try:
                diagnostics[name] = probe()
            except Exception as error:
                diagnostics[name] = str(error)
        (screenshots / "package-diagnostics.json").write_text(json.dumps(diagnostics, indent=2))
        raise
    finally:
        stop_owner(config)
        driver.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--xvfb", type=Path, required=True)
    parser.add_argument("--geckodriver", type=Path, required=True)
    parser.add_argument("--chrome", type=Path, default=Path("/opt/google/chrome/chrome"))
    parser.add_argument("--firefox", type=Path, default=Path("/usr/bin/firefox"))
    parser.add_argument("--browser", choices=("chrome", "firefox", "both"), default="both")
    parser.add_argument("--fuse", action="store_true", help="Mount an AppImage through FUSE instead of extract-and-run")
    parser.add_argument("--screenshots", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    if arguments.fuse and arguments.app.suffix != ".AppImage":
        parser.error("--fuse requires an AppImage")
    isolate()
    app = arguments.app.resolve(strict=True)
    home = Path(os.environ["VIDGRAB_VALIDATION_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    os.environ.update(HOME=str(home), XDG_DATA_HOME=str(home / "data"), XDG_CONFIG_HOME=str(home / "config"),
        XDG_CACHE_HOME=str(home / "cache"), VIDGRAB_DISABLE_TRAY="1")
    if arguments.fuse:
        os.environ.pop("APPIMAGE_EXTRACT_AND_RUN", None)
    else:
        os.environ["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    arguments.screenshots.mkdir(parents=True, exist_ok=True)
    receipt = {"app": str(app), "result": "running", "browsers": [], "fuse": arguments.fuse,
        "limits": ["Xvfb/software rendering, no physical tray or Wayland", "native browser packages, no Snap/Flatpak",
            "FUSE depends on the OS helper and kernel" if arguments.fuse else "AppImage uses extract-and-run, no FUSE mount",
            "driver-only Python; frozen tools checked separately with empty PATH"]}
    if arguments.report and arguments.report.exists():
        parser.error("Select a fresh report path")

    def checkpoint():
        if arguments.report:
            arguments.report.write_text(json.dumps(receipt, indent=2) + "\n")

    def firefox_failure(config, root):
        receipt["firefox_before_cleanup"] = {"config": config, "diagnostics": launch_diagnostics(home)}
        descriptor = home / "data/vidgrab-p02/native/host-config.json"
        if descriptor.exists():
            receipt["firefox_before_cleanup"]["descriptor"] = json.loads(descriptor.read_text())
        checkpoint()

    try:
        with virtual_display(arguments.xvfb) as display, fixture_server() as url:
            os.environ["DISPLAY"] = display["DISPLAY"]
            environment = {**display, "PATH": "/usr/bin:/bin"}
            receipt["ui"] = check_ui(app, environment, url, arguments.screenshots, fuse=arguments.fuse)
            checkpoint()
            original = browsers.configure
            browsers.configure = lambda bundle, root, browser: configure(app, root, environment, browser)
            try:
                if arguments.browser in ("chrome", "both"):
                    receipt["browsers"].append(browsers.check_chrome(app.parent, environment, url, arguments.chrome))
                    checkpoint()
                if arguments.browser in ("firefox", "both"):
                    receipt["browsers"].append(browsers.check_firefox(app.parent, environment, url, arguments.firefox, arguments.geckodriver,
                        on_failure=firefox_failure))
                    checkpoint()
            finally:
                browsers.configure = original
    except BaseException:
        receipt.update(result="failed", failure=traceback.format_exc(), diagnostics=launch_diagnostics(home))
        checkpoint()
        raise
    receipt["result"] = "passed"
    checkpoint()
    print(json.dumps(receipt, indent=2), flush=True)


if __name__ == "__main__":
    main()
