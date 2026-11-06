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

After provisioning, block external traffic inside the guest. Its SSH forward
uses gateway `10.0.2.2`; this nftables rule retains that control channel and local
fixture connections:

```text
table inet vidgrab_validation {
 chain output {
  type filter hook output priority 0; policy drop;
  oifname "lo" accept
  ip daddr 10.0.2.2 tcp sport 22 accept
 }
 chain input {
  type filter hook input priority 0; policy drop;
  iifname "lo" accept
  ip saddr 10.0.2.2 tcp dport 22 accept
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
