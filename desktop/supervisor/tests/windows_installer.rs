#![cfg(windows)]
use std::path::PathBuf;
use std::io::Write;
use sha2::{Digest, Sha256};
use vidgrab_supervisor::installation::{application, Layout, UserDirs};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut nonce = [0; 8];
        getrandom::fill(&mut nonce).unwrap();
        let root = std::env::temp_dir().join(format!(
            "vidgrab installer বাংলা {:x}", u64::from_ne_bytes(nonce)
        ));
        vidgrab_supervisor::windows::private_directory(&root).unwrap();
        Self(root)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
}

fn package(root: &Fixture) -> (Layout, UserDirs) {
    let package = root.0.join("owned package");
    for relative in ["", "engine", "engine/_internal", "engine/_internal/tools"] {
        vidgrab_supervisor::windows::private_directory(&package.join(relative)).unwrap();
    }
    for name in ["vidgrab-desktop.exe", "vidgrab-native-host.exe", "engine/vidgrab-engine.exe", "uninstall.exe", "engine/_internal/tools/manifest.json"] {
        vidgrab_supervisor::windows::create_private_file(&package.join(name)).unwrap().write_all(name.as_bytes()).unwrap();
    }
    let layout = Layout { app: package.join("vidgrab-desktop.exe"), native: package.join("vidgrab-native-host.exe"), engine: package.join("engine/vidgrab-engine.exe") };
    let dirs = UserDirs { home: root.0.join("home"), data_home: root.0.join("local"), config_home: root.0.join("local") };
    let receipt_dir = dirs.data_home.join("vidgrab-p02/application");
    vidgrab_supervisor::windows::private_directory(&receipt_dir).unwrap();
    let files = ["vidgrab-desktop.exe", "vidgrab-native-host.exe", "engine/vidgrab-engine.exe", "uninstall.exe", "engine/_internal/tools/manifest.json"].into_iter().map(|name| (name, format!("{:x}", Sha256::digest(name.as_bytes())))).collect::<std::collections::BTreeMap<_, _>>();
    vidgrab_supervisor::windows::create_private_file(&receipt_dir.join("windows-installation.json")).unwrap().write_all(&serde_json::to_vec(&serde_json::json!({"installation_version":1,"root":package,"files":files,"directories":["engine","engine/_internal","engine/_internal/tools"]})).unwrap()).unwrap();
    (layout, dirs)
}

#[test]
fn uninstall_refuses_changed_missing_or_additional_files_and_preserves_connection() {
    for change in ["changed", "missing", "additional", "receipt"] {
        let root = Fixture::new();
        let (layout, dirs) = package(&root);
        match change {
            "changed" => std::fs::write(&layout.native, b"changed native host").unwrap(),
            "missing" => std::fs::remove_file(&layout.engine).unwrap(),
            "additional" => std::fs::write(layout.app.parent().unwrap().join("personal.txt"), b"preserve me").unwrap(),
            _ => std::fs::write(dirs.data_home.join("vidgrab-p02/application/windows-installation.json"), b"{}").unwrap(),
        }
        assert!(application::prepare_uninstall(&layout, &dirs).is_err(), "Accepted {change} package");
        assert!(!dirs.bridge_dir().exists(), "Changed registration for {change}");
        assert!(layout.app.exists());
    }
}

#[test]
fn uninstall_refuses_a_nested_junction_before_changing_connection() {
    let root = Fixture::new();
    let (layout, dirs) = package(&root);
    let target = root.0.join("unrelated files");
    std::fs::create_dir(&target).unwrap();
    std::fs::write(target.join("personal.txt"), b"preserve me").unwrap();
    let link = layout.app.parent().unwrap().join("foreign directory");
    assert!(std::process::Command::new("cmd.exe").args(["/d", "/c", "mklink", "/J"]).arg(&link).arg(&target).output().unwrap().status.success());
    let refused = application::prepare_uninstall(&layout, &dirs).is_err();
    std::fs::remove_dir(&link).unwrap();
    assert!(refused, "Accepted an installed junction");
    assert!(!dirs.bridge_dir().exists());
    assert_eq!(std::fs::read(target.join("personal.txt")).unwrap(), b"preserve me");
}

#[test]
fn trusted_checker_records_exact_bytes_and_accepts_an_empty_unicode_destination() {
    let root = Fixture::new();
    let (layout, dirs) = package(&root);
    let package = layout.app.parent().unwrap();
    let before = std::fs::read(&layout.app).unwrap();
    application::verify_installation(package, &dirs).unwrap();
    application::record_installation(package, &dirs).unwrap();
    application::verify_installation(package, &dirs).unwrap();
    assert_eq!(std::fs::read(&layout.app).unwrap(), before);
    let destination = root.0.join("new installation বাংলা");
    application::prepare_install(&destination, &dirs).unwrap();
    assert!(!destination.exists());
    assert!(!dirs.bridge_dir().exists());
    std::fs::write(layout.app.parent().unwrap().join("personal.txt"), b"preserve me").unwrap();
    assert!(application::prepare_install(package, &dirs).is_err());
    assert!(!dirs.bridge_dir().exists());
}

#[test]
fn uninstall_refuses_an_unowned_package_before_changing_browser_connection() {
    let root = Fixture::new();
    let package = root.0.join("foreign package");
    vidgrab_supervisor::windows::private_directory(&package).unwrap();
    vidgrab_supervisor::windows::private_directory(&package.join("engine")).unwrap();
    for name in ["vidgrab-desktop.exe", "vidgrab-native-host.exe", "engine/vidgrab-engine.exe"] {
        vidgrab_supervisor::windows::create_private_file(&package.join(name)).unwrap().write_all(b"foreign bytes").unwrap();
    }
    let layout = Layout {
        app: package.join("vidgrab-desktop.exe"),
        native: package.join("vidgrab-native-host.exe"),
        engine: package.join("engine/vidgrab-engine.exe"),
    };
    let dirs = UserDirs {
        home: root.0.join("home"), data_home: root.0.join("local"), config_home: root.0.join("local")
    };
    assert!(application::prepare_uninstall(&layout, &dirs).is_err(),
        "An unowned destination must be refused before touching registration");
    assert!(!dirs.bridge_dir().exists());
    assert_eq!(std::fs::read(&layout.app).unwrap(), b"foreign bytes");
}
