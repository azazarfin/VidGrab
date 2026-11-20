//! P02 ownership spike with private Linux and Windows kernel boundaries.
#[cfg(not(any(target_os = "linux", windows)))]
compile_error!("The P02 native lifecycle spike supports Linux and Windows");

mod engine;
pub mod contract;
#[cfg(target_os = "linux")]
mod linux_process;
#[cfg(windows)]
pub mod windows;
pub mod installation;
pub mod legacy;
pub mod protocol;
pub mod transport;
pub mod updates;

use engine::Engine;
use serde::Deserialize;
use std::borrow::Cow;
use std::fs::{self, File, TryLockError};
#[cfg(target_os = "linux")]
use std::fs::OpenOptions;
use std::io::{self, Read, Seek, SeekFrom};
#[cfg(target_os = "linux")]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
#[cfg(target_os = "linux")]
use std::os::unix::net::UnixListener as Listener;
#[cfg(windows)]
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
#[cfg(windows)]
use windows::PipeListener as Listener;
use std::path::{Path, PathBuf};
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};
use std::time::Duration;

pub fn strip_windows_verbatim_prefix(raw: &str) -> Cow<'_, str> {
    if let Some(rest) = raw.strip_prefix(r"\\?\") {
        if let Some(unc) = rest.strip_prefix(r"UNC\") {
            return Cow::Owned(format!(r"\\{unc}"));
        }
        let bytes = rest.as_bytes();
        if bytes.len() >= 3
            && bytes[0].is_ascii_alphabetic()
            && bytes[1] == b':'
            && (bytes[2] == b'\\' || bytes[2] == b'/')
        {
            return Cow::Borrowed(rest);
        }
    }
    Cow::Borrowed(raw)
}

pub fn normalize_windows_shell_path(path: &Path) -> PathBuf {
    path.to_str()
        .map(|raw| PathBuf::from(strip_windows_verbatim_prefix(raw).as_ref()))
        .unwrap_or_else(|| path.to_path_buf())
}

fn is_word_char(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn has_sensitive_header_kv(line: &str) -> bool {
    const KEYS: &[&str] = &[
        "proxy-authorization",
        "authorization",
        "set-cookie",
        "cookies",
        "cookie",
        "referer",
        "user-agent",
        "user_agent",
        "http-headers",
        "http_headers",
        "request-headers",
        "request_headers",
        "headers",
        "auth-ref",
        "auth_ref",
        "probe-id",
        "probe_id",
        "credential-url",
        "credential_url",
    ];
    let bytes = line.as_bytes();
    let lower = line.to_ascii_lowercase();
    for key in KEYS {
        let mut search_from = 0usize;
        while let Some(rel) = lower[search_from..].find(key) {
            let start = search_from + rel;
            let end = start + key.len();
            let boundary_ok = start == 0 || !is_word_char(bytes[start - 1]);
            if boundary_ok {
                let mut idx = end;
                if idx < bytes.len() && (bytes[idx] == b'\'' || bytes[idx] == b'"') {
                    idx += 1;
                }
                while idx < bytes.len() && bytes[idx].is_ascii_whitespace() {
                    idx += 1;
                }
                if idx < bytes.len() && (bytes[idx] == b':' || bytes[idx] == b'=') {
                    return true;
                }
            }
            search_from = start + 1;
        }
    }
    false
}

fn starts_with_ignore_ascii_case(haystack: &str, prefix: &str) -> bool {
    haystack.len() >= prefix.len() && haystack.as_bytes()[..prefix.len()].eq_ignore_ascii_case(prefix.as_bytes())
}

pub fn redact_diagnostic_line(line: &str) -> String {
    if has_sensitive_header_kv(line) {
        return "Request details omitted from diagnostics".to_owned();
    }
    let bytes = line.as_bytes();
    let mut out = String::with_capacity(line.len().min(512));
    let mut i = 0usize;
    while i < line.len() {
        let rest = &line[i..];
        let boundary = i == 0 || !is_word_char(bytes[i - 1]);
        if boundary
            && (starts_with_ignore_ascii_case(rest, "https://")
                || starts_with_ignore_ascii_case(rest, "http://")
                || starts_with_ignore_ascii_case(rest, "https%3a%2f%2f")
                || starts_with_ignore_ascii_case(rest, "http%3a%2f%2f"))
        {
            out.push_str("[redacted URL]");
            while i < line.len() && !bytes[i].is_ascii_whitespace() {
                i += 1;
            }
            continue;
        }
        if boundary && starts_with_ignore_ascii_case(rest, "bearer") {
            let after_kw = i + 6;
            if after_kw < line.len() && bytes[after_kw].is_ascii_whitespace() {
                let mut k = after_kw;
                while k < line.len() && bytes[k].is_ascii_whitespace() {
                    k += 1;
                }
                if k < line.len() {
                    out.push_str("Bearer [redacted]");
                    while k < line.len() && !bytes[k].is_ascii_whitespace() {
                        k += 1;
                    }
                    i = k;
                    continue;
                }
            }
        }
        let token_len = if boundary && starts_with_ignore_ascii_case(rest, "access_token") {
            Some(12)
        } else if boundary && starts_with_ignore_ascii_case(rest, "token") {
            Some(5)
        } else {
            None
        };
        if let Some(kw_len) = token_len {
            let mut k = i + kw_len;
            if k < line.len() && (bytes[k] == b'\'' || bytes[k] == b'"') {
                k += 1;
            }
            while k < line.len() && bytes[k].is_ascii_whitespace() {
                k += 1;
            }
            if k < line.len() && (bytes[k] == b':' || bytes[k] == b'=') {
                k += 1;
                while k < line.len() && bytes[k].is_ascii_whitespace() {
                    k += 1;
                }
                if k < line.len() {
                    out.push_str("token=[redacted]");
                    while k < line.len() && !bytes[k].is_ascii_whitespace() {
                        k += 1;
                    }
                    i = k;
                    continue;
                }
            }
        }
        let ch = rest.chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    if out.len() > 512 {
        let mut end = 512;
        while !out.is_char_boundary(end) {
            end -= 1;
        }
        out.truncate(end);
    }
    out
}

pub fn read_redacted_engine_log_tail(data_dir: &Path) -> Vec<String> {
    const MAX_TAIL_BYTES: u64 = 16 * 1024;
    const MAX_LINES: usize = 50;
    let log_path = data_dir.join("logs").join("engine.log");
    let Ok(sym_meta) = fs::symlink_metadata(&log_path) else {
        return Vec::new();
    };
    if sym_meta.is_symlink() || !sym_meta.is_file() {
        return Vec::new();
    }
    #[cfg(target_os = "linux")]
    let mut file = match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(&log_path) {
        Ok(file) => file,
        Err(_) => return Vec::new(),
    };
    #[cfg(windows)]
    let mut file = {
        use windows_sys::Win32::Storage::FileSystem::{FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT};
        let Ok(file) = std::fs::OpenOptions::new().read(true).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT).open(&log_path) else {
            return Vec::new();
        };
        let Ok(meta) = file.metadata() else {
            return Vec::new();
        };
        if meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 || !meta.is_file() {
            return Vec::new();
        }
        file
    };
    #[cfg(not(any(target_os = "linux", windows)))]
    let mut file = match File::open(&log_path) {
        Ok(file) => file,
        Err(_) => return Vec::new(),
    };
    let Ok(meta) = file.metadata() else {
        return Vec::new();
    };
    if !meta.is_file() {
        return Vec::new();
    }
    let start = meta.len().saturating_sub(MAX_TAIL_BYTES);
    if start > 0 && file.seek(SeekFrom::Start(start)).is_err() {
        return Vec::new();
    }
    let mut raw = Vec::new();
    if (&mut file).take(MAX_TAIL_BYTES + 1).read_to_end(&mut raw).is_err() {
        return Vec::new();
    }
    let text = String::from_utf8_lossy(&raw);
    let slice = if start > 0 {
        match text.split_once('\n') {
            Some((_, rest)) => rest,
            None => text.as_ref(),
        }
    } else {
        text.as_ref()
    };
    let mut lines: Vec<String> = slice
        .lines()
        .map(str::trim_end)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .take(MAX_LINES)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .map(redact_diagnostic_line)
        .collect();
    lines.shrink_to_fit();
    lines
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub app: PathBuf,
    pub engine: PathBuf,
    pub data_dir: PathBuf,
    pub download_dir: PathBuf,
    pub chrome_origin: String,
    pub firefox_id: String,
}

impl Config {
    pub fn load(path: &Path) -> io::Result<Self> {
        let config: Self = serde_json::from_slice(&fs::read(path)?).map_err(io::Error::other)?;
        if [&config.app, &config.engine, &config.data_dir, &config.download_dir].iter().any(|path| !path.is_absolute()) {
            return Err(io::Error::other("Companion paths must be absolute"));
        }
        Ok(config)
    }
    pub fn ipc_dir(&self) -> PathBuf { self.data_dir.join("ipc") }
    pub fn socket_path(&self) -> PathBuf {
        #[cfg(target_os = "linux")] { self.ipc_dir().join("control.sock") }
        #[cfg(windows)] { windows::pipe_path(&self.data_dir) }
    }
}

#[derive(Clone, Copy)]
pub enum UiEvent { Activate, Stopped }

pub struct Owner {
    _lock: File,
    config: Config,
    listener: Listener,
    engine: Engine,
    stop: Arc<AtomicBool>,
}

impl Owner {
    pub fn start(config: Config) -> io::Result<Option<Self>> {
        fs::create_dir_all(&config.data_dir)?;
        let ipc = config.ipc_dir();
        #[cfg(target_os = "linux")]
        let lock = {
            match fs::create_dir(&ipc) {
                Ok(()) => fs::set_permissions(&ipc, fs::Permissions::from_mode(0o700))?,
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                    let metadata = fs::symlink_metadata(&ipc)?;
                    if !metadata.is_dir() || metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o777 != 0o700 {
                        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "IPC directory must be private and owned by this user"));
                    }
                }
                Err(error) => return Err(error),
            }
            OpenOptions::new().create(true).truncate(false).read(true).write(true).mode(0o600).open(ipc.join("owner.lock"))?
        };
        #[cfg(windows)]
        let lock = { windows::private_directory(&ipc)?; windows::open_lock(&ipc.join("owner.lock"))? };
        match lock.try_lock() {
            Ok(()) => (),
            Err(TryLockError::WouldBlock) => return Ok(None),
            Err(TryLockError::Error(error)) => return Err(error),
        }
        let socket = config.socket_path();
        #[cfg(target_os = "linux")]
        if socket.exists() { fs::remove_file(&socket)?; }
        let listener = Listener::bind(&socket)?;
        #[cfg(target_os = "linux")]
        {
            fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
            listener.set_nonblocking(true)?;
            // Orphaned descendants become reappable children of this Linux owner.
            if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1) } == -1 { return Err(io::Error::last_os_error()); }
        }
        Ok(Some(Self { _lock: lock, config, listener, engine: Engine::new(), stop: Arc::new(AtomicBool::new(false)) }))
    }

    pub fn stop_handle(&self) -> Arc<AtomicBool> { self.stop.clone() }

    pub fn run(mut self, notify: impl Fn(UiEvent)) {
        while !self.stop.load(Ordering::Acquire) {
            self.engine.tick(&self.config);
            match self.listener.accept() {
                Ok((mut connection, _)) => {
                    let result = (|| -> io::Result<()> {
                        connection.set_read_timeout(Some(Duration::from_secs(2)))?;
                        connection.set_write_timeout(Some(Duration::from_secs(2)))?;
                        if !transport::same_user(&connection)? { return Ok(()); }
                        let request: protocol::Request = match protocol::read_message(&mut connection)? {
                            Some(value) => value, None => return Ok(()),
                        };
                        let response = if let Err(code) = request.validate(false) {
                            protocol::failure(Some(&request.id), code)
                        } else if request.op == "quit" {
                            self.stop.store(true, Ordering::Release);
                            protocol::success(&request, self.engine.snapshot(&self.config.data_dir))
                        } else if request.op == "quit_if_idle" {
                            match self.engine.request("status", &Default::default(), &self.config.data_dir) {
                                Ok(data) if data.get("downloads_active").and_then(serde_json::Value::as_bool) == Some(false) => {
                                    self.stop.store(true, Ordering::Release);
                                    protocol::success(&request, data)
                                }
                                Ok(_) => protocol::failure(Some(&request.id), "DOWNLOADS_ACTIVE"),
                                Err(_) => protocol::failure(Some(&request.id), "ENGINE_UNAVAILABLE"),
                            }
                        } else if request.op == "activate" {
                            notify(UiEvent::Activate);
                            protocol::success(&request, self.engine.snapshot(&self.config.data_dir))
                        } else if request.op == "restart_engine" {
                            match self.engine.restart_now(&self.config) {
                                Ok(()) => protocol::success(&request, self.engine.snapshot(&self.config.data_dir)),
                                Err(_) => protocol::failure(Some(&request.id), "ENGINE_UNAVAILABLE"),
                            }
                        } else if request.op == "update.status" {
                            let state = updates::load_update_state(&self.config.data_dir);
                            protocol::success(&request, updates::status_snapshot(&self.config.data_dir, &state))
                        } else if request.op == "update.check" {
                            let url = request.params.get("url").and_then(serde_json::Value::as_str);
                            let channel = request.params.get("channel").and_then(serde_json::Value::as_str);
                            let snap = updates::check_for_updates(&self.config.data_dir, &self.config.engine, url, channel);
                            protocol::success(&request, snap)
                        } else if request.op == "update.apply" {
                            let url = request.params.get("url").and_then(serde_json::Value::as_str);
                            let channel = request.params.get("channel").and_then(serde_json::Value::as_str);
                            let idle_ok = if self.engine.state == "failed" {
                                Ok(true)
                            } else {
                                match self.engine.request("status", &Default::default(), &self.config.data_dir) {
                                    Ok(data) if data.get("downloads_active").and_then(serde_json::Value::as_bool) == Some(false) => Ok(true),
                                    Ok(_) => Err("DOWNLOADS_ACTIVE"),
                                    Err(_) => Err("ENGINE_UNAVAILABLE"),
                                }
                            };
                            match idle_ok {
                                Err("DOWNLOADS_ACTIVE") => protocol::failure_message(
                                    Some(&request.id),
                                    "DOWNLOADS_ACTIVE",
                                    "Finish active downloads before applying a VidGrab update",
                                ),
                                Err(code) => protocol::failure(Some(&request.id), code),
                                Ok(_) => {
                                    let needs_stage = url.is_some()
                                        || updates::load_update_state(&self.config.data_dir).pending_bundle.is_none();
                                    let stage_res = if needs_stage {
                                        updates::stage_update_from_manifest_url(
                                            &self.config.data_dir,
                                            &self.config.engine,
                                            url,
                                            channel,
                                        ).map(|_| ())
                                    } else {
                                        Ok(())
                                    };
                                    if let Err(err) = stage_res {
                                        protocol::failure_message(Some(&request.id), "UPDATE_INVALID", &err)
                                    } else {
                                        self.engine.stop_cleanly();
                                        match updates::activate_pending_update(&self.config.data_dir) {
                                            Err(err) => {
                                                let _ = self.engine.restart_now(&self.config);
                                                protocol::failure_message(Some(&request.id), "UPDATE_FAILED", &err)
                                            }
                                            Ok(_) => match self.engine.restart_now(&self.config) {
                                                Ok(()) => {
                                                    let state = updates::load_update_state(&self.config.data_dir);
                                                    protocol::success(
                                                        &request,
                                                        updates::status_snapshot(&self.config.data_dir, &state),
                                                    )
                                                }
                                                Err(_) => {
                                                    let state = updates::load_update_state(&self.config.data_dir);
                                                    let msg = state.rollback_notice.as_deref().unwrap_or(
                                                        "Update failed verification and restored the previous working version.",
                                                    );
                                                    protocol::failure_message(Some(&request.id), "UPDATE_FAILED", msg)
                                                }
                                            },
                                        }
                                    }
                                }
                            }
                        } else if request.op == "update.rollback" {
                            let idle_ok = if self.engine.state == "failed" {
                                Ok(true)
                            } else {
                                match self.engine.request("status", &Default::default(), &self.config.data_dir) {
                                    Ok(data) if data.get("downloads_active").and_then(serde_json::Value::as_bool) == Some(false) => Ok(true),
                                    Ok(_) => Err("DOWNLOADS_ACTIVE"),
                                    Err(_) => Err("ENGINE_UNAVAILABLE"),
                                }
                            };
                            match idle_ok {
                                Err("DOWNLOADS_ACTIVE") => protocol::failure_message(
                                    Some(&request.id),
                                    "DOWNLOADS_ACTIVE",
                                    "Finish active downloads before rolling back VidGrab",
                                ),
                                Err(code) => protocol::failure(Some(&request.id), code),
                                Ok(_) => {
                                    self.engine.stop_cleanly();
                                    match updates::manual_rollback(&self.config.data_dir) {
                                        Err(err) => {
                                            let _ = self.engine.restart_now(&self.config);
                                            protocol::failure_message(Some(&request.id), "UPDATE_FAILED", &err)
                                        }
                                        Ok(_) => {
                                            let _ = self.engine.restart_now(&self.config);
                                            let state = updates::load_update_state(&self.config.data_dir);
                                            protocol::success(
                                                &request,
                                                updates::status_snapshot(&self.config.data_dir, &state),
                                            )
                                        }
                                    }
                                }
                            }
                        } else if request.op == "status" && self.engine.state != "ready" {
                            protocol::success(&request, self.engine.snapshot(&self.config.data_dir))
                        } else if request.op == "v1.request" && self.engine.state == "ready" && !self.engine.supports_protocol(1) {
                            protocol::failure(Some(&request.id), "APP_INCOMPATIBLE")
                        } else {
                            match self.engine.request(&request.op, &request.params, &self.config.data_dir) {
                                Ok(data) => protocol::success(&request, data),
                                Err(_) => protocol::failure(Some(&request.id), "ENGINE_UNAVAILABLE"),
                            }
                        };
                        connection.set_write_timeout(Some(Duration::from_secs(2)))?;
                        protocol::write_message(&mut connection, &response)
                    })();
                    if let Err(error) = result { eprintln!("Companion IPC failed: {error}"); }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => std::thread::sleep(Duration::from_millis(25)),
                Err(error) => { eprintln!("Companion listener failed: {error}"); break; }
            }
        }
        self.engine = Engine::new(); // Drop and reap the owned group before releasing the lock.
        #[cfg(target_os = "linux")]
        let _ = fs::remove_file(self.config.socket_path());
        drop(self.listener);
        drop(self._lock);
        notify(UiEvent::Stopped);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_verbatim_prefix_normalizes_drive_and_unc_paths() {
        assert_eq!(strip_windows_verbatim_prefix(r"\\?\C:\Users\Alice\video.mp4"), r"C:\Users\Alice\video.mp4");
        assert_eq!(strip_windows_verbatim_prefix(r"\\?\UNC\server\share\video.mp4"), r"\\server\share\video.mp4");
        assert_eq!(strip_windows_verbatim_prefix(r"C:\Users\Alice\video.mp4"), r"C:\Users\Alice\video.mp4");
        assert_eq!(
            normalize_windows_shell_path(Path::new(r"\\?\D:\Media\বাংলা.mp4")),
            PathBuf::from(r"D:\Media\বাংলা.mp4")
        );
    }

    #[test]
    fn read_redacted_engine_log_tail_bounds_lines_and_scrubs_secrets() {
        let dir = std::env::temp_dir().join(format!("vidgrab-log-tail-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let logs = dir.join("logs");
        fs::create_dir_all(&logs).unwrap();
        let mut content = String::new();
        for idx in 0..65 {
            content.push_str(&format!("INFO line-{idx} https://example.com/watch?token=abc Bearer secret-tok token=xyz\n"));
        }
        content.push_str("DEBUG Authorization: Bearer supersecret\n");
        content.push_str("ERROR failed downloading বাংলা.mp4\n");
        fs::write(logs.join("engine.log"), content).unwrap();

        let tail = read_redacted_engine_log_tail(&dir);
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(tail.len(), 50);
        assert_eq!(tail[48], "Request details omitted from diagnostics");
        assert_eq!(tail[49], "ERROR failed downloading বাংলা.mp4");
        assert!(tail.iter().all(|line| !line.contains("secret-tok") && !line.contains("supersecret") && !line.contains("https://")));
        assert!(tail[0].contains("[redacted URL]") && tail[0].contains("Bearer [redacted]") && tail[0].contains("token=[redacted]"));
    }
}
