# Install and remove the Linux preview

These steps are for the VidGrab P02 preview on Ubuntu 24.04 LTS, x86_64.
The preview downloads local test audio; the full download manager and Windows
installation remain later work. Its state and media are separate from the older
VidGrab backend.

## Ubuntu package

1. In **Files**, open the downloaded **.deb** with **App Center**.
2. Choose **Install**, confirm App Center's package notice and authenticate with
   your Ubuntu account when prompted.
3. Open **VidGrab** from **Applications**.

To remove it, finish active downloads and open **Install and remove VidGrab** in
the desktop window. Choose **Prepare to remove…**, review what will be kept and
choose **Disconnect and quit**. In App Center, select **Manage** and wait
for the installed list to finish loading. Use **Search installed apps** to show
VidGrab, then choose **Uninstall** on its row. Authenticate with Ubuntu when
prompted.

If VidGrab is absent from Manage after a local reinstall, close and reopen App
Center. It can retain its earlier application catalog while its window stays
open. Reopening the `.deb` in Files opens App Center again; select Manage there.

## AppImage

1. In **Files**, select the downloaded **.AppImage** and open **Properties**.
   Turn on **Executable as Program** if it is off, then close Properties.
2. Open the AppImage. In VidGrab, expand **Install and remove VidGrab** and choose
   **Install AppImage**. The current download session remains open.
3. Open **VidGrab (AppImage preview)** from **Applications** on later visits.

The installed copy and its icon/receipt live in
`XDG_DATA_HOME/vidgrab-p02/application`; its Applications shortcut lives in
`XDG_DATA_HOME/applications`. The downloaded portable file remains available for
reinstallation. Browser registration points to the installed copy.

To remove the installed copy, finish active downloads, choose **Remove installed
copy…**, review the confirmation and choose **Remove and quit**. VidGrab removes
its managed AppImage and shortcut and disconnects browser access. **Keep VidGrab**
or Escape closes the confirmation without removing the app.

To remove only a portable download, quit VidGrab and move that downloaded file
to Trash in Files. An installed copy has its own removal control.

## What stays after removal

Completed media, history, selected state/media paths and the native launch
descriptor remain. The default preview state is
`XDG_DATA_HOME/vidgrab-p02/state`; default media is `~/Downloads/VidGrab-P02`.
Removal preserves explicit browser disconnection through reopening and reinstall.
Choose **Connect browsers** when you want access again; see
[browser connection setup](BROWSER_CONNECTION.md).

VidGrab refuses to replace or remove a managed file or browser host belonging to
another installation. Restore a changed managed copy or shortcut before trying
its removal again. This preview has no automatic app replacement/update flow.
