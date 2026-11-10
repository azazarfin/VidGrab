//! Per-user package discovery and browser connection state.
#[cfg(target_os = "linux")]
pub mod application;
#[cfg(windows)]
#[path = "installation/windows_application.rs"]
pub mod application;
use crate::Config;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
#[cfg(target_os = "linux")]
use std::fs::OpenOptions;
#[cfg(target_os = "linux")]
use std::fs::DirBuilder;
use std::io::{self, Write};
#[cfg(target_os = "linux")]
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

fn native_name() -> &'static str { if cfg!(windows) { "vidgrab-native-host.exe" } else { "vidgrab-native-host" } }
fn engine_name() -> &'static str { if cfg!(windows) { "vidgrab-engine.exe" } else { "vidgrab-engine" } }

pub const HOST_NAME: &str = "com.vidgrab.companion";
pub const CHROME_ORIGIN: &str = "chrome-extension://ikjpekhbbaehdhigakbahlhafaojbjpm/";
pub const FIREFOX_ID: &str = "vidgrab@azaz.local";

#[derive(Clone, Debug)]
pub struct UserDirs {
    pub home: PathBuf,
    pub data_home: PathBuf,
    pub config_home: PathBuf,
}

impl UserDirs {
    pub fn from_env() -> io::Result<Self> {
        #[cfg(windows)]
        let dirs = {
            let home = PathBuf::from(std::env::var_os("USERPROFILE").filter(|v| !v.is_empty())
                .ok_or_else(|| invalid_data("USERPROFILE must select a user directory"))?);
            let data_home = PathBuf::from(std::env::var_os("LOCALAPPDATA").filter(|v| !v.is_empty())
                .ok_or_else(|| invalid_data("LOCALAPPDATA must select a user directory"))?);
            Self { home, config_home: data_home.clone(), data_home }
        };
        #[cfg(target_os = "linux")]
        let dirs = {
            let home = PathBuf::from(std::env::var_os("HOME").filter(|v| !v.is_empty())
                .ok_or_else(|| invalid_data("HOME must select a user directory"))?);
            Self { data_home: std::env::var_os("XDG_DATA_HOME").filter(|v| !v.is_empty()).map(PathBuf::from).unwrap_or_else(|| home.join(".local/share")),
                config_home: std::env::var_os("XDG_CONFIG_HOME").filter(|v| !v.is_empty()).map(PathBuf::from).unwrap_or_else(|| home.join(".config")), home }
        };
        validate_dirs(&dirs)?;
        Ok(dirs)
    }
    pub fn bridge_dir(&self) -> PathBuf { self.data_home.join("vidgrab-p02/native") }
    pub fn config_path(&self) -> PathBuf { self.bridge_dir().join("host-config.json") }
    pub fn connection_path(&self) -> PathBuf { self.bridge_dir().join("browser-connection.json") }
}

#[derive(Clone, Debug)]
pub struct Layout {
    pub app: PathBuf,
    pub engine: PathBuf,
    pub native: PathBuf,
}

impl Layout {
    pub fn discover(executable: &Path, resources: &Path, appimage: Option<&Path>) -> io::Result<Self> {
        for path in [executable, resources] { absolute(path)?; }
        let executable = executable.canonicalize()?;
        let app = appimage.unwrap_or(&executable);
        absolute(app)?;
        let layout = Self { app: app.canonicalize()?, engine: resources.join("engine").join(engine_name()).canonicalize()?,
            native: executable.parent().ok_or_else(|| io::Error::other("Missing executable directory"))?.join(native_name()).canonicalize()? };
        for file in [&layout.app, &layout.engine, &layout.native] { executable_file(file)?; }
        let tools = layout.engine.parent().unwrap().join("_internal/tools/manifest.json");
        if !tools.is_file() { return Err(io::Error::new(io::ErrorKind::NotFound, "Missing frozen engine support tree")); }
        Ok(layout)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InstalledConfig {
    pub package_version: u32,
    pub app: PathBuf,
    pub data_dir: PathBuf,
    pub download_dir: PathBuf,
    pub chrome_origin: String,
    pub firefox_id: String,
}

impl InstalledConfig {
    pub fn load(path: &Path) -> io::Result<Self> {
        let config: Self = serde_json::from_slice(&fs::read(path)?).map_err(invalid_data)?;
        config.validate()?;
        Ok(config)
    }
    fn validate(&self) -> io::Result<()> {
        if self.package_version != 1 || self.chrome_origin != CHROME_ORIGIN || self.firefox_id != FIREFOX_ID
            || [&self.app, &self.data_dir, &self.download_dir].iter().any(|path| !path.is_absolute())
            || self.download_dir.starts_with(&self.data_dir) {
            return Err(invalid_data("Unsupported or invalid packaged companion configuration"));
        }
        Ok(())
    }
    pub fn runtime(&self, layout: &Layout) -> Config {
        Config { app: self.app.clone(), engine: layout.engine.clone(), data_dir: self.data_dir.clone(),
            download_dir: self.download_dir.clone(), chrome_origin: self.chrome_origin.clone(), firefox_id: self.firefox_id.clone() }
    }
}

pub fn register(layout: &Layout, dirs: &UserDirs) -> io::Result<InstalledConfig> {
    let config = bridge_config(layout, dirs)?;
    let native = dirs.bridge_dir().join(native_name());
    let manifests = manifests(dirs);
    #[cfg(windows)]
    crate::windows::registry::preflight(&manifests)?;
    for (path, firefox) in &manifests {
        check_destination(path)?;
        if path.exists() && !owned_manifest(path, &native, *firefox)? {
            return Err(permission_denied("An existing browser host manifest belongs to another installation"));
        }
    }
    write_bridge(layout, dirs, &config)?;
    for (path, firefox) in manifests.clone() {
        private_directory(path.parent().unwrap())?;
        atomic_write(&path, &json_bytes(&manifest(&native, firefox))?, 0o600)?;
    }
    #[cfg(windows)]
    crate::windows::registry::publish(&manifests)?;
    Ok(config)
}

fn check_bridge(dirs: &UserDirs) -> io::Result<()> {
    validate_dirs(dirs)?;
    let bridge = dirs.bridge_dir();
    check_directory(&bridge)?;
    if let Ok(metadata) = fs::symlink_metadata(&bridge) {
        #[cfg(windows)]
        let _ = &metadata;
        #[cfg(target_os = "linux")]
        if metadata.uid() != unsafe { libc::geteuid() } || metadata.permissions().mode() & 0o777 != 0o700 {
            return Err(permission_denied("The native bridge directory must be private and owned by this user"));
        }
        #[cfg(windows)]
        if !crate::windows::is_private(&bridge)? { return Err(permission_denied("The native bridge directory must be private and owned by this user")); }
    }
    let config_path = dirs.config_path();
    let native = bridge.join(native_name());
    for path in [&config_path, &native] { check_destination(path)?; }
    Ok(())
}

fn bridge_config(layout: &Layout, dirs: &UserDirs) -> io::Result<InstalledConfig> {
    check_bridge(dirs)?;
    for file in [&layout.app, &layout.engine, &layout.native] { absolute(file)?; executable_file(file)?; }
    let config_path = dirs.config_path();
    let mut config = if config_path.exists() { InstalledConfig::load(&config_path)? } else {
        InstalledConfig { package_version: 1, app: layout.app.clone(), data_dir: dirs.data_home.join("vidgrab-p02/state"),
            download_dir: dirs.home.join("Downloads/VidGrab-P02"), chrome_origin: CHROME_ORIGIN.into(), firefox_id: FIREFOX_ID.into() }
    };
    config.app = layout.app.clone();
    config.validate()?;
    for path in [&config.data_dir, &config.download_dir] {
        if path.starts_with(layout.engine.parent().unwrap().parent().unwrap()) || path.starts_with(layout.native.parent().unwrap()) {
            return Err(invalid_data("Runtime and media directories must be outside the package"));
        }
    }
    Ok(config)
}

fn write_bridge(layout: &Layout, dirs: &UserDirs, config: &InstalledConfig) -> io::Result<()> {
    let bridge = dirs.bridge_dir();
    let native = bridge.join(native_name());
    private_directory(&bridge)?;
    atomic_write(&native, &fs::read(&layout.native)?, 0o700)?;
    atomic_write(&dirs.config_path(), &json_bytes(config)?, 0o600)
}

fn owned_registrations(dirs: &UserDirs) -> io::Result<Vec<PathBuf>> {
    validate_dirs(dirs)?;
    let native = dirs.bridge_dir().join(native_name());
    let mut owned = Vec::new();
    for (path, firefox) in manifests(dirs) {
        check_destination(&path)?;
        if path.exists() && owned_manifest(&path, &native, firefox)? { owned.push(path); }
    }
    Ok(owned)
}

pub fn unregister(dirs: &UserDirs) -> io::Result<usize> {
    let owned = owned_registrations(dirs)?;
    #[cfg(windows)]
    crate::windows::registry::remove_owned(&manifests(dirs))?;
    for path in &owned { fs::remove_file(path)?; }
    // User-owned runtime, media and the selected paths remain available for reconnecting.
    Ok(owned.len())
}

#[derive(Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ManifestState { Missing, Registered, Conflict }

#[derive(Debug, Serialize)]
pub struct ConnectionStatus {
    pub enabled: bool,
    pub bridge_ready: bool,
    pub chrome: ManifestState,
    pub firefox: ManifestState,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ConnectionPreference { connection_version: u32, enabled: bool }

fn connection_enabled(path: &Path) -> io::Result<bool> {
    check_destination(path)?;
    if !path.exists() { return Ok(true); }
    let preference: ConnectionPreference = serde_json::from_slice(&fs::read(path)?).map_err(invalid_data)?;
    if preference.connection_version != 1 { return Err(invalid_data("Unsupported browser connection preference")); }
    Ok(preference.enabled)
}

pub fn connection_status(layout: &Layout, dirs: &UserDirs) -> io::Result<ConnectionStatus> {
    check_bridge(dirs)?;
    let enabled = connection_enabled(&dirs.connection_path())?;
    let native = dirs.bridge_dir().join(native_name());
    let config = if dirs.config_path().exists() { Some(InstalledConfig::load(&dirs.config_path())?) } else { None };
    let bridge_ready = config.is_some_and(|config| config.app == layout.app)
        && executable_file(&native).is_ok();
    let states = manifests(dirs).into_iter().map(|(path, firefox)| {
        check_destination(&path)?;
        Ok(if !path.exists() { ManifestState::Missing }
            else if owned_manifest(&path, &native, firefox)? { ManifestState::Registered }
            else { ManifestState::Conflict })
    }).collect::<io::Result<Vec<_>>>()?;
    #[cfg(windows)]
    let states = {
        let registry = crate::windows::registry::states(&manifests(dirs))?;
        states.into_iter().zip(registry).map(|(file, registry)| {
            if file == ManifestState::Conflict || registry == ManifestState::Conflict { ManifestState::Conflict }
            else if file == ManifestState::Registered && registry == ManifestState::Registered { ManifestState::Registered }
            else { ManifestState::Missing }
        }).collect::<Vec<_>>()
    };
    let mut states = states.into_iter();
    Ok(ConnectionStatus { enabled, bridge_ready, chrome: states.next().unwrap(), firefox: states.next().unwrap() })
}

pub fn connect(layout: &Layout, dirs: &UserDirs) -> io::Result<InstalledConfig> {
    check_bridge(dirs)?;
    // Refuse an unsafe preference before register() writes any package files.
    connection_enabled(&dirs.connection_path())?;
    let config = register(layout, dirs)?;
    atomic_write(&dirs.connection_path(), &json_bytes(&ConnectionPreference { connection_version: 1, enabled: true })?, 0o600)?;
    Ok(config)
}

pub fn disconnect(dirs: &UserDirs) -> io::Result<usize> {
    check_bridge(dirs)?;
    connection_enabled(&dirs.connection_path())?;
    let owned = owned_registrations(dirs)?;
    private_directory(&dirs.bridge_dir())?;
    // Persist revocation before removing manifests. An interrupted removal must
    // not let a remaining host or the next launch enable browser access again.
    atomic_write(&dirs.connection_path(), &json_bytes(&ConnectionPreference { connection_version: 1, enabled: false })?, 0o600)?;
    #[cfg(windows)]
    crate::windows::registry::remove_owned(&manifests(dirs))?;
    for path in &owned { fs::remove_file(path)?; }
    Ok(owned.len())
}

pub fn prepare(layout: &Layout, dirs: &UserDirs) -> io::Result<InstalledConfig> {
    if connection_enabled(&dirs.connection_path())? { return register(layout, dirs); }
    let config = bridge_config(layout, dirs)?;
    write_bridge(layout, dirs, &config)?;
    unregister(dirs)?;
    Ok(config)
}

#[derive(Deserialize)]
#[serde(untagged)]
pub enum HostConfig { Staged(Config), Packaged(InstalledConfig) }
impl HostConfig {
    pub fn load(path: &Path) -> io::Result<Self> {
        let config: Self = serde_json::from_slice(&fs::read(path)?).map_err(invalid_data)?;
        match &config {
            Self::Packaged(config) => config.validate()?,
            Self::Staged(config) => {
                if [&config.app, &config.engine, &config.data_dir, &config.download_dir].iter().any(|path| !path.is_absolute()) {
                    return Err(invalid_data("Companion paths must be absolute"));
                }
            }
        }
        Ok(config)
    }
    pub fn launch_arg(&self) -> &str { match self { Self::Staged(_) => "--config", Self::Packaged(_) => "--package-config" } }
    pub fn app(&self) -> &Path { match self { Self::Staged(config) => &config.app, Self::Packaged(config) => &config.app } }
    pub fn socket_path(&self) -> PathBuf {
        match self {
            Self::Staged(config) => config.socket_path(),
            Self::Packaged(config) => {
                #[cfg(target_os = "linux")] { config.data_dir.join("ipc/control.sock") }
                #[cfg(windows)] { crate::windows::pipe_path(&config.data_dir) }
            }
        }
    }
    pub fn chrome_origin(&self) -> &str { match self { Self::Staged(config) => &config.chrome_origin, Self::Packaged(config) => &config.chrome_origin } }
    pub fn firefox_id(&self) -> &str { match self { Self::Staged(config) => &config.firefox_id, Self::Packaged(config) => &config.firefox_id } }
    pub fn browser_enabled(&self, config_path: &Path) -> io::Result<bool> {
        match self { Self::Staged(_) => Ok(true), Self::Packaged(_) => connection_enabled(&config_path.with_file_name("browser-connection.json")) }
    }
}

fn absolute(path: &Path) -> io::Result<()> {
    if !path.is_absolute() { return Err(io::Error::new(io::ErrorKind::InvalidInput, "Package and user paths must be absolute")); }
    Ok(())
}
fn invalid_data(error: impl Into<Box<dyn std::error::Error + Send + Sync>>) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, error) }
fn permission_denied(message: &str) -> io::Error { io::Error::new(io::ErrorKind::PermissionDenied, message) }
fn executable_file(path: &Path) -> io::Result<()> {
    let metadata = fs::metadata(path)?;
    if !metadata.is_file() { return Err(invalid_data("Missing executable package component")); }
    #[cfg(target_os = "linux")]
    if metadata.permissions().mode() & 0o111 == 0 { return Err(invalid_data("Missing executable package component")); }
    Ok(())
}
fn validate_dirs(dirs: &UserDirs) -> io::Result<()> {
    for path in [&dirs.home, &dirs.data_home, &dirs.config_home] { absolute(path)?; check_directory(path)?; }
    Ok(())
}
#[cfg(target_os = "linux")]
fn check_directory(path: &Path) -> io::Result<()> {
    for directory in path.ancestors() {
        match fs::symlink_metadata(directory) {
            Ok(metadata) if metadata.is_symlink() || !metadata.is_dir() => return Err(permission_denied("Registration directories must not be symlinks or files")),
            Ok(_) => (),
            Err(error) if error.kind() == io::ErrorKind::NotFound => (),
            Err(error) => return Err(error),
        }
    }
    Ok(())
}
#[cfg(windows)]
fn check_directory(path: &Path) -> io::Result<()> { crate::windows::check_directory(path) }
fn check_destination(path: &Path) -> io::Result<()> {
    check_directory(path.parent().ok_or_else(|| invalid_data("Missing registration directory"))?)?;
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if !metadata.is_file() { return Err(permission_denied("Registration files must be regular files owned by this user")); }
            #[cfg(target_os = "linux")]
            if metadata.uid() != unsafe { libc::geteuid() } { return Err(permission_denied("Registration files must be regular files owned by this user")); }
            #[cfg(windows)]
            crate::windows::check_owned(path)?;
            Ok(())
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}
#[cfg(target_os = "linux")]
fn private_directory(path: &Path) -> io::Result<()> { DirBuilder::new().recursive(true).mode(0o700).create(path) }
#[cfg(windows)]
fn private_directory(path: &Path) -> io::Result<()> { crate::windows::private_directory(path) }
#[cfg(target_os = "linux")]
fn manifests(dirs: &UserDirs) -> [(PathBuf, bool); 2] {
    [(dirs.config_home.join("google-chrome/NativeMessagingHosts").join(format!("{HOST_NAME}.json")), false),
        (dirs.home.join(".mozilla/native-messaging-hosts").join(format!("{HOST_NAME}.json")), true)]
}
#[cfg(windows)]
fn manifests(dirs: &UserDirs) -> [(PathBuf, bool); 2] {
    [(dirs.bridge_dir().join("chrome.json"), false), (dirs.bridge_dir().join("firefox.json"), true)]
}
fn manifest(native: &Path, firefox: bool) -> Value {
    let mut value = json!({"name": HOST_NAME, "description": "VidGrab P02 desktop companion", "path": native, "type": "stdio"});
    if firefox { value["allowed_extensions"] = json!([FIREFOX_ID]); } else { value["allowed_origins"] = json!([CHROME_ORIGIN]); }
    value
}
fn owned_manifest(path: &Path, native: &Path, firefox: bool) -> io::Result<bool> {
    let value: Value = match serde_json::from_slice(&fs::read(path)?) { Ok(value) => value, Err(_) => return Ok(false) };
    let expected = manifest(native, firefox);
    Ok(["name", "path", "type", if firefox { "allowed_extensions" } else { "allowed_origins" }].iter().all(|key| value[*key] == expected[*key]))
}
fn json_bytes(value: &impl Serialize) -> io::Result<Vec<u8>> {
    let mut bytes = serde_json::to_vec_pretty(value).map_err(invalid_data)?;
    bytes.push(b'\n');
    Ok(bytes)
}
fn atomic_write(path: &Path, bytes: &[u8], mode: u32) -> io::Result<()> {
    let mut nonce = [0u8; 8];
    getrandom::fill(&mut nonce).map_err(io::Error::other)?;
    let temporary = path.with_extension(format!("{:016x}.tmp", u64::from_ne_bytes(nonce)));
    let result = (|| {
        #[cfg(target_os = "linux")]
        let mut options = OpenOptions::new();
        #[cfg(target_os = "linux")]
        options.write(true).create_new(true);
        #[cfg(target_os = "linux")]
        options.mode(mode).custom_flags(libc::O_NOFOLLOW);
        #[cfg(target_os = "linux")]
        let mut file = options.open(&temporary)?;
        #[cfg(windows)]
        let mut file = { let _ = mode; crate::windows::create_private_file(&temporary)? };
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        #[cfg(target_os = "linux")] { fs::rename(&temporary, path) }
        #[cfg(windows)] { crate::windows::replace(&temporary, path) }
    })();
    if result.is_err() { let _ = fs::remove_file(&temporary); }
    result
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("vidgrab-package-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn dirs(&self) -> UserDirs {
            UserDirs { home: self.0.join("home with বাংলা"), data_home: self.0.join("data"), config_home: self.0.join("config") }
        }
        fn layout(&self, name: &str) -> Layout {
            let root = self.0.join(name);
            let app = root.join("usr/bin/vidgrab-desktop");
            let native = root.join("usr/bin/vidgrab-native-host");
            let engine = root.join("usr/lib/VidGrab/engine/vidgrab-engine");
            for file in [&app, &native, &engine] {
                fs::create_dir_all(file.parent().unwrap()).unwrap();
                fs::write(file, name.as_bytes()).unwrap();
                fs::set_permissions(file, fs::Permissions::from_mode(0o755)).unwrap();
            }
            let manifest = engine.parent().unwrap().join("_internal/tools/manifest.json");
            fs::create_dir_all(manifest.parent().unwrap()).unwrap();
            fs::write(manifest, "{}").unwrap();
            Layout { app, engine, native }
        }
    }
    impl Drop for Fixture { fn drop(&mut self) { fs::remove_dir_all(&self.0).unwrap(); } }
    fn chrome(dirs: &UserDirs) -> PathBuf { dirs.config_home.join("google-chrome/NativeMessagingHosts").join(format!("{HOST_NAME}.json")) }
    fn firefox(dirs: &UserDirs) -> PathBuf { dirs.home.join(".mozilla/native-messaging-hosts").join(format!("{HOST_NAME}.json")) }

    #[test]
    fn discovers_engine_resources_separately_from_the_debian_executable() {
        let fixture = Fixture::new();
        let expected = fixture.layout("deb");
        let result = Layout::discover(&expected.app, expected.engine.parent().unwrap().parent().unwrap(), None);
        assert!(result.is_ok(), "A complete Debian layout must be discoverable: {result:?}");
        let actual = result.unwrap();
        assert_eq!(actual.app, expected.app);
        assert_eq!(actual.engine, expected.engine);
        assert_eq!(actual.native, expected.native);
    }

    #[test]
    fn appimage_launch_target_is_the_image_file_instead_of_its_mount() {
        let fixture = Fixture::new();
        let mounted = fixture.layout("temporary mount");
        let image = fixture.0.join("VidGrab with বাংলা.AppImage");
        fs::write(&image, "image").unwrap();
        fs::set_permissions(&image, fs::Permissions::from_mode(0o755)).unwrap();
        let result = Layout::discover(&mounted.app, mounted.engine.parent().unwrap().parent().unwrap(), Some(&image));
        assert!(result.is_ok(), "AppImage discovery must accept a stable launch target: {result:?}");
        let actual = result.unwrap();
        assert_eq!(actual.app, image);
        assert_eq!(actual.engine, mounted.engine);
    }

    #[test]
    fn an_appimage_in_the_home_folder_allows_normal_user_state_and_media() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        fs::create_dir_all(&dirs.home).unwrap();
        let image = dirs.home.join("VidGrab.AppImage");
        fs::write(&image, b"image").unwrap();
        fs::set_permissions(&image, fs::Permissions::from_mode(0o755)).unwrap();
        let mounted = fixture.layout("mount");
        let layout = Layout::discover(&mounted.app, mounted.engine.parent().unwrap().parent().unwrap(), Some(&image)).unwrap();
        let result = register(&layout, &dirs);
        assert!(result.is_ok(), "The parent of a portable image is a user folder, not protected package storage: {result:?}");
        assert_eq!(result.unwrap().download_dir, dirs.home.join("Downloads/VidGrab-P02"));
    }

    #[test]
    fn incomplete_packages_are_rejected_before_registration() {
        let fixture = Fixture::new();
        let layout = fixture.layout("broken");
        fs::remove_file(&layout.native).unwrap();
        assert_eq!(Layout::discover(&layout.app, layout.engine.parent().unwrap().parent().unwrap(), None).unwrap_err().kind(), io::ErrorKind::NotFound);
        assert!(!fixture.dirs().data_home.exists());
    }

    #[test]
    fn registers_a_private_stable_host_without_persisting_mounted_engine_paths() {
        let fixture = Fixture::new();
        let layout = fixture.layout("mount");
        let dirs = fixture.dirs();
        let result = register(&layout, &dirs);
        assert!(result.is_ok(), "A clean per-user registration must succeed: {result:?}");
        let saved = result.unwrap();
        let host = dirs.bridge_dir().join(native_name());
        assert_eq!(fs::read(&host).unwrap(), fs::read(&layout.native).unwrap());
        assert_eq!(fs::metadata(dirs.bridge_dir()).unwrap().permissions().mode() & 0o777, 0o700);
        assert_eq!(fs::metadata(&host).unwrap().permissions().mode() & 0o777, 0o700);
        let chrome: Value = serde_json::from_slice(&fs::read(chrome(&dirs)).unwrap()).unwrap();
        let firefox: Value = serde_json::from_slice(&fs::read(firefox(&dirs)).unwrap()).unwrap();
        assert_eq!(chrome["path"], json!(host));
        assert_eq!(chrome["allowed_origins"], json!([CHROME_ORIGIN]));
        assert_eq!(firefox["allowed_extensions"], json!([FIREFOX_ID]));
        let config: Value = serde_json::from_slice(&fs::read(dirs.config_path()).unwrap()).unwrap();
        assert!(config.get("engine").is_none());
        assert_eq!(InstalledConfig::load(&dirs.config_path()).unwrap().app, layout.app);
        assert_eq!(saved.runtime(&layout).engine, layout.engine);
    }

    #[test]
    fn relocation_repairs_the_launcher_and_preserves_selected_state_and_media() {
        let fixture = Fixture::new();
        let first = fixture.layout("first");
        let second = fixture.layout("moved বাংলা");
        let dirs = fixture.dirs();
        let result = register(&first, &dirs);
        assert!(result.is_ok(), "Initial registration must succeed: {result:?}");
        let mut original = result.unwrap();
        original.data_dir = fixture.0.join("selected state");
        original.download_dir = fixture.0.join("selected media বাংলা");
        fs::write(dirs.config_path(), serde_json::to_vec(&original).unwrap()).unwrap();
        let repaired = register(&second, &dirs).unwrap();
        assert_eq!(repaired.app, second.app);
        assert_eq!(repaired.data_dir, original.data_dir);
        assert_eq!(repaired.download_dir, original.download_dir);
        assert_eq!(fs::read(dirs.bridge_dir().join(native_name())).unwrap(), b"moved \xe0\xa6\xac\xe0\xa6\xbe\xe0\xa6\x82\xe0\xa6\xb2\xe0\xa6\xbe");
    }

    #[test]
    fn foreign_browser_manifests_are_preserved_before_any_registration_writes() {
        let fixture = Fixture::new();
        let layout = fixture.layout("deb");
        let dirs = fixture.dirs();
        let foreign = firefox(&dirs);
        fs::create_dir_all(foreign.parent().unwrap()).unwrap();
        fs::write(&foreign, br#"{"name":"com.vidgrab.companion","path":"/another/host"}"#).unwrap();
        let original = fs::read(&foreign).unwrap();
        let result = register(&layout, &dirs);
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(fs::read(foreign).unwrap(), original);
        assert!(!dirs.data_home.exists());
        assert!(!chrome(&dirs).exists());
    }

    #[test]
    fn unregister_removes_only_matching_manifests_and_preserves_user_files() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        let result = register(&fixture.layout("deb"), &dirs);
        assert!(result.is_ok(), "Initial registration must succeed: {result:?}");
        let config = result.unwrap();
        fs::create_dir_all(&config.data_dir).unwrap();
        fs::write(config.data_dir.join("vidgrab.db"), b"state").unwrap();
        fs::create_dir_all(&config.download_dir).unwrap();
        fs::write(config.download_dir.join("finished.mp3"), b"media").unwrap();
        fs::write(firefox(&dirs), br#"{"name":"com.vidgrab.companion","path":"/another/host"}"#).unwrap();
        assert_eq!(unregister(&dirs).unwrap(), 1);
        assert!(!chrome(&dirs).exists());
        assert!(firefox(&dirs).exists());
        assert_eq!(fs::read(config.data_dir.join("vidgrab.db")).unwrap(), b"state");
        assert_eq!(fs::read(config.download_dir.join("finished.mp3")).unwrap(), b"media");
        assert!(dirs.config_path().exists());
    }

    #[test]
    fn symlinked_registration_destinations_are_rejected_without_touching_targets() {
        for destination in ["bridge", "config", "manifest"] {
            let fixture = Fixture::new();
            let dirs = fixture.dirs();
            let target = fixture.0.join("unrelated");
            let link = match destination { "bridge" => dirs.bridge_dir(), "config" => dirs.config_path(), _ => chrome(&dirs) };
            if destination == "bridge" { fs::create_dir(&target).unwrap(); } else { fs::write(&target, "preserved").unwrap(); }
            fs::create_dir_all(link.parent().unwrap()).unwrap();
            symlink(&target, &link).unwrap();
            assert_eq!(register(&fixture.layout("deb"), &dirs).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
            if destination == "bridge" { assert_eq!(fs::read_dir(&target).unwrap().count(), 0); }
            else { assert_eq!(fs::read_to_string(&target).unwrap(), "preserved"); }
        }
    }

    #[test]
    fn installed_configs_reject_newer_versions_and_relative_paths() {
        let fixture = Fixture::new();
        let config = fixture.0.join("invalid.json");
        for contents in [json!({"package_version":2, "app":"/app", "data_dir":"/state", "download_dir":"/media", "chrome_origin":CHROME_ORIGIN, "firefox_id":FIREFOX_ID}),
            json!({"package_version":1, "app":"relative", "data_dir":"/state", "download_dir":"/media", "chrome_origin":CHROME_ORIGIN, "firefox_id":FIREFOX_ID})] {
            fs::write(&config, serde_json::to_vec(&contents).unwrap()).unwrap();
            assert_eq!(InstalledConfig::load(&config).unwrap_err().kind(), io::ErrorKind::InvalidData);
        }
    }

    #[test]
    fn host_configs_select_packaged_activation_and_preserve_explicit_staging() {
        let fixture = Fixture::new();
        let path = fixture.0.join("host.json");
        let layout = fixture.layout("deb");
        for (contents, argument) in [
            (json!({"package_version":1, "app":layout.app, "data_dir":fixture.0.join("state"), "download_dir":fixture.0.join("media"), "chrome_origin":CHROME_ORIGIN, "firefox_id":FIREFOX_ID}), "--package-config"),
            (json!({"app":layout.app, "engine":layout.engine, "data_dir":fixture.0.join("state"), "download_dir":fixture.0.join("media"), "chrome_origin":CHROME_ORIGIN, "firefox_id":FIREFOX_ID}), "--config"),
        ] {
            fs::write(&path, serde_json::to_vec(&contents).unwrap()).unwrap();
            let result = HostConfig::load(&path);
            assert!(result.is_ok(), "Both host layouts must load");
            let host = result.unwrap();
            assert_eq!(host.launch_arg(), argument);
            assert_eq!(host.app(), layout.app);
            assert_eq!(host.socket_path(), fixture.0.join("state/ipc/control.sock"));
            assert_eq!(host.chrome_origin(), CHROME_ORIGIN);
            assert_eq!(host.firefox_id(), FIREFOX_ID);
        }
    }

    #[test]
    fn connection_status_reads_a_fresh_home_without_creating_files() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        let layout = fixture.layout("deb");
        let status = connection_status(&layout, &dirs).unwrap();
        assert!(status.enabled);
        assert!(!status.bridge_ready);
        assert_eq!(status.chrome, ManifestState::Missing);
        assert_eq!(status.firefox, ManifestState::Missing);
        assert!(!dirs.home.exists() && !dirs.data_home.exists() && !dirs.config_home.exists());
    }

    #[test]
    fn disconnect_persists_across_app_reopen_and_preserves_user_files() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        let layout = fixture.layout("deb");
        let config = connect(&layout, &dirs).unwrap();
        fs::create_dir_all(&config.data_dir).unwrap();
        fs::create_dir_all(&config.download_dir).unwrap();
        let state = config.data_dir.join("vidgrab.db");
        let media = config.download_dir.join("finished.mp3");
        fs::write(&state, b"kept state").unwrap();
        fs::write(&media, b"kept media").unwrap();
        assert!(connection_status(&layout, &dirs).unwrap().bridge_ready);
        assert_eq!(disconnect(&dirs).unwrap(), 2);
        let reopened = prepare(&layout, &dirs).unwrap();
        assert_eq!(reopened.data_dir, config.data_dir);
        assert!(!connection_status(&layout, &dirs).unwrap().enabled);
        assert!(!chrome(&dirs).exists() && !firefox(&dirs).exists());
        assert_eq!(fs::read(&state).unwrap(), b"kept state");
        assert_eq!(fs::read(&media).unwrap(), b"kept media");
        assert!(dirs.config_path().is_file());
        connect(&layout, &dirs).unwrap();
        let status = connection_status(&layout, &dirs).unwrap();
        assert!(status.enabled && status.bridge_ready);
        assert_eq!(status.chrome, ManifestState::Registered);
        assert_eq!(status.firefox, ManifestState::Registered);
        assert_eq!(fs::read(&media).unwrap(), b"kept media");
    }

    #[test]
    fn disconnected_relocation_repairs_only_the_local_bridge() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        let first = fixture.layout("first");
        let moved = fixture.layout("moved");
        let original = connect(&first, &dirs).unwrap();
        disconnect(&dirs).unwrap();
        let saved = prepare(&moved, &dirs).unwrap();
        assert_eq!(saved.app, moved.app);
        assert_eq!(saved.data_dir, original.data_dir);
        assert_eq!(saved.download_dir, original.download_dir);
        assert!(!chrome(&dirs).exists() && !firefox(&dirs).exists());
        assert_eq!(fs::read(dirs.bridge_dir().join(native_name())).unwrap(), b"moved");
        let status = connection_status(&moved, &dirs).unwrap();
        assert!(!status.enabled && status.bridge_ready);
    }

    #[test]
    fn conflict_is_reported_and_reconnection_preserves_foreign_registration() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        let layout = fixture.layout("deb");
        connect(&layout, &dirs).unwrap();
        disconnect(&dirs).unwrap();
        let foreign = br#"{"name":"com.vidgrab.companion","path":"/another/host"}"#;
        fs::write(firefox(&dirs), foreign).unwrap();
        assert_eq!(connection_status(&layout, &dirs).unwrap().firefox, ManifestState::Conflict);
        assert_eq!(connect(&layout, &dirs).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert!(!connection_status(&layout, &dirs).unwrap().enabled);
        assert!(!chrome(&dirs).exists());
        assert_eq!(fs::read(firefox(&dirs)).unwrap(), foreign);
        assert_eq!(disconnect(&dirs).unwrap(), 0);
        assert_eq!(fs::read(firefox(&dirs)).unwrap(), foreign);
    }

    #[test]
    fn unsafe_connection_preference_is_refused_before_mutation() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        let layout = fixture.layout("deb");
        connect(&layout, &dirs).unwrap();
        let original = fs::read(chrome(&dirs)).unwrap();
        let target = fixture.0.join("unrelated.json");
        fs::write(&target, b"preserved").unwrap();
        fs::remove_file(dirs.connection_path()).unwrap();
        symlink(&target, dirs.connection_path()).unwrap();
        assert_eq!(disconnect(&dirs).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(connect(&layout, &dirs).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(connection_status(&layout, &dirs).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(fs::read(&target).unwrap(), b"preserved");
        assert_eq!(fs::read(chrome(&dirs)).unwrap(), original);
    }

    #[test]
    fn an_existing_packaged_host_observes_disconnection_without_reloading_config() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        let layout = fixture.layout("deb");
        connect(&layout, &dirs).unwrap();
        let host = HostConfig::load(&dirs.config_path()).unwrap();
        assert!(host.browser_enabled(&dirs.config_path()).unwrap());
        disconnect(&dirs).unwrap();
        assert!(!host.browser_enabled(&dirs.config_path()).unwrap());
        connect(&layout, &dirs).unwrap();
        assert!(host.browser_enabled(&dirs.config_path()).unwrap());
    }

    #[test]
    fn unsupported_connection_preference_is_rejected() {
        let fixture = Fixture::new();
        let dirs = fixture.dirs();
        let layout = fixture.layout("deb");
        connect(&layout, &dirs).unwrap();
        for bytes in [br#"{"connection_version":2,"enabled":false}"#.as_slice(),
            br#"{"connection_version":1,"enabled":null}"#.as_slice(),
            br#"{"connection_version":1,"enabled":true,"unknown":1}"#.as_slice()] {
            fs::write(dirs.connection_path(), bytes).unwrap();
            assert_eq!(prepare(&layout, &dirs).unwrap_err().kind(), io::ErrorKind::InvalidData);
        }
    }
}
