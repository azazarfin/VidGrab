"""Opt-in visual checks with a temporary v1 service and mocked browser delivery."""

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.client_ui_support import ClientFixture


MEASURE = """() => {
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
  const samples = [];
  for (const el of document.querySelectorAll('.input-hint, .switch-desc, .section-desc, .tab-subtitle, .nav-item.active, .btn-primary, .alert-warning')) {
    if (!el.getClientRects().length) continue;
    const style = getComputedStyle(el);
    const gradientColors = style.backgroundImage.match(/rgba?\\([^)]+\\)/g) || [];
    samples.push({selector: el.id || el.className, fontSize: parseFloat(style.fontSize),
      contrast: Math.min(ratio(channels(style.color), background(el)),
        ...gradientColors.map(color => ratio(channels(style.color), channels(color))))});
  }
  for (const el of document.querySelectorAll('.form-input')) {
    if (!el.getClientRects().length || !el.placeholder) continue;
    samples.push({selector: el.id + '::placeholder', fontSize: parseFloat(getComputedStyle(el).fontSize),
      contrast: ratio(channels(getComputedStyle(el, '::placeholder').color), background(el))});
  }
  const containers = [...document.querySelectorAll('.sidebar, .content-area, .settings-content, .card, .form-row, .footer')];
  const overflow = containers.some(el => el.getClientRects().length && (
    el.scrollWidth > el.clientWidth + 1 || el.getBoundingClientRect().right > innerWidth + 1));
  return {samples, overflow,
    headings: [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(el => Number(el.tagName[1]))};
}"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true", help="Record current findings without failing")
    parser.add_argument("--chrome", type=Path, default=Path("/opt/google/chrome/chrome"),
                        help="Explicit Chrome executable for the selected validation target")
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=False)
    findings, blocked, errors, measurements = [], [], [], []

    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        if url.scheme not in ("http", "https"):
            route.continue_()
            return
        blocked.append(request.url)
        route.abort()

    with ClientFixture(args.screenshots / "runtime") as fixture, sync_playwright() as playwright:
        fixture.engine.app.state.control.component_versions = {"yt_dlp": "2026.6.9"}
        browser = playwright.chromium.launch(
            executable_path=str(args.chrome), headless=True,
            args=["--disable-background-networking", "--disable-component-update", "--disable-sync",
                  "--host-resolver-rules=MAP * ~NOTFOUND"],
        )
        try:
            for width, height in ((1280, 900), (768, 900), (390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce")
                try:
                    fixture.install(context)
                    context.route("**/*", route_request)
                    page = context.new_page()
                    page.set_default_timeout(5000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto((Path(__file__).resolve().parents[1] / "extension/options/options.html").as_uri())
                    page.wait_for_load_state("networkidle")
                    page.locator("#status-message").filter(has_text="Settings loaded successfully.").wait_for()
                    for tab in ("general", "sponsorblock", "postprocess", "advanced"):
                        page.locator("#btn-tab-" + tab).click()
                        page.locator("#tab-" + tab).wait_for(state="visible")
                        if page.locator("#btn-tab-" + tab).get_attribute("aria-pressed") != "true":
                            findings.append(f"{width}px/{tab}: selected section is not announced")
                        if tab == "sponsorblock" and not page.locator("#chk-sponsorblock-enabled").is_checked():
                            page.locator("label.switch:has(#chk-sponsorblock-enabled)").click()
                        if tab == "sponsorblock" and not page.get_by_role("checkbox", name="SponsorBlock Integration").count():
                            findings.append(f"{width}px/{tab}: unlabeled integration switch")
                        result = page.evaluate(MEASURE)
                        measurements.append({"width": width, "tab": tab, **result})
                        scope = f"{width}px/{tab}"
                        if result["overflow"]:
                            findings.append(scope + ": horizontal overflow")
                        if any(b > a + 1 for a, b in zip(result["headings"], result["headings"][1:])):
                            findings.append(scope + ": skipped heading level")
                        for sample in result["samples"]:
                            if sample["contrast"] < 4.5 or sample["fontSize"] < 12:
                                findings.append(f"{scope}: {sample['selector']} ({sample['contrast']:.2f}:1, {sample['fontSize']}px)")
                        if width == 1280 or tab in ("general", "advanced"):
                            page.screenshot(path=str(args.screenshots / f"{width}-{tab}.png"))
                    assert page.locator("#chk-auto-update-ytdlp").is_disabled()
                    assert not page.locator("#chk-auto-update-ytdlp").is_checked()
                    assert page.locator("#btn-update-ytdlp").is_disabled()
                    page.locator("#btn-save-settings").click()
                    page.locator("#status-message").filter(has_text="Settings saved and synced successfully.").wait_for()
                    assert fixture.settings()["values"]["auto_update_ytdlp"] is False
                finally:
                    context.close()
        finally:
            browser.close()
            fixture.write_report(args.screenshots / "fixture-report.json")
    report = {"playwright": version("playwright"), "screenshots": str(args.screenshots),
              "findings": findings, "page_errors": errors, "blocked_requests": sorted(set(blocked)),
              "surfaces_checked": len(measurements),
              "minimum_sample_contrast": min(sample["contrast"] for result in measurements for sample in result["samples"])}
    print(json.dumps(report, indent=2))
    return 1 if errors or (findings and not args.baseline) else 0


if __name__ == "__main__":
    raise SystemExit(main())
