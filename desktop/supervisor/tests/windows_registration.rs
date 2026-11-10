#![cfg(windows)]
use std::io;
use std::path::PathBuf;
use std::ptr::{null, null_mut};
use vidgrab_supervisor::{
    installation::{self, Layout, ManifestState, UserDirs},
    windows,
};
use windows_sys::Win32::{Foundation::ERROR_FILE_NOT_FOUND, System::Registry::*};
fn key(browser: &str) -> String {
    format!(r"Software\{browser}\NativeMessagingHosts\com.vidgrab.companion")
}
fn value(browser: &str, view: u32) -> io::Result<Option<PathBuf>> {
    let key: Vec<u16> = key(browser).encode_utf16().chain([0]).collect();
    let mut handle = null_mut();
    let code = unsafe {
        RegOpenKeyExW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            0,
            KEY_READ | view,
            &mut handle,
        )
    };
    if code == ERROR_FILE_NOT_FOUND {
        return Ok(None);
    }
    if code != 0 {
        return Err(io::Error::from_raw_os_error(code as i32));
    }
    let mut kind = 0;
    let mut size = 0;
    let code =
        unsafe { RegQueryValueExW(handle, null(), null(), &mut kind, null_mut(), &mut size) };
    if code == ERROR_FILE_NOT_FOUND {
        unsafe {
            RegCloseKey(handle);
        }
        return Ok(None);
    }
    if code != 0 {
        unsafe {
            RegCloseKey(handle);
        }
        return Err(io::Error::from_raw_os_error(code as i32));
    }
    let mut bytes = vec![0u16; size as usize / 2];
    let code = unsafe {
        RegQueryValueExW(
            handle,
            null(),
            null(),
            &mut kind,
            bytes.as_mut_ptr().cast(),
            &mut size,
        )
    };
    unsafe {
        RegCloseKey(handle);
    }
    assert_eq!(code, 0);
    assert_eq!(kind, REG_SZ);
    assert_eq!(bytes.pop(), Some(0));
    Ok(Some(PathBuf::from(String::from_utf16(&bytes).unwrap())))
}
struct Fixture {
    root: PathBuf,
    dirs: UserDirs,
    layout: Layout,
}
impl Fixture {
    fn new() -> Self {
        // These exact production keys must be empty before this VM-only test.
        for browser in [r"Google\Chrome", "Mozilla"] {
            for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY] {
                assert!(
                    value(browser, view).unwrap().is_none(),
                    "A browser registration already exists; preserve it"
                );
            }
        }
        let mut nonce = [0u8; 8];
        getrandom::fill(&mut nonce).unwrap();
        let root = std::env::temp_dir().join(format!(
            "vidgrab registration বাংলা {:x}",
            u64::from_ne_bytes(nonce)
        ));
        windows::private_directory(&root).unwrap();
        let package = root.join("package");
        std::fs::create_dir_all(package.join("engine/_internal/tools")).unwrap();
        for name in [
            "app.exe",
            "vidgrab-native-host.exe",
            "engine/vidgrab-engine.exe",
            "engine/_internal/tools/manifest.json",
        ] {
            std::fs::write(package.join(name), b"fixture").unwrap();
        }
        let layout = Layout {
            app: package.join("app.exe"),
            native: package.join("vidgrab-native-host.exe"),
            engine: package.join("engine/vidgrab-engine.exe"),
        };
        let dirs = UserDirs {
            home: root.join("home"),
            data_home: root.join("local"),
            config_home: root.join("config"),
        };
        Self { root, dirs, layout }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = installation::disconnect(&self.dirs);
        let _ = std::fs::remove_dir_all(&self.root);
    }
}
#[test]
fn packaged_connection_registers_both_browsers_and_persists_revocation() {
    let fixture = Fixture::new();
    installation::connect(&fixture.layout, &fixture.dirs).unwrap();
    for (browser, file) in [
        (r"Google\Chrome", "chrome.json"),
        ("Mozilla", "firefox.json"),
    ] {
        for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY] {
            assert_eq!(
                value(browser, view).unwrap(),
                Some(fixture.dirs.bridge_dir().join(file)),
                "The browser must discover the actual per-user native manifest"
            );
        }
    }
    let connected = installation::connection_status(&fixture.layout, &fixture.dirs).unwrap();
    assert_eq!(connected.chrome, ManifestState::Registered);
    assert_eq!(connected.firefox, ManifestState::Registered);
    let state = fixture.dirs.data_home.join("vidgrab-p02/state");
    std::fs::create_dir_all(&state).unwrap();
    std::fs::write(state.join("preserved.txt"), b"history").unwrap();
    installation::disconnect(&fixture.dirs).unwrap();
    installation::prepare(&fixture.layout, &fixture.dirs).unwrap();
    let disconnected = installation::connection_status(&fixture.layout, &fixture.dirs).unwrap();
    assert!(!disconnected.enabled);
    assert_eq!(
        std::fs::read(state.join("preserved.txt")).unwrap(),
        b"history"
    );
    for browser in [r"Google\Chrome", "Mozilla"] {
        for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY] {
            assert!(value(browser, view).unwrap().is_none());
        }
    }
    assert!(fixture.dirs.config_path().is_file());
    assert!(fixture
        .dirs
        .bridge_dir()
        .join("vidgrab-native-host.exe")
        .is_file());
}

#[test]
fn package_activation_preserves_an_identical_loaded_bridge() {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ;
    let fixture = Fixture::new();
    installation::prepare(&fixture.layout, &fixture.dirs).unwrap();
    let bridge = fixture.dirs.bridge_dir().join("vidgrab-native-host.exe");
    // A running Windows executable allows reads but prevents replacement.
    let _loaded = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(&bridge)
        .unwrap();
    installation::prepare(&fixture.layout, &fixture.dirs).unwrap();
    assert_eq!(std::fs::read(&bridge).unwrap(), b"fixture");
    std::fs::write(&fixture.layout.native, b"new version").unwrap();
    assert!(
        installation::prepare(&fixture.layout, &fixture.dirs).is_err(),
        "Changed bridge bytes must still obey Windows sharing restrictions"
    );
    assert_eq!(std::fs::read(&bridge).unwrap(), b"fixture");
}
