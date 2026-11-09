use gio::glib::variant::ToVariant;
use gio::prelude::{AppInfoExt, DBusProxyExt};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, atomic::{AtomicBool, Ordering}};
use std::time::Duration;
use tauri::{Manager, State, menu::{Menu, MenuItem}, tray::TrayIconBuilder};
use vidgrab_supervisor::{Config, Owner, UiEvent, protocol, transport};
use vidgrab_supervisor::installation::{self, Layout, UserDirs};
use vidgrab_supervisor::installation::application;

struct Shutdown { stopped: Mutex<bool>, ready: Condvar }

struct Maintenance(Arc<AtomicBool>);
impl Drop for Maintenance {
    fn drop(&mut self) { self.0.store(false, Ordering::Release); }
}

struct DesktopState {
    config: Config,
    stop: Arc<AtomicBool>,
    tray_available: bool,
    registration: Option<Arc<Mutex<(Layout, UserDirs, bool)>>>,
    shutdown: Arc<Shutdown>,
    maintenance: Arc<AtomicBool>,
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
    if let Some(registration) = state.registration.clone() {
        let information = tauri::async_runtime::spawn_blocking(move || -> Result<Value, String> {
            let selected = registration.lock().map_err(|_| "Browser connection settings are unavailable".to_owned())?;
            let mut information = json!({});
            match installation::connection_status(&selected.0, &selected.1) {
                Ok(connection) => information["browser_connection"] = json!(connection),
                Err(error) => information["browser_connection_error"] = json!(error.to_string()),
            }
            match application::status(&selected.0, &selected.1, selected.2) {
                Ok(status) => information["application"] = json!(status),
                Err(error) => information["application_error"] = json!(error.to_string()),
            }
            Ok(information)
        }).await.map_err(|_| "The browser connection check failed".to_owned())?;
        match information {
            Ok(information) => for (key, item) in information.as_object().unwrap() { value[key] = item.clone(); },
            Err(error) => value["browser_connection_error"] = json!(error),
        }
    }
    Ok(value)
}

#[tauri::command]
async fn install_appimage(state: State<'_, DesktopState>) -> Result<Value, String> {
    let registration = state.registration.clone().ok_or("Installation controls require a packaged launch")?;
    tauri::async_runtime::spawn_blocking(move || {
        let mut selected = registration.lock().map_err(|_| "Installation settings are unavailable".to_owned())?;
        selected.0 = application::install(&selected.0, &selected.1, selected.2, include_bytes!("../icons/icon.png"))
            .map_err(|error| error.to_string())?;
        application::status(&selected.0, &selected.1, selected.2).map(|status| json!(status)).map_err(|error| error.to_string())
    }).await.map_err(|_| "The AppImage installation failed".to_owned())?
}

fn software_manager() -> Result<gio::DesktopAppInfo, String> {
    ["/var/lib/snapd/desktop/applications/snap-store_snap-store.desktop", "/usr/share/applications/org.gnome.Software.desktop"]
        .iter().find_map(gio::DesktopAppInfo::from_filename)
        .ok_or_else(|| "Open your desktop's software manager to remove the VidGrab package".to_owned())
}

#[tauri::command]
fn open_software_manager() -> Result<(), String> {
    software_manager()?.launch(&[], None::<&gio::AppLaunchContext>).map_err(|error| error.to_string())
}

#[tauri::command]
async fn remove_application(app: tauri::AppHandle, state: State<'_, DesktopState>) -> Result<Value, String> {
    let registration = state.registration.clone().ok_or("Installation controls require a packaged launch")?;
    state.maintenance.compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|_| "An installation action is already running".to_owned())?;
    let _maintenance = Maintenance(state.maintenance.clone());
    let config = state.config.clone();
    let shutdown = state.shutdown.clone();
    let (result, is_image) = tauri::async_runtime::spawn_blocking(move || {
        let selected = registration.lock().map_err(|_| "Installation settings are unavailable".to_owned())?;
        application::check_removal(&selected.1, selected.2).map_err(|error| error.to_string())?;
        if !selected.2 { software_manager()?; }
        command(&config, "quit_if_idle", Default::default())?;
        let stopped = shutdown.stopped.lock().map_err(|_| "The app shutdown state is unavailable".to_owned())?;
        let (stopped, _) = shutdown.ready.wait_timeout_while(stopped, Duration::from_secs(10), |stopped| !*stopped)
            .map_err(|_| "The app shutdown state is unavailable".to_owned())?;
        if !*stopped { return Err("VidGrab has not finished shutting down; keep the installed files and try again".to_owned()); }
        if selected.2 { application::remove(&selected.1).map_err(|error| error.to_string())?; }
        else { installation::disconnect(&selected.1).map_err(|error| error.to_string())?; }
        application::status(&selected.0, &selected.1, selected.2)
            .map(|status| (json!(status), selected.2)).map_err(|error| error.to_string())
    }).await.map_err(|_| "The removal action failed; reopen VidGrab to try again".to_owned())??;
    if !is_image { open_software_manager()?; }
    app.exit(0);
    Ok(result)
}

#[tauri::command]
async fn set_browser_connection(enabled: bool, state: State<'_, DesktopState>) -> Result<Value, String> {
    let registration = state.registration.clone().ok_or("Browser connection controls require a packaged launch")?;
    tauri::async_runtime::spawn_blocking(move || {
        let selected = registration.lock().map_err(|_| "Browser connection settings are unavailable".to_owned())?;
        if enabled { installation::connect(&selected.0, &selected.1).map_err(|error| error.to_string())?; }
        else { installation::disconnect(&selected.1).map_err(|error| error.to_string())?; }
        installation::connection_status(&selected.0, &selected.1).map(|status| json!(status)).map_err(|error| error.to_string())
    }).await.map_err(|_| "The browser connection change failed".to_owned())?
}

#[tauri::command]
async fn download_fixture(url: String, state: State<'_, DesktopState>) -> Result<Value, String> {
    let config = state.config.clone();
    let params = json!({"url": url}).as_object().unwrap().clone();
    tauri::async_runtime::spawn_blocking(move || command(&config, "fixture.download", params))
        .await.map_err(|_| "The download request failed".to_owned())?
}

#[tauri::command]
fn quit_companion(app: tauri::AppHandle, state: State<'_, DesktopState>) {
    if state.shutdown.stopped.lock().is_ok_and(|stopped| *stopped) { app.exit(0); }
    else { state.stop.store(true, Ordering::Release); }
}

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
    let mut registration = None;
    let config = if arguments.len() == 2 && arguments[0] == "--config" {
        Config::load(&PathBuf::from(&arguments[1]))?
    } else {
        let packaged = arguments.len() == 2 && arguments[0] == "--package-config";
        let diagnostic = arguments.len() == 1 && ["--check-package", "--browser-connection-status", "--register-browser-host", "--unregister-browser-host", "--installation-status", "--install-appimage", "--remove-appimage"].iter().any(|flag| arguments[0] == *flag);
        if !arguments.is_empty() && !packaged && !diagnostic {
            return Err("Use a packaged launch, --config FILE, --check-package, --installation-status, --install-appimage, --remove-appimage, --register-browser-host or --unregister-browser-host".into());
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
            println!("{}", json!({"removed": installation::disconnect(&dirs)?}));
            return Ok(());
        }
        if packaged && PathBuf::from(&arguments[1]) != dirs.config_path() {
            return Err("The package activation configuration must match this user's registered bridge".into());
        }
        let environment = tauri::Env::default();
        let resources = tauri::utils::platform::resource_dir(context.package_info(), &environment)?;
        let image = std::env::var_os("APPIMAGE");
        let layout = Layout::discover(&std::env::current_exe()?, &resources, image.as_deref().map(Path::new))?;
        let is_image = image.is_some();
        if arguments.first().is_some_and(|arg| arg == "--installation-status") {
            println!("{}", json!(application::status(&layout, &dirs, is_image)?));
            return Ok(());
        }
        if arguments.first().is_some_and(|arg| arg == "--install-appimage") {
            let installed = application::install(&layout, &dirs, is_image, include_bytes!("../icons/icon.png"))?;
            println!("{}", json!(application::status(&installed, &dirs, is_image)?));
            return Ok(());
        }
        if arguments.first().is_some_and(|arg| arg == "--remove-appimage") {
            if !is_image { return Err("Only an AppImage has a managed copy to remove".into()); }
            application::remove(&dirs)?;
            println!("{}", json!(application::status(&layout, &dirs, is_image)?));
            return Ok(());
        }
        if arguments.first().is_some_and(|arg| arg == "--browser-connection-status") {
            println!("{}", serde_json::to_string(&installation::connection_status(&layout, &dirs)?)?);
            return Ok(());
        }
        if arguments.first().is_some_and(|arg| arg == "--register-browser-host") {
            let saved = installation::connect(&layout, &dirs)?;
            println!("{}", serde_json::to_string(&saved)?);
            return Ok(());
        }
        let layout = application::preferred_layout(&layout, &dirs, is_image);
        let saved = installation::prepare(&layout, &dirs)?;
        registration = Some(Arc::new(Mutex::new((layout.clone(), dirs, is_image))));
        saved.runtime(&layout)
    };
    let Some(owner) = Owner::start(config.clone())? else {
        let _ = command(&config, "activate", Default::default());
        return Ok(());
    };
    let stop = owner.stop_handle();
    let exit_stop = stop.clone();
    let shutdown = Arc::new(Shutdown { stopped: Mutex::new(false), ready: Condvar::new() });
    let owner_shutdown = shutdown.clone();
    let maintenance = Arc::new(AtomicBool::new(false));
    let owner_maintenance = maintenance.clone();
    let application = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![companion_status, set_browser_connection, install_appimage, remove_application, open_software_manager, download_fixture, quit_companion])
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
            app.manage(DesktopState { config, stop, tray_available, registration, shutdown, maintenance });
            let handle = app.handle().clone();
            std::thread::spawn(move || owner.run(move |event| match event {
                UiEvent::Activate => {
                    let activate = handle.clone();
                    let _ = handle.run_on_main_thread(move || {
                        if let Some(window) = activate.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); }
                    });
                }
                UiEvent::Stopped => {
                    if let Ok(mut stopped) = owner_shutdown.stopped.lock() {
                        *stopped = true;
                        owner_shutdown.ready.notify_all();
                    }
                    if !owner_maintenance.load(Ordering::Acquire) { handle.exit(0); }
                }
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
