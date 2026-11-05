use gio::glib::variant::ToVariant;
use gio::prelude::DBusProxyExt;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};
use tauri::{Manager, State, menu::{Menu, MenuItem}, tray::TrayIconBuilder};
use vidgrab_supervisor::{Config, Owner, UiEvent, protocol, transport};
use vidgrab_supervisor::installation::{self, Layout, UserDirs};

struct DesktopState {
    config: Config,
    stop: Arc<AtomicBool>,
    tray_available: bool,
}

fn command(config: &Config, operation: &str, params: serde_json::Map<String, Value>) -> Result<Value, String> {
    let request = protocol::Request { spike_version: 1, id: "desktop".into(), op: operation.into(), params };
    request.validate(false).map_err(|_| "Use a numeric loopback fixture.wav URL".to_owned())?;
    let response = transport::call(config, &request).map_err(|_| "The companion is unavailable".to_owned())?;
    if response["ok"] == true { Ok(response["data"].clone()) }
    else { Err(response["error"]["message"].as_str().unwrap_or("The companion request failed").to_owned()) }
}

#[tauri::command]
async fn companion_status(state: State<'_, DesktopState>) -> Result<Value, String> {
    let config = state.config.clone();
    let tray = state.tray_available;
    let mut value = tauri::async_runtime::spawn_blocking(move || command(&config, "status", Default::default()))
        .await.map_err(|_| "The status request failed".to_owned())??;
    value["tray_available"] = json!(tray);
    Ok(value)
}

#[tauri::command]
async fn download_fixture(url: String, state: State<'_, DesktopState>) -> Result<Value, String> {
    let config = state.config.clone();
    let params = json!({"url": url}).as_object().unwrap().clone();
    tauri::async_runtime::spawn_blocking(move || command(&config, "fixture.download", params))
        .await.map_err(|_| "The download request failed".to_owned())?
}

#[tauri::command]
fn quit_companion(state: State<'_, DesktopState>) { state.stop.store(true, Ordering::Release); }

fn tray_host_available() -> bool {
    if std::env::var_os("VIDGRAB_DISABLE_TRAY").is_some() { return false; }
    let proxy = gio::DBusProxy::for_bus_sync(
        gio::BusType::Session, gio::DBusProxyFlags::DO_NOT_LOAD_PROPERTIES,
        None::<&gio::DBusInterfaceInfo>, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        None::<&gio::Cancellable>,
    );
    proxy.ok().and_then(|proxy| proxy.call_sync(
        "NameHasOwner", Some(&("org.kde.StatusNotifierWatcher",).to_variant()),
        gio::DBusCallFlags::NONE, 1000, None::<&gio::Cancellable>,
    ).ok()).and_then(|value| value.get::<(bool,)>()).is_some_and(|value| value.0)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    let context = tauri::generate_context!();
    let config = if arguments.len() == 2 && arguments[0] == "--config" {
        Config::load(&PathBuf::from(&arguments[1]))?
    } else {
        let packaged = arguments.len() == 2 && arguments[0] == "--package-config";
        let diagnostic = arguments.len() == 1 && ["--check-package", "--register-browser-host", "--unregister-browser-host"].iter().any(|flag| arguments[0] == *flag);
        if !arguments.is_empty() && !packaged && !diagnostic {
            return Err("Use a packaged launch, --config FILE, --check-package, --register-browser-host or --unregister-browser-host".into());
        }
        if arguments.first().is_some_and(|arg| arg == "--check-package") {
            let environment = tauri::Env::default();
            let resources = tauri::utils::platform::resource_dir(context.package_info(), &environment)?;
            let image = std::env::var_os("APPIMAGE");
            let layout = Layout::discover(&std::env::current_exe()?, &resources, image.as_deref().map(Path::new))?;
            println!("{}", json!({"app":layout.app, "engine":layout.engine, "native":layout.native}));
            return Ok(());
        }
        let home = PathBuf::from(std::env::var_os("HOME").ok_or("HOME must select an explicit user directory")?);
        let dirs = UserDirs { data_home: std::env::var_os("XDG_DATA_HOME").filter(|value| !value.is_empty()).map(PathBuf::from).unwrap_or_else(|| home.join(".local/share")),
            config_home: std::env::var_os("XDG_CONFIG_HOME").filter(|value| !value.is_empty()).map(PathBuf::from).unwrap_or_else(|| home.join(".config")), home };
        if arguments.first().is_some_and(|arg| arg == "--unregister-browser-host") {
            println!("{}", json!({"removed": installation::unregister(&dirs)?}));
            return Ok(());
        }
        if packaged && PathBuf::from(&arguments[1]) != dirs.config_path() {
            return Err("The package activation configuration must match this user's registered bridge".into());
        }
        let environment = tauri::Env::default();
        let resources = tauri::utils::platform::resource_dir(context.package_info(), &environment)?;
        let image = std::env::var_os("APPIMAGE");
        let layout = Layout::discover(&std::env::current_exe()?, &resources, image.as_deref().map(Path::new))?;
        let saved = installation::register(&layout, &dirs)?;
        if arguments.first().is_some_and(|arg| arg == "--register-browser-host") {
            println!("{}", serde_json::to_string(&saved)?);
            return Ok(());
        }
        saved.runtime(&layout)
    };
    let Some(owner) = Owner::start(config.clone())? else {
        let _ = command(&config, "activate", Default::default());
        return Ok(());
    };
    let stop = owner.stop_handle();
    let exit_stop = stop.clone();
    let application = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![companion_status, download_fixture, quit_companion])
        .setup(move |app| {
            let mut tray_available = false;
            if tray_host_available() {
                if let Some(icon) = app.default_window_icon().cloned() {
                    let open = MenuItem::with_id(app, "open", "Open VidGrab", true, None::<&str>)?;
                    let quit = MenuItem::with_id(app, "quit", "Quit VidGrab", true, None::<&str>)?;
                    let menu = Menu::with_items(app, &[&open, &quit])?;
                    tray_available = TrayIconBuilder::new().icon(icon).menu(&menu).tooltip("VidGrab")
                        .on_menu_event(|handle, event| {
                            if event.id.as_ref() == "open" {
                                if let Some(window) = handle.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); }
                            } else if event.id.as_ref() == "quit" {
                                handle.state::<DesktopState>().stop.store(true, Ordering::Release);
                            }
                        }).build(app).is_ok();
                }
            }
            app.manage(DesktopState { config, stop, tray_available });
            let handle = app.handle().clone();
            std::thread::spawn(move || owner.run(move |event| match event {
                UiEvent::Activate => {
                    let activate = handle.clone();
                    let _ = handle.run_on_main_thread(move || {
                        if let Some(window) = activate.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); }
                    });
                }
                UiEvent::Stopped => handle.exit(0),
            }));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                if window.state::<DesktopState>().tray_available { let _ = window.hide(); }
            }
        })
        .build(context)?;
    application.run(move |_handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !exit_stop.load(Ordering::Acquire) { api.prevent_exit(); exit_stop.store(true, Ordering::Release); }
        }
    });
    Ok(())
}
