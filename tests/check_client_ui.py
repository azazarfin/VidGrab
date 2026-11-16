"""Opt-in actual renderer journeys against a temporary, non-executing v1 service."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from tests.client_ui_support import ClientFixture


FIELD_CONTRAST = """element => {
  const style = getComputedStyle(element);
  const luminance = color => color.match(/[\\d.]+/g).slice(0, 3).map(Number)
    .map(channel => channel / 255).map(channel => channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4)
    .reduce((sum, channel, index) => sum + channel * [.2126, .7152, .0722][index], 0);
  const foreground = luminance(style.color), background = luminance(style.backgroundColor);
  return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
}"""


def detected_metadata_journey(context, fixture):
    title = 'Detected" onmouseover="window.pwned=1'
    url = 'https://fixture.invalid/media.mp4?quote="'
    fixture.preferences["detected_videos_0"] = [{"url": url, "title": title}]
    page = context.new_page()
    try:
        page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.locator('[data-tab="detected"]').click()
        expect(page.locator("#detected-list .card-title")).to_have_text(title)
        expect(page.locator("#detected-list .card-title")).to_have_attribute("title", title)
        expect(page.locator("#detected-list .detected-item-checkbox")).to_have_attribute("data-url", url)
        assert not page.locator("[onmouseover]").count()
        page.locator("#detected-list .card-title").hover()
        assert page.evaluate("window.pwned") is None
    finally:
        page.close()
        fixture.preferences.pop("detected_videos_0", None)


def browser_journey(browser, fixture, root):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    fixture.install(context)
    blocked, errors = [], []
    context.route("**/*", lambda route: (blocked.append(route.request.url), route.abort()) if urlsplit(route.request.url).scheme in ("http", "https") else route.continue_())
    context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
    try:
        detected_metadata_journey(context, fixture)
        options = context.new_page()
        options.goto((Path.cwd() / "extension/options/options.html").as_uri())
        options.wait_for_load_state("networkidle")
        expect(options.locator("#status-message")).to_contain_text("Settings loaded", timeout=7000)
        options.locator("#input-max-concurrent").select_option("6")
        options.locator("#btn-save-settings").click()
        expect(options.locator("#status-message")).to_contain_text("saved", timeout=7000)
        assert fixture.settings()["values"]["max_concurrent"] == 6
        patches = [request for request in fixture.requests if request["operation"] == "settings.patch"]
        assert patches[-1]["params"]["patch"] == {"max_concurrent": 6}, patches[-1]
        assert fixture.preferences["backendUrl"] == "http://untrusted.invalid:8000"
        assert "default_format" not in fixture.preferences
        options.locator("#input-max-concurrent").select_option("7")
        fixture.patch({"max_concurrent": 8, "default_format": "best[height<=480]"})
        options.locator("#btn-save-settings").click()
        expect(options.locator("#status-message")).to_contain_text("changed", timeout=7000)
        assert options.locator("#input-max-concurrent").input_value() == "7"
        assert fixture.settings()["values"]["max_concurrent"] == 8
        options.locator("#btn-reload-settings").click()
        expect(options.locator("#input-max-concurrent")).to_have_value("8")
        options.locator("#btn-tab-advanced").click()
        expect(options.locator("#input-backend-url")).to_be_disabled()
        expect(options.locator("#permission-status")).to_contain_text("Cookies: granted")
        fixture.pending = [{"key": "reviewed-notice", "operation": "downloads.enqueue"}]
        options.locator("#btn-tab-general").click()
        options.locator("#btn-tab-advanced").click()
        expect(options.get_by_role("button", name="Dismiss reviewed notice")).to_be_visible(timeout=7000)
        before = len(fixture.requests)
        options.get_by_role("button", name="Dismiss reviewed notice").click()
        expect(options.get_by_role("button", name="Dismiss reviewed notice")).to_have_count(0, timeout=7000)
        assert not fixture.pending
        assert not any(request["operation"] == "downloads.enqueue" for request in fixture.requests[before:])
        options.screenshot(path=str(root / "options-connected.png"), full_page=True)

        fixture.preferences["detected_videos_0"] = [{"url": "https://fixture.invalid/batch.mp4", "title": "Batch fixture"}]
        popup = context.new_page()
        popup.set_viewport_size({"width": 400, "height": 600})
        popup.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
        popup.wait_for_load_state("networkidle")
        expect(popup.locator("#connection-status")).to_contain_text("Connected", timeout=7000)
        expect(popup.locator("#backend-error-overlay")).to_be_hidden()
        popup.get_by_label("Media URL", exact=True).fill("https://fixture.invalid/media.mp4")
        popup.locator("#download-btn").click()
        expect(popup.locator("#modal-title")).to_contain_text("Fixture <img")
        assert popup.evaluate("window.pwned") is None
        expect(popup.get_by_role("dialog", name="Download Options", exact=True)).to_be_visible()
        for selector in ("#modal-download-dir-input", "#modal-output-template-input"):
            contrast = popup.locator(selector).evaluate(FIELD_CONTRAST)
            assert contrast >= 4.5, (selector, contrast)
        focus_trace = []
        for _ in range(18):
            popup.keyboard.press("Tab")
            focus_trace.append(popup.evaluate("""() => ({
                tag: document.activeElement.tagName, id: document.activeElement.id,
                inside: document.activeElement.closest('#format-modal') !== null,
                document_focus: document.hasFocus(), open: document.querySelector('#format-modal').open
            })"""))
            # Native dialogs allow Tab to reach browser chrome, then return to
            # the dialog. No element behind the dialog may receive page focus.
            assert focus_trace[-1]["inside"] or not focus_trace[-1]["document_focus"], focus_trace
        assert any(item["inside"] for item in focus_trace[10:]), focus_trace
        popup.locator("#modal-close-btn").focus()
        for _ in range(18):
            popup.keyboard.press("Shift+Tab")
            assert popup.evaluate("!document.hasFocus() || document.activeElement.closest('#format-modal') !== null")
        popup.locator("#modal-close-btn").focus()
        popup.keyboard.press("Escape")
        expect(popup.locator("#format-modal")).to_be_hidden()
        expect(popup.locator("#download-btn")).to_be_focused()
        popup.locator("#download-btn").click()
        expect(popup.locator("#modal-start-btn")).to_be_enabled(timeout=7000)
        expect(popup.locator("#format-select option").first).to_contain_text("best[height<=480]")
        expect(popup.locator("#modal-download-dir-input")).to_have_attribute("readonly", "")
        popup.locator("#modal-start-btn").click()
        expect(popup.locator("#format-modal")).to_be_hidden(timeout=7000)
        expect(popup.locator("#modal-start-btn")).to_be_hidden(timeout=7000)
        enqueue = [request for request in fixture.requests if request["operation"] == "downloads.enqueue"][-1]
        assert enqueue["params"].get("format_id") is None
        first = fixture.jobs()[0]
        fixture.set_status(first["id"], "waiting_for_auth")
        popup.locator('[data-tab="queue"]').click()
        expect(popup.get_by_role("button", name="Share fresh cookies")).to_be_visible(timeout=7000)
        fixture.preferences["cookieForwardingEnabled"] = True
        popup.get_by_role("button", name="Share fresh cookies").click()
        expect(popup.get_by_role("button", name="Share fresh cookies")).to_have_count(0, timeout=7000)
        assert fixture.jobs()[0]["id"] == first["id"]
        assert fixture.jobs()[0]["status"] == "queued"
        assert fixture.context_calls[-1]["job_id"] == first["id"]
        assert "fixture-cookie-secret" not in json.dumps(fixture.preferences)
        protected = popup.locator("#task-card-" + first["id"])
        protected.get_by_role("button", name="Cancel", exact=True).click()
        popup.locator('[data-tab="history"]').click()
        expect(protected.get_by_role("button", name="Retry", exact=True)).to_be_visible(timeout=7000)
        protected.get_by_role("button", name="Retry", exact=True).click()
        popup.locator('[data-tab="queue"]').click()
        expect(protected.get_by_role("button", name="Cancel", exact=True)).to_be_visible(timeout=7000)
        retry = [request for request in fixture.requests if request["operation"] == "downloads.action"][-1]
        assert retry["params"]["action"] == "retry"
        assert retry["params"]["auth_ref"]
        assert fixture.context_calls[-1]["job_id"] == first["id"]

        popup.locator('[data-tab="detected"]').click()
        expect(popup.get_by_text("Batch fixture", exact=True)).to_be_visible(timeout=7000)
        selection = popup.locator("label.checkbox-container:has(#detected-select-all)")
        expect(selection).to_be_visible()
        selection.click()
        expect(popup.get_by_role("checkbox", name="Select all detected videos", exact=True)).to_be_checked()
        popup.locator("#detected-download-btn").click()
        expect(popup.get_by_role("dialog", name="Batch Download Options", exact=True)).to_be_visible()
        popup.keyboard.press("Escape")
        expect(popup.locator("#batch-modal")).to_be_hidden()
        expect(popup.locator("#detected-download-btn")).to_be_focused()
        fixture.lost_operations.add("downloads.enqueue")
        popup.locator("#detected-download-btn").click()
        popup.locator("#batch-modal-start-btn").click()
        expect(popup.locator("#client-status")).to_contain_text("may have", timeout=7000)
        expect(popup.locator("#batch-modal")).to_be_hidden()
        expect(popup.locator("#check-request-btn")).to_be_focused()
        count = len(fixture.jobs())
        popup.locator("#check-request-btn").click()
        expect(popup.locator("#check-request-btn")).to_be_hidden(timeout=7000)
        assert len(fixture.jobs()) == count

        fixture.lost_operations.add("downloads.enqueue")
        popup.locator("#video-url-input").fill("https://fixture.invalid/second.mp4")
        popup.locator("#download-btn").click()
        expect(popup.locator("#modal-start-btn")).to_be_enabled(timeout=7000)
        popup.locator("#modal-start-btn").click()
        expect(popup.locator("#client-status")).to_contain_text("may have", timeout=7000)
        count = len(fixture.jobs())
        popup.locator("#check-request-btn").click()
        expect(popup.locator("#check-request-btn")).to_be_hidden(timeout=7000)
        expect(popup.locator("#modal-start-btn")).to_be_hidden(timeout=7000)
        assert len(fixture.jobs()) == count
        writes = [request for request in fixture.requests if request["operation"] == "downloads.enqueue"]
        assert writes[-1]["idempotency_key"] == writes[-2]["idempotency_key"]
        popup.screenshot(path=str(root / "popup-auth-replay.png"), full_page=True)
        fixture.excluded_features = {"downloads.source"}
        popup.reload()
        expect(popup.locator("#connection-status")).to_contain_text("Update required", timeout=7000)
        expect(popup.locator("#backend-error-overlay h3")).to_have_text("Update required")
        assert not popup.locator("#backend-error-overlay code").count()
        assert not errors, errors
    finally:
        fixture.excluded_features.clear()
        context.close()
    return {"page_errors": errors, "blocked_requests": len(blocked), "scope": "Chrome renderer; mocked browser/native delivery; real temporary v1 service"}


def desktop_journey(browser, fixture, root, url):
    context = browser.new_context(viewport={"width": 620, "height": 780})
    fixture.install(context)
    errors = []
    allowed = urlsplit(url)
    context.route("**/*", lambda route: route.continue_() if (urlsplit(route.request.url).hostname, urlsplit(route.request.url).port) == (allowed.hostname, allowed.port) else route.abort())
    try:
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.wait_for_load_state("networkidle")
        expect(page.get_by_label("Media URL", exact=True)).to_be_visible(timeout=7000)
        expect(page.get_by_text("Legacy decoy", exact=True)).to_have_count(0)
        page.get_by_label("Media URL", exact=True).fill("https://fixture.invalid/desktop.mp4")
        page.get_by_role("button", name="Download", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="queued")).to_be_visible(timeout=7000)
        assert fixture.jobs()
        page.get_by_text("Download defaults", exact=True).click()
        page.get_by_label("Default format", exact=True).fill("best[height<=360]")
        page.get_by_role("button", name="Save defaults", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="Defaults saved")).to_be_visible(timeout=7000)
        assert fixture.settings()["values"]["default_format"] == "best[height<=360]"
        page.get_by_label("Default format", exact=True).fill("best[height<=180]")
        fixture.patch({"default_format": "best[height<=240]"})
        page.get_by_role("button", name="Save defaults", exact=True).click()
        expect(page.get_by_role("alert").first).to_contain_text("changed", timeout=7000)
        expect(page.get_by_label("Default format", exact=True)).to_have_value("best[height<=180]")
        assert fixture.settings()["values"]["default_format"] == "best[height<=240]"
        page.get_by_role("button", name="Reload defaults", exact=True).click()
        expect(page.get_by_label("Default format", exact=True)).to_have_value("best[height<=240]")
        fixture.lost_operations.add("downloads.enqueue")
        page.get_by_label("Media URL", exact=True).fill("https://fixture.invalid/desktop-lost.mp4")
        page.get_by_role("button", name="Download", exact=True).click()
        expect(page.get_by_role("alert").filter(has_text="may have")).to_be_visible(timeout=7000)
        count = len(fixture.jobs())
        page.get_by_role("button", name="Check request", exact=True).click()
        expect(page.get_by_role("button", name="Check request", exact=True)).to_have_count(0, timeout=7000)
        assert len(fixture.jobs()) == count
        writes = [request for request in fixture.requests if request["operation"] == "downloads.enqueue"]
        assert writes[-1]["idempotency_key"] == writes[-2]["idempotency_key"]
        page.screenshot(path=str(root / "desktop-v1.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.screenshot(path=str(root / "desktop-narrow.png"), full_page=True)
        jobs = fixture.jobs()
        target = next(job for job in jobs if job["url"] == "https://fixture.invalid/desktop.mp4")
        row = page.locator(".job").nth(jobs.index(target))
        row.get_by_role("button", name="Cancel", exact=True).click()
        expect(row.get_by_text("Cancelled", exact=True)).to_be_visible(timeout=7000)
        action = [request for request in fixture.requests if request["operation"] == "downloads.action"][-1]
        assert action["params"]["job_id"] == target["id"]
        assert action["params"]["expected_revision"] == target["revision"]
        row.get_by_role("button", name="Retry", exact=True).click()
        expect(row.get_by_text("Queued", exact=True)).to_be_visible(timeout=7000)
        for index in range(26):
            fixture.request("downloads.enqueue", {"url": f"https://fixture.invalid/page-{index}.mp4", "title": f"Page fixture {index}"}, key=f"page-{index}")
        expect(page.get_by_role("button", name="Load more downloads", exact=True)).to_be_visible(timeout=7000)
        page.get_by_role("button", name="Load more downloads", exact=True).click()
        expect(page.locator(".job")).to_have_count(len(fixture.jobs()), timeout=7000)
        assert not errors, errors
    finally:
        context.close()
    return {"page_errors": errors, "scope": "Chrome renderer; mocked Tauri delivery; real temporary v1 service"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path, required=True)
    parser.add_argument("--desktop-url")
    parser.add_argument("--scope", choices=("browser", "desktop", "all"), default="browser")
    parser.add_argument("--chrome", type=Path, default=Path("/opt/google/chrome/chrome"),
                        help="Explicit Chrome executable for the selected validation target")
    arguments = parser.parse_args()
    arguments.screenshots.mkdir(parents=True, exist_ok=False)
    fixture = ClientFixture(arguments.screenshots / "runtime")
    report = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(arguments.chrome), headless=True,
                                                 args=["--disable-background-networking", "--disable-component-update", "--disable-sync"])
            try:
                if arguments.scope in ("browser", "all"):
                    report["browser"] = browser_journey(browser, fixture, arguments.screenshots)
                if arguments.scope in ("desktop", "all"):
                    assert arguments.desktop_url
                    report["desktop"] = desktop_journey(browser, fixture, arguments.screenshots, arguments.desktop_url)
            finally:
                browser.close()
        print(json.dumps(report, indent=2))
    finally:
        fixture.write_report(arguments.screenshots / "fixture-report.json")
        fixture.close()


if __name__ == "__main__":
    main()
