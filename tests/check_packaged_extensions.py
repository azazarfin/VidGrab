"""Opt-in browser & Playwright verification of packaged extension artifacts."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.native_support import WebDriver, wait_for


def load_packager():
    spec = importlib.util.spec_from_file_location("vidgrab_package_extension", ROOT / "packaging" / "package_extension.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_packaged_chrome(archive: Path, expected_origin: str, executable: Path, *, bundle: Path | None = None) -> dict:
    from playwright.sync_api import expect, sync_playwright

    with tempfile.TemporaryDirectory(prefix="vidgrab-pkg-chrome-") as scratch:
        root = Path(scratch)
        unpacked = root / "chrome-ext"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(unpacked)
        assert not (unpacked / "manifest.firefox.json").exists(), "Chrome archive must exclude manifest.firefox.json"
        assert (unpacked / "fonts" / "LICENSE.txt").is_file(), "Chrome archive must bundle extension font license"

        profile = root / "profile"
        profile.mkdir()
        env = {**os.environ}
        if bundle:
            host_cfg = json.loads((bundle / "host-config.json").read_text())
            native_hosts = profile / "NativeMessagingHosts"
            native_hosts.mkdir(parents=True)
            manifest = {
                "name": "com.vidgrab.companion",
                "description": "VidGrab packaged extension test",
                "path": str(bundle / ("vidgrab-native-host.exe" if os.name == "nt" else "vidgrab-native-host")),
                "type": "stdio",
                "allowed_origins": [expected_origin],
            }
            (native_hosts / "com.vidgrab.companion.json").write_text(json.dumps(manifest))
            _ = host_cfg

        errors: list[str] = []
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                executable_path=str(executable),
                headless=True,
                env=env,
                ignore_default_args=["--disable-extensions"],
                args=[
                    "--enable-unsafe-extension-debugging",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            try:
                browser = context.browser
                assert browser is not None
                cdp = browser.new_browser_cdp_session()
                version = cdp.send("Browser.getVersion")
                identifier = cdp.send("Extensions.loadUnpacked", {"path": str(unpacked)})["id"]
                actual_origin = f"chrome-extension://{identifier}/"
                assert actual_origin == expected_origin, (actual_origin, expected_origin)
                worker = next((w for w in context.service_workers if identifier in w.url), None)
                if worker is None:
                    worker = context.wait_for_event("serviceworker", timeout=15000)
                assert worker.evaluate("Boolean(self.vidGrabAPI && self.vidGrabNative)"), "Packaged Chrome worker failed to initialize"

                popup = context.new_page()
                popup.on("pageerror", lambda err: errors.append(f"popup: {err}"))
                popup.goto(f"{actual_origin}popup/popup.html", wait_until="domcontentloaded")
                expect(popup.locator("[data-tab='queue']")).to_be_visible()
                expect(popup.locator("[data-tab='detected']")).to_be_visible()
                popup.close()

                options = context.new_page()
                options.on("pageerror", lambda err: errors.append(f"options: {err}"))
                options.goto(f"{actual_origin}options/options.html", wait_until="domcontentloaded")
                expect(options.get_by_role("heading", name="VidGrab Browser Settings")).to_be_visible()
                options.close()
            finally:
                context.close()
        assert not errors, errors
        return {
            "browser": "chrome",
            "archive": archive.name,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "extension_origin": expected_origin,
            "product": version["product"],
            "result": "passed",
        }


def check_packaged_firefox(archive: Path, expected_id: str, executable: Path, geckodriver: Path, env: dict | None = None) -> dict:
    firefox_uuid = "a83f0d5b-09e4-4f27-83d5-b8ea30954d0c"
    with tempfile.TemporaryDirectory(prefix="vidgrab-pkg-firefox-") as scratch:
        root = Path(scratch)
        profile = root / "profile"
        profile.mkdir()
        prefs = {
            "extensions.webextensions.uuids": json.dumps({expected_id: firefox_uuid}),
            "app.update.auto": False,
            "app.update.enabled": False,
            "browser.startup.homepage": "about:blank",
            "browser.shell.checkDefaultBrowser": False,
            "datareporting.healthreport.uploadEnabled": False,
            "toolkit.telemetry.enabled": False,
            "network.trr.mode": 5,
        }
        (profile / "user.js").write_text(
            "\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items())
        )
        driver = WebDriver(geckodriver, env or os.environ.copy(), root / "driver.log", arguments=("--allow-system-access",))
        try:
            capabilities = driver.start({
                "browserName": "firefox",
                "moz:firefoxOptions": {
                    "binary": str(executable),
                    "args": ["-headless", "-no-remote", "-profile", str(profile)],
                },
            })
            installed_id = driver.call("/moz/addon/install", {"path": str(archive), "temporary": True})
            assert installed_id == expected_id, (installed_id, expected_id)

            driver.call("/url", {"url": f"moz-extension://{firefox_uuid}/popup/popup.html"})
            tabs = wait_for(
                lambda: driver.script("return document.querySelectorAll('[data-tab]').length") or None,
                label="packaged Firefox popup render",
            )
            assert int(tabs) == 2, tabs

            driver.call("/url", {"url": f"moz-extension://{firefox_uuid}/options/options.html"})
            heading = wait_for(
                lambda: driver.script("return document.querySelector('h1')?.textContent") or None,
                label="packaged Firefox options render",
            )
            assert heading and "VidGrab" in heading, heading
        finally:
            driver.close()
    return {
        "browser": "firefox",
        "archive": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "extension_id": expected_id,
        "browser_version": capabilities.get("browserVersion"),
        "result": "passed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, help="Existing packaged extension directory (or build fresh temporary)")
    parser.add_argument("--chrome", type=Path, default=Path("/opt/google/chrome/chrome"))
    parser.add_argument("--firefox", type=Path)
    parser.add_argument("--geckodriver", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="vidgrab-pkg-ext-verify-") as scratch:
        if args.artifacts_dir:
            pkg_dir = args.artifacts_dir.resolve(strict=True)
            receipt = json.loads((pkg_dir / "extension-manifest.json").read_text(encoding="utf-8"))
        else:
            pkg_dir = Path(scratch) / "staged"
            receipt = load_packager().package_extensions(ROOT / "extension", pkg_dir)

        results = {"chrome_origin": receipt["chrome_origin"], "firefox_id": receipt["firefox_id"], "checks": []}
        if args.chrome.is_file():
            results["checks"].append(
                check_packaged_chrome(pkg_dir / "vidgrab-chrome-extension.zip", receipt["chrome_origin"], args.chrome)
            )
        if args.firefox and args.geckodriver:
            results["checks"].append(
                check_packaged_firefox(pkg_dir / "vidgrab-firefox-extension.xpi", receipt["firefox_id"], args.firefox, args.geckodriver)
            )
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
