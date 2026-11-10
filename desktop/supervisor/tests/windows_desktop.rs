#![cfg(windows)]
use std::{env, fs, path::PathBuf};
use vidgrab_supervisor::installation::{self, application, Layout, UserDirs};

#[test]
fn windows_user_paths_ignore_home_and_xdg_and_require_absolute_windows_bases() {
    let saved: Vec<_> = ["USERPROFILE", "LOCALAPPDATA", "HOME", "XDG_DATA_HOME"].into_iter()
        .map(|name| (name, env::var_os(name))).collect();
    env::set_var("USERPROFILE", r"C:\Users\Preview বাংলা");
    env::set_var("LOCALAPPDATA", r"C:\Users\Preview বাংলা\AppData\Local");
    env::set_var("HOME", r"C:\wrong");
    env::set_var("XDG_DATA_HOME", r"C:\wrong");
    let dirs = UserDirs::from_env().unwrap();
    assert_eq!(dirs.home, PathBuf::from(r"C:\Users\Preview বাংলা"));
    assert_eq!(dirs.data_home, PathBuf::from(r"C:\Users\Preview বাংলা\AppData\Local"));
    assert!(dirs.bridge_dir().starts_with(&dirs.data_home));
    env::set_var("LOCALAPPDATA", "relative");
    assert!(UserDirs::from_env().is_err());
    for (name, value) in saved { match value { Some(value) => env::set_var(name, value), None => env::remove_var(name) } }
}

#[test]
fn windows_installation_inspection_does_not_create_storage_and_refuses_an_active_owner() {
    let root = env::temp_dir().join(format!("vidgrab desktop বাংলা {}", std::process::id()));
    assert!(!root.exists());
    fs::create_dir(&root).unwrap();
    let dirs = UserDirs { home: root.join("home"), data_home: root.join("local"), config_home: root.join("local") };
    let layout = Layout { app: root.join("package/VidGrab.exe"), engine: root.join("package/engine/vidgrab-engine.exe"), native: root.join("package/vidgrab-native-host.exe") };
    let status = application::status(&layout, &dirs, false).unwrap();
    assert_eq!(status.kind, "windows");
    assert_eq!(status.app, layout.app);
    assert!(!dirs.data_home.exists());
    application::check_removal(&dirs, false).unwrap();
    assert!(!dirs.data_home.exists());
    for p in [&layout.app, &layout.engine, &layout.native] { fs::create_dir_all(p.parent().unwrap()).unwrap(); fs::write(p, b"fixture").unwrap(); }
    let config = installation::prepare(&layout, &dirs).unwrap();
    let ipc = config.data_dir.join("ipc");
    vidgrab_supervisor::windows::private_directory(&ipc).unwrap();
    let file = vidgrab_supervisor::windows::create_private_file(&ipc.join("owner.lock")).unwrap();
    file.try_lock().unwrap();
    assert!(application::check_idle(&dirs).is_err());
    drop(file);
    application::check_idle(&dirs).unwrap();
    installation::disconnect(&dirs).unwrap();
    fs::remove_dir_all(root).unwrap();
}
