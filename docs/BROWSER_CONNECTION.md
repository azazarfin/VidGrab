# Firefox Flatpak native connection setup

Firefox Flatpak **159.0.1** can connect to the VidGrab P02 preview through Ubuntu's built-in WebExtensions portal after you enable one Firefox setting. Its default value is `0`, which reports “No such native application com.vidgrab.companion” even when VidGrab is registered. The tested setup uses the number `1`.

This guide covers the P02 native connection test on Ubuntu 24.04.5/GNOME 46. The preview's regular extension popup and options still use the earlier backend. The desktop **Connected** label reports app readiness; the native test confirms Firefox access. Full release installation and connection controls remain unfinished.

## Set up the connection

1. Open the installed VidGrab preview or the corrected AppImage from the location where you intend to keep it. Opening the app registers its browser connection. Reopen an AppImage after moving it to repair the saved launch path.
2. Load a Firefox development build of VidGrab as a temporary add-on in `about:debugging#/runtime/this-firefox`. Use the Firefox manifest and existing ID, `vidgrab@azaz.local`; release/store installation remains a later gate. The diagnostic below can create a fresh XPI from the current source.
3. In Firefox, open `about:config` and accept its browser notice if shown. Search for the exact existing preference:

   ```text
   widget.use-xdg-desktop-portal.native-messaging
   ```

4. Edit its **Number** value to **`1`** and save with the checkmark. Fully quit Firefox and reopen it. Use the existing integer preference; if this preference is absent in your Firefox build, that configuration has not been validated.
5. Keep Firefox visible and focused when starting the extension's native connection test. In GNOME's prompt, verify that Firefox wants to launch **VidGrab P02 desktop companion** (`com.vidgrab.companion`) and choose **Allow**. This permission is a separate desktop decision from the Firefox setting.
6. Confirm that the native test returns `state: "ready"`. The tested app remains responsible for a queued fixture download after Firefox closes.

If you installed Firefox Flatpak during the current desktop session, restart the desktop session before testing. The validation guest needed a reboot so GNOME could discover `org.mozilla.firefox.desktop` through the Flatpak export directories. A session that predates installation can reject portal access even when Firefox is visible.

VidGrab does not change your Firefox preferences or grant desktop permission automatically. The verified configuration retains stock Flatpak permissions and browser/content seccomp and NoNewPrivs. Enabling this portal preference does not require a Flatpak filesystem override or disabling browser confinement.

## Run the P02 diagnostic

The regular popup has no native connection button yet. To create a fresh diagnostic XPI without replacing the tracked `dist/` archives, run from the repository root after [development setup](DEVELOPMENT.md):

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
| “No such native application com.vidgrab.companion” | Check the existing Firefox integer preference is `1`, restart Firefox, and open VidGrab once from its current location to register/repair the host. A missing registration and a disabled portal can produce the same error. |
| Desktop denies access or no Allow prompt appears | Keep Firefox visible and focused, restart a desktop session that predates Flatpak installation, and review the application's WebExtensions permission in desktop privacy settings if you previously denied it. |
| `APP_MISSING` | Restore/open the installed app or AppImage at its intended location so the saved launch target is repaired. |
| `REQUEST_OUTCOME_UNKNOWN` | Inspect status/history before deciding what to do next. The request may already have taken effect; do not repeat a download or other mutation automatically. |

The user selected guided native portal setup on **2026-11-08**. There is no paired loopback fallback in this preview. Other Firefox/portal versions and distributions need separate evidence; see [ADR-013](DECISIONS.md#adr-013--guide-firefox-flatpak-users-through-native-portal-setup) and [VALIDATION.md](VALIDATION.md).
