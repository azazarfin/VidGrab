//! Desktop preferences and app-controlled OS login autostart with ownership checks.
use super::*;

#[cfg(target_os = "linux")]
const AUTOSTART_FILE: &str = "com.vidgrab.desktop.desktop";
#[cfg(windows)]
const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
#[cfg(windows)]
const RUN_VALUE: &str = "VidGrab";

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopPreferences {
    pub preferences_version: u32,
    pub start_at_login: bool,
    pub close_to_tray: bool,
    pub start_minimized: bool,
    pub onboarding_completed: bool,
}

impl Default for DesktopPreferences {
    fn default() -> Self {
        Self {
            preferences_version: 1,
            start_at_login: false,
            close_to_tray: true,
            start_minimized: false,
            onboarding_completed: false,
        }
    }
}

pub fn load_preferences(path: &Path) -> io::Result<DesktopPreferences> {
    check_destination(path)?;
    if !path.exists() {
        return Ok(DesktopPreferences::default());
    }
    let prefs: DesktopPreferences = serde_json::from_slice(&fs::read(path)?).map_err(invalid_data)?;
    if prefs.preferences_version != 1 {
        return Err(invalid_data("Unsupported desktop preferences version"));
    }
    Ok(prefs)
}

pub fn save_preferences(path: &Path, prefs: &DesktopPreferences) -> io::Result<()> {
    if prefs.preferences_version != 1 {
        return Err(invalid_data("Unsupported desktop preferences version"));
    }
    check_destination(path)?;
    if let Some(parent) = path.parent() {
        private_directory(parent)?;
    }
    atomic_write(path, &json_bytes(prefs)?, 0o600)
}

#[cfg(target_os = "linux")]
pub fn autostart_path(dirs: &UserDirs) -> PathBuf {
    dirs.config_home.join("autostart").join(AUTOSTART_FILE)
}

#[cfg(target_os = "linux")]
fn desktop_value(path: &Path) -> io::Result<String> {
    let text = path
        .to_str()
        .ok_or_else(|| invalid_data("Application paths must be valid Unicode"))?;
    if text.chars().any(char::is_control) {
        return Err(invalid_data("Application paths cannot contain control characters"));
    }
    Ok(text.replace('\\', "\\\\"))
}

#[cfg(target_os = "linux")]
fn desktop_entry(app: &Path, start_minimized: bool) -> io::Result<Vec<u8>> {
    let executable = desktop_value(app)?
        .replace('\\', "\\\\")
        .replace('"', "\\\\\"")
        .replace('$', "\\\\$")
        .replace('`', "\\\\`")
        .replace('%', "%%");
    let exec_line = if start_minimized {
        format!("Exec=\"{executable}\" --start-minimized")
    } else {
        format!("Exec=\"{executable}\"")
    };
    Ok(format!(
        "[Desktop Entry]\nType=Application\nName=VidGrab\nComment=VidGrab download manager\n{exec_line}\nTerminal=false\nCategories=Utility;\nX-GNOME-Autostart-enabled=true\nX-VidGrab-Autostart=1\n"
    )
    .into_bytes())
}

#[cfg(target_os = "linux")]
fn is_owned_desktop_entry(bytes: &[u8]) -> bool {
    std::str::from_utf8(bytes).is_ok_and(|text| {
        text.lines()
            .any(|line| line.trim_end_matches('\r') == "X-VidGrab-Autostart=1")
    })
}

#[cfg(target_os = "linux")]
pub fn state(app: &Path, dirs: &UserDirs) -> io::Result<ManifestState> {
    let _ = app;
    validate_dirs(dirs)?;
    let path = autostart_path(dirs);
    check_destination(&path)?;
    if !path.exists() {
        return Ok(ManifestState::Missing);
    }
    Ok(if is_owned_desktop_entry(&fs::read(&path)?) {
        ManifestState::Registered
    } else {
        ManifestState::Conflict
    })
}

#[cfg(target_os = "linux")]
pub fn enable(app: &Path, dirs: &UserDirs, start_minimized: bool) -> io::Result<()> {
    validate_dirs(dirs)?;
    absolute(app)?;
    executable_file(app)?;
    let path = autostart_path(dirs);
    check_destination(&path)?;
    if path.exists() && !is_owned_desktop_entry(&fs::read(&path)?) {
        return Err(permission_denied(
            "An existing autostart entry belongs to another installation",
        ));
    }
    private_directory(path.parent().unwrap())?;
    atomic_write(&path, &desktop_entry(app, start_minimized)?, 0o600)
}

#[cfg(target_os = "linux")]
pub fn remove_owned(dirs: &UserDirs, _app: Option<&Path>) -> io::Result<bool> {
    validate_dirs(dirs)?;
    let path = autostart_path(dirs);
    check_destination(&path)?;
    if !path.exists() {
        return Ok(false);
    }
    if !is_owned_desktop_entry(&fs::read(&path)?) {
        return Ok(false);
    }
    fs::remove_file(&path)?;
    Ok(true)
}

#[cfg(windows)]
fn command_line(app: &Path, start_minimized: bool) -> io::Result<String> {
    let text = app
        .to_str()
        .ok_or_else(|| invalid_data("Application paths must be valid Unicode"))?;
    if text.chars().any(char::is_control) || text.contains('"') {
        return Err(invalid_data("Invalid application path for Windows startup"));
    }
    Ok(if start_minimized {
        format!("\"{text}\" --start-minimized")
    } else {
        format!("\"{text}\"")
    })
}

#[cfg(windows)]
mod win_run {
    use super::*;
    use crate::windows::{registry, wide};
    use std::ffi::OsStr;
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::System::Registry::*;

    pub(super) struct RunEntry<'a> {
        pub key_path: &'a str,
        pub value_name: &'a str,
    }

    impl<'a> RunEntry<'a> {
        pub(super) fn read_state(&self, app: &Path) -> io::Result<ManifestState> {
            registry::reject_links(self.key_path, 0)?;
            let Some(key) = registry::open(self.key_path, 0, KEY_READ)? else {
                return Ok(ManifestState::Missing);
            };
            let name = wide(OsStr::new(self.value_name))?;
            let mut kind = 0;
            let mut size = 0;
            let code = unsafe {
                RegQueryValueExW(key.0, name.as_ptr(), null(), &mut kind, null_mut(), &mut size)
            };
            if registry::missing(code) {
                return Ok(ManifestState::Missing);
            }
            registry::checked(code)?;
            if kind != REG_SZ || size == 0 || size > 65536 || size % 2 != 0 {
                return Ok(ManifestState::Conflict);
            }
            let mut text = vec![0u16; size as usize / 2];
            registry::checked(unsafe {
                RegQueryValueExW(
                    key.0,
                    name.as_ptr(),
                    null(),
                    &mut kind,
                    text.as_mut_ptr().cast(),
                    &mut size,
                )
            })?;
            if kind != REG_SZ
                || size as usize != text.len() * 2
                || text.last() != Some(&0)
                || text[..text.len() - 1].contains(&0)
            {
                return Ok(ManifestState::Conflict);
            }
            let raw = String::from_utf16(&text[..text.len() - 1]).map_err(io::Error::other)?;
            let plain = command_line(app, false)?;
            let minimized = command_line(app, true)?;
            Ok(if raw == plain || raw == minimized {
                ManifestState::Registered
            } else {
                ManifestState::Conflict
            })
        }

        pub(super) fn write(&self, app: &Path, start_minimized: bool) -> io::Result<()> {
            if self.read_state(app)? == ManifestState::Conflict {
                return Err(permission_denied(
                    "An existing Windows Run entry for VidGrab belongs to another installation",
                ));
            }
            let key_wide = wide(OsStr::new(self.key_path))?;
            let name_wide = wide(OsStr::new(self.value_name))?;
            let value_wide = wide(OsStr::new(&command_line(app, start_minimized)?))?;
            let mut key = null_mut();
            registry::checked(unsafe {
                RegCreateKeyExW(
                    HKEY_CURRENT_USER,
                    key_wide.as_ptr(),
                    0,
                    null(),
                    REG_OPTION_NON_VOLATILE,
                    KEY_READ | KEY_WRITE,
                    null(),
                    &mut key,
                    null_mut(),
                )
            })?;
            let key = registry::Key(key);
            registry::checked(unsafe {
                RegSetValueExW(
                    key.0,
                    name_wide.as_ptr(),
                    0,
                    REG_SZ,
                    value_wide.as_ptr().cast(),
                    (value_wide.len() * 2) as u32,
                )
            })?;
            registry::checked(unsafe { RegFlushKey(key.0) })
        }

        pub(super) fn remove_owned(&self, app: &Path) -> io::Result<bool> {
            if self.read_state(app)? != ManifestState::Registered {
                return Ok(false);
            }
            let Some(key) = registry::open(self.key_path, 0, KEY_READ | KEY_WRITE)? else {
                return Ok(false);
            };
            let name_wide = wide(OsStr::new(self.value_name))?;
            registry::checked(unsafe { RegDeleteValueW(key.0, name_wide.as_ptr()) })?;
            registry::checked(unsafe { RegFlushKey(key.0) })?;
            Ok(true)
        }
    }
}

#[cfg(windows)]
pub fn state(app: &Path, dirs: &UserDirs) -> io::Result<ManifestState> {
    validate_dirs(dirs)?;
    win_run::RunEntry {
        key_path: RUN_KEY,
        value_name: RUN_VALUE,
    }
    .read_state(app)
}

#[cfg(windows)]
pub fn enable(app: &Path, dirs: &UserDirs, start_minimized: bool) -> io::Result<()> {
    validate_dirs(dirs)?;
    absolute(app)?;
    executable_file(app)?;
    win_run::RunEntry {
        key_path: RUN_KEY,
        value_name: RUN_VALUE,
    }
    .write(app, start_minimized)
}

#[cfg(windows)]
pub fn remove_owned(dirs: &UserDirs, app: Option<&Path>) -> io::Result<bool> {
    validate_dirs(dirs)?;
    let Some(app) = app else {
        return Ok(false);
    };
    win_run::RunEntry {
        key_path: RUN_KEY,
        value_name: RUN_VALUE,
    }
    .remove_owned(app)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Fixture {
        root: PathBuf,
        dirs: UserDirs,
        app: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "vidgrab-autostart-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            #[cfg(windows)]
            let home = root.join("home বাংলা % $ ` single'");
            #[cfg(not(windows))]
            let home = root.join("home বাংলা % $ ` quoted\"");
            fs::create_dir(&home).unwrap();
            let dirs = UserDirs {
                data_home: home.join("data"),
                config_home: home.join("config"),
                home,
            };
            let app = root.join("bin/vidgrab-desktop");
            fs::create_dir_all(app.parent().unwrap()).unwrap();
            fs::write(&app, b"owned desktop binary").unwrap();
            #[cfg(target_os = "linux")]
            fs::set_permissions(&app, fs::Permissions::from_mode(0o755)).unwrap();
            Self { root, dirs, app }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn preferences_round_trip_and_read_only_defaults() {
        let fixture = Fixture::new();
        let prefs_path = fixture.dirs.preferences_path();
        assert!(!prefs_path.exists());
        let defaults = load_preferences(&prefs_path).unwrap();
        assert_eq!(defaults, DesktopPreferences::default());
        assert!(!fixture.dirs.data_home.exists());

        let updated = DesktopPreferences {
            preferences_version: 1,
            start_at_login: true,
            close_to_tray: false,
            start_minimized: true,
            onboarding_completed: true,
        };
        save_preferences(&prefs_path, &updated).unwrap();
        assert_eq!(load_preferences(&prefs_path).unwrap(), updated);

        let mut bad = updated.clone();
        bad.preferences_version = 2;
        assert!(save_preferences(&prefs_path, &bad).is_err());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_autostart_writes_escaped_entry_and_toggles_minimized() {
        let fixture = Fixture::new();
        assert_eq!(state(&fixture.app, &fixture.dirs).unwrap(), ManifestState::Missing);
        enable(&fixture.app, &fixture.dirs, false).unwrap();
        assert_eq!(state(&fixture.app, &fixture.dirs).unwrap(), ManifestState::Registered);
        let content = fs::read_to_string(autostart_path(&fixture.dirs)).unwrap();
        assert!(content.contains("X-VidGrab-Autostart=1"));
        assert!(!content.contains("--start-minimized"));

        enable(&fixture.app, &fixture.dirs, true).unwrap();
        let minimized = fs::read_to_string(autostart_path(&fixture.dirs)).unwrap();
        assert!(minimized.contains("--start-minimized"));

        assert!(remove_owned(&fixture.dirs, Some(&fixture.app)).unwrap());
        assert_eq!(state(&fixture.app, &fixture.dirs).unwrap(), ManifestState::Missing);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_autostart_refuses_symlinks_and_preserves_foreign_entries() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        let entry = autostart_path(&fixture.dirs);
        fs::create_dir_all(entry.parent().unwrap()).unwrap();

        let target = fixture.root.join("foreign-target");
        fs::write(&target, b"foreign").unwrap();
        symlink(&target, &entry).unwrap();
        assert_eq!(enable(&fixture.app, &fixture.dirs, false).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        fs::remove_file(&entry).unwrap();

        fs::write(&entry, b"[Desktop Entry]\nName=Foreign\nExec=/usr/bin/other\n").unwrap();
        assert_eq!(state(&fixture.app, &fixture.dirs).unwrap(), ManifestState::Conflict);
        assert_eq!(enable(&fixture.app, &fixture.dirs, false).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert!(!remove_owned(&fixture.dirs, Some(&fixture.app)).unwrap());
        assert!(entry.exists());
    }

    #[cfg(windows)]
    #[test]
    fn windows_run_entry_round_trips_and_preserves_foreign_values() {
        use crate::windows::wide;
        use std::ffi::OsStr;
        use windows_sys::Win32::System::Registry::{RegDeleteTreeW, HKEY_CURRENT_USER};

        let fixture = Fixture::new();
        let mut nonce = [0u8; 8];
        getrandom::fill(&mut nonce).unwrap();
        let test_key = format!(r"Software\VidGrabP06AutostartTests\{:x}", u64::from_ne_bytes(nonce));
        let entry = win_run::RunEntry {
            key_path: &test_key,
            value_name: "VidGrab",
        };
        assert_eq!(entry.read_state(&fixture.app).unwrap(), ManifestState::Missing);
        entry.write(&fixture.app, true).unwrap();
        assert_eq!(entry.read_state(&fixture.app).unwrap(), ManifestState::Registered);
        assert!(entry.remove_owned(&fixture.app).unwrap());
        assert_eq!(entry.read_state(&fixture.app).unwrap(), ManifestState::Missing);

        let foreign = fixture.root.join("foreign.exe");
        win_run::RunEntry {
            key_path: &test_key,
            value_name: "VidGrab",
        }
        .write(&foreign, false)
        .unwrap();
        assert_eq!(entry.read_state(&fixture.app).unwrap(), ManifestState::Conflict);
        assert_eq!(entry.write(&fixture.app, false).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert!(!entry.remove_owned(&fixture.app).unwrap());

        let key_w = wide(OsStr::new(&test_key)).unwrap();
        unsafe {
            RegDeleteTreeW(HKEY_CURRENT_USER, key_w.as_ptr());
        }
    }
}
