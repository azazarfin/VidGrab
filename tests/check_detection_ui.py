"""Opt-in P05 renderer fixtures; fake browser delivery, real temporary v1 state."""
import argparse
import json
import traceback
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.client_ui_support import ClientFixture

URL = "https://fixture.invalid/media.mp4?sig=a%2Fb+Z&part=2&part=1"
FRAME = "https://frame.fixture.invalid/player"
PAGE = "https://fixture.invalid/media.mp4"


def candidate(identity="selected-frame", frame=FRAME, tab=0, media=URL, **extra):
    return {"schema_version": 1, "id": identity, "url": media, "media_url": media,
            "page_url": PAGE, "frame_url": frame, "kind": "video", "source": "element",
            "title": 'বাংলা "media" <img onerror=window.pwned=1>', "observed_content_type": "video/mp4",
            "browser_local": False, "confidence": "observed", "browser_context": {"tab_id": tab, "frame_id": 7,
                "document_id": "fixture-document", "cookie_store_id": "fixture-container"}, **extra}


def content_overlay(browser, root):
    context = browser.new_context(viewport={"width": 640, "height": 400})
    page = context.new_page()
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="""
        <title>বাংলা media fixture</title><video id="player" controls width="300" height="180"></video>
        <audio id="audio" controls><source src="/song.mp3?sig=a%2Fb" type="audio/mpeg"></audio>
        """) if route.request.url == "https://page.fixture.invalid/watch" else route.abort())
    page.add_init_script("""(() => {
        window.__messages = [];
        window.chrome = {runtime: {lastError:null, id:'fixture-extension',
            onMessage: {addListener(listener) {window.__frameContext = listener;}},
            sendMessage(message, callback) {window.__messages.push(message); callback?.({success:true});}},
            storage: {local: {set() {throw new Error('An overlay must hand off through the background');}}}};
    })();""")
    try:
        page.goto("https://page.fixture.invalid/watch")
        page.wait_for_load_state("networkidle")
        page.evaluate("document.querySelector('#player').src = '" + URL + "'")
        for path in ("lib/media.js", "content/detector.js", "content/overlay.js"):
            page.add_script_tag(path=str(Path("extension") / path))
        page.add_style_tag(path="extension/content/content.css")
        page.locator("#player").hover()
        button = page.get_by_role("button", name="Select media in VidGrab", exact=True)
        expect(button).to_be_visible()
        button.focus()
        page.keyboard.press("Enter")
        expect(button).to_contain_text("Selected")
        page.wait_for_function("getComputedStyle(document.querySelector('#vidgrab-overlay-btn')).opacity === '1'", timeout=2000)
        selected = page.evaluate("window.__messages.find(item => item.type === 'video_selected')")
        assert selected["media_url"] == URL
        assert selected["frame_url"] == "https://page.fixture.invalid/watch"
        assert not page.evaluate("window.__messages.some(item => item.type === 'download_request')")
        page.screenshot(path=str(root / "overlay.png"))
        assert not failures, failures
        return {"selected_url_literal": True, "native_button_keyboard": True, "reports": page.evaluate("window.__messages.length")}
    finally:
        context.close()


def popup_handoff(browser, root):
    with ClientFixture(root / "service") as fixture:
        selected = candidate()
        fixture.preferences["detected_videos_0"] = [selected]
        fixture.preferences["pendingVideoCandidate"] = selected
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            expect(page.get_by_role("dialog", name="Download Options", exact=True)).to_be_visible()
            expect(page.locator("#modal-start-btn")).to_be_enabled(timeout=7000)
            assert fixture.probe_urls == [URL]
            assert fixture.context_calls[0]["referer"] == "https://frame.fixture.invalid/"
            assert fixture.context_calls[0]["cookies"] == []
            page.screenshot(path=str(root / "popup-handoff.png"))
            page.locator("#modal-start-btn").click()
            expect(page.locator("#format-modal")).to_be_hidden(timeout=7000)
            probe = next(item for item in fixture.requests if item["operation"] == "probes.create")
            enqueue = next(item for item in fixture.requests if item["operation"] == "downloads.enqueue")
            assert probe["params"]["auth_ref"] == enqueue["params"]["auth_ref"]
            assert enqueue["params"]["url"] == URL
            return {"same_context": True, "literal_url": True, "jobs": len(fixture.jobs())}
        finally:
            context.close()


def popup_scope(browser, root):
    with ClientFixture(root / "service") as fixture:
        fixture.preferences["pendingVideoCandidate"] = candidate(tab=9)
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            expect(page.locator("#backend-error-overlay")).to_be_hidden()
            assert fixture.probe_urls == []
            assert "pendingVideoCandidate" in fixture.preferences, "Another tab's pending selection must remain available"
            expect(page.locator("#format-modal")).to_be_hidden()
            return {"foreign_tab_preserved": True, "automatic_probes": 0}
        finally:
            context.close()


def popup_selection(browser, root):
    with ClientFixture(root / "service") as fixture:
        fixture.preferences["detected_videos_0"] = [candidate("frame-a"), candidate("frame-b", frame="https://other.fixture.invalid/player")]
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.locator('[data-tab="detected"]').click()
            rows = page.locator("#detected-list .download-card")
            expect(rows).to_have_count(2)
            expect(rows.first.locator(".card-title")).to_have_text(candidate()["title"])
            assert not page.locator("[onerror]").count()
            rows.first.locator("label.checkbox-container").click()
            expect(page.locator("#detected-selected-count")).to_have_text("1 selected")
            assert rows.nth(1).locator("input[type=checkbox]").is_checked() is False
            rows.nth(1).locator("label.checkbox-container").click()
            expect(page.locator("#detected-select-all")).to_be_checked()
            rows.nth(1).locator("label.checkbox-container").click()
            page.locator("#detected-remove-btn").click()
            expect(rows).to_have_count(1)
            assert fixture.preferences["detected_videos_0"][0]["id"] == "frame-b"
            assert page.evaluate("window.pwned") is None
            page.screenshot(path=str(root / "scoped-selection.png"))
            return {"independent_frame_selection": True, "dom_safe_title": True}
        finally:
            context.close()


def _measure_notice_contrast(page, selectors):
    return page.evaluate("""(selectors) => {
      const channels = color => color.match(/[\\d.]+/g).map(Number);
      const composite = (top, base) => top.slice(0, 3).map((v, i) => v * (top[3] ?? 1) + base[i] * (1 - (top[3] ?? 1)));
      const luminance = rgb => rgb.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
        .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      const ratio = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
      const background = el => {
        const layers = [];
        for (let node = el; node; node = node.parentElement) layers.push(channels(getComputedStyle(node).backgroundColor));
        return layers.reverse().reduce((base, top) => composite(top, base), [7, 7, 12]);
      };
      return selectors.map(selector => {
        const el = document.querySelector(selector);
        if (!el || !el.getClientRects().length) return { selector, visible: false, contrast: 0, fontSize: 0 };
        const style = getComputedStyle(el);
        return {
          selector,
          visible: true,
          fontSize: parseFloat(style.fontSize),
          contrast: ratio(channels(style.color), background(el)),
          rightClipped: el.getBoundingClientRect().right > innerWidth + 1 || el.scrollWidth > el.clientWidth + 1,
        };
      });
    }""", selectors)


def observation_preference(browser, root):
    notice_metrics = {}
    with ClientFixture(root / "service") as fixture:
        context = browser.new_context(viewport={"width": 1100, "height": 820})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        popup = None
        try:
            page.goto((Path.cwd() / "extension/options/options.html").as_uri())
            page.wait_for_load_state("networkidle")
            toggle = page.get_by_label("Observe media requests", exact=True)
            restart_notice = page.locator("#request-observation-restart-notice")
            expect(toggle).to_have_count(1)
            expect(restart_notice).to_be_hidden()

            # Before granting, Popup Detected notice stays hidden even though CSS sets display: flex when visible.
            popup = context.new_page()
            popup.set_viewport_size({"width": 320, "height": 580})
            popup.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            popup.wait_for_load_state("networkidle")
            popup.locator('[data-tab="detected"]').click()
            expect(popup.locator("#detected-restart-notice")).to_be_hidden()
            popup.locator('[data-tab="queue"]').click()

            assert fixture.preferences["requestObservationEnabled"] is False
            fixture.permission_approval = False
            toggle.locator("..").click()
            expect(toggle).not_to_be_checked()
            expect(restart_notice).to_be_hidden()
            assert fixture.preferences["requestObservationEnabled"] is False
            fixture.permission_approval = True
            toggle.locator("..").click()
            expect(toggle).to_be_checked()
            page.wait_for_function("document.querySelector('#permission-status').textContent.includes('Request observation: granted')")
            expect(restart_notice).to_be_visible()
            expect(restart_notice).to_contain_text("Restart Chrome")
            expect(page.locator("#status-message")).to_contain_text("Restart Chrome")
            assert fixture.preferences["requestObservationEnabled"] is True
            assert fixture.permission_requests == [{"permissions": ["webRequest", "webNavigation"]}] * 2

            # Verify visible Options notice contrast, font size and zero overflow at narrow 390x844 viewport.
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            options_samples = _measure_notice_contrast(page, ["#request-observation-restart-notice", "#request-observation-restart-notice strong"])
            for sample in options_samples:
                assert sample["visible"] and not sample["rightClipped"]
                assert sample["fontSize"] >= 12, sample
                assert sample["contrast"] >= 4.5, sample
            notice_metrics["options_min_contrast"] = min(sample["contrast"] for sample in options_samples)
            page.set_viewport_size({"width": 1100, "height": 820})

            # Already-open Popup refreshes #detected-restart-notice when switching to Detected tab,
            # and a freshly opened Popup shows it without overflow.
            popup.locator('[data-tab="detected"]').click()
            expect(popup.locator("#detected-restart-notice")).to_be_visible()
            popup.close()
            popup = context.new_page()
            popup.set_viewport_size({"width": 320, "height": 580})
            popup.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            popup.wait_for_load_state("networkidle")
            popup.locator('[data-tab="detected"]').click()
            popup_notice = popup.locator("#detected-restart-notice")
            expect(popup_notice).to_be_visible()
            expect(popup_notice).to_contain_text("Restart Chrome")
            assert popup.evaluate("document.documentElement.scrollWidth <= innerWidth")
            popup_samples = _measure_notice_contrast(
                popup, ["#detected-restart-notice p", "#detected-restart-notice strong", "#detected-restart-settings-btn"]
            )
            for sample in popup_samples:
                assert sample["visible"] and not sample["rightClipped"]
                assert sample["fontSize"] >= 12, sample
                assert sample["contrast"] >= 4.5, sample
            notice_metrics["popup_min_contrast"] = min(sample["contrast"] for sample in popup_samples)
            popup.screenshot(path=str(root / "popup-chrome-restart-notice.png"))
            popup.close()
            popup = None

            toggle.locator("..").click()
            expect(toggle).not_to_be_checked()
            expect(restart_notice).to_be_hidden()
            assert fixture.preferences["requestObservationEnabled"] is False
            toggle.locator("..").click()
            expect(toggle).to_be_checked()
            expect(restart_notice).to_be_visible()
            page.screenshot(path=str(root / "observation-preferences.png"))

            # Full browser restart clears the Chrome fresh-grant restart notice in both Options and Popup.
            fixture.restart_browser()
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(restart_notice).to_be_hidden()
            popup = context.new_page()
            popup.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            popup.wait_for_load_state("networkidle")
            popup.locator('[data-tab="detected"]').click()
            expect(popup.locator("#detected-restart-notice")).to_be_hidden()
            popup.close()
            popup = None
            assert not any(item["operation"] == "settings.patch" for item in fixture.requests)
        finally:
            if popup is not None:
                popup.close()
            context.close()

    with ClientFixture(root / "firefox") as ff_fixture:
        ff_fixture.is_firefox = True
        context = browser.new_context(viewport={"width": 1100, "height": 820})
        ff_fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        popup = None
        try:
            page.goto((Path.cwd() / "extension/options/options.html").as_uri())
            page.wait_for_load_state("networkidle")
            toggle = page.get_by_label("Observe media requests", exact=True)
            toggle.locator("..").click()
            expect(toggle).to_be_checked()
            expect(page.locator("#request-observation-restart-notice")).to_be_hidden()
            popup = context.new_page()
            popup.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            popup.wait_for_load_state("networkidle")
            popup.locator('[data-tab="detected"]').click()
            expect(popup.locator("#detected-restart-notice")).to_be_hidden()
            popup.close()
            popup = None
        finally:
            if popup is not None:
                popup.close()
            context.close()
    return {"declined_keeps_off": True, "explicit_grants": True, "browser_only": True, "chrome_restart_notice": True, **notice_metrics}


def expired_format_review(browser, root):
    with ClientFixture(root / "service") as fixture:
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.get_by_label("Media URL", exact=True).fill(URL)
            page.locator("#download-btn").click()
            expect(page.locator("#modal-start-btn")).to_be_enabled()
            page.locator("#format-select").select_option("720")
            fixture.now += 301
            page.locator("#modal-start-btn").click()
            expect(page.locator("#modal-author")).to_contain_text("refreshed once")
            assert fixture.probe_urls == [URL, URL]
            assert fixture.jobs() == [], "Refreshing a format list requires a second explicit Start Download"
            assert page.locator("#format-select").input_value() == "720"
            page.screenshot(path=str(root / "expired-format-review.png"))
            page.locator("#modal-start-btn").click()
            expect(page.locator("#format-modal")).to_be_hidden()
            assert len(fixture.jobs()) == 1
            assert len(fixture.probe_urls) == 2
            return {"one_reprobe": True, "quality_kept": True, "explicit_enqueue": True}
        finally:
            context.close()


def probe_recovery_choices(browser, root):
    from backend.services.retry_policy import RequestFailure
    results = {}
    for name, code in (("login", "AUTH_REQUIRED"), ("throttle", "UPSTREAM_RATE_LIMITED"), ("unsupported", "REQUEST_REJECTED")):
        with ClientFixture(root / name) as fixture:
            fixture.probe_error = RequestFailure(code, retry_at=fixture.now + 60 if name == "throttle" else None)
            context = browser.new_context(viewport={"width": 420, "height": 620})
            fixture.install(context)
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
            page = context.new_page()
            try:
                page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
                page.wait_for_load_state("networkidle")
                page.get_by_label("Media URL", exact=True).fill(URL)
                page.locator("#download-btn").click()
                action = page.locator("#probe-action-btn")
                expect(action).to_be_visible()
                if name == "login":
                    expect(action).to_have_text("Open source page")
                    expect(page.locator("#fresh-probe-btn")).to_be_visible()
                elif name == "throttle":
                    expect(action).to_contain_text("Retry after")
                    expect(action).to_be_disabled()
                else:
                    expect(action).to_have_text("Choose another source")
                    action.click()
                    expect(page.locator("#format-modal")).to_be_hidden()
                    expect(page.locator("#panel-detected")).to_have_class("tab-content active")
                assert fixture.probe_urls == [URL]
                assert fixture.jobs() == []
                page.screenshot(path=str(root / (name + ".png")))
                results[name] = True
            finally:
                context.close()
    return results


def expired_format_limits(browser, root):
    with ClientFixture(root / "service") as fixture:
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.get_by_label("Media URL", exact=True).fill(URL)
            page.locator("#download-btn").click()
            expect(page.locator("#modal-start-btn")).to_be_enabled()
            page.locator("#format-select").select_option("720")
            page.get_by_label("Title", exact=True).fill("Keep my title বাংলা")
            fixture.probe_formats = [{**fixture.probe_formats[0], "format_id": "replacement"}]
            fixture.now += 301
            page.locator("#modal-start-btn").click()
            expect(page.locator("#modal-author")).to_contain_text("refreshed once")
            assert page.locator("#format-select").input_value() == "replacement"
            expect(page.get_by_label("Title", exact=True)).to_have_value("Keep my title বাংলা")
            assert len(fixture.probe_urls) == 2
            fixture.now += 301
            page.locator("#modal-start-btn").click()
            expect(page.locator("#modal-author")).to_contain_text("expired again")
            expect(page.locator("#modal-start-btn")).to_be_disabled()
            expect(page.locator("#fresh-probe-btn")).to_be_visible()
            assert len(fixture.probe_urls) == 2, "A second expiry must not trigger another automatic probe"
            assert fixture.jobs() == []
            page.screenshot(path=str(root / "second-expiry.png"))
            return {"equivalent_format_id": True, "title_preserved": True, "second_expiry_explicit": True}
        finally:
            context.close()


def probe_context_scope_review(browser, root):
    with ClientFixture(root / "service") as fixture:
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.get_by_label("Media URL", exact=True).fill(URL)
            page.locator("#download-btn").click()
            expect(page.locator("#modal-start-btn")).to_be_enabled()
            page.locator("#format-select").select_option("720")
            fixture.preferences["cookieForwardingEnabled"] = True
            page.locator("#modal-start-btn").click()
            expect(page.locator("#modal-author")).to_contain_text("same media and browser context")
            page.screenshot(path=str(root / "context-scope-rejected.png"))
            expect(page.locator("#modal-start-btn")).to_be_disabled()
            expect(page.locator("#fresh-probe-btn")).to_be_visible()
            assert fixture.probe_urls == [URL]
            assert fixture.jobs() == []
            page.locator("#fresh-probe-btn").click()
            expect(page.locator("#modal-start-btn")).to_be_enabled()
            assert fixture.probe_urls == [URL, URL]
            assert fixture.jobs() == []
            page.locator("#format-select").select_option("720")
            page.locator("#modal-start-btn").click()
            expect(page.locator("#format-modal")).to_be_hidden()
            assert len(fixture.jobs()) == 1
            return {"changed_context_rejected": True, "explicit_fresh_probe": True, "explicit_enqueue": True}
        finally:
            context.close()


def default_selections_omit_witnesses(browser, root):
    results = {}
    for selection in ("defaults", "audio", "playlist"):
        with ClientFixture(root / selection) as fixture:
            if selection == "playlist":
                fixture.probe_extra = {"_type": "playlist", "entries": [
                    {"id": "fixture-item", "url": URL, "title": "Playlist item", "duration": 12}]}
            context = browser.new_context(viewport={"width": 420, "height": 620})
            fixture.install(context)
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
            page = context.new_page()
            try:
                page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
                page.wait_for_load_state("networkidle")
                page.get_by_label("Media URL", exact=True).fill(URL)
                page.locator("#download-btn").click()
                expect(page.locator("#modal-start-btn")).to_be_enabled()
                if selection == "defaults":
                    page.locator("#format-select").select_option("")
                elif selection == "audio":
                    page.locator("#format-select").select_option("720")
                    page.locator("#option-audio-only").locator("..").click()
                    expect(page.locator("#option-audio-only")).to_be_checked()
                fixture.now += 301
                page.locator("#modal-start-btn").click()
                expect(page.locator("#format-modal")).to_be_hidden()
                enqueue = next(item for item in fixture.requests if item["operation"] == "downloads.enqueue")
                assert "probe_id" not in enqueue["params"]
                assert len(fixture.jobs()) == len(fixture.probe_urls) == 1
                results[selection] = True
            finally:
                context.close()
    return results


def incompatible_probe(browser, root):
    with ClientFixture(root / "service") as fixture:
        fixture.excluded_features.add("probes.fresh")
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.get_by_label("Media URL", exact=True).fill(URL)
            page.locator("#download-btn").click()
            expect(page.locator("#modal-author")).to_contain_text("Update VidGrab")
            expect(page.locator("#probe-action-btn")).to_have_text("Review VidGrab update")
            expect(page.locator("#modal-start-btn")).to_be_disabled()
            assert fixture.probe_urls == []
            assert fixture.jobs() == []
            page.screenshot(path=str(root / "incompatible-probe.png"))
            return {"upgrade_guidance": True, "no_probe_or_enqueue": True}
        finally:
            context.close()


def job_frame_context(browser, root):
    with ClientFixture(root / "service") as fixture:
        fixture.preferences["detected_videos_0"] = [candidate("frame-a"), candidate("frame-b", frame="https://other.fixture.invalid/player")]
        reference = fixture.request("auth.create", {"instance_id": fixture.engine.app.state.owner.id, "url": URL,
                                    "cookies": [], "referer": "https://frame.fixture.invalid/"}, "initial-headers")["reference"]
        job = fixture.request("downloads.enqueue", {"url": URL, "auth_ref": reference}, "initial-job")["job_id"]
        fixture.set_status(job, "waiting_for_auth")
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(fixture.runtime.db_path)) as db, db:
            db.execute("UPDATE downloads SET error_code='BROWSER_CONTEXT_REQUIRED' WHERE id=?", (job,))
        context = browser.new_context(viewport={"width": 320, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.locator('[data-tab="queue"]').click()
            card = page.locator("#task-card-" + job)
            choice = card.get_by_label("Source frame", exact=True)
            expect(choice).to_be_visible()
            expect(card.get_by_role("button", name="Share fresh context", exact=True)).to_be_disabled()
            assert fixture.context_calls == []
            choice.select_option("frame-b")
            page.screenshot(path=str(root / "job-frame-context.png"))
            card.get_by_role("button", name="Share fresh context", exact=True).click()
            expect(card.locator(".card-status-badge")).to_have_text("queued")
            assert fixture.context_calls[-1]["job_id"] == job
            assert fixture.context_calls[-1]["referer"] == "https://other.fixture.invalid/"
            assert fixture.context_calls[-1]["cookies"] == []
            card.get_by_role("button", name="Cancel", exact=True).click()
            expect(card.get_by_label("Source frame", exact=True)).to_have_value("frame-b")
            card.get_by_role("button", name="Retry", exact=True).click()
            expect(card.locator(".card-status-badge")).to_have_text("queued")
            assert fixture.context_calls[-1]["referer"] == "https://other.fixture.invalid/"
            assert len(fixture.context_calls) == 2
            assert len(fixture.jobs()) == 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            return {"explicit_frame": True, "retry_retains_choice": True, "header_only": True, "compact_width": 320}
        finally:
            context.close()


def lost_witness_reply(browser, root):
    with ClientFixture(root / "service") as fixture:
        fixture.lost_operations.add("downloads.enqueue")
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.get_by_label("Media URL", exact=True).fill(URL)
            page.locator("#download-btn").click()
            expect(page.locator("#modal-start-btn")).to_be_enabled()
            page.locator("#format-select").select_option("720")
            page.locator("#modal-start-btn").click()
            expect(page.locator("#check-request-btn")).to_be_visible()
            assert len(fixture.probe_urls) == 1
            assert len(fixture.jobs()) == 1
            fixture.now += 301
            page.locator("#check-request-btn").click()
            expect(page.locator("#client-status")).to_contain_text("Request checked")
            submissions = [request for request in fixture.requests if request["operation"] == "downloads.enqueue"]
            assert len(submissions) == 2
            assert submissions[0]["params"] == submissions[1]["params"]
            assert submissions[0]["idempotency_key"] == submissions[1]["idempotency_key"]
            assert len(fixture.jobs()) == len(fixture.probe_urls) == 1
            page.screenshot(path=str(root / "lost-witness-reply.png"))
            return {"explicit_review": True, "expired_witness_receipt_replay": True, "automatic_reprobes": 0}
        finally:
            context.close()


def corrected_submission(browser, root):
    with ClientFixture(root / "service") as fixture:
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.get_by_label("Media URL", exact=True).fill(URL)
            page.locator("#download-btn").click()
            expect(page.locator("#modal-start-btn")).to_be_enabled()
            page.get_by_label("Title", exact=True).fill("x" * 513)
            page.locator("#modal-start-btn").click()
            expect(page.locator("#modal-author")).to_contain_text("contract")
            expect(page.locator("#modal-start-btn")).to_be_enabled()
            assert fixture.jobs() == []
            page.get_by_label("Title", exact=True).fill("Corrected title")
            page.locator("#modal-start-btn").click()
            expect(page.locator("#format-modal")).to_be_hidden()
            assert len(fixture.jobs()) == 1
            return {"definite_rejection_correctable": True}
        finally:
            context.close()


def closed_expiry_review(browser, root):
    with ClientFixture(root / "service") as fixture:
        context = browser.new_context(viewport={"width": 420, "height": 620})
        fixture.install(context)
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        try:
            page.goto((Path.cwd() / "extension/popup/popup.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.get_by_label("Media URL", exact=True).fill(URL)
            page.locator("#download-btn").click()
            expect(page.locator("#modal-start-btn")).to_be_enabled()
            page.locator("#format-select").select_option("720")
            fixture.now += 301
            page.evaluate("""() => {
                const original = window.__vidgrabControl;
                window.__vidgrabControl = async message => {
                    const reply = await original(message);
                    if (message.type === 'companion.request' && message.request.operation === 'downloads.enqueue') {
                        await new Promise(resolve => setTimeout(resolve, 300));
                        window.__expiryReply = true;
                    }
                    return reply;
                };
            }""")
            page.locator("#modal-start-btn").click()
            page.locator("#modal-close-btn").click()
            page.wait_for_function("window.__expiryReply === true")
            expect(page.locator("#format-modal")).to_be_hidden()
            assert fixture.probe_urls == [URL]
            assert fixture.jobs() == []
            return {"closed_dialog_stays_closed": True, "automatic_reprobes": 0}
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--chrome", default="/opt/google/chrome/chrome")
    parser.add_argument("--case", choices=["all", "overlay", "handoff", "scope", "selection", "permissions", "expired", "recovery", "expiry-limits",
                                          "context-scope", "default-witnesses", "incompatible", "job-frame", "lost-witness",
                                          "corrected-submission", "closed-expiry"], default="all")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    results, failed = {}, {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chrome, headless=True)
        try:
            for name, run in {"overlay": content_overlay, "handoff": popup_handoff, "scope": popup_scope, "selection": popup_selection,
                              "permissions": observation_preference, "expired": expired_format_review, "recovery": probe_recovery_choices,
                              "expiry-limits": expired_format_limits, "context-scope": probe_context_scope_review,
                              "default-witnesses": default_selections_omit_witnesses, "incompatible": incompatible_probe, "job-frame": job_frame_context,
                              "lost-witness": lost_witness_reply, "corrected-submission": corrected_submission, "closed-expiry": closed_expiry_review}.items():
                if args.case not in ("all", name):
                    continue
                output = root / name
                output.mkdir()
                try:
                    results[name] = run(browser, output)
                except Exception as error:
                    failed[name] = str(error) or traceback.format_exc()
        finally:
            browser.close()
    report = {"ok": not failed, "passed": results, "failed": failed, "scope": "headless Chromium, mocked browser delivery; temporary non-executing v1 service"}
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(bool(failed))


if __name__ == "__main__":
    main()
