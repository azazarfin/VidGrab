//! Windows installer preparation. The OS installer owns installed files.
use super::*;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{File, OpenOptions, TryLockError};
use std::io::Read;
use std::os::windows::fs::MetadataExt;
use std::os::windows::fs::OpenOptionsExt;
use std::time::{Duration, Instant};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT,
};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    installation_version: u32,
    root: PathBuf,
    files: BTreeMap<String, String>,
    directories: BTreeSet<String>,
}

const REQUIRED_FILES: &[&str] = &[
    "vidgrab-desktop.exe",
    "vidgrab-native-host.exe",
    "engine/vidgrab-engine.exe",
    "engine/_internal/tools/manifest.json",
    "uninstall.exe",
];

fn receipt_path(dirs: &UserDirs) -> PathBuf {
    dirs.data_home
        .join("vidgrab-p02/application/windows-installation.json")
}

fn read_receipt(dirs: &UserDirs) -> io::Result<Option<Receipt>> {
    let path = receipt_path(dirs);
    check_destination(&path)?;
    if path.parent().unwrap().exists() && !crate::windows::is_private(path.parent().unwrap())? {
        return Err(permission_denied(
            "The installation receipt directory must be private",
        ));
    }
    if !path.exists() {
        return Ok(None);
    }
    if !crate::windows::is_private(&path)? {
        return Err(permission_denied(
            "The installation receipt must be private and owned by this user",
        ));
    }
    let saved: Receipt = serde_json::from_slice(&fs::read(path)?).map_err(invalid_data)?;
    if saved.installation_version != 1
        || !saved.root.is_absolute()
        || REQUIRED_FILES
            .iter()
            .any(|name| !saved.files.contains_key(*name))
        || saved
            .files
            .values()
            .any(|hash| hash.len() != 64 || !hash.bytes().all(|byte| byte.is_ascii_hexdigit()))
    {
        return Err(invalid_data(
            "Unsupported or invalid Windows installation receipt",
        ));
    }
    Ok(Some(saved))
}

fn selected_root(root: &Path, dirs: &UserDirs) -> io::Result<()> {
    validate_dirs(dirs)?;
    absolute(root)?;
    if root.components().any(|part| {
        matches!(
            part,
            std::path::Component::ParentDir | std::path::Component::CurDir
        )
    }) {
        return Err(invalid_data(
            "Installation directories must not contain dot components",
        ));
    }
    check_directory(root)?;
    if root.parent().is_none()
        || [&dirs.home, &dirs.data_home, &dirs.config_home]
            .iter()
            .any(|path| path.starts_with(root))
        || root.starts_with(dirs.data_home.join("vidgrab-p02"))
    {
        return Err(permission_denied(
            "Select a separate application installation directory",
        ));
    }
    if dirs.config_path().exists() {
        check_bridge(dirs)?;
        let config = InstalledConfig::load(&dirs.config_path())?;
        if [&config.data_dir, &config.download_dir]
            .iter()
            .any(|path| path.starts_with(root) || root.starts_with(path))
        {
            return Err(permission_denied(
                "Installed files must be separate from downloads and application state",
            ));
        }
    }
    if root.exists() {
        crate::windows::check_owned(root)?;
    }
    Ok(())
}

fn package_tree(root: &Path) -> io::Result<(BTreeMap<String, String>, BTreeSet<String>)> {
    fn visit(
        root: &Path,
        directory: &Path,
        files: &mut BTreeMap<String, String>,
        directories: &mut BTreeSet<String>,
    ) -> io::Result<()> {
        check_directory(directory)?;
        for entry in fs::read_dir(directory)? {
            let path = entry?.path();
            let metadata = fs::symlink_metadata(&path)?;
            if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(permission_denied(
                    "Installed files and directories must not be reparse points",
                ));
            }
            crate::windows::check_owned(&path)?;
            let name = path
                .strip_prefix(root)
                .map_err(invalid_data)?
                .to_str()
                .ok_or_else(|| invalid_data("Installation paths must be Unicode"))?
                .replace('\\', "/");
            if metadata.is_dir() {
                directories.insert(name);
                visit(root, &path, files, directories)?;
            } else if metadata.is_file() {
                let mut file = OpenOptions::new()
                    .read(true)
                    .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
                    .open(&path)?;
                if file.metadata()?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                    return Err(permission_denied(
                        "Installed files must not be reparse points",
                    ));
                }
                let mut hash = Sha256::new();
                let mut buffer = [0; 65536];
                loop {
                    let count = file.read(&mut buffer)?;
                    if count == 0 {
                        break;
                    }
                    hash.update(&buffer[..count]);
                }
                files.insert(name, format!("{:x}", hash.finalize()));
            } else {
                return Err(permission_denied("Installed files must be regular files"));
            }
        }
        Ok(())
    }
    let mut files = BTreeMap::new();
    let mut directories = BTreeSet::new();
    if root.exists() {
        visit(root, root, &mut files, &mut directories)?;
    }
    Ok((files, directories))
}

/// Read-only destination verification, performed by an installer-embedded supervisor.
pub fn verify_installation(root: &Path, dirs: &UserDirs) -> io::Result<()> {
    selected_root(root, dirs)?;
    let saved = read_receipt(dirs)?
        .ok_or_else(|| permission_denied("This folder has no trusted installation receipt"))?;
    let (files, directories) = package_tree(root)?;
    if saved.installation_version != 1
        || saved.root.canonicalize()? != root.canonicalize()?
        || files != saved.files
        || directories != saved.directories
        || REQUIRED_FILES.iter().any(|name| !files.contains_key(*name))
    {
        return Err(permission_denied("The installation is unowned or has changed; restore its original files before continuing"));
    }
    Ok(())
}

pub fn verify_destination(root: &Path, dirs: &UserDirs) -> io::Result<()> {
    selected_root(root, dirs)?;
    let saved = read_receipt(dirs)?;
    if root.exists() && fs::read_dir(root)?.next().is_some() {
        verify_installation(root, dirs)?;
    } else if let Some(saved) = saved {
        check_directory(&saved.root)?;
        if saved.root.exists() && fs::read_dir(&saved.root)?.next().is_some() {
            return Err(permission_denied(
                "Uninstall the current copy before selecting another installation folder",
            ));
        }
    }
    Ok(())
}

/// Called only by the trusted setup after its payload and uninstaller are written.
pub fn record_installation(root: &Path, dirs: &UserDirs) -> io::Result<()> {
    selected_root(root, dirs)?;
    read_receipt(dirs)?;
    let (files, directories) = package_tree(root)?;
    if REQUIRED_FILES.iter().any(|name| !files.contains_key(*name)) {
        return Err(invalid_data(
            "The installer did not write a complete package",
        ));
    }
    let path = receipt_path(dirs);
    check_destination(&path)?;
    private_directory(path.parent().unwrap())?;
    atomic_write(
        &path,
        &json_bytes(&Receipt {
            installation_version: 1,
            root: root.canonicalize()?,
            files,
            directories,
        })?,
        0o600,
    )
}

pub fn prepare_install(root: &Path, dirs: &UserDirs) -> io::Result<()> {
    verify_destination(root, dirs)?;
    if root.exists() && fs::read_dir(root)?.next().is_some() {
        prepare_uninstall(&installer_layout(root), dirs)?;
    }
    Ok(())
}

pub fn installer_layout(root: &Path) -> Layout {
    Layout {
        app: root.join("vidgrab-desktop.exe"),
        native: root.join("vidgrab-native-host.exe"),
        engine: root.join("engine/vidgrab-engine.exe"),
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Status {
    pub kind: &'static str,
    pub installed: bool,
    pub app: PathBuf,
    pub shortcut: Option<PathBuf>,
}

pub fn status(layout: &Layout, dirs: &UserDirs, _is_image: bool) -> io::Result<Status> {
    validate_dirs(dirs)?;
    Ok(Status {
        kind: "windows",
        installed: layout
            .app
            .parent()
            .is_some_and(|p| p.join("uninstall.exe").is_file()),
        app: layout.app.clone(),
        shortcut: None,
    })
}

pub fn preferred_layout(layout: &Layout, _dirs: &UserDirs, _is_image: bool) -> Layout {
    layout.clone()
}

pub fn install(
    _layout: &Layout,
    _dirs: &UserDirs,
    _is_image: bool,
    _icon: &[u8],
) -> io::Result<Layout> {
    Err(invalid_data(
        "Open the Windows installer to install VidGrab for this user",
    ))
}

pub fn check_removal(dirs: &UserDirs, _is_image: bool) -> io::Result<()> {
    check_bridge(dirs)?;
    connection_enabled(&dirs.connection_path())?;
    owned_registrations(dirs)?;
    Ok(())
}

fn idle_lock(dirs: &UserDirs) -> io::Result<Option<File>> {
    check_bridge(dirs)?;
    if !dirs.config_path().exists() {
        return Ok(None);
    }
    let config = InstalledConfig::load(&dirs.config_path())?;
    let path = config.data_dir.join("ipc/owner.lock");
    check_destination(&path)?;
    let file = match OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&path)
    {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    crate::windows::check_owned(&path)?;
    match file.try_lock() {
        Ok(()) => Ok(Some(file)),
        Err(TryLockError::WouldBlock) => Err(permission_denied(
            "Quit VidGrab before changing the installed files",
        )),
        Err(TryLockError::Error(error)) => Err(error),
    }
}

pub fn check_idle(dirs: &UserDirs) -> io::Result<()> {
    idle_lock(dirs).map(|_| ())
}

/// Used by NSIS before any installed file is replaced or removed. Never starts an owner.
pub fn prepare_uninstall(layout: &Layout, dirs: &UserDirs) -> io::Result<()> {
    verify_installation(
        layout
            .app
            .parent()
            .ok_or_else(|| invalid_data("Missing installation directory"))?,
        dirs,
    )?;
    check_removal(dirs, false)?;
    if idle_lock(dirs).is_err_and(|error| error.kind() == io::ErrorKind::PermissionDenied) {
        let config = InstalledConfig::load(&dirs.config_path())?.runtime(layout);
        let request = crate::protocol::Request {
            spike_version: 1,
            id: "installer".into(),
            op: "quit_if_idle".into(),
            params: Default::default(),
        };
        let reply = crate::transport::call(&config, &request)?;
        if reply["ok"] != true {
            return Err(permission_denied(
                reply["error"]["message"]
                    .as_str()
                    .unwrap_or("Quit VidGrab before uninstalling"),
            ));
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            match idle_lock(dirs) {
                Ok(_) => break,
                Err(error)
                    if error.kind() == io::ErrorKind::PermissionDenied
                        && Instant::now() < deadline =>
                {
                    std::thread::sleep(Duration::from_millis(50))
                }
                Err(error) => return Err(error),
            }
        }
    }
    let _lock = idle_lock(dirs)?;
    disconnect(dirs)?;
    autostart::remove_owned(dirs, Some(&layout.app))?;
    Ok(())
}

pub fn remove(_dirs: &UserDirs) -> io::Result<()> {
    Err(invalid_data("Use Installed apps to uninstall VidGrab"))
}
