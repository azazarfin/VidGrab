"""Opt-in native Chrome/Firefox handoff using private Linux registration."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.native_support import WebDriver, fixture_server, isolate, local_call, stop_owner, virtual_display, wait_for

ROOT = Path(__file__).resolve().parents[1]
FIREFOX_UUID = "a83f0d5b-09e4-4f27-83d5-b8ea30954d0c"


def configure(bundle, root, browser):
    root.mkdir()
    config = json.loads((bundle / "host-config.json").read_text())
    config.update(app=str(bundle / "vidgrab-desktop"), data_dir=str(root / "state"), download_dir=str(root / "media"))
    (root / "host-config.json").write_text(json.dumps(config))
    shutil.copy2(bundle / "vidgrab-native-host", root / "vidgrab-native-host")
    home = Path(os.environ["VIDGRAB_VALIDATION_HOME"])
    # Chrome resolves user host manifests from its selected user data directory.
    directory = root / "profile" / "NativeMessagingHosts" if browser == "chrome" else home / ".mozilla/native-messaging-hosts"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"name": "com.vidgrab.companion", "description": "Isolated VidGrab lifecycle validation",
                "path": str(root / "vidgrab-native-host"), "type": "stdio"}
    manifest.update({"allowed_origins": [config["chrome_origin"]]} if browser == "chrome"
                    else {"allowed_extensions": [config["firefox_id"]]})
    return config, directory / "com.vidgrab.companion.json", manifest


def completed_job(config, task_id):
    def completed():
        snapshot = local_call(config)
        job = next((job for job in snapshot.get("jobs", []) if job["id"] == task_id), None)
        if job and job["status"] == "failed":
            raise AssertionError(job)
        return (snapshot, job) if job and job["status"] == "complete" else None
    snapshot, job = wait_for(completed, timeout=30, label="download after browser exit")
    media = Path(job["file_path"])
    assert media.is_file() and media.suffix == ".mp3", job
    return snapshot, hashlib.sha256(media.read_bytes()).hexdigest()


def check_chrome(bundle, environment, url, executable):
    from playwright.sync_api import sync_playwright
    root = Path(os.environ["VIDGRAB_VALIDATION_HOME"]) / "chrome-check"
    config, registration, manifest = configure(bundle, root, "chrome")
    # Remote debugging requires a profile outside Chrome's default data directory.
    profile = root / "profile"
    contexts = []
    def native(worker, operation, params=None):
        return worker.evaluate('''async ({operation, params}) => {
          let timer;
          try {
            return await Promise.race([
              vidGrabNative.send(operation, params),
              new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Native response deadline exceeded')), 25000); })
            ]);
          } finally { clearTimeout(timer); }
        }''', {"operation": operation, "params": params or {}})
    def connect(playwright):
        context = playwright.chromium.launch_persistent_context(
            str(profile), executable_path=str(executable), headless=True, env=environment,
            ignore_default_args=["--disable-extensions"],
            args=["--enable-unsafe-extension-debugging", "--disable-background-networking",
                  "--disable-component-update", "--no-first-run", "--no-default-browser-check"],
        )
        contexts.append(context)
        browser = context.browser
        assert browser, "Chrome must expose its browser CDP session"
        cdp = browser.new_browser_cdp_session()
        version = cdp.send("Browser.getVersion")
        identifier = cdp.send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
        assert f"chrome-extension://{identifier}/" == config["chrome_origin"], identifier
        worker = next((worker for worker in context.service_workers if identifier in worker.url), None)
        if worker is None:
            worker = context.wait_for_event("serviceworker", timeout=15000)
        assert identifier in worker.url, worker.url
        assert worker.evaluate("Boolean(self.vidGrabAPI && self.vidGrabNative)"), "Chrome background imports failed"
        return context, worker, version
    with sync_playwright() as playwright:
        try:
            context, worker, version = connect(playwright)
            print('chrome: background loaded', flush=True)
            missing = worker.evaluate("async () => { try { await vidGrabNative.send('status'); return ''; } catch (error) { return error.message; } }")
            assert missing and not Path(config["data_dir"]).exists(), missing
            registration.write_text(json.dumps(manifest))
            print('chrome: native registration written', flush=True)
            started = native(worker, 'status')
            print('chrome: native owner ready', flush=True)
            assert started["state"] == "ready", started
            result = native(worker, 'fixture.download', {"url": url})
            task = result["task_id"]
            wait_for(lambda: any(job["id"] == task and job["status"] == "downloading" for job in local_call(config).get("jobs", [])), label="Chrome fixture active")
            print('chrome: closing browser during download', flush=True)
            context.close()
            print('chrome: browser closed', flush=True)
            contexts.remove(context)
            alive = local_call(config)
            assert alive["supervisor_pid"] == started["supervisor_pid"]
            assert alive["engine_pid"] == started["engine_pid"]
            assert any(job["id"] == task and job["status"] == "downloading" for job in alive["jobs"]), alive
            complete, checksum = completed_job(config, task)
            stop_owner(config)
            reopened, worker, _ = connect(playwright)
            restored = native(worker, 'status')
            assert restored["supervisor_pid"] != complete["supervisor_pid"]
            assert any(job["id"] == task and job["status"] == "complete" for job in restored["jobs"]), restored
            reopened.close()
            contexts.remove(reopened)
            return {"browser": "chrome", "version": version["product"], "extension_id": config["chrome_origin"],
                    "missing_registration": missing, "media_sha256": checksum, "result": "passed"}
        finally:
            print('chrome: cleaning isolated owner and browser', flush=True)
            stop_owner(config)
            for context in contexts:
                context.close()


def firefox_fixture(root):
    extension = root / "extension"
    shutil.copytree(ROOT / "extension", extension)
    shutil.copy2(extension / "manifest.firefox.json", extension / "manifest.json")
    (extension / "native-check.html").write_text('''<!doctype html><meta charset="utf-8">
<title>VidGrab isolated native check</title><input id="url" type="url">
<button id="status">Status</button><button id="download">Download fixture</button>
<pre id="result"></pre><script src="native-check.js"></script>''')
    (extension / "native-check.js").write_text('''
async function run(op) {
  const output = document.getElementById('result'); output.textContent = '';
  try {
    const background = await browser.runtime.getBackgroundPage();
    if (!background.vidGrabAPI || !background.vidGrabNative) throw new Error('Firefox background imports failed');
    const params = op === 'fixture.download' ? {url: document.getElementById('url').value} : {};
    const data = await background.vidGrabNative.send(op, params);
    output.textContent = JSON.stringify({ok: true, id: browser.runtime.id, data});
  } catch (error) { output.textContent = JSON.stringify({ok: false, message: error.message}); }
}
document.getElementById('status').onclick = () => run('status');
document.getElementById('download').onclick = () => run('fixture.download');
''')
    archive = root / "extension.xpi"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(extension.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(extension))
    return archive


def check_firefox(bundle, environment, url, executable, geckodriver, *, on_failure=None):
    root = Path(os.environ["VIDGRAB_VALIDATION_HOME"]) / "firefox-check"
    config, registration, manifest = configure(bundle, root, "firefox")
    archive = firefox_fixture(root)
    profile = root / "profile"
    profile.mkdir()
    preferences = {
        "extensions.webextensions.uuids": json.dumps({config["firefox_id"]: FIREFOX_UUID}),
        "app.update.auto": False, "app.update.enabled": False,
        "browser.startup.homepage": "about:blank", "browser.shell.checkDefaultBrowser": False,
        "datareporting.healthreport.uploadEnabled": False, "toolkit.telemetry.enabled": False,
        "network.trr.mode": 5,
    }
    (profile / "user.js").write_text("\n".join(f"user_pref({json.dumps(key)}, {json.dumps(value)});" for key, value in preferences.items()))
    driver = None
    def connect():
        nonlocal driver
        driver = WebDriver(geckodriver, environment, root / "driver.log", arguments=("--allow-system-access",))
        capabilities = driver.start({"browserName": "firefox", "moz:firefoxOptions": {
            "binary": str(executable), "args": ["-headless", "-no-remote", "-profile", str(profile)]}})
        identifier = driver.call("/moz/addon/install", {"path": str(archive), "temporary": True})
        assert identifier == config["firefox_id"], identifier
        driver.call("/url", {"url": f"moz-extension://{FIREFOX_UUID}/native-check.html"})
        return capabilities
    def invoke(selector):
        driver.click(selector)
        result = wait_for(lambda: driver.script("return document.getElementById('result').textContent"), label="Firefox native response")
        return json.loads(result)
    try:
        capabilities = connect()
        missing = invoke("#status")
        assert not missing["ok"] and not Path(config["data_dir"]).exists(), missing
        registration.write_text(json.dumps(manifest))
        response = invoke("#status")
        assert response["ok"] and response["id"] == config["firefox_id"], response
        started = response["data"]
        assert started["state"] == "ready", started
        driver.fill("#url", url)
        response = invoke("#download")
        assert response["ok"], response
        task = response["data"]["task_id"]
        wait_for(lambda: any(job["id"] == task and job["status"] == "downloading" for job in local_call(config).get("jobs", [])), label="Firefox fixture active")
        driver.close()
        driver = None
        alive = local_call(config)
        assert alive["supervisor_pid"] == started["supervisor_pid"]
        assert alive["engine_pid"] == started["engine_pid"]
        assert any(job["id"] == task and job["status"] == "downloading" for job in alive["jobs"]), alive
        complete, checksum = completed_job(config, task)
        stop_owner(config)
        connect()
        response = invoke("#status")
        assert response["ok"], response
        restored = response["data"]
        assert restored["supervisor_pid"] != complete["supervisor_pid"]
        assert any(job["id"] == task and job["status"] == "complete" for job in restored["jobs"]), restored
        return {"browser": "firefox", "version": capabilities["browserVersion"], "extension_id": config["firefox_id"],
                "missing_registration": missing["message"], "media_sha256": checksum, "result": "passed"}
    except BaseException:
        if on_failure:
            on_failure(config, root)
        raise
    finally:
        if driver:
            driver.close()
        stop_owner(config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--xvfb", type=Path, required=True)
    parser.add_argument("--geckodriver", type=Path, required=True)
    parser.add_argument("--chrome", type=Path, default=Path("/opt/google/chrome/chrome"))
    parser.add_argument("--firefox", type=Path, default=Path("/usr/bin/firefox"))
    parser.add_argument("--browser", choices=("chrome", "firefox", "both"), default="both")
    arguments = parser.parse_args()
    isolate()
    with virtual_display(arguments.xvfb) as environment, fixture_server() as url:
        if arguments.browser in ("chrome", "both"):
            print(json.dumps(check_chrome(arguments.bundle, environment, url, arguments.chrome), indent=2), flush=True)
        if arguments.browser in ("firefox", "both"):
            print(json.dumps(check_firefox(arguments.bundle, environment, url, arguments.firefox, arguments.geckodriver), indent=2), flush=True)


if __name__ == "__main__":
    main()
