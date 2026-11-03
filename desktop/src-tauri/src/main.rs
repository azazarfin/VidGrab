use gio::glib::variant::ToVariant;
use gio::prelude::DBusProxyExt;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};
use tauri::{Manager, State, menu::{Menu, MenuItem}, tray::TrayIconBuilder};
use vidgrab_supervisor::{Config, Owner, UiEvent, protocol, transport};

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
    if arguments.len() != 2 || arguments[0] != "--config" {
        return Err("P02 spike: launch with --config and an explicit staged runtime configuration".into());
    }
    let config = Config::load(&PathBuf::from(&arguments[1]))?;
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
        .build(tauri::generate_context!())?;
    application.run(move |_handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !exit_stop.load(Ordering::Acquire) { api.prevent_exit(); exit_stop.store(true, Ordering::Release); }
        }
    });
    Ok(())
}
