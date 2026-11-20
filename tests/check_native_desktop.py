"""Opt-in compiled Tauri/WebKit check in an isolated Linux desktop session."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.native_support import WebDriver, close_window, fixture_server, isolate, local_call, stop_owner, virtual_display, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--xvfb", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    arguments = parser.parse_args()
    isolate()
    arguments.screenshots.mkdir(parents=True, exist_ok=True)
    root = Path(os.environ["VIDGRAB_VALIDATION_HOME"]) / "desktop path বাংলা"
    root.mkdir()
    config = json.loads((arguments.bundle / "host-config.json").read_text())
    config.update(app=str(arguments.bundle / "vidgrab-desktop"), data_dir=str(root / "state"), download_dir=str(root / "media"))
    path = root / "host-config.json"
    path.write_text(json.dumps(config))
    with virtual_display(arguments.xvfb) as environment, fixture_server() as url:
        os.environ["DISPLAY"] = environment["DISPLAY"]
        driver = WebDriver("/usr/bin/WebKitWebDriver", {**environment, "TAURI_WEBVIEW_AUTOMATION": "true"}, root / "driver.log")
        try:
            capabilities = driver.start({"webkitgtk:browserOptions": {"binary": config["app"], "args": ["--config", str(path)]}})
            wait_for(lambda: driver.script("return document.querySelector('.connection')?.textContent === 'Connected'"), label="native connected UI")
            body = driver.script("return document.body.innerText")
            assert "Add URL or batch" in body and "This desktop has no tray" in body, body
            assert driver.script("return document.querySelectorAll('[role=alert]').length") == 0
            first = local_call(config)
            assert first["state"] == "ready"
            driver.screenshot(arguments.screenshots / "native-empty.png")
            close_window("VidGrab")
            assert driver.script("return document.querySelector('footer .secondary').textContent") == "Quit VidGrab"
            repeated = subprocess.run([config["app"], "--config", str(path)], env=environment, capture_output=True, timeout=10)
            assert repeated.returncode == 0, repeated.stderr.decode()
            assert local_call(config)["supervisor_pid"] == first["supervisor_pid"]
            assert local_call(config)["engine_pid"] == first["engine_pid"]
            driver.fill("#media-url", "file:///fixture.wav")
            driver.click(".download-form button.primary")
            wait_for(lambda: driver.script("return document.querySelector('[role=alert]')?.textContent.includes('HTTP or HTTPS')"), label="native fixture validation")
            driver.click("#toggle-media-options")
            driver.click("#option-audio-only")
            driver.fill("#media-url", url)
            driver.click(".download-form button.primary")
            wait_for(lambda: (snapshot := local_call(config)).get("jobs") and snapshot["jobs"][0]["status"] == "complete", timeout=30, label="frozen MP3 completion")
            driver.click('[data-desktop-tab="history"]')
            wait_for(lambda: driver.script("return document.querySelector('.history-view .job-status')?.textContent === 'Finished'"), timeout=15, label="frozen MP3 download in native UI")
            completed = local_call(config)
            job = completed["jobs"][0]
            media = Path(job["file_path"])
            assert media.is_file() and media.suffix == ".mp3", job
            checksum = hashlib.sha256(media.read_bytes()).hexdigest()
            assert driver.script("return document.querySelectorAll('[role=alert]').length") == 0
            driver.screenshot(arguments.screenshots / "native-complete.png")
            os.kill(completed["engine_pid"], signal.SIGKILL)
            recovered = wait_for(lambda: (snapshot if (snapshot := local_call(config)).get("engine_generation") == 2 and snapshot.get("state") == "ready" else None), label="native owned engine recovery")
            assert recovered["supervisor_pid"] == first["supervisor_pid"]
            assert recovered["jobs"][0]["status"] == "complete"
            assert hashlib.sha256(media.read_bytes()).hexdigest() == checksum
            wait_for(lambda: driver.script("return document.querySelector('.connection')?.textContent === 'Connected' && document.querySelectorAll('[role=alert]').length === 0"), label="native UI recovery")
            driver.click("footer .secondary")
            endpoint = Path(config["data_dir"]) / "ipc" / "control.sock"
            wait_for(lambda: not endpoint.exists() and not Path(f'/proc/{first["supervisor_pid"]}').exists() and not Path(f'/proc/{recovered["engine_pid"]}').exists(), timeout=8, label="explicit native Quit")
            print(json.dumps({"result": "passed", "webview": capabilities, "checks": ["compiled UI", "no-tray close fallback", "repeat activation", "frozen WAV to MP3", "engine recovery", "explicit Quit"], "media_sha256": checksum}, indent=2))
        except BaseException:
            diagnostics = {}
            for name, probe in (
                ("page", lambda: driver.script("return {url:location.href, title:document.title, body:document.body.innerText, html:document.documentElement.outerHTML.slice(0,6000), tauri:typeof window.__TAURI_INTERNALS__}")),
                ("owner", lambda: local_call(config)),
                ("driver", lambda: (root / "driver.log").read_text(errors="replace")[-6000:]),
            ):
                try:
                    diagnostics[name] = probe()
                except Exception as error:
                    diagnostics[name] = str(error)
            try:
                driver.screenshot(arguments.screenshots / "native-failure.png")
            except Exception:
                pass
            (arguments.screenshots / "native-diagnostics.json").write_text(json.dumps(diagnostics, indent=2))
            print(json.dumps(diagnostics, indent=2), flush=True)
            raise
        finally:
            stop_owner(config)
            driver.close()


if __name__ == "__main__":
    main()
