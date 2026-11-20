#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

#[cfg(target_os = "linux")]
use gio::glib::variant::ToVariant;
#[cfg(target_os = "linux")]
use gio::prelude::{AppInfoExt, DBusProxyExt, FileExt};
#[cfg(target_os = "linux")]
use gtk::prelude::{FileChooserExt, NativeDialogExt};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::Read;
#[cfg(target_os = "linux")]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
#[cfg(windows)]
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, atomic::{AtomicBool, Ordering}, mpsc};
use std::time::Duration;
use tauri::{Emitter, Manager, State, menu::{Menu, MenuItem}, tray::TrayIconBuilder};
use vidgrab_supervisor::{Config, Owner, UiEvent, contract, protocol, transport};
use vidgrab_supervisor::installation::{self, Layout, ManifestState, UserDirs};
use vidgrab_supervisor::installation::application;
use vidgrab_supervisor::installation::autostart::{self, DesktopPreferences};

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
    preferences: Arc<Mutex<DesktopPreferences>>,
}

fn command(config: &Config, operation: &str, params: serde_json::Map<String, Value>) -> Result<Value, String> {
    let request = protocol::Request { spike_version: 1, id: "desktop".into(), op: operation.into(), params };
    request.validate(false).map_err(|_| "Invalid local companion request".to_owned())?;
    let response = transport::call(config, &request).map_err(|_| "The companion is unavailable".to_owned())?;
    if response["ok"] == true { Ok(response["data"].clone()) }
    else { Err(response["error"]["message"].as_str().unwrap_or("The companion request failed").to_owned()) }
}

fn v1_command(config: &Config, operation: &str, params: Value) -> Result<Value, String> {
    let request_id = format!("desktop-{operation}");
    let request_obj = json!({
        "protocol_version": 1,
        "request_id": request_id,
        "operation": operation,
        "params": params,
    });
    let outer = protocol::Request {
        spike_version: 1,
        id: request_id.clone(),
        op: "v1.request".into(),
        params: request_obj.as_object().cloned().unwrap_or_default(),
    };
    outer.validate(false).map_err(|_| "Invalid companion v1 request".to_owned())?;
    let response = transport::call(config, &outer).map_err(|_| "The companion engine is unavailable".to_owned())?;
    if response["ok"] != true {
        return Err(response["error"]["message"].as_str().unwrap_or("The companion request failed").to_owned());
    }
    let inner = &response["data"];
    if inner["ok"] != true {
        return Err(inner["error"]["message"].as_str().unwrap_or("The engine rejected the request").to_owned());
    }
    Ok(inner["data"].clone())
}

fn preferences_path_for(state: &DesktopState) -> Result<PathBuf, String> {
    if let Some(registration) = state.registration.as_ref() {
        let selected = registration.lock().map_err(|_| "Desktop settings are unavailable".to_owned())?;
        Ok(selected.1.preferences_path())
    } else {
        Ok(state.config.data_dir.join("desktop-preferences.json"))
    }
}

fn preferences_payload(
    prefs: &DesktopPreferences,
    tray_available: bool,
    registration: &Option<Arc<Mutex<(Layout, UserDirs, bool)>>>,
) -> Value {
    let mut payload = json!({
        "preferences_version": prefs.preferences_version,
        "start_at_login": prefs.start_at_login,
        "close_to_tray": prefs.close_to_tray,
        "start_minimized": prefs.start_minimized,
        "onboarding_completed": prefs.onboarding_completed,
        "tray_available": tray_available,
        "packaged": registration.is_some(),
        "autostart_state": "unavailable",
    });
    if let Some(registration) = registration {
        if let Ok(selected) = registration.lock() {
            match autostart::state(&selected.0.app, &selected.1) {
                Ok(ManifestState::Missing) => payload["autostart_state"] = json!("missing"),
                Ok(ManifestState::Registered) => payload["autostart_state"] = json!("registered"),
                Ok(ManifestState::Conflict) => payload["autostart_state"] = json!("conflict"),
                Err(error) => {
                    payload["autostart_state"] = json!("error");
                    payload["autostart_error"] = json!(error.to_string());
                }
            }
        }
    }
    payload
}

#[tauri::command]
async fn protocol_request(state: State<'_, DesktopState>, request: Value) -> Result<Value, String> {
    let config = state.config.clone();
    let request_id = contract::request_id(&request).map(str::to_owned);
    let failed_id = request_id.clone();
    let mutation = contract::is_mutation(&request);
    let result = tauri::async_runtime::spawn_blocking(move || {
        let outer = protocol::Request { spike_version: 1, id: request_id.clone().unwrap_or_else(|| "invalid".into()),
            op: "v1.request".into(), params: request.as_object().cloned().unwrap_or_default() };
        if let Err(code) = outer.validate(false) { return contract::failure(request_id.as_deref(), code); }
        match transport::call(&config, &outer) {
            Ok(response) if response["ok"] == true && response["id"] == outer.id => response["data"].clone(),
            Ok(response) => contract::failure(request_id.as_deref(), response["error"]["code"].as_str().unwrap_or("BAD_RESPONSE")),
            Err(_) => contract::failure(request_id.as_deref(), if mutation { "REQUEST_OUTCOME_UNKNOWN" } else { "ENGINE_UNAVAILABLE" }),
        }
    }).await;
    Ok(result.unwrap_or_else(|_| contract::failure(failed_id.as_deref(), if mutation { "REQUEST_OUTCOME_UNKNOWN" } else { "ENGINE_UNAVAILABLE" })))
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
async fn get_desktop_preferences(state: State<'_, DesktopState>) -> Result<Value, String> {
    let path = preferences_path_for(&state)?;
    let tray_available = state.tray_available;
    let registration = state.registration.clone();
    let cached = state.preferences.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let prefs = autostart::load_preferences(&path).map_err(|error| error.to_string())?;
        if let Ok(mut guard) = cached.lock() {
            *guard = prefs.clone();
        }
        Ok(preferences_payload(&prefs, tray_available, &registration))
    }).await.map_err(|_| "Failed to read desktop preferences".to_owned())?
}

#[tauri::command]
async fn set_desktop_preferences(preferences: Value, state: State<'_, DesktopState>) -> Result<Value, String> {
    let object = preferences.as_object().ok_or("Preferences must be a JSON object")?;
    let path = preferences_path_for(&state)?;
    let tray_available = state.tray_available;
    let registration = state.registration.clone();
    let cached = state.preferences.clone();
    let start_at_login = object.get("start_at_login").and_then(Value::as_bool);
    let close_to_tray = object.get("close_to_tray").and_then(Value::as_bool);
    let start_minimized = object.get("start_minimized").and_then(Value::as_bool);
    let onboarding_completed = object.get("onboarding_completed").and_then(Value::as_bool);

    tauri::async_runtime::spawn_blocking(move || {
        let mut next = autostart::load_preferences(&path).map_err(|error| error.to_string())?;
        if let Some(value) = start_at_login { next.start_at_login = value; }
        if let Some(value) = close_to_tray { next.close_to_tray = value; }
        if let Some(value) = start_minimized { next.start_minimized = value; }
        if let Some(value) = onboarding_completed { next.onboarding_completed = value; }

        if let Some(registration) = registration.as_ref() {
            let selected = registration.lock().map_err(|_| "Installation settings are unavailable".to_owned())?;
            if next.start_at_login {
                autostart::enable(&selected.0.app, &selected.1, next.start_minimized)
                    .map_err(|error| error.to_string())?;
            } else {
                autostart::remove_owned(&selected.1, Some(&selected.0.app))
                    .map_err(|error| error.to_string())?;
            }
        } else if next.start_at_login {
            return Err("Login autostart requires a packaged VidGrab installation".to_owned());
        }

        autostart::save_preferences(&path, &next).map_err(|error| error.to_string())?;
        if let Ok(mut guard) = cached.lock() {
            *guard = next.clone();
        }
        Ok(preferences_payload(&next, tray_available, &registration))
    }).await.map_err(|_| "Failed to save desktop preferences".to_owned())?
}

#[tauri::command]
async fn restart_engine(state: State<'_, DesktopState>) -> Result<Value, String> {
    let config = state.config.clone();
    tauri::async_runtime::spawn_blocking(move || command(&config, "restart_engine", Default::default()))
        .await.map_err(|_| "The engine restart request failed".to_owned())?
}

#[cfg(target_os = "linux")]
fn linux_pick_folder(app: &tauri::AppHandle, initial: Option<PathBuf>) -> Result<Option<String>, String> {
    let (tx, rx) = mpsc::sync_channel(1);
    app.run_on_main_thread(move || {
        let dialog = gtk::FileChooserNative::new(
            Some("Choose download folder"),
            None::<&gtk::Window>,
            gtk::FileChooserAction::SelectFolder,
            Some("Select"),
            Some("Cancel"),
        );
        if let Some(dir) = initial.filter(|path| path.is_dir()) {
            let _ = dialog.set_current_folder(dir);
        }
        let result = if dialog.run() == gtk::ResponseType::Accept {
            match dialog.filename() {
                Some(path) => path.to_str().map(|s| Some(s.to_owned()))
                    .ok_or_else(|| "Selected folder path must be valid Unicode".to_owned()),
                None => Ok(None),
            }
        } else {
            Ok(None)
        };
        dialog.destroy();
        let _ = tx.send(result);
    }).map_err(|error| error.to_string())?;
    rx.recv().map_err(|_| "Folder selection was interrupted".to_owned())?
}

#[cfg(target_os = "linux")]
fn linux_pick_file(app: &tauri::AppHandle, save: bool, initial_name: Option<&str>) -> Result<Option<PathBuf>, String> {
    let (tx, rx) = mpsc::sync_channel(1);
    let initial_name = initial_name.map(str::to_owned);
    app.run_on_main_thread(move || {
        let (title, action, accept) = if save {
            ("Export diagnostics report", gtk::FileChooserAction::Save, "Save")
        } else {
            ("Import URL list", gtk::FileChooserAction::Open, "Open")
        };
        let dialog = gtk::FileChooserNative::new(
            Some(title),
            None::<&gtk::Window>,
            action,
            Some(accept),
            Some("Cancel"),
        );
        if save {
            dialog.set_do_overwrite_confirmation(true);
            if let Some(name) = initial_name {
                dialog.set_current_name(&name);
            }
            let filter = gtk::FileFilter::new();
            filter.set_name(Some("JSON files (*.json)"));
            filter.add_pattern("*.json");
            dialog.add_filter(filter);
        } else {
            let filter = gtk::FileFilter::new();
            filter.set_name(Some("Text URL lists (*.txt)"));
            filter.add_pattern("*.txt");
            dialog.add_filter(filter);
        }
        let result = if dialog.run() == gtk::ResponseType::Accept {
            Ok(dialog.filename())
        } else {
            Ok(None)
        };
        dialog.destroy();
        let _ = tx.send(result);
    }).map_err(|error| error.to_string())?;
    rx.recv().map_err(|_| "File selection was interrupted".to_owned())?
}

#[cfg(windows)]
fn windows_pick_folder() -> Result<Option<String>, String> {
    use std::ffi::c_void;
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::Foundation::ERROR_CANCELLED;
    use windows_sys::Win32::System::Com::{
        CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, CoCreateInstance, CoInitializeEx,
        CoTaskMemFree, CoUninitialize,
    };
    use windows_sys::Win32::UI::Shell::{
        FOS_FORCEFILESYSTEM, FOS_PICKFOLDERS, FileOpenDialog, SIGDN_FILESYSPATH,
    };
    use windows_sys::core::{GUID, HRESULT, PWSTR};

    const IID_IFILEOPENDIALOG: GUID = GUID::from_u128(0xd57c7288_d4ad_4768_be02_9d969532d960);
    const HRESULT_CANCELLED: HRESULT = (0x80070000_u32 | ERROR_CANCELLED) as i32;

    #[repr(C)]
    struct UnknownVtbl {
        _qi: usize,
        _add_ref: usize,
        release: unsafe extern "system" fn(*mut c_void) -> u32,
    }
    #[repr(C)]
    struct FileOpenDialogVtbl {
        unknown: UnknownVtbl,
        show: unsafe extern "system" fn(*mut c_void, *mut c_void) -> HRESULT,
        _rest1: [usize; 5],
        set_options: unsafe extern "system" fn(*mut c_void, u32) -> HRESULT,
        get_options: unsafe extern "system" fn(*mut c_void, *mut u32) -> HRESULT,
        _rest2: [usize; 9],
        get_result: unsafe extern "system" fn(*mut c_void, *mut *mut c_void) -> HRESULT,
    }
    #[repr(C)]
    struct ShellItemVtbl {
        unknown: UnknownVtbl,
        _bind: usize,
        _parent: usize,
        get_display_name: unsafe extern "system" fn(*mut c_void, i32, *mut PWSTR) -> HRESULT,
    }

    unsafe {
        let init = CoInitializeEx(null(), COINIT_APARTMENTTHREADED as u32);
        let did_init = init >= 0;
        let mut dialog: *mut c_void = null_mut();
        let hr = CoCreateInstance(&FileOpenDialog, null_mut(), CLSCTX_INPROC_SERVER, &IID_IFILEOPENDIALOG, &mut dialog);
        if hr < 0 || dialog.is_null() {
            if did_init { CoUninitialize(); }
            return Err("Could not open the Windows folder chooser".to_owned());
        }
        let vtbl = &*(*(dialog as *const *const FileOpenDialogVtbl));
        let mut options = 0u32;
        if (vtbl.get_options)(dialog, &mut options) >= 0 {
            let _ = (vtbl.set_options)(dialog, options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM);
        }
        let show_hr = (vtbl.show)(dialog, null_mut());
        if show_hr == HRESULT_CANCELLED {
            (vtbl.unknown.release)(dialog);
            if did_init { CoUninitialize(); }
            return Ok(None);
        }
        if show_hr < 0 {
            (vtbl.unknown.release)(dialog);
            if did_init { CoUninitialize(); }
            return Err("Folder selection failed".to_owned());
        }
        let mut item: *mut c_void = null_mut();
        let res_hr = (vtbl.get_result)(dialog, &mut item);
        (vtbl.unknown.release)(dialog);
        if res_hr < 0 || item.is_null() {
            if did_init { CoUninitialize(); }
            return Err("Could not read selected folder".to_owned());
        }
        let item_vtbl = &*(*(item as *const *const ShellItemVtbl));
        let mut raw_path: PWSTR = null_mut();
        let name_hr = (item_vtbl.get_display_name)(item, SIGDN_FILESYSPATH, &mut raw_path);
        (item_vtbl.unknown.release)(item);
        if name_hr < 0 || raw_path.is_null() {
            if did_init { CoUninitialize(); }
            return Err("Selected item is not a filesystem folder".to_owned());
        }
        let mut len = 0usize;
        while *raw_path.add(len) != 0 { len += 1; }
        let selected = String::from_utf16(std::slice::from_raw_parts(raw_path, len))
            .map_err(|_| "Selected folder path must be valid Unicode".to_owned());
        CoTaskMemFree(raw_path.cast());
        if did_init { CoUninitialize(); }
        selected.map(Some)
    }
}

#[cfg(windows)]
fn windows_pick_file(save: bool, initial_name: Option<&str>) -> Result<Option<PathBuf>, String> {
    use windows_sys::Win32::UI::Controls::Dialogs::{
        GetOpenFileNameW, GetSaveFileNameW, OFN_FILEMUSTEXIST, OFN_NOCHANGEDIR, OFN_OVERWRITEPROMPT,
        OFN_PATHMUSTEXIST, OPENFILENAMEW,
    };

    let mut buffer = vec![0u16; 32768];
    if let Some(name) = initial_name {
        let encoded: Vec<u16> = name.encode_utf16().collect();
        let count = encoded.len().min(buffer.len() - 1);
        buffer[..count].copy_from_slice(&encoded[..count]);
    }
    let filter: Vec<u16> = if save {
        "JSON files (*.json)\0*.json\0All files (*.*)\0*.*\0\0".encode_utf16().collect()
    } else {
        "Text URL lists (*.txt)\0*.txt\0All files (*.*)\0*.*\0\0".encode_utf16().collect()
    };
    let def_ext: Vec<u16> = if save { "json\0" } else { "txt\0" }.encode_utf16().collect();
    let title: Vec<u16> = if save { "Export diagnostics report\0" } else { "Import URL list\0" }.encode_utf16().collect();
    let mut ofn: OPENFILENAMEW = unsafe { std::mem::zeroed() };
    ofn.lStructSize = std::mem::size_of::<OPENFILENAMEW>() as u32;
    ofn.lpstrFilter = filter.as_ptr();
    ofn.lpstrFile = buffer.as_mut_ptr();
    ofn.nMaxFile = buffer.len() as u32;
    ofn.lpstrDefExt = def_ext.as_ptr();
    ofn.lpstrTitle = title.as_ptr();
    ofn.Flags = OFN_NOCHANGEDIR | OFN_PATHMUSTEXIST | if save { OFN_OVERWRITEPROMPT } else { OFN_FILEMUSTEXIST };

    let ok = unsafe { if save { GetSaveFileNameW(&mut ofn) } else { GetOpenFileNameW(&mut ofn) } };
    if ok == 0 {
        return Ok(None);
    }
    let len = buffer.iter().position(|c| *c == 0).unwrap_or(buffer.len());
    let path = String::from_utf16(&buffer[..len]).map_err(|_| "Selected path must be valid Unicode".to_owned())?;
    Ok(Some(PathBuf::from(path)))
}

#[tauri::command]
async fn pick_download_folder(app: tauri::AppHandle, state: State<'_, DesktopState>) -> Result<Option<String>, String> {
    let initial = Some(state.config.download_dir.clone());
    #[cfg(target_os = "linux")]
    { linux_pick_folder(&app, initial) }
    #[cfg(windows)]
    {
        let _ = (app, initial);
        tauri::async_runtime::spawn_blocking(windows_pick_folder)
            .await.map_err(|_| "Folder selection failed".to_owned())?
    }
}

#[tauri::command]
async fn pick_url_list_file(app: tauri::AppHandle) -> Result<Option<Value>, String> {
    #[cfg(target_os = "linux")]
    let selected = linux_pick_file(&app, false, None)?;
    #[cfg(windows)]
    let selected = {
        let _ = app;
        tauri::async_runtime::spawn_blocking(|| windows_pick_file(false, None))
            .await.map_err(|_| "File selection failed".to_owned())??
    };
    let Some(path) = selected else { return Ok(None); };
    tauri::async_runtime::spawn_blocking(move || {
        #[cfg(target_os = "linux")]
        let mut file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(&path)
            .map_err(|error| error.to_string())?;
        #[cfg(windows)]
        let mut file = {
            use windows_sys::Win32::Storage::FileSystem::{FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT};
            let f = std::fs::OpenOptions::new().read(true).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT).open(&path)
                .map_err(|error| error.to_string())?;
            if f.metadata().map_err(|e| e.to_string())?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err("Refused symlinked or reparse-point URL list file".to_owned());
            }
            f
        };
        let meta = file.metadata().map_err(|error| error.to_string())?;
        if !meta.is_file() {
            return Err("Selected URL list must be a regular file".to_owned());
        }
        const MAX_BYTES: u64 = 512 * 1024;
        if meta.len() > MAX_BYTES {
            return Err("URL list file exceeds the 512 KiB limit".to_owned());
        }
        let mut content = String::new();
        (&mut file).take(MAX_BYTES + 1).read_to_string(&mut content)
            .map_err(|_| "URL list file must be valid UTF-8 text".to_owned())?;
        let mut urls = Vec::new();
        let mut truncated = false;
        for line in content.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }
            if urls.len() >= 200 {
                truncated = true;
                break;
            }
            urls.push(trimmed.to_owned());
        }
        Ok(Some(json!({
            "path": path.to_string_lossy(),
            "urls": urls,
            "truncated": truncated,
        })))
    }).await.map_err(|_| "Failed to read URL list file".to_owned())?
}

fn validate_job_id(job_id: &str) -> Result<(), String> {
    if job_id.is_empty() || job_id.len() > 128 || !job_id.bytes().all(|byte| (33..=126).contains(&byte)) {
        return Err("Invalid download job identifier".to_owned());
    }
    Ok(())
}

fn validate_job_media_path(config: &Config, raw_path: &str, check_open_safe: bool) -> Result<PathBuf, String> {
    if raw_path.is_empty() {
        return Err("This download has no saved output file".to_owned());
    }
    let path = PathBuf::from(raw_path);
    if !path.is_absolute() || path.components().any(|component| matches!(component, Component::ParentDir)) {
        return Err("Refused invalid download file path".to_owned());
    }
    let metadata = fs::symlink_metadata(&path).map_err(|_| "The downloaded file no longer exists on disk".to_owned())?;
    if metadata.is_symlink() || !metadata.is_file() {
        return Err("Refused to access a symlink or non-regular file".to_owned());
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err("Refused to access a reparse-point file".to_owned());
        }
    }
    if path.starts_with(&config.data_dir) {
        return Err("Refused to open internal application state files".to_owned());
    }
    let canon = path.canonicalize().map_err(|_| "The downloaded file path could not be resolved".to_owned())?;
    if let Ok(canon_data) = config.data_dir.canonicalize() {
        if canon.starts_with(&canon_data) {
            return Err("Refused to open internal application state files".to_owned());
        }
    }
    if check_open_safe {
        #[cfg(target_os = "linux")]
        if metadata.permissions().mode() & 0o111 != 0 {
            return Err("Refused to directly execute a file with executable permissions; use Show in folder".to_owned());
        }
        let ext = path.extension().and_then(|e| e.to_str()).map(|s| s.to_ascii_lowercase()).unwrap_or_default();
        const SAFE_MEDIA_EXTS: &[&str] = &[
            "mp4", "mkv", "webm", "mov", "avi", "flv", "ts", "m2ts", "mts", "m4v", "3gp", "wmv", "ogv", "mpg", "mpeg",
            "mp3", "m4a", "m4b", "opus", "ogg", "oga", "wav", "flac", "aac", "wma", "alac", "aiff",
            "srt", "vtt", "ass", "ssa", "ttml", "lrc",
            "jpg", "jpeg", "png", "webp", "gif",
        ];
        if !SAFE_MEDIA_EXTS.contains(&ext.as_str()) {
            return Err("Only media, subtitle, and thumbnail files can be opened directly; use Show in folder".to_owned());
        }
    }
    #[cfg(windows)]
    let canon = vidgrab_supervisor::normalize_windows_shell_path(&canon);
    Ok(canon)
}

#[tauri::command]
async fn reveal_download(job_id: String, state: State<'_, DesktopState>) -> Result<(), String> {
    validate_job_id(&job_id)?;
    let config = state.config.clone();
    let path = tauri::async_runtime::spawn_blocking(move || {
        let job = v1_command(&config, "downloads.get", json!({"job_id": job_id}))?;
        let raw_path = job["file_path"].as_str().unwrap_or_default();
        validate_job_media_path(&config, raw_path, false)
    }).await.map_err(|_| "Failed to look up download".to_owned())??;

    #[cfg(target_os = "linux")]
    {
        let file = gio::File::for_path(&path);
        let uri = file.uri().to_string();
        let shown = gio::DBusProxy::for_bus_sync(
            gio::BusType::Session,
            gio::DBusProxyFlags::DO_NOT_LOAD_PROPERTIES,
            None::<&gio::DBusInterfaceInfo>,
            "org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1",
            None::<&gio::Cancellable>,
        ).ok().and_then(|proxy| {
            let items: [&str; 1] = [uri.as_str()];
            proxy.call_sync(
                "ShowItems",
                Some(&(&items[..], "").to_variant()),
                gio::DBusCallFlags::NONE,
                2000,
                None::<&gio::Cancellable>,
            ).ok()
        }).is_some();
        if !shown {
            let parent = path.parent().ok_or("Download has no parent folder")?;
            let folder_uri = gio::File::for_path(parent).uri();
            gio::AppInfo::launch_default_for_uri(&folder_uri, None::<&gio::AppLaunchContext>)
                .map_err(|error| error.to_string())?;
        }
        Ok(())
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use std::ptr::null;
        use windows_sys::Win32::System::Com::{COINIT_APARTMENTTHREADED, CoInitializeEx, CoUninitialize};
        use windows_sys::Win32::UI::Shell::{ILCreateFromPathW, ILFree, SHOpenFolderAndSelectItems};

        let wide: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        unsafe {
            let init = CoInitializeEx(null(), COINIT_APARTMENTTHREADED as u32);
            let pidl = ILCreateFromPathW(wide.as_ptr());
            if pidl.is_null() {
                if init >= 0 { CoUninitialize(); }
                return Err("Could not locate file for Explorer".to_owned());
            }
            let hr = SHOpenFolderAndSelectItems(pidl, 0, null(), 0);
            ILFree(pidl);
            if init >= 0 { CoUninitialize(); }
            if hr < 0 {
                return Err("Failed to reveal download in Explorer".to_owned());
            }
        }
        Ok(())
    }
}

#[tauri::command]
async fn open_download(job_id: String, state: State<'_, DesktopState>) -> Result<(), String> {
    validate_job_id(&job_id)?;
    let config = state.config.clone();
    let path = tauri::async_runtime::spawn_blocking(move || {
        let job = v1_command(&config, "downloads.get", json!({"job_id": job_id}))?;
        let raw_path = job["file_path"].as_str().unwrap_or_default();
        validate_job_media_path(&config, raw_path, true)
    }).await.map_err(|_| "Failed to look up download".to_owned())??;

    #[cfg(target_os = "linux")]
    {
        let uri = gio::File::for_path(&path).uri();
        gio::AppInfo::launch_default_for_uri(&uri, None::<&gio::AppLaunchContext>)
            .map_err(|error| error.to_string())
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use std::ptr::null;
        use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};

        let verb: Vec<u16> = "open\0".encode_utf16().collect();
        let target: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
        let result = unsafe {
            ShellExecuteW(std::ptr::null_mut(), verb.as_ptr(), target.as_ptr(), null(), null(), SW_SHOWNORMAL)
        };
        if result as isize > 32 { Ok(()) }
        else { Err("Could not open the downloaded file with the default application".to_owned()) }
    }
}

fn collect_diagnostics_report(
    config: &Config,
    tray: bool,
    registration: &Option<Arc<Mutex<(Layout, UserDirs, bool)>>>,
    prefs_path: &Path,
) -> Value {
    let supervisor_status = command(config, "status", Default::default())
        .unwrap_or_else(|err| json!({"state": "unavailable", "error": err}));
    let engine_diagnostics = v1_command(config, "diagnostics.get", json!({}))
        .unwrap_or_else(|err| json!({"error": err}));
    let prefs = autostart::load_preferences(prefs_path).unwrap_or_default();
    let engine_log_tail = vidgrab_supervisor::read_redacted_engine_log_tail(&config.data_dir);
    let mut report = json!({
        "supervisor": supervisor_status,
        "engine": engine_diagnostics,
        "desktop_preferences": preferences_payload(&prefs, tray, registration),
        "engine_log_tail": engine_log_tail,
    });
    if let Some(registration) = registration.as_ref() {
        if let Ok(selected) = registration.lock() {
            match installation::connection_status(&selected.0, &selected.1) {
                Ok(conn) => report["browser_connection"] = json!(conn),
                Err(error) => report["browser_connection_error"] = json!(error.to_string()),
            }
            match application::status(&selected.0, &selected.1, selected.2) {
                Ok(app) => report["application"] = json!(app),
                Err(error) => report["application_error"] = json!(error.to_string()),
            }
        }
    }
    report
}

#[tauri::command]
async fn diagnostics_report(state: State<'_, DesktopState>) -> Result<Value, String> {
    let config = state.config.clone();
    let tray = state.tray_available;
    let registration = state.registration.clone();
    let prefs_path = preferences_path_for(&state)?;
    tauri::async_runtime::spawn_blocking(move || {
        Ok(collect_diagnostics_report(&config, tray, &registration, &prefs_path))
    }).await.map_err(|_| "Failed to collect diagnostics".to_owned())?
}

#[tauri::command]
async fn export_diagnostics_file(app: tauri::AppHandle, state: State<'_, DesktopState>) -> Result<Option<String>, String> {
    #[cfg(target_os = "linux")]
    let selected = linux_pick_file(&app, true, Some("vidgrab-diagnostics.json"))?;
    #[cfg(windows)]
    let selected = {
        let _ = app;
        tauri::async_runtime::spawn_blocking(|| windows_pick_file(true, Some("vidgrab-diagnostics.json")))
            .await.map_err(|_| "Save dialog failed".to_owned())??
    };
    let Some(path) = selected else { return Ok(None); };

    let report = diagnostics_report(state).await?;
    tauri::async_runtime::spawn_blocking(move || {
        match fs::symlink_metadata(&path) {
            Ok(meta) if meta.is_symlink() || !meta.is_file() => {
                return Err("Refused to overwrite a symlink or non-regular file".to_owned());
            }
            Ok(_meta) => {
                #[cfg(windows)]
                {
                    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
                    if _meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                        return Err("Refused to overwrite a reparse-point file".to_owned());
                    }
                }
            }
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.to_string()),
        }
        let mut bytes = serde_json::to_vec_pretty(&report).map_err(|error| error.to_string())?;
        bytes.push(b'\n');
        #[cfg(target_os = "linux")]
        {
            use std::io::Write;
            let mut file = OpenOptions::new().write(true).create(true)
                .mode(0o600).custom_flags(libc::O_NOFOLLOW).open(&path)
                .map_err(|error| error.to_string())?;
            if !file.metadata().map_err(|error| error.to_string())?.is_file() {
                return Err("Refused to overwrite a non-regular diagnostics file".to_owned());
            }
            file.set_len(0).map_err(|error| error.to_string())?;
            file.write_all(&bytes).map_err(|error| error.to_string())?;
            file.sync_all().map_err(|error| error.to_string())?;
        }
        #[cfg(windows)]
        {
            use std::io::Write;
            use windows_sys::Win32::Storage::FileSystem::{FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT};
            let mut file = OpenOptions::new().write(true).create(true)
                .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT).open(&path)
                .map_err(|error| error.to_string())?;
            let meta = file.metadata().map_err(|error| error.to_string())?;
            if meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 || !meta.is_file() {
                return Err("Refused to overwrite a reparse-point or non-regular diagnostics file".to_owned());
            }
            file.set_len(0).map_err(|error| error.to_string())?;
            file.write_all(&bytes).map_err(|error| error.to_string())?;
            file.sync_all().map_err(|error| error.to_string())?;
        }
        Ok(Some(path.to_string_lossy().into_owned()))
    }).await.map_err(|_| "Failed to save diagnostics report".to_owned())?
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

#[cfg(target_os = "linux")]
fn software_manager() -> Result<gio::DesktopAppInfo, String> {
    ["/var/lib/snapd/desktop/applications/snap-store_snap-store.desktop", "/usr/share/applications/org.gnome.Software.desktop"]
        .iter().find_map(gio::DesktopAppInfo::from_filename)
        .ok_or_else(|| "Open your desktop's software manager to remove the VidGrab package".to_owned())
}

#[tauri::command]
fn open_software_manager() -> Result<(), String> {
    #[cfg(target_os = "linux")]
    { software_manager()?.launch(&[], None::<&gio::AppLaunchContext>).map_err(|error| error.to_string()) }
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};
        let target: Vec<u16> = "ms-settings:appsfeatures".encode_utf16().chain([0]).collect();
        let result = unsafe { ShellExecuteW(std::ptr::null_mut(), std::ptr::null(), target.as_ptr(), std::ptr::null(), std::ptr::null(), SW_SHOWNORMAL) };
        if result as isize > 32 { Ok(()) } else { Err("Open Settings → Apps → Installed apps to uninstall VidGrab".into()) }
    }
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
        #[cfg(target_os = "linux")]
        if !selected.2 { software_manager()?; }
        command(&config, "quit_if_idle", Default::default())?;
        let stopped = shutdown.stopped.lock().map_err(|_| "The app shutdown state is unavailable".to_owned())?;
        let (stopped, _) = shutdown.ready.wait_timeout_while(stopped, Duration::from_secs(10), |stopped| !*stopped)
            .map_err(|_| "The app shutdown state is unavailable".to_owned())?;
        if !*stopped { return Err("VidGrab has not finished shutting down; keep the installed files and try again".to_owned()); }
        if selected.2 { application::remove(&selected.1).map_err(|error| error.to_string())?; }
        else {
            installation::disconnect(&selected.1).map_err(|error| error.to_string())?;
            autostart::remove_owned(&selected.1, Some(&selected.0.app)).map_err(|error| error.to_string())?;
        }
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
    #[cfg(windows)]
    { return true; }
    #[cfg(target_os = "linux")]
    {
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
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let raw_args: Vec<_> = std::env::args_os().skip(1).collect();
    let start_minimized = raw_args.iter().any(|arg| arg == "--start-minimized");
    let arguments: Vec<_> = raw_args.into_iter().filter(|arg| arg != "--start-minimized").collect();
    let context = tauri::generate_context!();
    let mut registration = None;
    let config = if arguments.len() == 2 && arguments[0] == "--config" {
        Config::load(&PathBuf::from(&arguments[1]))?
    } else {
        let packaged = arguments.len() == 2 && arguments[0] == "--package-config";
        let diagnostic = !start_minimized && arguments.len() == 1 && [
            "--check-package", "--browser-connection-status", "--register-browser-host",
            "--unregister-browser-host", "--installation-status", "--install-appimage",
            "--remove-appimage", "--prepare-uninstall",
        ].iter().any(|flag| arguments[0] == *flag);
        if !arguments.is_empty() && !packaged && !diagnostic {
            return Err("Use a packaged launch, --config FILE, [--start-minimized], --check-package, --installation-status, --install-appimage, --remove-appimage, --register-browser-host or --unregister-browser-host".into());
        }
        if arguments.first().is_some_and(|arg| arg == "--check-package") {
            let environment = tauri::Env::default();
            let resources = tauri::utils::platform::resource_dir(context.package_info(), &environment)?;
            let image = std::env::var_os("APPIMAGE");
            let layout = Layout::discover(&std::env::current_exe()?, &resources, image.as_deref().map(Path::new))?;
            println!("{}", json!({"app":layout.app, "engine":layout.engine, "native":layout.native}));
            return Ok(());
        }
        let dirs = UserDirs::from_env()?;
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
        #[cfg(windows)]
        if arguments.first().is_some_and(|arg| arg == "--prepare-uninstall") {
            application::prepare_uninstall(&layout, &dirs)?;
            println!("{}", json!({"prepared": true}));
            return Ok(());
        }
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
    let initial_prefs = if let Some(registration) = registration.as_ref() {
        let selected = registration.lock().map_err(|_| "Desktop preferences are unavailable")?;
        autostart::load_preferences(&selected.1.preferences_path()).unwrap_or_default()
    } else {
        autostart::load_preferences(&config.data_dir.join("desktop-preferences.json")).unwrap_or_default()
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
    let preferences = Arc::new(Mutex::new(initial_prefs));
    let application = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            protocol_request,
            companion_status,
            set_browser_connection,
            install_appimage,
            remove_application,
            open_software_manager,
            download_fixture,
            quit_companion,
            get_desktop_preferences,
            set_desktop_preferences,
            restart_engine,
            pick_download_folder,
            pick_url_list_file,
            reveal_download,
            open_download,
            diagnostics_report,
            export_diagnostics_file,
        ])
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
                                if let Some(window) = handle.get_webview_window("main") {
                                    let _ = window.unminimize();
                                    let _ = window.show();
                                    let _ = window.set_focus();
                                }
                            } else if event.id.as_ref() == "quit" {
                                handle.state::<DesktopState>().stop.store(true, Ordering::Release);
                            }
                        }).build(app).is_ok();
                }
            }
            if start_minimized && tray_available {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
            }
            app.manage(DesktopState { config, stop, tray_available, registration, shutdown, maintenance, preferences });
            let handle = app.handle().clone();
            std::thread::spawn(move || owner.run(move |event| match event {
                UiEvent::Activate => {
                    let activate = handle.clone();
                    let _ = handle.run_on_main_thread(move || {
                        if let Some(window) = activate.get_webview_window("main") {
                            let _ = window.unminimize();
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
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
                let state = window.state::<DesktopState>();
                let close_to_tray = state.preferences.lock().map(|prefs| prefs.close_to_tray).unwrap_or(true);
                if close_to_tray {
                    if state.tray_available {
                        let _ = window.hide();
                    }
                } else {
                    let config = state.config.clone();
                    let stop = state.stop.clone();
                    let stopped = state.shutdown.stopped.lock().is_ok_and(|flag| *flag);
                    let win = window.clone();
                    std::thread::spawn(move || {
                        let active = match command(&config, "status", Default::default()) {
                            Ok(status) => status.get("downloads_active").and_then(Value::as_bool)
                                .unwrap_or_else(|| status.get("state").and_then(Value::as_str) == Some("ready")),
                            Err(_) => false,
                        };
                        if !active {
                            if stopped {
                                win.app_handle().exit(0);
                            } else {
                                stop.store(true, Ordering::Release);
                            }
                        } else {
                            let _ = win.unminimize();
                            let _ = win.show();
                            let _ = win.set_focus();
                            let _ = win.emit("vidgrab://confirm-quit", json!({"downloads_active": true}));
                        }
                    });
                }
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

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        #[cfg(windows)]
        if std::env::args_os().len() == 1 {
            use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_OK, MB_ICONERROR};
            let message: Vec<u16> = error.to_string().encode_utf16().chain([0]).collect();
            let title: Vec<u16> = "VidGrab".encode_utf16().chain([0]).collect();
            unsafe { MessageBoxW(std::ptr::null_mut(), message.as_ptr(), title.as_ptr(), MB_OK | MB_ICONERROR); }
        }
        std::process::exit(1);
    }
}
