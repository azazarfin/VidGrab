#!/bin/bash
# Invoked by cloud-init inside a disposable Ubuntu guest, never on the host.
set -euo pipefail
test "$(cat /etc/hostname)" = "vidgrab-p02-ubuntu-validation"
test -r /mnt/inputs/source/tests/gnome_probe/metadata.json
export DEBIAN_FRONTEND=noninteractive
trap 'printf "\nVIDGRAB_VM_PROVISION_FAILED line=%s\n" "$LINENO" >/dev/ttyS0' ERR

apt-get update -qq
apt-get install -y --no-install-recommends ubuntu-session gnome-shell gdm3 \
  gnome-shell-extension-appindicator ubuntu-settings yaru-theme-gtk yaru-theme-icon \
  xwayland xserver-xorg-core libgl1-mesa-dri libegl-mesa0 libgbm1 \
  libwebkit2gtk-4.1-0 libayatana-appindicator3-1 webkit2gtk-driver fuse3 \
  python3-venv curl ca-certificates fonts-liberation libdbus-glib-1-2 \
  binutils nftables dbus-x11
apt-get install -y --no-install-recommends /mnt/inputs/chrome.deb
/mnt/inputs/uv venv --python /usr/bin/python3 /opt/vidgrab-tests/venv
/mnt/inputs/uv pip install --python /opt/vidgrab-tests/venv/bin/python playwright==1.64.1

mkdir -p /home/vidgrabtest/packages \
  /home/vidgrabtest/.local/share/gnome-shell/extensions/vidgrab-validation@local
cp /mnt/inputs/VidGrab_0.1.0_amd64.deb /home/vidgrabtest/packages/
install -m755 /mnt/inputs/VidGrab_0.1.0_amd64.AppImage /home/vidgrabtest/packages/
cp /mnt/inputs/source/tests/gnome_probe/* \
  /home/vidgrabtest/.local/share/gnome-shell/extensions/vidgrab-validation@local/
chown -R vidgrabtest:vidgrabtest /home/vidgrabtest/packages /home/vidgrabtest/.local

cat >/etc/gdm3/custom.conf <<'GDM'
[daemon]
AutomaticLoginEnable = true
AutomaticLogin = vidgrabtest
WaylandEnable = true
GDM
mkdir -p /var/lib/AccountsService/users
cat >/var/lib/AccountsService/users/vidgrabtest <<'ACCOUNT'
[User]
Session=ubuntu
XSession=ubuntu
SystemAccount=false
ACCOUNT
runuser -u vidgrabtest -- dbus-run-session -- bash -eu <<'SESSION'
gsettings set org.gnome.shell enabled-extensions "['ubuntu-appindicators@ubuntu.com', 'vidgrab-validation@local']"
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
SESSION
rm -f /usr/sbin/policy-rc.d
systemctl set-default graphical.target
systemctl start gdm3
printf '\nVIDGRAB_VM_PROVISION_READY\n' >/dev/ttyS0
