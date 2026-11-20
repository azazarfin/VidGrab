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
        assert options.locator("#btn-save-settings").count() == 0
        assert options.locator("#input-max-concurrent").count() == 0
        expect(options.locator("#summary-default-format")).to_have_text("bestvideo+bestaudio/best")
        expect(options.locator("#summary-max-concurrent")).to_have_text("3")
        expect(options.locator("#summary-transliterate-bangla")).to_have_text("Transliterate to Latin")
        fixture.patch({"max_concurrent": 8, "default_format": "best[height<=480]", "transliterate_bangla_filenames": False})
        options.locator("#btn-reload-settings").click()
        expect(options.locator("#summary-max-concurrent")).to_have_text("8", timeout=7000)
        expect(options.locator("#summary-default-format")).to_have_text("best[height<=480]")
        expect(options.locator("#summary-transliterate-bangla")).to_have_text("Keep native Unicode")
        patch_count_before = sum(1 for r in fixture.requests if r["operation"] == "settings.patch")
        options.locator("label.switch:has(#chk-cookie-forwarding)").click()
        expect(options.locator("#status-message")).to_contain_text("Cookie forwarding enabled", timeout=7000)
        assert fixture.preferences["cookieForwardingEnabled"] is True
        assert sum(1 for r in fixture.requests if r["operation"] == "settings.patch") == patch_count_before
        expect(options.locator("#permission-status")).to_contain_text("Cookies: granted")
        options.locator("#btn-open-app").click()
        expect(options.locator("#status-message")).to_contain_text("Opening VidGrab", timeout=7000)
        assert fixture.activated >= 1
        fixture.pending = [{"key": "reviewed-notice", "operation": "downloads.enqueue"}]
        options.reload()
        options.wait_for_load_state("networkidle")
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
        protected.get_by_role("button", name="Pause", exact=True).click()
        expect(protected.locator(".card-status-badge")).to_have_text("Paused", timeout=7000)
        protected.get_by_role("button", name="Resume", exact=True).click()
        expect(protected.locator(".card-status-badge")).to_have_text("queued", timeout=7000)
        protected.get_by_role("button", name="Cancel", exact=True).click()
        expect(popup.locator("#recent-list #task-card-" + first["id"])).to_be_visible(timeout=7000)
        expect(protected.get_by_role("button", name="Retry", exact=True)).to_be_visible(timeout=7000)
        protected.get_by_role("button", name="Retry", exact=True).click()
        expect(popup.locator("#queue-list #task-card-" + first["id"])).to_be_visible(timeout=7000)
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
        popup.locator('[data-tab="detected"]').click()
        expect(popup.locator("#panel-detected")).to_be_visible()
        assert not errors, errors
    finally:
        fixture.excluded_features.clear()
        context.close()
    return {"page_errors": errors, "blocked_requests": len(blocked), "scope": "Chrome renderer; mocked browser/native delivery; real temporary v1 service"}


def desktop_journey(browser, fixture, root, url):
    context = browser.new_context(viewport={"width": 980, "height": 820})
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
        expect(page.locator(".onboarding-banner")).to_be_visible(timeout=7000)
        page.get_by_role("button", name="Finish setup", exact=True).click()
        expect(page.locator(".onboarding-banner")).to_have_count(0, timeout=7000)
        assert fixture.desktop_preferences["onboarding_completed"] is True

        # Queue add + priority/options + inspect formats + multi-line paste + pause/resume + cancel/retry
        page.get_by_label("Media URL", exact=True).fill("https://fixture.invalid/inspect-me.mp4")
        page.get_by_role("button", name="Inspect formats", exact=True).click()
        expect(page.get_by_label("Format / resolution", exact=True)).to_be_visible(timeout=7000)
        page.get_by_label("Format / resolution", exact=True).select_option("720")
        page.get_by_role("button", name="Download", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="queued")).to_be_visible(timeout=7000)
        inspected_enqueue = [r for r in fixture.requests if r["operation"] == "downloads.enqueue"][-1]
        assert inspected_enqueue["params"]["format_id"] == "720"
        assert inspected_enqueue["params"]["probe_id"]

        page.get_by_role("button", name="Paste URL list", exact=True).click()
        page.get_by_label("Paste URLs (one per line)", exact=True).fill(
            "https://fixture.invalid/paste-1.mp4\nfile:///invalid-scheme\nhttps://fixture.invalid/paste-2.mp4"
        )
        page.get_by_role("button", name="Review pasted URLs", exact=True).click()
        expect(page.locator(".batch-preview")).to_contain_text("2 of 3 URLs ready", timeout=7000)
        expect(page.locator(".batch-preview .error")).to_contain_text("HTTP or HTTPS")
        page.get_by_role("button", name="Queue selected", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="2 downloads queued")).to_be_visible(timeout=7000)

        page.get_by_role("button", name="Options", exact=True).click()
        page.get_by_label("Priority", exact=True).select_option("10")
        page.get_by_label("Media URL", exact=True).fill("https://fixture.invalid/desktop.mp4")
        page.get_by_role("button", name="Download", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="queued")).to_be_visible(timeout=7000)
        desktop_job = next(j for j in fixture.jobs() if j["url"] == "https://fixture.invalid/desktop.mp4")
        assert desktop_job["priority"] == 10
        row = page.locator(".queue .job").filter(has=page.locator(".meta-chip", has_text="Priority +10"))
        row.get_by_role("button", name="Pause", exact=True).click()
        expect(row.locator(".job-status")).to_have_text("Paused", timeout=7000)
        row.get_by_role("button", name="Resume", exact=True).click()
        expect(row.locator(".job-status")).to_have_text("Queued", timeout=7000)
        row.get_by_role("button", name="Cancel", exact=True).click()
        expect(page.locator(".queue .job").filter(has=page.locator(".meta-chip", has_text="Priority +10"))).to_have_count(0, timeout=7000)

        # History tab: search/filter, retry, delete entry keeping saved file, clear history dialog
        kept_file = fixture.runtime.download_dir / "kept-desktop.mp4"
        kept_file.parent.mkdir(parents=True, exist_ok=True)
        kept_file.write_bytes(b"kept")
        page.locator('[data-desktop-tab="history"]').click()
        hist_row = page.locator(".history-view .job").first
        expect(hist_row.locator(".job-status")).to_have_text("Cancelled", timeout=7000)
        page.get_by_label("Search history", exact=True).fill("Download")
        expect(page.locator(".history-view .job")).to_have_count(1)
        page.get_by_label("Search history", exact=True).fill("zzz-missing-query")
        expect(page.locator(".history-view .job")).to_have_count(0)
        page.get_by_label("Search history", exact=True).fill("")
        page.get_by_label("Filter history status", exact=True).select_option("complete")
        expect(page.locator(".history-view .job")).to_have_count(0)
        page.get_by_label("Filter history status", exact=True).select_option("cancelled")
        expect(page.locator(".history-view .job")).to_have_count(1)
        page.get_by_label("Filter history status", exact=True).select_option("all")
        hist_row.get_by_role("button", name="Retry", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="Download action accepted")).to_be_visible(timeout=7000)
        fixture.set_status(desktop_job["id"], "complete")
        with __import__("contextlib").closing(__import__("sqlite3").connect(fixture.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET file_path=? WHERE id=?", (str(kept_file), desktop_job["id"]))
        page.locator(".history-view").get_by_role("button", name="Refresh", exact=True).click()
        expect(page.locator(".history-view .job")).to_have_count(1, timeout=7000)
        page.locator(".history-view .job").first.get_by_role("button", name="Remove entry", exact=True).click()
        expect(page.locator(".history-view .job")).to_have_count(0, timeout=7000)
        assert kept_file.is_file(), "Removing history entry must keep files on disk"

        # Seed finished history entry and confirm Clear history modal keeps saved files
        extra_done = fixture.request("downloads.enqueue", {"url": "https://fixture.invalid/clear-hist.mp4", "title": "Clear history item"}, key="clear-hist-item")["job_id"]
        fixture.set_status(extra_done, "complete")
        with __import__("contextlib").closing(__import__("sqlite3").connect(fixture.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET file_path=? WHERE id=?", (str(kept_file), extra_done))
        page.locator(".history-view").get_by_role("button", name="Refresh", exact=True).click()
        expect(page.locator(".history-view .job")).to_have_count(1, timeout=7000)
        page.get_by_role("button", name="Clear history", exact=True).click()
        expect(page.get_by_role("dialog")).to_contain_text("Downloaded files saved on disk are kept.")
        expect(page.get_by_role("button", name="Keep history", exact=True)).to_be_focused()
        page.get_by_role("button", name="Keep history", exact=True).click()
        expect(page.locator(".history-view .job")).to_have_count(1)
        page.get_by_role("button", name="Clear history", exact=True).click()
        page.get_by_role("button", name="Clear history records", exact=True).click()
        expect(page.locator(".history-view .job")).to_have_count(0, timeout=7000)
        assert kept_file.is_file(), "Clearing history must keep files on disk"

        # Settings tab: defaults + speed/quality/codec/threads conversion + legacy args clear + conflict + Bangla toggle + scheduling note
        with __import__("contextlib").closing(__import__("sqlite3").connect(fixture.runtime.db_path)) as db, db:
            db.execute("UPDATE config SET value=? WHERE key='custom_ytdlp_args'", (json.dumps("--no-mtime"),))
            db.execute("INSERT INTO protocol_events(topic,entity_id,revision,kind) VALUES('settings','settings',?,'changed')", (fixture.settings()["revision"],))
        page.locator('[data-desktop-tab="settings"]').click()
        expect(page.get_by_role("heading", name="Download defaults")).to_be_visible(timeout=7000)
        expect(page.get_by_text("Scheduled downloads run while VidGrab is open and the computer is awake; missed times run on next startup.", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Clear legacy arguments", exact=True)).to_be_visible(timeout=7000)
        page.get_by_role("button", name="Clear legacy arguments", exact=True).click()
        expect(page.get_by_role("button", name="Clear legacy arguments", exact=True)).to_have_count(0, timeout=7000)
        assert fixture.settings()["values"]["custom_ytdlp_args"] == ""
        page.get_by_label("Default format", exact=True).fill("best[height<=360]")
        page.get_by_label("Preferred quality", exact=True).select_option("best")
        page.get_by_label("Post-processing codec", exact=True).select_option("mkv")
        page.get_by_label("Fragments / threads per job", exact=True).fill("24")
        page.get_by_label("Speed limit (KiB/s, 0 unlimited)", exact=True).fill("2048")
        page.get_by_label("Transliterate Bangla titles to Latin filenames", exact=True).uncheck()
        page.get_by_role("button", name="Save defaults", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="Defaults saved")).to_be_visible(timeout=7000)
        saved_vals = fixture.settings()["values"]
        assert saved_vals["default_format"] == "best[height<=360]"
        assert saved_vals["default_quality"] == "best"
        assert saved_vals["postprocess_codec"] == "mkv"
        assert saved_vals["download_threads"] == 24
        assert saved_vals["speed_limit"] == 2048 * 1024
        assert saved_vals["transliterate_bangla_filenames"] is False
        page.reload(wait_until="networkidle")
        page.locator('[data-desktop-tab="settings"]').click()
        expect(page.get_by_label("Speed limit (KiB/s, 0 unlimited)", exact=True)).to_have_value("2048", timeout=7000)
        expect(page.get_by_label("Preferred quality", exact=True)).to_have_value("best")
        expect(page.get_by_label("Post-processing codec", exact=True)).to_have_value("mkv")
        expect(page.get_by_label("Fragments / threads per job", exact=True)).to_have_value("24")
        page.get_by_label("Default format", exact=True).fill("best[height<=180]")
        fixture.patch({"default_format": "best[height<=240]"})
        page.get_by_role("button", name="Save defaults", exact=True).click()
        expect(page.get_by_role("alert").first).to_contain_text("changed", timeout=7000)
        expect(page.get_by_label("Default format", exact=True)).to_have_value("best[height<=180]")
        assert fixture.settings()["values"]["default_format"] == "best[height<=240]"
        page.get_by_role("button", name="Reload defaults", exact=True).click()
        expect(page.get_by_label("Default format", exact=True)).to_have_value("best[height<=240]")

        # Browser & Diagnostics tabs
        page.locator('[data-desktop-tab="browser"]').click()
        expect(page.get_by_role("heading", name="Browser connections")).to_be_visible(timeout=7000)
        page.locator('[data-desktop-tab="diagnostics"]').click()
        expect(page.get_by_role("heading", name="Diagnostics & components")).to_be_visible(timeout=7000)
        expect(page.get_by_text("2026.6.9", exact=True)).to_be_visible(timeout=7000)
        expect(page.get_by_text("INFO engine ready", exact=True)).to_be_visible(timeout=7000)
        expect(page.locator(".browser-registration div", has_text="Engine generation").locator("dd")).to_have_text("1")
        page.get_by_role("button", name="Copy redacted diagnostics", exact=True).click()
        expect(page.get_by_role("status").filter(has_text="Redacted diagnostics copied")).to_be_visible(timeout=7000)
        page.get_by_role("button", name="Restart engine", exact=True).click()
        assert fixture.restarts == 1
        expect(page.locator(".browser-registration div", has_text="Engine generation").locator("dd")).to_have_text("2", timeout=7000)

        # Queue lost-reply, responsive 390px, auth/format recovery, and pagination
        page.locator('[data-desktop-tab="queue"]').click()
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

        header_context = fixture.request("auth.create", {"instance_id": fixture.engine.app.state.owner.id,
            "url": "https://fixture.invalid/header-context.mp4", "cookies": [], "referer": "https://fixture.invalid/"}, key="desktop-header-context")
        header_job = fixture.request("downloads.enqueue", {"url": "https://fixture.invalid/header-context.mp4",
            "auth_ref": header_context["reference"], "title": "Header context fixture"}, key="desktop-header-job")["job_id"]
        fixture.set_status(header_job, "waiting_for_auth", error_code="BROWSER_CONTEXT_REQUIRED")
        header_row = page.locator(".queue .job").filter(has=page.get_by_text("Header context fixture", exact=True))
        expect(header_row.locator(".job-status")).to_contain_text("browser context", timeout=7000)
        expect(header_row.locator(".helper").filter(has_text="source page")).to_be_visible()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.screenshot(path=str(root / "desktop-header-context.png"), full_page=True)
        count = len(fixture.jobs())
        fixture.set_status(header_job, "failed", error_code="FORMAT_UNAVAILABLE")
        page.locator(".queue .pill", has_text="All loaded").click()
        expect(header_row.locator(".helper").filter(has_text="current formats")).to_be_visible(timeout=7000)
        expect(header_row.locator(".file-name")).to_have_text("https://fixture.invalid/header-context.mp4")
        assert len(fixture.jobs()) == count, "Format recovery guidance must not submit another job"
        for index in range(26):
            fixture.request("downloads.enqueue", {"url": f"https://fixture.invalid/page-{index}.mp4", "title": f"Page fixture {index}"}, key=f"page-{index}")
        expect(page.get_by_role("button", name="Load more downloads", exact=True)).to_be_visible(timeout=7000)
        page.get_by_role("button", name="Load more downloads", exact=True).click()
        expect(page.locator(".queue .job")).to_have_count(len(fixture.jobs()), timeout=7000)

        # Verify Queue tab (Unfinished filter) still surfaces an active job when >= 25 finished jobs sort ahead of it
        all_jobs = sorted(fixture.jobs(), key=lambda j: j["id"])
        assert len(all_jobs) >= 26
        for finished_job in all_jobs[:-1]:
            if finished_job["status"] not in ("complete", "failed", "cancelled"):
                fixture.set_status(finished_job["id"], "complete")
        tail_active_job = all_jobs[-1]
        fixture.set_status(tail_active_job["id"], "queued")
        page.reload(wait_until="networkidle")
        expect(page.locator(".queue .job")).to_have_count(1, timeout=7000)
        expect(page.locator(".queue .job").first).to_contain_text(tail_active_job["title"] or "Download", timeout=7000)
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
