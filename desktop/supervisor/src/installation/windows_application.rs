//! Windows installer preparation. The OS installer owns installed files.
use super::*;
use std::fs::{File, OpenOptions, TryLockError};
use std::os::windows::fs::OpenOptionsExt;
use std::time::{Duration, Instant};
use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT;

#[derive(Clone, Debug, Serialize)]
pub struct Status {
    pub kind: &'static str,
    pub installed: bool,
    pub app: PathBuf,
    pub shortcut: Option<PathBuf>,
}

pub fn status(layout: &Layout, dirs: &UserDirs, _is_image: bool) -> io::Result<Status> {
    validate_dirs(dirs)?;
    Ok(Status { kind: "windows", installed: layout.app.parent().is_some_and(|p| p.join("uninstall.exe").is_file()),
        app: layout.app.clone(), shortcut: None })
}

pub fn preferred_layout(layout: &Layout, _dirs: &UserDirs, _is_image: bool) -> Layout { layout.clone() }

pub fn install(_layout: &Layout, _dirs: &UserDirs, _is_image: bool, _icon: &[u8]) -> io::Result<Layout> {
    Err(invalid_data("Open the Windows installer to install VidGrab for this user"))
}

pub fn check_removal(dirs: &UserDirs, _is_image: bool) -> io::Result<()> {
    check_bridge(dirs)?;
    connection_enabled(&dirs.connection_path())?;
    owned_registrations(dirs)?;
    Ok(())
}

fn idle_lock(dirs: &UserDirs) -> io::Result<Option<File>> {
    check_bridge(dirs)?;
    if !dirs.config_path().exists() { return Ok(None); }
    let config = InstalledConfig::load(&dirs.config_path())?;
    let path = config.data_dir.join("ipc/owner.lock");
    check_destination(&path)?;
    let file = match OpenOptions::new().read(true).write(true).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT).open(&path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    crate::windows::check_owned(&path)?;
    match file.try_lock() {
        Ok(()) => Ok(Some(file)),
        Err(TryLockError::WouldBlock) => Err(permission_denied("Quit VidGrab before changing the installed files")),
        Err(TryLockError::Error(error)) => Err(error),
    }
}

pub fn check_idle(dirs: &UserDirs) -> io::Result<()> { idle_lock(dirs).map(|_| ()) }

/// Used by NSIS before any installed file is replaced or removed. Never starts an owner.
pub fn prepare_uninstall(layout: &Layout, dirs: &UserDirs) -> io::Result<()> {
    check_removal(dirs, false)?;
    if idle_lock(dirs).is_err_and(|error| error.kind() == io::ErrorKind::PermissionDenied) {
        let config = InstalledConfig::load(&dirs.config_path())?.runtime(layout);
        let request = crate::protocol::Request { spike_version: 1, id: "installer".into(), op: "quit_if_idle".into(), params: Default::default() };
        let reply = crate::transport::call(&config, &request)?;
        if reply["ok"] != true { return Err(permission_denied(reply["error"]["message"].as_str().unwrap_or("Quit VidGrab before uninstalling"))); }
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            match idle_lock(dirs) {
                Ok(_) => break,
                Err(error) if error.kind() == io::ErrorKind::PermissionDenied && Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
                Err(error) => return Err(error),
            }
        }
    }
    let _lock = idle_lock(dirs)?;
    disconnect(dirs)?;
    Ok(())
}

pub fn remove(_dirs: &UserDirs) -> io::Result<()> { Err(invalid_data("Use Installed apps to uninstall VidGrab")) }
