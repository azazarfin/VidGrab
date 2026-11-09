//! Explicit per-user AppImage installation. State and native descriptors stay separate.
use super::*;
use sha2::{Digest, Sha256};
use std::fs::{File, TryLockError};
use std::io::Read;

const SHORTCUT: &str = "com.vidgrab.P02.AppImage.desktop";

#[derive(Clone, Debug, Serialize)]
pub struct Status {
    pub kind: &'static str,
    pub installed: bool,
    pub app: PathBuf,
    pub shortcut: Option<PathBuf>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    installation_version: u32,
    image_sha256: String,
    image_bytes: u64,
    icon_sha256: String,
}

fn directory(dirs: &UserDirs) -> PathBuf { dirs.data_home.join("vidgrab-p02/application") }
fn image(dirs: &UserDirs) -> PathBuf { directory(dirs).join("VidGrab.AppImage") }
fn icon(dirs: &UserDirs) -> PathBuf { directory(dirs).join("icon.png") }
fn receipt(dirs: &UserDirs) -> PathBuf { directory(dirs).join("installation.json") }
fn shortcut(dirs: &UserDirs) -> PathBuf { dirs.data_home.join("applications").join(SHORTCUT) }

fn desktop_value(path: &Path) -> io::Result<String> {
    let text = path.to_str().ok_or_else(|| invalid_data("Applications paths must be valid Unicode"))?;
    if text.chars().any(char::is_control) { return Err(invalid_data("Applications paths cannot contain control characters")); }
    Ok(text.replace('\\', "\\\\"))
}

fn desktop_entry(dirs: &UserDirs) -> io::Result<Vec<u8>> {
    // Desktop Entry Exec has its own quoting and percent-field expansion; no shell is used.
    let executable = desktop_value(&image(dirs))?.replace('\\', "\\\\")
        .replace('"', "\\\\\"").replace('$', "\\\\$").replace('`', "\\\\`").replace('%', "%%");
    Ok(format!("[Desktop Entry]\nType=Application\nName=VidGrab (AppImage preview)\nComment=VidGrab P02 desktop companion\nExec=\"{executable}\"\nIcon={}\nTerminal=false\nCategories=Utility;\nX-VidGrab-Installation=1\n", desktop_value(&icon(dirs))?).into_bytes())
}

fn checksum(path: &Path) -> io::Result<String> {
    let mut file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65_536];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 { break; }
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn owned(dirs: &UserDirs, verify_image: bool) -> io::Result<Option<Receipt>> {
    validate_dirs(dirs)?;
    let paths = [image(dirs), icon(dirs), shortcut(dirs), receipt(dirs)];
    for path in &paths { check_destination(path)?; }
    if !paths[3].exists() {
        if paths.iter().any(|path| path.exists()) {
            return Err(permission_denied("An Applications file belongs to another installation"));
        }
        return Ok(None);
    }
    let saved: Receipt = serde_json::from_slice(&fs::read(&paths[3])?).map_err(invalid_data)?;
    if saved.installation_version != 1 || [&saved.image_sha256, &saved.icon_sha256].iter()
        .any(|hash| hash.len() != 64 || !hash.bytes().all(|byte| byte.is_ascii_hexdigit())) {
        return Err(invalid_data("Unsupported AppImage installation receipt"));
    }
    if paths[..3].iter().any(|path| !path.is_file())
        || fs::metadata(&paths[0])?.len() != saved.image_bytes
        || executable_file(&paths[0]).is_err()
        || fs::read(&paths[2])? != desktop_entry(dirs)?
        || checksum(&paths[1])? != saved.icon_sha256
        || (verify_image && checksum(&paths[0])? != saved.image_sha256) {
        return Err(permission_denied("The installed AppImage or Applications shortcut has changed; restore it before continuing"));
    }
    Ok(Some(saved))
}

pub fn status(layout: &Layout, dirs: &UserDirs, is_image: bool) -> io::Result<Status> {
    if !is_image { return Ok(Status { kind: "deb", installed: true, app: layout.app.clone(), shortcut: None }); }
    let installed = owned(dirs, false)?.is_some();
    Ok(Status { kind: "appimage", installed, app: if installed { image(dirs) } else { layout.app.clone() },
        shortcut: installed.then(|| shortcut(dirs)) })
}

pub fn preferred_layout(layout: &Layout, dirs: &UserDirs, is_image: bool) -> Layout {
    if is_image && layout.app != image(dirs) {
        if let Ok(Some(saved)) = owned(dirs, true) {
            if checksum(&layout.app).is_ok_and(|hash| hash == saved.image_sha256) {
                return Layout { app: image(dirs), engine: layout.engine.clone(), native: layout.native.clone() };
            }
        }
    }
    layout.clone()
}

fn copy_image(source: &Path, destination: &Path) -> io::Result<()> {
    let mut source = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(source)?;
    let metadata = source.metadata()?;
    if !metadata.is_file() || metadata.uid() != unsafe { libc::geteuid() } {
        return Err(permission_denied("Install an AppImage file owned by this user"));
    }
    let mut nonce = [0u8; 8];
    getrandom::fill(&mut nonce).map_err(io::Error::other)?;
    let temporary = destination.with_extension(format!("{:016x}.tmp", u64::from_ne_bytes(nonce)));
    let result = (|| {
        let mut file = OpenOptions::new().write(true).create_new(true).mode(0o700).custom_flags(libc::O_NOFOLLOW).open(&temporary)?;
        io::copy(&mut source, &mut file)?;
        file.sync_all()?;
        fs::rename(&temporary, destination)
    })();
    if result.is_err() { let _ = fs::remove_file(&temporary); }
    result
}

pub fn install(layout: &Layout, dirs: &UserDirs, is_image: bool, icon_bytes: &[u8]) -> io::Result<Layout> {
    if !is_image { return Err(invalid_data("Only an AppImage can be installed in Applications this way")); }
    let existing = owned(dirs, true)?;
    let config = bridge_config(layout, dirs)?;
    if connection_enabled(&dirs.connection_path())? {
        let native = dirs.bridge_dir().join("vidgrab-native-host");
        for (path, firefox) in manifests(dirs) {
            check_destination(&path)?;
            if path.exists() && !owned_manifest(&path, &native, firefox)? {
                return Err(permission_denied("An existing browser host manifest belongs to another installation"));
            }
        }
    }
    let folder = directory(dirs);
    for path in [&config.data_dir, &config.download_dir] {
        if path.starts_with(&folder) || folder.starts_with(path) {
            return Err(invalid_data("State and media must stay outside the managed application folder"));
        }
    }
    let desktop = desktop_entry(dirs)?;
    let source_hash = checksum(&layout.app)?;
    let installed = Layout { app: image(dirs), engine: layout.engine.clone(), native: layout.native.clone() };
    if let Some(saved) = existing {
        if saved.image_sha256 != source_hash {
            return Err(permission_denied("A different AppImage belongs to another installation; open that copy to remove it first"));
        }
        prepare(&installed, dirs)?;
        return Ok(installed);
    }
    let saved = Receipt { installation_version: 1, image_sha256: source_hash,
        image_bytes: fs::metadata(&layout.app)?.len(), icon_sha256: format!("{:x}", Sha256::digest(icon_bytes)) };
    private_directory(&folder)?;
    private_directory(shortcut(dirs).parent().unwrap())?;
    let mut created = Vec::new();
    let result = (|| {
        copy_image(&layout.app, &installed.app)?; created.push(installed.app.clone());
        atomic_write(&icon(dirs), icon_bytes, 0o600)?; created.push(icon(dirs));
        atomic_write(&shortcut(dirs), &desktop, 0o600)?; created.push(shortcut(dirs));
        atomic_write(&receipt(dirs), &json_bytes(&saved)?, 0o600)?; created.push(receipt(dirs));
        prepare(&installed, dirs)?;
        Ok(installed)
    })();
    if result.is_err() { for path in created.into_iter().rev() { let _ = fs::remove_file(path); } }
    result
}

fn idle_lock(dirs: &UserDirs) -> io::Result<Option<File>> {
    check_bridge(dirs)?;
    let config = InstalledConfig::load(&dirs.config_path())?;
    let path = config.data_dir.join("ipc/owner.lock");
    check_destination(&path)?;
    let lock = match OpenOptions::new().read(true).write(true).custom_flags(libc::O_NOFOLLOW).open(&path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    match lock.try_lock() {
        Ok(()) => Ok(Some(lock)),
        Err(TryLockError::WouldBlock) => Err(permission_denied("Quit VidGrab before removing its installed copy")),
        Err(TryLockError::Error(error)) => Err(error),
    }
}

pub fn check_removal(dirs: &UserDirs, is_image: bool) -> io::Result<()> {
    if is_image && owned(dirs, true)?.is_none() {
        return Err(invalid_data("Install this AppImage before removing its managed copy"));
    }
    check_bridge(dirs)?;
    connection_enabled(&dirs.connection_path())?;
    owned_registrations(dirs)?;
    Ok(())
}

pub fn remove(dirs: &UserDirs) -> io::Result<()> {
    check_removal(dirs, true)?;
    let _lock = idle_lock(dirs)?;
    disconnect(dirs)?;
    for path in [shortcut(dirs), image(dirs), icon(dirs), receipt(dirs)] { fs::remove_file(path)?; }
    match fs::remove_dir(directory(dirs)) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::DirectoryNotEmpty => Ok(()),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture { root: PathBuf, dirs: UserDirs, layout: Layout }
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("vidgrab-application-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            fs::create_dir(&root).unwrap();
            let home = root.join("home বাংলা % $ ` quoted\"");
            fs::create_dir(&home).unwrap();
            let dirs = UserDirs { data_home: home.join("data"), config_home: home.join("config"), home };
            let layout = Layout { app: root.join("downloaded.AppImage"), engine: root.join("mount/engine/vidgrab-engine"), native: root.join("mount/bin/vidgrab-native-host") };
            for path in [&layout.app, &layout.engine, &layout.native] {
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                fs::write(path, b"owned fixture executable").unwrap();
                fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
            }
            Self { root, dirs, layout }
        }
        fn install(&self) -> Layout { install(&self.layout, &self.dirs, true, b"fixture icon").unwrap() }
    }
    impl Drop for Fixture { fn drop(&mut self) { fs::remove_dir_all(&self.root).unwrap(); } }

    #[test]
    fn installation_status_is_read_only() {
        let fixture = Fixture::new();
        let state = status(&fixture.layout, &fixture.dirs, true).unwrap();
        assert!(!state.installed);
        assert!(!fixture.dirs.data_home.exists());
        assert!(!fixture.dirs.config_home.exists());
    }

    #[test]
    fn shortcut_symlinks_are_refused_before_copying_or_registration() {
        let fixture = Fixture::new();
        let target = fixture.root.join("foreign");
        fs::write(&target, b"foreign shortcut").unwrap();
        fs::create_dir_all(shortcut(&fixture.dirs).parent().unwrap()).unwrap();
        symlink(&target, shortcut(&fixture.dirs)).unwrap();
        assert!(install(&fixture.layout, &fixture.dirs, true, b"icon").is_err());
        assert_eq!(fs::read(target).unwrap(), b"foreign shortcut");
        assert!(!image(&fixture.dirs).exists());
        assert!(!fixture.dirs.bridge_dir().exists());
    }

    #[test]
    fn selected_state_cannot_overlap_the_application_copy() {
        let fixture = Fixture::new();
        let mut config = prepare(&fixture.layout, &fixture.dirs).unwrap();
        config.data_dir = directory(&fixture.dirs);
        fs::write(fixture.dirs.config_path(), json_bytes(&config).unwrap()).unwrap();
        assert!(install(&fixture.layout, &fixture.dirs, true, b"icon").is_err());
        assert!(!image(&fixture.dirs).exists());
    }

    #[test]
    fn changed_image_bytes_are_preserved_on_a_refused_removal() {
        let fixture = Fixture::new();
        let installed = fixture.install();
        connect(&installed, &fixture.dirs).unwrap();
        let preference = fs::read(fixture.dirs.connection_path()).unwrap();
        let mut bytes = fs::read(&installed.app).unwrap(); bytes[0] ^= 1;
        fs::write(&installed.app, &bytes).unwrap();
        assert!(remove(&fixture.dirs).is_err());
        assert_eq!(fs::read(installed.app).unwrap(), bytes);
        assert_eq!(fs::read(fixture.dirs.connection_path()).unwrap(), preference);
    }

    #[test]
    fn removal_preserves_unrelated_files_in_the_application_folder() {
        let fixture = Fixture::new();
        let installed = fixture.install();
        let note = directory(&fixture.dirs).join("user note.txt");
        fs::write(&note, b"keep my note").unwrap();
        let result = remove(&fixture.dirs);
        assert!(result.is_ok(), "Unrelated files must be preserved: {result:?}");
        assert!(!installed.app.exists());
        assert!(!shortcut(&fixture.dirs).exists());
        assert_eq!(fs::read(note).unwrap(), b"keep my note");
        assert!(!connection_enabled(&fixture.dirs.connection_path()).unwrap());
    }
}
