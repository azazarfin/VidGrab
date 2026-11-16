# Disposable native Ubuntu desktop checks

This recipe runs the existing preview packages in an Ubuntu GNOME Wayland KVM
guest, with its own kernel, user home and normal OS FUSE helper. It installs no
host services or native manifests. The GNOME probe is test code: install it only
in this disposable guest. It observes window/panel state and asks the compositor
to close the selected VidGrab window; it does not emulate an indicator host.

The host needs readable/writable `/dev/kvm`, QEMU, `qemu-img`, `genisoimage`, SSH,
4 GiB for the guest and at least 6 GiB of disk space. Keep control sockets and SSH
keys in a fresh mode-0700 Linux directory under `/tmp`. A qcow2 disk may live on
NTFS: the guest filesystem retains Unix modes. Do not bind real user homes or
application state into the guest.

## Recover missing controls

If the private control directory or SSH key is gone, retain the old disks and
recover into a **new qcow2 child overlay**, with explicit `-F qcow2` and the
verified absolute backing path. Verify every backing link, recorded disk
size/mtime/inode/device and the cloud-image checksum first. A disk alone does not
restore launch, SSH or QMP authority.

Create a fresh mode-0700 control directory on a Linux filesystem and a new
disposable SSH key. Use a new NoCloud instance ID with only the existing test
user's `ssh_authorized_keys` and read-only input mount. Do not reuse the initial
provisioning `runcmd`. Set QEMU user networking `restrict=on` before boot, retain
the guest drop firewall, and keep input sharing read-only. Print only the guest
public SSH host key to the private serial output; pin it after verifying the
launched PID/creation identity, VM name and selected new disk. Subsequent SSH must
use batch mode and strict host checking.

Record the exact launch and private paths in the new `vm.json`. Verify native
kernel, guest identity, networking, cloud-init completion, current user display
and GNOME probe before tests. Hash existing validation/installed files and record
package/timer state before changes; pause only selected guest timers and restore
their original states during cleanup. Audit the old backing disks again after
the new guest shuts down. Private key/user-data files do not belong in Git or
evidence exports.

The 2026-11-16 P04 recovery uses
`/home/azaz/.local/share/vidgrab-validation/ubuntu-p04-i2ailciy/vm.json` and a fresh
overlay under `packaging/out/p04-native-clients-afl2yjvl/`. The original repaired
disk, original guest disk and Noble cloud image are preserved. Recovery proves
control availability. Subsequent [P04 native acceptance](../../HANDOFF.md#p04-native-client-acceptance-checkpoint--2026-11-16) passes GNOME/Wayland/WebKit and Chrome/Firefox journeys. Both apt timers are restored, exact temporary registrations removed, ext4 profiles/state preserved, and only the owned overlay stopped normally with exact QEMU exit observed. Recovered controls now record stopped state; original backing disks remain preserved. Reverify controls before any later authorized boot.

## Inputs and launch

Create a fresh read-only `inputs/` directory containing these explicitly selected
files:

| Path | Input |
| --- | --- |
| `source/` | `git archive HEAD` extracted into an empty directory; no ignored DB, profiles or local assets |
| `VidGrab_0.1.0_amd64.deb` | The Ubuntu glibc-2.39 preview |
| `VidGrab_0.1.0_amd64.AppImage` | The corrected image, after `prepare_appimage.py` |
| `chrome.deb` | Selected native Chrome test package |
| `firefox/`, `geckodriver` | Selected Firefox/Gecko test binaries |
| `uv` | Verified standalone uv used only for test-driver setup |

Fetch the Noble cloud image and its `SHA256SUMS` from
`https://cloud-images.ubuntu.com/releases/noble/release/`, verify the checksum and
record both URL and hash. The 2026-11-07 run used
`ubuntu-24.04-server-cloudimg-amd64.img`, SHA-256
`04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94`.
The moving release URL may change; do not treat this hash as a future download.

Create a 22 GiB sparse qcow2 overlay with the verified image as its explicit qcow2
backing file. Generate a disposable SSH key. Replace the public-key placeholder
in [user-data.example](user-data.example), save it as `seed/user-data`, and write
a unique `instance-id` to `seed/meta-data`. Make a NoCloud ISO with volume ID
`cidata` using `genisoimage -joliet -rock`. Private keys and actual user-data stay
outside Git; pin the new guest SSH host key before test execution.

Use these QEMU arguments, replacing paths and the free loopback SSH port. Keep
the exact command, base-image checksum and source commit in a local receipt:

```text
-name CONTROL_BASENAME
-machine q35,accel=kvm -cpu host -smp 4 -m 4096
-drive file=GUEST.qcow2,format=qcow2,if=virtio,cache=writeback
-drive file=SEED.iso,format=raw,media=cdrom,readonly=on
-fsdev local,id=inputs,path=INPUTS,security_model=none,readonly=on
-device virtio-9p-pci,fsdev=inputs,mount_tag=inputs
-netdev user,id=net0,hostfwd=tcp:127.0.0.1:PORT-:22
-device virtio-net-pci,netdev=net0
-device virtio-vga -display vnc=unix:CONTROL/vnc.sock
-device qemu-xhci -device usb-tablet
-serial file:CONTROL/console.log
-qmp unix:CONTROL/qmp.sock,server=on,wait=off
-monitor none -pidfile CONTROL/qemu.pid -daemonize
```

Cloud-init invokes [provision.sh](provision.sh) inside the guest. Wait for its
serial completion event, then use `cloud-init status --wait` over SSH. The script
keeps only this disposable session awake: GNOME's default five-minute lock
disables user extensions and otherwise invalidates unattended tray observations.
Watch the session-bus names `com.vidgrab.Validation` and
`org.kde.StatusNotifierWatcher` with `gdbus wait`, rather than polling processes.

## Run checks inside the guest

Install the selected `.deb` with apt. Select the actual display variables from
`systemctl --user show-environment`; GNOME creates the display after startup, so
`/proc/gnome-shell/environ` does not contain the current display. Preserve that
session's `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`. Set
`XDG_SESSION_TYPE=wayland` only after verifying the seat session through
`loginctl`. Set `GDK_BACKEND=wayland`, `LIBGL_ALWAYS_SOFTWARE=1` and
`WEBKIT_DISABLE_DMABUF_RENDERER=1` for the application checks.

After provisioning, block external traffic inside the guest with
[isolation.nft](isolation.nft). Its SSH forward uses gateway `10.0.2.2`; the rules
retain that control channel, loopback fixtures and only QEMU's DHCP exchange.
A guest using DHCP needs these scoped rules after reboot. Also confirm its
interface is managed with `networkctl`: firewall allowances cannot configure an
unmanaged interface. DNS and general external traffic remain blocked:

```text
table inet vidgrab_validation {
 chain output {
  type filter hook output priority 0; policy drop;
  oifname "lo" accept
  ip daddr 10.0.2.2 tcp sport 22 accept
  ip daddr { 10.0.2.2, 255.255.255.255 } udp sport 68 udp dport 67 accept
 }
 chain input {
  type filter hook input priority 0; policy drop;
  iifname "lo" accept
  ip saddr 10.0.2.2 tcp dport 22 accept
  ip saddr 10.0.2.2 udp sport 67 udp dport 68 accept
 }
}
```

With those guest-only display variables selected, run from the source root:

```sh
VIDGRAB_VALIDATION_VM_ISOLATED=1 PYTHONPATH=/usr/lib/python3/dist-packages \
  /opt/vidgrab-tests/venv/bin/python -B tests/check_wayland_desktop.py \
  --app /usr/bin/vidgrab-desktop --home /tmp/vidgrab-deb-home \
  --screenshots /tmp/vidgrab-deb-screenshots --report /tmp/vidgrab-wayland-deb.json
VIDGRAB_VALIDATION_VM_ISOLATED=1 PYTHONPATH=/usr/lib/python3/dist-packages \
  /opt/vidgrab-tests/venv/bin/python -B tests/check_wayland_desktop.py \
  --app /home/vidgrabtest/packages/VidGrab_0.1.0_amd64.AppImage --fuse \
  --home /tmp/vidgrab-image-home --screenshots /tmp/vidgrab-image-screenshots \
  --report /tmp/vidgrab-wayland-image.json
```

Home/report paths must be fresh. Check `/usr/bin/fusermount3` is root-owned mode
4755 on the guest Linux filesystem; no tmpfs helper replacement or container
capability override is needed. Capture the virtual display through QMP
`screendump` when useful. The check records the native Wayland client, the
matching mapped GNOME indicator, compositor Close, hidden-window MP3 completion,
real D-Bus menu Open/Quit, activation, recovery and FUSE unmount.

Run native Chrome→Firefox handoff with content sandboxes enabled, using a
different fresh home/report for each package:

```sh
VIDGRAB_VALIDATION_VM_ISOLATED=1 PYTHONPATH=/usr/lib/python3/dist-packages \
  /opt/vidgrab-tests/venv/bin/python -B tests/check_wayland_browsers.py \
  --app /usr/bin/vidgrab-desktop --home /tmp/vidgrab-browser-deb-home \
  --report /tmp/vidgrab-browser-deb.json
```

Repeat with the corrected AppImage path and fresh image paths. The check verifies
missing registration, package bridge activation, an active download after full
browser shutdown and completed history after restart. A local stream event holds
the WAV after its first 4096 bytes until browser exit is observed. Chrome requires
its sandbox and checks renderer seccomp/NoNewPrivs; Firefox checks extension
content processes. Record any AppArmor namespace warning rather than claiming
full confinement. This does not test Snap/Flatpak.

## Strict Firefox Snap

Install the stable Firefox Snap only inside the disposable guest during a
recorded provisioning network interval. Restore [isolation.nft](isolation.nft)
before acceptance checks. The strict browser uses its canonical
`/snap/bin/firefox` launcher and fresh profiles under the guest's
`~/snap/firefox/common/`; the portal reads native registration in that guest's
actual user home. The external Gecko driver attaches to loopback Marionette;
its system-access environment flag is scoped only to the test browser.

On the host, from the repository root:

```sh
python3 -B tests/check_firefox_snap.py --vm /tmp/CONTROL/vm.json \
  --app /usr/bin/vidgrab-desktop \
  --home /home/vidgrabtest/validation/snap-deb \
  --guest-report /home/vidgrabtest/validation/snap-deb.json \
  --screenshots /tmp/CONTROL/snap-deb-screenshots \
  --report /tmp/CONTROL/snap-deb-outer.json
```

Repeat with `/home/vidgrabtest/packages/VidGrab_0.1.0_amd64.AppImage`, `--fuse`
and fresh paths. The harness requires the named private VM and its running
receipt. It focuses visible Firefox, observes GNOME's real permission dialog and
clicks Allow with the virtual tablet. Headless GNOME access can cache a denial;
preparation/cleanup resets only this fixture's exact native-host permission via
structured D-Bus calls and never grants it programmatically. Checks cover native
activation, repeated activation, one gated download after full browser exit,
completed history after owner restart, `APP_MISSING`, disconnection and both
AppImage mounts disappearing. Failures capture the display before cleanup.

The selected Ubuntu 24.04.5/Firefox 159.0.1 Snap configuration passes both
packages through the WebExtensions portal with enforced AppArmor and content
seccomp/NoNewPrivs. Snap entrypoints now delegate to the shared portal host/guest
journey; both package regressions still pass.

## Firefox Flatpak native portal

Install Flatpak and official Flathub Firefox only in this disposable guest during
a recorded provisioning interval, then restore [isolation.nft](isolation.nft)
before checks. The validated app is Firefox 159.0.1, Flatpak 1.14.6 and runtime
`org.freedesktop.Platform/x86_64/25.08`, with no user permission overrides. Start
a new GNOME session after installation so Flatpak export directories and
`org.mozilla.firefox.desktop` are discoverable; the recorded guest reboot fixed
portal identity denial from its older session.

Stock Firefox profiles have integer preference
`widget.use-xdg-desktop-portal.native-messaging = 0`. With no explicit opt-in,
the harness records a missing-host failure even after canonical registration,
then verifies cleanup. Keep that negative receipt rather than claiming native
activation passed. For the user-selected guided native path, explicitly request
the setting in a fresh test profile:

```sh
python3 -B tests/check_firefox_flatpak.py --vm /tmp/CONTROL/vm.json \
  --app /usr/bin/vidgrab-desktop \
  --home /home/vidgrabtest/validation/flatpak-deb \
  --guest-report /home/vidgrabtest/validation/flatpak-deb.json \
  --screenshots /tmp/CONTROL/flatpak-deb-screenshots \
  --report /tmp/CONTROL/flatpak-deb-outer.json --enable-native-portal
```

Repeat with the corrected AppImage, `--fuse` and fresh paths. The explicit flag
writes integer `1` only in these test profiles; it is rejected for Snap and does
not alter Flatpak permissions or grant consent. The launcher is
`flatpak run --user org.mozilla.firefox`, profiles are under the guest's
`~/.var/app/org.mozilla.firefox/`, and the portal reads canonical registration
from the actual guest user home. Keep the visible browser focused and use the
real virtual-tablet Allow click. Permission preparation/cleanup clears only
`webextensions/com.vidgrab.companion/org.mozilla.firefox` inside this guest.

Both packages pass native Wayland activation, repeated activation, one gated
download after full browser exit, completed history, missing-app/disconnection
and cleanup. `flatpak ps` can report outer bwrap in the host namespace. Map
browser/content PIDs only through that instance's descendants at the browser
namespace depth, including nested content namespaces. Kill the exact owned
Flatpak instance and verify observed processes exited; killing only outer bwrap
can leave Firefox alive. Browser/content private Flatpak PID namespaces,
seccomp 2 and NoNewPrivs 1 are required. Record Firefox's remaining user-namespace
warning instead of claiming additional Firefox namespaces work.

Seventeen opt-in helpers pass on both Fedora and Ubuntu, including eight
Flatpak mapping/cleanup cases; [DEVELOPMENT.md](../../docs/DEVELOPMENT.md) gives
the command. [The setup guide](../../docs/BROWSER_CONNECTION.md) is manual;
regular popup migration and terminal-free release controls remain unfinished.
There is no paired fallback in this preview. Other configurations and Windows
retain their own gates.

Current controls use `guest-network-repaired.qcow2` over the preserved original
disk and `resume-seed.iso`; read the current `vm.json`. Latest final audit passes,
and the VM is stopped. The guest poweroff request did not exit QEMU within 30 s;
private QMP quit verified process exit in 31.136 s. Preserve
`flatpak-shutdown.json` as a fallback result, separately from the older successful
Snap shutdown. Flatpak/Snap shared receipts and logs are in `flatpak-evidence/`
with commands/screenshots alongside; these remain ephemeral, excluded from Git.

## Virtual tray input and package removal

On the **host**, use the private `vm.json` receipt to drive actual virtual USB
mouse/keyboard tray input. Its VM name must match the control directory basename:

```sh
python3 -B tests/check_gnome_pointer.py --vm /tmp/CONTROL/vm.json \
  --app /usr/bin/vidgrab-desktop --home /tmp/vidgrab-pointer-deb-home \
  --screenshots /tmp/vidgrab-pointer-deb-screenshots \
  --report /tmp/vidgrab-pointer-deb.json
```

Repeat with the guest AppImage path, `--fuse` and fresh home/screenshots/report
paths. Each pointer state is observed inside GNOME before the next HID report;
the test focuses the title, sends Alt+F4 and clicks the real indicator's Open and
Quit entries. It does not call the probe's window-close method for this check.

After both desktop checks Quit, use their completed fixture homes for CLI
removal/repair, inside the guest:

```sh
VIDGRAB_VALIDATION_VM_ISOLATED=1 /opt/vidgrab-tests/venv/bin/python -B \
  tests/check_package_installation.py \
  --deb /home/vidgrabtest/packages/VidGrab_0.1.0_amd64.deb \
  --appimage /home/vidgrabtest/packages/VidGrab_0.1.0_amd64.AppImage \
  --deb-home /tmp/vidgrab-deb-home --image-home /tmp/vidgrab-image-home \
  --report /tmp/vidgrab-installation.json
```

This removes/reinstalls the selected guest `.deb`, relocates the actual image
through a Unicode path, removes/restores its registered launch path, checks the
stable bridge's `APP_MISSING` response and compares completed state/media hashes.
The original image bytes are retained privately during removal and restored in
cleanup. Guest networking remains blocked; do not use APT's `--no-download` for
the local artifact because Ubuntu APT rejects its acquire path. Package-command
errors retain both output streams. This is preview CLI lifecycle evidence,
separate from terminal-free release connection/disconnection/uninstaller UX.

Copy receipts, screenshots and selected logs out before shutting down the guest.
Quit any selected app owner first, then power off the guest or use its private
QMP `quit`. Preserve the qcow2 overlay only when continuing another isolated
gate. Save the guest-only nftables rules to `/etc/nftables.conf` and enable that
guest service if retaining the disk, or reapply the rules before resumed checks;
runtime rules alone do not survive reboot. On resumption, validate the recorded
paths before starting QEMU and wait for its current GNOME/session bus again.
If provisioning another browser distribution, record the temporary guest network
interval and restore the rules before acceptance checks. These observations cover
GNOME on a virtual display; KDE, physical input, Snap/Flatpak and Windows require
their own evidence.

## Rebuilt guide through visible Firefox setup

For manual onboarding acceptance, use the Flatpak command above with `--manual-native-portal` instead of `--enable-native-portal`, and fresh paths. This mode opens the packaged help, sets the existing Number preference through observed input, fully quits Firefox, restarts it and reloads the temporary add-on. It verifies the preference survived without preseeding and observes that Firefox removed the temporary add-on. The actual rebuilt `.deb` and FUSE AppImage pass this journey.

When rebuilding preview bytes at the same 0.1.0 version, APT may keep the installed executable. Explicitly reinstall the selected package and verify the installed-vs-extracted executable hash before checking the guide. During this checkpoint, the disposable guest's offline unattended updater held the frontend lock; only identified apt-upgrade processes were stopped, no active dpkg installation or lock file was removed, and the firewall stayed blocked. Pause guest timers during validation and restore them before shutdown. Preserve earlier failure receipts and record actual cleanup/shutdown; the VM state in `vm.json` is authoritative.

## Persistent browser controls and cleanup

The current Ubuntu previews pass `tests/check_browser_connections.py` from the host with the private running `vm.json`, fresh guest home/report paths and host screenshots/report paths. Repeat for the AppImage with `--fuse`. See DEVELOPMENT for the complete command. Actual pointer clicks prove disconnection during an active download, copied-host rejection, completion/history after reopening still disconnected, foreign-manifest refusal and reconnection.

`check_wayland_browsers.py` now unregisters the canonical host and removes only its verified copied diagnostic Chrome alias, refusing foreign/symlink destinations. Both native browser journeys pass again; removal preserves completed state/media hashes. Refresh the host's copied `inputs/source` snapshot after repository edits and compare guest hashes before checking it; the read-only share is not the working repository.

The earlier accepted browser/control reports and selected logs are in `connection-controls-evidence/` under the control root. `connection-controls-final-audit-retry2.json` records ten media and twelve databases, no selected processes/mounts/registrations, removed Snap/Flatpak permissions, restored apt timers and preserved drop firewall/modes. That checkpoint shutdown is in `connection-controls-shutdown.json`. The later terminal-free release checkpoint below has the current audit/shutdown; always use current `vm.json`, preserving older receipts and the original backing disk. Windows remains deferred.


## Terminal-free release installation/removal

The release GUI driver is documented in [DEVELOPMENT.md](../../docs/DEVELOPMENT.md#packaged-application-installation-and-removal).
Use only this disposable guest, a copied/hash-verified source snapshot, a selected
fixture descriptor and fresh report/screenshots. Guest preparation additionally
needs Files, App Center, `python3-pyatspi`, `tesseract-ocr-eng` and GdkPixbuf.
Authentication uses a private test file in the VM controls; do not copy it into
source or evidence exports. User-facing steps are in
[LINUX_INSTALLATION.md](../../docs/LINUX_INSTALLATION.md).

Keep App Center row loading separate from package absence. Filter for VidGrab and
bind its uninstall button to the name in the same captured frame; scrolling an
unfiltered list while it loads can select another package. The driver checks
other installed package states after desktop/icon triggers finish. Finish with
state/media/descriptor, process/mount/registration/permission, firewall, FUSE and
apt-timer audits, then orderly guest poweroff and observed QEMU exit.

The 2026-11-10 final `.deb` cycle 13 and AppImage cycle 6 pass; seven actual-image
file cases also pass. `release-ux-final-audit.json` verifies 28 media/41 databases,
1,041 unrelated OS package states and cleanup. `release-ux-shutdown.json` records
orderly poweroff in 9.203 s with observed QEMU exit; current `vm.json` is stopped.
`release-ux-evidence/manifest.json` indexes 135 selected files and 103 screenshots,
including the labelled earlier partial journey and the five-package App Center
incident/exact-version restoration. Do not copy the private authentication/key
files. Keep Ubuntu's packaged accessibility environment defaults; removing a
runtime override does not remove those `/etc/environment.d` defaults.

## Final-package startup and Linux review

The unchanged final packages pass three sequential fresh-state runs each on
2026-11-10 using the Wayland commands above. Registered launch to Connected has
median **0.854 s** for the `.deb` (0.839–2.034 s) and **1.478 s** for the actual
FUSE AppImage (1.456–1.760 s). Registration and WebKitWebDriver process/readiness
are outside the timer; its launch/session command and observation overhead are
included. No OS cache eviction, cold-install, physical-device or release latency
budget is claimed. All six tray/download/recovery/Quit journeys also pass.

`startup-final-audit.json` verifies 34 media/47 databases, preserving all earlier
28/41 hashes and 1,041 unrelated OS package states. Selected processes, image
mounts, manifests, managed files and portal permissions are absent; timers,
firewall and FUSE/fstab modes pass. `startup-shutdown.json` records orderly
poweroff in 8.705 s with QEMU exit observed and no fallback; current `vm.json` is
stopped. `startup-summary.json` and `startup-evidence/` retain samples, the earlier
prelaunch missing-`gi` harness failure, selected logs/screenshots and source
hashes. The selected Ubuntu Linux ADR-001 review passes; Windows and final
cross-platform acceptance remain deferred/open, and P03 has not started.
