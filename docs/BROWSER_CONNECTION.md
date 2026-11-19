# Firefox Flatpak native connection setup

Firefox Flatpak **159.0.1** can connect to the VidGrab P02 preview through Ubuntu's built-in WebExtensions portal after you enable one Firefox setting. Its default value is `0`, which reports “No such native application com.vidgrab.companion” even when VidGrab is registered. The tested setup uses the number `1`.

This guide records the preserved P02 native-connection previews on Ubuntu 24.04.5/GNOME 46. Their popup/options use the earlier backend, and their native test confirms Firefox access. Current source popup/options now use native v1 with a matching app and authoritative app settings; they show **Update required** when the app lacks `downloads.source`. The P04 source migration has Fedora renderer/native evidence; changed target/installed-browser/GUI acceptance and final release remain open. See [development](DEVELOPMENT.md#p04-client-migration--2026-11-16) for the current source workflow.

## Set up the connection

1. Open the installed VidGrab preview or the corrected AppImage from the location where you intend to keep it. In **Browser connections**, use **Connect browsers** or **Repair connection** when needed. Reopen an AppImage after moving it to repair the saved launch path. Opening preserves an earlier explicit disconnection; reconnect with the button when you want browser access again.
2. In Firefox, open `about:config` and accept its browser notice if shown. Search for the exact existing preference:

   ```text
   widget.use-xdg-desktop-portal.native-messaging
   ```

3. Edit its **Number** value to **`1`** and save with the checkmark. Fully quit Firefox and reopen it. Use the existing integer preference; if this preference is absent in your Firefox build, that configuration has not been validated.
4. After restarting Firefox, load the temporary VidGrab add-on in `about:debugging#/runtime/this-firefox`. Temporary add-ons are removed by a Firefox restart, so load it again after each restart used in this preview. Use the Firefox manifest and existing ID, `vidgrab@azaz.local`; release/store installation remains a later gate. The diagnostic below can create a fresh XPI from the current source.
5. Keep Firefox visible and focused when starting the extension's native connection test. In GNOME's prompt, verify that Firefox wants to launch **VidGrab P02 desktop companion** (`com.vidgrab.companion`) and choose **Allow**. This permission is a separate desktop decision from the Firefox setting.
6. Confirm that the native test returns `state: "ready"`. The tested app remains responsible for a queued fixture download after Firefox closes.

If you installed Firefox Flatpak during the current desktop session, restart the desktop session before testing. The validation guest needed a reboot so GNOME could discover `org.mozilla.firefox.desktop` through the Flatpak export directories. A session that predates installation can reject portal access even when Firefox is visible.

VidGrab does not change your Firefox preferences or grant desktop permission automatically. The verified configuration retains stock Flatpak permissions and browser/content seccomp and NoNewPrivs. Enabling this portal preference does not require a Flatpak filesystem override or disabling browser confinement.

## Manage browser registration

Choose **Disconnect browsers** to remove browser access while active downloads keep running. Completed files and history remain available, and disconnection persists when VidGrab reopens or the AppImage moves. Use **Connect browsers** to reconnect, or **Repair connection** when a registered host needs repair.

**Registered** describes the browser host manifest. Confirm actual browser access with the native test and any real desktop Allow prompt. VidGrab refuses to overwrite a host belonging to another installation. See [ADR-014](DECISIONS.md#adr-014--preserve-explicit-linux-browser-disconnection) for the preview policy.

## Run the P02 diagnostic

The preserved P02 previews use this separate native diagnostic; the current source popup checks native v1 access when opened. To create a fresh diagnostic XPI without replacing the tracked `dist/` archives, run from the repository root after [development setup](DEVELOPMENT.md):

```sh
.venv/bin/python -B - <<'PY'
from pathlib import Path
import tempfile
from tests.check_installed_browsers import firefox_fixture

directory = Path(tempfile.mkdtemp(prefix='vidgrab-firefox-diagnostic-'))
print(firefox_fixture(directory))
PY
```

In Firefox's `about:debugging#/runtime/this-firefox`, choose **Load Temporary Add-on**, select the printed XPI, then select **Inspect** for VidGrab. Use its extension background console:

```js
await vidGrabNative.send('status')
```

This read-only request can launch the registered app when it is closed. Keep the visible Firefox window focused while GNOME requests permission. After a successful status response, an explicit activation request is available:

```js
await vidGrabNative.send('activate')
```

Fixture download checks use a selected local validation server and disposable state; follow [DEVELOPMENT.md](DEVELOPMENT.md) and [the VM recipe](../tests/ubuntu_vm/README.md). Do not use a real download queue as a connection fixture.

## When connection fails

| Result | Recovery |
| --- | --- |
| “No such native application com.vidgrab.companion” | Check the existing Firefox integer preference is `1`, restart Firefox, and use Connect/Repair in VidGrab from its current location. A missing registration and a disabled portal can produce the same error. |
| Desktop denies access or no Allow prompt appears | Keep Firefox visible and focused, restart a desktop session that predates Flatpak installation, and review the application's WebExtensions permission in desktop privacy settings if you previously denied it. |
| `BROWSER_DISCONNECTED` | Open VidGrab and choose Connect browsers when you want to restore access. Downloads/history remain available while disconnected. |
| `APP_MISSING` | Restore/open the installed app or AppImage at its intended location so the saved launch target is repaired. |
| `REQUEST_OUTCOME_UNKNOWN` | Inspect status/history before deciding what to do next. The request may already have taken effect; do not repeat a download or other mutation automatically. |

The user selected guided native portal setup on **2026-11-08**. There is no paired loopback fallback in this preview. Other Firefox/portal versions and distributions need separate evidence; see [ADR-013](DECISIONS.md#adr-013--guide-firefox-flatpak-users-through-native-portal-setup) and [VALIDATION.md](VALIDATION.md).

## Current source downloads, defaults and cookies

Use a matching current companion build with the source extension. Open VidGrab, connect/repair browser registration and open the popup to negotiate native v1. **Connected** confirms the current native request succeeded; **Update required** requests a compatible app/extension. The stored legacy backend URL is shown as a deprecated browser value and is ignored for native transport.

App download defaults are shared by the desktop and extension. Options saves only edited settings under the loaded revision. If another window changes settings, your edits remain visible; use Reload app settings to review the current values before saving. The desktop Download defaults disclosure follows the same revision/reload behavior.

Enable cookie forwarding only when you want to share cookies for the selected source. The browser's cookie/site grants remain a separate requirement, and Settings reports cookie/native/notification/request-observation/origin grants separately from preferences. **Observe media requests** keeps `webRequest` and `webNavigation` as optional permissions: Firefox activates request metadata capture immediately once granted, whereas in Chrome a fresh grant within an already-running browser session surfaces persistent **Restart Chrome to activate request observation** notices in Options (`Request observation: granted (restart Chrome to activate)`) and the popup's **Detected** panel until you close all Chrome windows and reopen Chrome once (or until `onHeadersReceived` events actually arrive); DOM/page media detection works immediately without restarting, and VidGrab never reloads the extension or replays mutations automatically. Sign in to the source site before using Share fresh cookies on an authentication-waiting job. That action and protected retries use a fresh context bound to the exact URL/job; cookie payloads/auth references remain volatile. Desktop authentication waits direct you to this browser action.

If a request reply is lost, use Check request to replay its original payload/key explicitly. Reconnect only refreshes state. After a background/view restart, the original credential payload may be unavailable; review Downloads and dismiss the reviewed notice in Settings before making a new decision. Dismissing a notice changes no engine download or setting. A failed batch stops before submitting its remaining items and exposes the request review action.

This source workflow is covered by isolated renderer/control/native fixtures under [ADR-027](DECISIONS.md#adr-027--p04-client-sessions-and-explicit-request-review) and [ADR-028](DECISIONS.md#adr-028--scoped-media-discovery-and-format-review). The preserved Snap/Flatpak/installed-browser evidence above belongs to P02 packages, while controlled/live P05 evidence is recorded in [VALIDATION.md](VALIDATION.md). VidGrab still leaves real Firefox preferences, browser optional-permission prompts and desktop Allow decisions to the user.
