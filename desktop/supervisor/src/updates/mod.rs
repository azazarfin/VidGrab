//! Signed application/engine bundle updates, staging, idle gating, and automatic rollback.

pub mod backups;
pub mod ed25519;
pub mod tar;

use crate::redact_diagnostic_line;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap as StdBTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(target_os = "linux")]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
#[cfg(windows)]
use std::os::windows::fs::MetadataExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const SUPPORTED_PROTOCOL_VERSION: u16 = 1;
pub const MAX_MANIFEST_BYTES: usize = 256 * 1024;
pub const MAX_BUNDLE_BYTES: u64 = 512 * 1024 * 1024;
pub const REQUIRED_NOTICE_FILES: &[&str] = &[
    "FONTS_LICENSE.txt",
    "GPLv3.ffmpeg.txt",
    "LICENSE",
    "THIRD_PARTY.md",
];

// Pinned public key for release signature verification (no private key is stored in the repository).
// CI overrides via VIDGRAB_UPDATE_PUBLIC_KEY at compile time; test harnesses can opt in with
// VIDGRAB_ALLOW_TEST_UPDATE_KEY=1 + updates/config.json test_public_key_hex.
const fn configured_or_default(value: Option<&'static str>, fallback: &'static str) -> &'static str {
    if let Some(value) = value {
        if !value.is_empty() {
            return value;
        }
    }
    fallback
}

const DEFAULT_PUBLIC_KEY_HEX: &str = configured_or_default(
    option_env!("VIDGRAB_UPDATE_PUBLIC_KEY"),
    "6ee71cdd58c6f7bd787f9383eb0db61ed3157e0290ed1ed30370ab41b121561e",
);

#[cfg(target_os = "linux")]
const PLATFORM_DEFAULT_MANIFEST_URL: &str =
    "https://github.com/azazarfin/VidGrab/releases/latest/download/vidgrab-update-manifest-linux-x86_64.json";
#[cfg(windows)]
const PLATFORM_DEFAULT_MANIFEST_URL: &str =
    "https://github.com/azazarfin/VidGrab/releases/latest/download/vidgrab-update-manifest-windows-x64.json";

const DEFAULT_MANIFEST_URL: &str = configured_or_default(
    option_env!("VIDGRAB_UPDATE_MANIFEST_URL"),
    PLATFORM_DEFAULT_MANIFEST_URL,
);

pub fn current_platform() -> &'static str {
    #[cfg(target_os = "linux")]
    { "linux-x86_64" }
    #[cfg(windows)]
    { "windows-x64" }
}

pub fn engine_executable_name() -> &'static str {
    #[cfg(windows)]
    { "vidgrab-engine.exe" }
    #[cfg(not(windows))]
    { "vidgrab-engine" }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct UpdateConfig {
    #[serde(default)]
    pub manifest_url: Option<String>,
    #[serde(default)]
    pub channel: Option<String>,
    #[serde(default)]
    pub test_public_key_hex: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct InstalledBundle {
    pub version: String,
    pub engine_path: PathBuf,
    pub bundle_dir: PathBuf,
    pub sha256: String,
    pub notices_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct PendingBundle {
    pub version: String,
    pub channel: String,
    pub engine_path: PathBuf,
    pub bundle_dir: PathBuf,
    pub sha256: String,
    pub notices_sha256: String,
    pub notes: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct LastCheckStatus {
    pub checked_at: u64,
    pub status: String, // "up_to_date" | "update_available" | "offline" | "error"
    pub available_version: Option<String>,
    pub channel: String,
    pub notes: Option<String>,
    pub message: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct UpdateState {
    #[serde(default)]
    pub active_bundle: Option<InstalledBundle>,
    #[serde(default)]
    pub previous_bundle: Option<InstalledBundle>,
    #[serde(default)]
    pub pending_bundle: Option<PendingBundle>,
    #[serde(default)]
    pub unverified_activation: bool,
    #[serde(default)]
    pub rollback_notice: Option<String>,
    #[serde(default)]
    pub last_check: Option<LastCheckStatus>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct UpdateManifest {
    pub update_manifest_version: u16,
    pub version: String,
    pub channel: String,
    pub protocol_min: u16,
    pub protocol_max: u16,
    pub platform: String,
    pub url: String,
    pub size: u64,
    pub sha256: String,
    pub third_party_notices_sha256: String,
    #[serde(default)]
    pub format: Option<String>,
    #[serde(default)]
    pub notes: Option<String>,
    pub signature: String,
}

#[derive(Clone, Debug)]
pub struct EngineTarget {
    pub path: PathBuf,
    pub version: String,
    pub unverified: bool,
}

fn check_no_symlink_or_reparse(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            if meta.is_symlink() {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    format!("Refused symlinked path: {}", path.display()),
                ));
            }
            #[cfg(windows)]
            {
                use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
                if meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                    return Err(io::Error::new(
                        io::ErrorKind::PermissionDenied,
                        format!("Refused reparse-point path: {}", path.display()),
                    ));
                }
            }
            Ok(())
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err),
    }
}

fn ensure_private_dir(path: &Path) -> io::Result<()> {
    check_no_symlink_or_reparse(path)?;
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            if !meta.is_dir() {
                return Err(io::Error::new(io::ErrorKind::AlreadyExists, "Expected directory"));
            }
            #[cfg(target_os = "linux")]
            if meta.uid() != unsafe { libc::geteuid() } {
                return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Update directory owned by another user"));
            }
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => {
            fs::create_dir_all(path)?;
            #[cfg(target_os = "linux")]
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        }
        Err(err) => return Err(err),
    }
    Ok(())
}

fn atomic_write_private(path: &Path, bytes: &[u8]) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        ensure_private_dir(parent)?;
    }
    check_no_symlink_or_reparse(path)?;
    let mut nonce = [0u8; 8];
    getrandom::fill(&mut nonce).map_err(io::Error::other)?;
    let tmp = path.with_extension(format!("{:016x}.tmp", u64::from_ne_bytes(nonce)));
    let res = (|| -> io::Result<()> {
        #[cfg(target_os = "linux")]
        let mut f = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&tmp)?;
        #[cfg(not(target_os = "linux"))]
        let mut f = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        fs::rename(&tmp, path)?;
        Ok(())
    })();
    if res.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    res
}

fn updates_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("updates")
}

fn state_path(data_dir: &Path) -> PathBuf {
    updates_dir(data_dir).join("state.json")
}

fn config_path(data_dir: &Path) -> PathBuf {
    updates_dir(data_dir).join("config.json")
}

pub fn load_update_config(data_dir: &Path) -> UpdateConfig {
    let path = config_path(data_dir);
    if check_no_symlink_or_reparse(&path).is_err() {
        return UpdateConfig::default();
    }
    let Ok(bytes) = fs::read(&path) else {
        return UpdateConfig::default();
    };
    serde_json::from_slice(&bytes).unwrap_or_default()
}

pub fn load_update_state(data_dir: &Path) -> UpdateState {
    let path = state_path(data_dir);
    if check_no_symlink_or_reparse(&path).is_err() {
        return UpdateState::default();
    }
    let Ok(bytes) = fs::read(&path) else {
        return UpdateState::default();
    };
    serde_json::from_slice(&bytes).unwrap_or_default()
}

pub fn save_update_state(data_dir: &Path, state: &UpdateState) -> io::Result<()> {
    let bytes = serde_json::to_vec_pretty(state).map_err(io::Error::other)?;
    atomic_write_private(&state_path(data_dir), &bytes)
}

fn active_manifest_url(data_dir: &Path, explicit_url: Option<&str>) -> String {
    if let Some(u) = explicit_url.map(str::trim).filter(|s| !s.is_empty()) {
        return u.to_owned();
    }
    if let Ok(env_url) = std::env::var("VIDGRAB_UPDATE_MANIFEST_URL") {
        let trimmed = env_url.trim();
        if !trimmed.is_empty() {
            return trimmed.to_owned();
        }
    }
    let cfg = load_update_config(data_dir);
    if let Some(u) = cfg.manifest_url.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        return u.to_owned();
    }
    DEFAULT_MANIFEST_URL.to_owned()
}

fn active_channel(data_dir: &Path, explicit_channel: Option<&str>) -> String {
    if let Some(c) = explicit_channel.map(str::trim).filter(|s| !s.is_empty()) {
        return c.to_owned();
    }
    let cfg = load_update_config(data_dir);
    cfg.channel
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("stable")
        .to_owned()
}

fn pinned_public_key_hex(data_dir: &Path, explicit_test_key: Option<&str>) -> Result<[u8; 32], String> {
    let allow_test_key = std::env::var_os("VIDGRAB_ALLOW_TEST_UPDATE_KEY").as_deref() == Some(std::ffi::OsStr::new("1"));
    if let Some(override_hex) = explicit_test_key {
        if !allow_test_key {
            return Err("Test update signing keys are disabled in production mode".to_owned());
        }
        return ed25519::decode_hex::<32>(override_hex);
    }
    if allow_test_key {
        let cfg = load_update_config(data_dir);
        if let Some(ref hex) = cfg.test_public_key_hex {
            return ed25519::decode_hex::<32>(hex);
        }
    }
    ed25519::decode_hex::<32>(DEFAULT_PUBLIC_KEY_HEX)
}

fn sort_json_value(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut sorted = StdBTreeMap::new();
            for (k, v) in map {
                sorted.insert(k.clone(), sort_json_value(v));
            }
            let mut out = serde_json::Map::new();
            for (k, v) in sorted {
                out.insert(k, v);
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(sort_json_value).collect()),
        other => other.clone(),
    }
}

pub fn canonical_manifest_signing_bytes(raw_json: &[u8]) -> Result<(UpdateManifest, Vec<u8>), String> {
    let mut top: Value = serde_json::from_slice(raw_json).map_err(|_| "Update manifest is not valid JSON".to_owned())?;
    let manifest: UpdateManifest = serde_json::from_value(top.clone())
        .map_err(|err| format!("Invalid update manifest structure: {err}"))?;
    let obj = top.as_object_mut().ok_or("Update manifest must be a JSON object")?;
    obj.remove("signature");
    let canonical = sort_json_value(&Value::Object(obj.clone()));
    let bytes = serde_json::to_vec(&canonical).map_err(|err| err.to_string())?;
    Ok((manifest, bytes))
}

#[derive(Eq, PartialEq, Ord, PartialOrd, Debug)]
struct SemVer {
    major: u64,
    minor: u64,
    patch: u64,
    is_stable: bool,
    pre: String,
}

fn parse_semver(raw: &str) -> Option<SemVer> {
    let trimmed = raw.trim().strip_prefix('v').unwrap_or(raw.trim());
    if trimmed.is_empty() || trimmed.len() > 64 {
        return None;
    }
    let (core, pre) = match trimmed.split_once('-') {
        Some((c, p)) => (c, p.to_owned()),
        None => (trimmed, String::new()),
    };
    let parts: Vec<&str> = core.split('.').collect();
    if parts.len() != 3 {
        return None;
    }
    Some(SemVer {
        major: parts[0].parse().ok()?,
        minor: parts[1].parse().ok()?,
        patch: parts[2].parse().ok()?,
        is_stable: pre.is_empty(),
        pre,
    })
}

pub fn compare_versions(a: &str, b: &str) -> Result<std::cmp::Ordering, String> {
    let va = parse_semver(a).ok_or_else(|| "Invalid semantic version in current state".to_owned())?;
    let vb = parse_semver(b).ok_or_else(|| "Invalid semantic version in update manifest".to_owned())?;
    Ok(va.cmp(&vb))
}

pub fn validate_update_url(url: &str) -> Result<(), String> {
    if url.len() > 2048 || url.chars().any(char::is_control) {
        return Err("Update URL failed format validation".to_owned());
    }
    if url.starts_with("https://") {
        return Ok(());
    }
    if let Some(rest) = url.strip_prefix("http://127.0.0.1:") {
        if let Some((port, path)) = rest.split_once('/') {
            if port.parse::<u16>().is_ok_and(|p| p > 0) && !path.is_empty() {
                return Ok(());
            }
        }
    }
    Err("Update URL must use HTTPS (or loopback HTTP for local testing)".to_owned())
}

pub fn verify_manifest_bytes(raw_json: &[u8], public_key: &[u8; 32], expected_channel: &str) -> Result<UpdateManifest, String> {
    let (manifest, signing_bytes) = canonical_manifest_signing_bytes(raw_json)?;
    if manifest.update_manifest_version != 1 {
        return Err(format!("Unsupported update_manifest_version: {}", manifest.update_manifest_version));
    }
    if manifest.platform != current_platform() {
        return Err(format!("Update manifest targets '{}', expected '{}'", manifest.platform, current_platform()));
    }
    if manifest.channel != expected_channel {
        return Err(format!("Update channel mismatch: expected '{}', got '{}'", expected_channel, manifest.channel));
    }
    if parse_semver(&manifest.version).is_none() {
        return Err("Invalid update version string".to_owned());
    }
    if manifest.protocol_min == 0 || manifest.protocol_min > manifest.protocol_max {
        return Err("Invalid protocol compatibility range in update manifest".to_owned());
    }
    if !(manifest.protocol_min..=manifest.protocol_max).contains(&SUPPORTED_PROTOCOL_VERSION) {
        return Err(format!(
            "Update v{} requires protocol v{}-v{} which is incompatible with this app shell (v{})",
            manifest.version, manifest.protocol_min, manifest.protocol_max, SUPPORTED_PROTOCOL_VERSION
        ));
    }
    validate_update_url(&manifest.url)?;
    if manifest.size == 0 || manifest.size > MAX_BUNDLE_BYTES {
        return Err("Update artifact size is out of allowed bounds".to_owned());
    }
    ed25519::decode_hex::<32>(&manifest.sha256).map_err(|_| "Invalid artifact sha256 digest".to_owned())?;
    ed25519::decode_hex::<32>(&manifest.third_party_notices_sha256)
        .map_err(|_| "Invalid third_party_notices_sha256 digest".to_owned())?;
    let sig_bytes = ed25519::decode_hex::<64>(&manifest.signature)
        .map_err(|_| "Invalid Ed25519 signature hex encoding".to_owned())?;
    if !ed25519::verify(public_key, &signing_bytes, &sig_bytes) {
        return Err("Update manifest signature verification failed".to_owned());
    }
    Ok(manifest)
}

fn decode_chunked(body: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let mut cursor = body;
    loop {
        let Some(pos) = cursor.windows(2).position(|w| w == b"\r\n") else {
            return Err("Malformed chunked HTTP response".to_owned());
        };
        let size_line = std::str::from_utf8(&cursor[..pos]).map_err(|_| "Invalid HTTP chunk header".to_owned())?;
        let size_hex = size_line.split(';').next().unwrap_or("").trim();
        let chunk_len = usize::from_str_radix(size_hex, 16).map_err(|_| "Invalid HTTP chunk size".to_owned())?;
        cursor = &cursor[pos + 2..];
        if chunk_len == 0 {
            break;
        }
        if cursor.len() < chunk_len + 2 {
            return Err("Truncated chunked HTTP response".to_owned());
        }
        out.extend_from_slice(&cursor[..chunk_len]);
        cursor = &cursor[chunk_len + 2..];
    }
    Ok(out)
}

fn fetch_loopback_http(url: &str, max_bytes: usize) -> Result<Vec<u8>, String> {
    let rest = url.strip_prefix("http://127.0.0.1:").ok_or("Expected loopback URL")?;
    let (port_str, raw_path) = rest.split_once('/').ok_or("Missing URL path")?;
    let port: u16 = port_str.parse().map_err(|_| "Invalid loopback port")?;
    let path = format!("/{}", raw_path);
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(3))
        .map_err(|_| "Update server unreachable (connection refused or timed out)".to_owned())?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));

    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUser-Agent: VidGrab-Updater/{APP_VERSION}\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).map_err(|_| "Update request failed while sending".to_owned())?;

    let mut raw = Vec::new();
    let cap = max_bytes.saturating_add(64 * 1024);
    let mut limited = (&mut stream).take(cap as u64 + 1);
    limited.read_to_end(&mut raw).map_err(|_| "Update server read timed out".to_owned())?;
    if raw.len() > cap {
        return Err("Update server response exceeded maximum allowed size".to_owned());
    }
    let Some(hdr_end) = raw.windows(4).position(|w| w == b"\r\n\r\n") else {
        return Err("Update server returned an incomplete HTTP response".to_owned());
    };
    let headers = String::from_utf8_lossy(&raw[..hdr_end]);
    let mut lines = headers.lines();
    let status_line = lines.next().unwrap_or("");
    let code: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    if code != 200 {
        return Err(format!("Update server returned HTTP {code}"));
    }
    let chunked = headers
        .lines()
        .any(|line| line.to_ascii_lowercase().starts_with("transfer-encoding:") && line.to_ascii_lowercase().contains("chunked"));
    let body = &raw[hdr_end + 4..];
    let decoded = if chunked { decode_chunked(body)? } else { body.to_vec() };
    if decoded.len() > max_bytes {
        return Err("Update payload exceeded size limit".to_owned());
    }
    Ok(decoded)
}

fn fetch_remote_via_engine(engine_path: &Path, url: &str, out_file: &Path, max_bytes: u64, timeout_secs: u64) -> Result<(), String> {
    check_no_symlink_or_reparse(engine_path).map_err(|e| e.to_string())?;
    let mut cmd = Command::new(engine_path);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.arg("--fetch-update")
        .arg("--url")
        .arg(url)
        .arg("--out")
        .arg(out_file)
        .arg("--max-bytes")
        .arg(max_bytes.to_string())
        .arg("--timeout")
        .arg(timeout_secs.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = cmd.output().map_err(|_| "Could not run engine HTTPS fetch helper".to_owned())?;
    if !output.status.success() {
        let err_line = String::from_utf8_lossy(&output.stderr)
            .lines()
            .last()
            .map(redact_diagnostic_line)
            .unwrap_or_else(|| "Update server unreachable".to_owned());
        return Err(if err_line.is_empty() { "Update server unreachable".to_owned() } else { err_line });
    }
    Ok(())
}

fn fetch_to_temp(data_dir: &Path, engine_path: &Path, url: &str, max_bytes: u64) -> Result<PathBuf, String> {
    validate_update_url(url)?;
    let staging_dir = updates_dir(data_dir).join("staging");
    ensure_private_dir(&staging_dir).map_err(|e| e.to_string())?;
    let mut nonce = [0u8; 8];
    getrandom::fill(&mut nonce).map_err(|e| e.to_string())?;
    let out_file = staging_dir.join(format!("dl-{:016x}.tmp", u64::from_ne_bytes(nonce)));

    if url.starts_with("http://127.0.0.1:") {
        let max_usize = usize::try_from(max_bytes).map_err(|_| "Size cap too large".to_owned())?;
        let bytes = fetch_loopback_http(url, max_usize)?;
        atomic_write_private(&out_file, &bytes).map_err(|e| e.to_string())?;
        return Ok(out_file);
    }

    fetch_remote_via_engine(engine_path, url, &out_file, max_bytes, 6)?;
    Ok(out_file)
}

pub fn compute_notices_digest(notices_dir: &Path) -> Result<String, String> {
    check_no_symlink_or_reparse(notices_dir).map_err(|e| e.to_string())?;
    let meta = fs::symlink_metadata(notices_dir)
        .map_err(|_| "Staged bundle is missing required third-party notices directory".to_owned())?;
    if !meta.is_dir() {
        return Err("Notices path in staged bundle is not a directory".to_owned());
    }
    let mut index_lines = String::new();
    for &filename in REQUIRED_NOTICE_FILES {
        let p = notices_dir.join(filename);
        check_no_symlink_or_reparse(&p).map_err(|e| e.to_string())?;
        let fmeta = fs::symlink_metadata(&p)
            .map_err(|_| format!("Staged bundle is missing required notice file: {filename}"))?;
        if !fmeta.is_file() {
            return Err(format!("Notice entry {filename} is not a regular file"));
        }
        let bytes = fs::read(&p).map_err(|e| e.to_string())?;
        let file_hash = format!("{:x}", Sha256::digest(&bytes));
        index_lines.push_str(&format!("{file_hash}  {filename}\n"));
    }
    Ok(format!("{:x}", Sha256::digest(index_lines.as_bytes())))
}

fn normalize_extracted_bundle(staging_dir: &Path) -> Result<PathBuf, String> {
    let exe_name = engine_executable_name();
    if staging_dir.join(exe_name).is_file() {
        return Ok(staging_dir.to_path_buf());
    }
    if staging_dir.join("engine").join(exe_name).is_file() {
        return Ok(staging_dir.join("engine"));
    }
    Err(format!("Update archive does not contain {exe_name}"))
}

fn find_notices_dir(bundle_dir: &Path) -> Result<PathBuf, String> {
    let internal = bundle_dir.join("_internal").join("notices");
    if internal.is_dir() {
        return Ok(internal);
    }
    let top = bundle_dir.join("notices");
    if top.is_dir() {
        return Ok(top);
    }
    Err("Staged bundle does not contain third-party notices".to_owned())
}

fn run_engine_self_check(engine_path: &Path) -> Result<Value, String> {
    check_no_symlink_or_reparse(engine_path).map_err(|e| e.to_string())?;
    let mut cmd = Command::new(engine_path);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.arg("--self-check")
        .env("PATH", "")
        .env("PYTHONPATH", "")
        .env("PYTHONHOME", "")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let output = cmd.output().map_err(|_| "Failed to spawn staged engine for --self-check".to_owned())?;
    if !output.status.success() {
        return Err("Staged engine failed --self-check".to_owned());
    }
    let parsed: Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| "Staged engine --self-check returned invalid JSON".to_owned())?;
    if parsed.get("spike_version").and_then(Value::as_u64) != Some(1) {
        return Err("Staged engine --self-check reported incompatible spike_version".to_owned());
    }
    Ok(parsed)
}

fn copy_file_streaming_private(
    src: &Path,
    dst: &Path,
    #[cfg(target_os = "linux")] exec: bool,
) -> io::Result<()> {
    if let Some(parent) = dst.parent() {
        ensure_private_dir(parent)?;
    }
    check_no_symlink_or_reparse(src)?;
    check_no_symlink_or_reparse(dst)?;
    let mut nonce = [0u8; 8];
    getrandom::fill(&mut nonce).map_err(io::Error::other)?;
    let tmp = dst.with_extension(format!("{:016x}.tmp", u64::from_ne_bytes(nonce)));
    let res = (|| -> io::Result<()> {
        #[cfg(target_os = "linux")]
        let mut reader = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(src)?;
        #[cfg(not(target_os = "linux"))]
        let mut reader = File::open(src)?;

        #[cfg(target_os = "linux")]
        let perm = if exec { 0o755 } else { 0o644 };
        #[cfg(target_os = "linux")]
        let mut writer = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(perm)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&tmp)?;
        #[cfg(not(target_os = "linux"))]
        let mut writer = OpenOptions::new().write(true).create_new(true).open(&tmp)?;

        let mut buf = [0u8; 65_536];
        loop {
            let n = reader.read(&mut buf)?;
            if n == 0 {
                break;
            }
            writer.write_all(&buf[..n])?;
        }
        writer.sync_all()?;
        #[cfg(target_os = "linux")]
        fs::set_permissions(&tmp, fs::Permissions::from_mode(perm))?;
        fs::rename(&tmp, dst)?;
        Ok(())
    })();
    if res.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    res
}

fn copy_dir_recursive_safe(src: &Path, dst: &Path) -> io::Result<()> {
    ensure_private_dir(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let s = entry.path();
        check_no_symlink_or_reparse(&s)?;
        let d = dst.join(entry.file_name());
        let ft = entry.file_type()?;
        if ft.is_dir() {
            copy_dir_recursive_safe(&s, &d)?;
        } else if ft.is_file() {
            #[cfg(target_os = "linux")]
            let exec = (entry.metadata()?.permissions().mode() & 0o111) != 0;
            copy_file_streaming_private(
                &s,
                &d,
                #[cfg(target_os = "linux")]
                exec,
            )?;
        }
    }
    Ok(())
}

pub fn check_for_updates(
    data_dir: &Path,
    builtin_engine: &Path,
    explicit_manifest_url: Option<&str>,
    explicit_channel: Option<&str>,
) -> Value {
    let channel = active_channel(data_dir, explicit_channel);
    let url = active_manifest_url(data_dir, explicit_manifest_url);
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let current_version = active_version(data_dir);

    let result = (|| -> Result<LastCheckStatus, String> {
        let public_key = pinned_public_key_hex(data_dir, None)?;
        let tmp_manifest = fetch_to_temp(data_dir, builtin_engine, &url, MAX_MANIFEST_BYTES as u64)?;
        let raw_bytes = fs::read(&tmp_manifest).map_err(|e| e.to_string());
        let _ = fs::remove_file(&tmp_manifest);
        let raw_bytes = raw_bytes?;
        let manifest = verify_manifest_bytes(&raw_bytes, &public_key, &channel)?;
        let ordering = compare_versions(&manifest.version, &current_version)?;
        if ordering == std::cmp::Ordering::Greater {
            Ok(LastCheckStatus {
                checked_at: now,
                status: "update_available".to_owned(),
                available_version: Some(manifest.version),
                channel: channel.clone(),
                notes: manifest.notes,
                message: None,
            })
        } else {
            Ok(LastCheckStatus {
                checked_at: now,
                status: "up_to_date".to_owned(),
                available_version: Some(manifest.version),
                channel: channel.clone(),
                notes: manifest.notes,
                message: None,
            })
        }
    })();

    let mut state = load_update_state(data_dir);
    let check_status = match result {
        Ok(status) => status,
        Err(err) => {
            let safe_msg = redact_diagnostic_line(&err);
            let offline = safe_msg.contains("unreachable") || safe_msg.contains("timed out") || safe_msg.contains("refused");
            LastCheckStatus {
                checked_at: now,
                status: if offline { "offline".to_owned() } else { "error".to_owned() },
                available_version: None,
                channel,
                notes: None,
                message: Some(safe_msg),
            }
        }
    };
    state.last_check = Some(check_status.clone());
    let _ = save_update_state(data_dir, &state);
    status_snapshot(data_dir, &state)
}

pub fn stage_verified_update_from_bytes(
    data_dir: &Path,
    builtin_engine: &Path,
    manifest_bytes: &[u8],
    artifact_bytes_override: Option<&[u8]>,
    expected_channel: Option<&str>,
    test_public_key_hex: Option<&str>,
) -> Result<PendingBundle, String> {
    let public_key = pinned_public_key_hex(data_dir, test_public_key_hex)?;
    let channel = active_channel(data_dir, expected_channel);
    let manifest = verify_manifest_bytes(manifest_bytes, &public_key, &channel)?;

    let current_ver = active_version(data_dir);
    if compare_versions(&manifest.version, &current_ver)? != std::cmp::Ordering::Greater {
        return Err(format!("Manifest v{} is not newer than active v{current_ver}", manifest.version));
    }

    let staging_root = updates_dir(data_dir).join("staging");
    ensure_private_dir(&staging_root).map_err(|e| e.to_string())?;
    let mut nonce = [0u8; 8];
    getrandom::fill(&mut nonce).map_err(|e| e.to_string())?;
    let tag = format!("{:016x}", u64::from_ne_bytes(nonce));

    let downloaded_archive = if let Some(bytes) = artifact_bytes_override {
        let p = staging_root.join(format!("artifact-{tag}.pkg"));
        atomic_write_private(&p, bytes).map_err(|e| e.to_string())?;
        p
    } else {
        fetch_to_temp(data_dir, builtin_engine, &manifest.url, manifest.size)?
    };

    let stage_result = (|| -> Result<PendingBundle, String> {
        check_no_symlink_or_reparse(&downloaded_archive).map_err(|e| e.to_string())?;
        let meta = fs::symlink_metadata(&downloaded_archive).map_err(|e| e.to_string())?;
        if meta.len() != manifest.size {
            return Err(format!("Downloaded update size {} does not match manifest {}", meta.len(), manifest.size));
        }
        let mut hasher = Sha256::new();
        let mut f = File::open(&downloaded_archive).map_err(|e| e.to_string())?;
        let mut buf = [0u8; 65_536];
        loop {
            let n = f.read(&mut buf).map_err(|e| e.to_string())?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
        }
        let actual_sha = format!("{:x}", hasher.finalize());
        if actual_sha != manifest.sha256.to_ascii_lowercase() {
            return Err("Downloaded update SHA-256 checksum mismatch".to_owned());
        }

        let unpacked_root = staging_root.join(format!("unpacked-{tag}"));
        let fmt = manifest.format.as_deref().unwrap_or_else(|| {
            if manifest.url.ends_with(".tar.gz") || manifest.url.ends_with(".tgz") {
                "tar.gz"
            } else if manifest.url.ends_with(".zip") {
                "zip"
            } else {
                "tar"
            }
        });

        let bundles_root = updates_dir(data_dir).join("bundles");
        let final_bundle_dir = bundles_root.join(format!("v{}-{tag}", manifest.version));

        let unpack_and_copy = (|| -> Result<String, String> {
            match fmt {
                "tar" => tar::extract_tar_file(&downloaded_archive, &unpacked_root).map_err(|e| e.to_string())?,
                "tar.gz" | "zip" => {
                    let mut cmd = Command::new(builtin_engine);
                    #[cfg(windows)]
                    cmd.creation_flags(CREATE_NO_WINDOW);
                    cmd.arg("--unpack-update")
                        .arg("--archive")
                        .arg(&downloaded_archive)
                        .arg("--dest")
                        .arg(&unpacked_root)
                        .arg("--format")
                        .arg(fmt)
                        .stdin(Stdio::null())
                        .stdout(Stdio::piped())
                        .stderr(Stdio::piped());
                    let out = cmd.output().map_err(|_| "Failed to invoke update archive unpacker".to_owned())?;
                    if !out.status.success() {
                        let msg = String::from_utf8_lossy(&out.stderr).trim().to_owned();
                        return Err(if msg.is_empty() {
                            "Failed to unpack verified update archive".to_owned()
                        } else {
                            redact_diagnostic_line(&msg)
                        });
                    }
                }
                other => return Err(format!("Unsupported update archive format '{other}'")),
            }

            let src_bundle = normalize_extracted_bundle(&unpacked_root)?;
            let notices_dir = find_notices_dir(&src_bundle)?;
            let notices_digest = compute_notices_digest(&notices_dir)?;
            if notices_digest != manifest.third_party_notices_sha256.to_ascii_lowercase() {
                return Err("Staged update third-party notices digest mismatch".to_owned());
            }

            ensure_private_dir(&bundles_root).map_err(|e| e.to_string())?;
            copy_dir_recursive_safe(&src_bundle, &final_bundle_dir).map_err(|e| e.to_string())?;
            Ok(notices_digest)
        })();
        let _ = fs::remove_dir_all(&unpacked_root);
        let notices_digest = match unpack_and_copy {
            Ok(digest) => digest,
            Err(err) => {
                let _ = fs::remove_dir_all(&final_bundle_dir);
                return Err(err);
            }
        };

        let engine_path = final_bundle_dir.join(engine_executable_name());
        let publish_res = (|| -> Result<PendingBundle, String> {
            run_engine_self_check(&engine_path)?;

            let pending = PendingBundle {
                version: manifest.version,
                channel: manifest.channel,
                engine_path,
                bundle_dir: final_bundle_dir.clone(),
                sha256: manifest.sha256.to_ascii_lowercase(),
                notices_sha256: notices_digest,
                notes: manifest.notes,
            };
            let mut state = load_update_state(data_dir);
            state.pending_bundle = Some(pending.clone());
            save_update_state(data_dir, &state).map_err(|e| e.to_string())?;
            Ok(pending)
        })();
        if publish_res.is_err() {
            let _ = fs::remove_dir_all(&final_bundle_dir);
        }
        publish_res
    })();

    let _ = fs::remove_file(&downloaded_archive);
    stage_result
}

pub fn stage_update_from_manifest_url(
    data_dir: &Path,
    builtin_engine: &Path,
    explicit_manifest_url: Option<&str>,
    explicit_channel: Option<&str>,
) -> Result<PendingBundle, String> {
    let url = active_manifest_url(data_dir, explicit_manifest_url);
    let tmp_manifest = fetch_to_temp(data_dir, builtin_engine, &url, MAX_MANIFEST_BYTES as u64)?;
    let raw = fs::read(&tmp_manifest).map_err(|e| e.to_string());
    let _ = fs::remove_file(&tmp_manifest);
    stage_verified_update_from_bytes(data_dir, builtin_engine, &raw?, None, explicit_channel, None)
}

fn reconcile_last_check_for_active_version(state: &mut UpdateState, active_version: &str) -> bool {
    let Some(check) = state.last_check.as_mut() else {
        return false;
    };
    if check.status != "update_available" {
        return false;
    }
    let Some(checked_ver) = check.available_version.as_deref() else {
        return false;
    };
    let Ok(ord) = compare_versions(checked_ver, active_version) else {
        return false;
    };
    if ord == std::cmp::Ordering::Greater {
        return false;
    }
    check.status = "up_to_date".to_owned();
    check.available_version = Some(active_version.to_owned());
    check.message = None;
    true
}

pub fn activate_pending_update(data_dir: &Path) -> Result<UpdateState, String> {
    let mut state = load_update_state(data_dir);
    let pending = state
        .pending_bundle
        .take()
        .ok_or_else(|| "No staged update is ready to activate".to_owned())?;
    check_no_symlink_or_reparse(&pending.engine_path).map_err(|e| e.to_string())?;
    if !pending.engine_path.is_file() {
        return Err("Staged engine executable is missing".to_owned());
    }

    let current_ver = active_version(data_dir);
    backups::create_pre_update_backup(data_dir, &current_ver, &pending.version)
        .map_err(|err| format!("Pre-update backup failed; update aborted: {}", redact_diagnostic_line(&err.to_string())))?;

    let activated_version = pending.version.clone();
    state.previous_bundle = state.active_bundle.take();
    state.active_bundle = Some(InstalledBundle {
        version: pending.version,
        engine_path: pending.engine_path,
        bundle_dir: pending.bundle_dir,
        sha256: pending.sha256,
        notices_sha256: pending.notices_sha256,
    });
    state.unverified_activation = true;
    state.rollback_notice = None;
    reconcile_last_check_for_active_version(&mut state, &activated_version);
    save_update_state(data_dir, &state).map_err(|e| e.to_string())?;
    Ok(state)
}

pub fn resolve_engine_target(data_dir: &Path, builtin_engine: &Path) -> EngineTarget {
    let state = load_update_state(data_dir);
    if let Some(active) = state.active_bundle {
        let bundles_root = updates_dir(data_dir).join("bundles");
        if check_no_symlink_or_reparse(&active.engine_path).is_ok()
            && active.bundle_dir.starts_with(&bundles_root)
            && active.engine_path.starts_with(&active.bundle_dir)
            && active.engine_path.is_file()
        {
            return EngineTarget {
                path: active.engine_path,
                version: active.version,
                unverified: state.unverified_activation,
            };
        }
    }
    EngineTarget {
        path: builtin_engine.to_path_buf(),
        version: APP_VERSION.to_owned(),
        unverified: false,
    }
}

pub fn mark_active_verified(data_dir: &Path) {
    let mut state = load_update_state(data_dir);
    let current_ver = state
        .active_bundle
        .as_ref()
        .map(|b| b.version.clone())
        .unwrap_or_else(|| APP_VERSION.to_owned());
    let check_updated = reconcile_last_check_for_active_version(&mut state, &current_ver);
    let was_unverified = state.unverified_activation;
    if !was_unverified && !check_updated {
        return;
    }
    state.unverified_activation = false;
    let _ = save_update_state(data_dir, &state);
    if was_unverified {
        prune_stale_bundles(data_dir, &state);
    }
}

pub fn rollback_failed_activation(data_dir: &Path, reason: &str) -> Result<UpdateState, String> {
    let mut state = load_update_state(data_dir);
    let failed = state.active_bundle.take();
    state.active_bundle = state.previous_bundle.take();
    state.unverified_activation = false;
    let from_ver = failed.as_ref().map(|b| b.version.as_str()).unwrap_or("unknown");
    let to_ver = state.active_bundle.as_ref().map(|b| b.version.as_str()).unwrap_or(APP_VERSION);
    let redacted_reason = redact_diagnostic_line(reason);
    state.rollback_notice = Some(format!(
        "Update to v{from_ver} failed verification ({redacted_reason}) and automatically rolled back to v{to_ver}."
    ));
    save_update_state(data_dir, &state).map_err(|e| e.to_string())?;
    if let Some(bad) = failed {
        if bad.bundle_dir.starts_with(updates_dir(data_dir).join("bundles")) {
            let _ = fs::remove_dir_all(&bad.bundle_dir);
        }
    }
    prune_stale_bundles(data_dir, &state);
    Ok(state)
}

pub fn manual_rollback(data_dir: &Path) -> Result<UpdateState, String> {
    let mut state = load_update_state(data_dir);
    let current = state
        .active_bundle
        .take()
        .ok_or_else(|| "Already running the built-in bundle version".to_owned())?;
    state.active_bundle = state.previous_bundle.take();
    state.unverified_activation = false;
    let to_ver = state.active_bundle.as_ref().map(|b| b.version.as_str()).unwrap_or(APP_VERSION);
    state.rollback_notice = Some(format!("Rolled back manually from v{} to v{to_ver}.", current.version));
    save_update_state(data_dir, &state).map_err(|e| e.to_string())?;
    if current.bundle_dir.starts_with(updates_dir(data_dir).join("bundles")) {
        let _ = fs::remove_dir_all(&current.bundle_dir);
    }
    prune_stale_bundles(data_dir, &state);
    Ok(state)
}

fn prune_stale_bundles(data_dir: &Path, state: &UpdateState) {
    let bundles_root = updates_dir(data_dir).join("bundles");
    let Ok(entries) = fs::read_dir(&bundles_root) else {
        return;
    };
    let keep_paths: Vec<&PathBuf> = [
        state.active_bundle.as_ref().map(|b| &b.bundle_dir),
        state.previous_bundle.as_ref().map(|b| &b.bundle_dir),
        state.pending_bundle.as_ref().map(|b| &b.bundle_dir),
    ]
    .into_iter()
    .flatten()
    .collect();
    for entry in entries.flatten() {
        let path = entry.path();
        if !keep_paths.iter().any(|k| *k == &path) {
            let _ = fs::remove_dir_all(path);
        }
    }
}

pub fn active_version(data_dir: &Path) -> String {
    let state = load_update_state(data_dir);
    state
        .active_bundle
        .as_ref()
        .map(|b| b.version.clone())
        .unwrap_or_else(|| APP_VERSION.to_owned())
}

pub fn status_snapshot(data_dir: &Path, state: &UpdateState) -> Value {
    let cfg = load_update_config(data_dir);
    let current_ver = state
        .active_bundle
        .as_ref()
        .map(|b| b.version.clone())
        .unwrap_or_else(|| APP_VERSION.to_owned());
    let backup_list = backups::list_backups(data_dir);
    json!({
        "app_version": APP_VERSION,
        "engine_bundle_version": current_ver,
        "protocol_version": SUPPORTED_PROTOCOL_VERSION,
        "platform": current_platform(),
        "channel": active_channel(data_dir, cfg.channel.as_deref()),
        "rollback_available": state.active_bundle.is_some(),
        "previous_version": state.previous_bundle.as_ref().map(|b| b.version.as_str()).unwrap_or_else(|| if state.active_bundle.is_some() { APP_VERSION } else { "" }),
        "pending_update": state.pending_bundle.as_ref().map(|p| json!({
            "version": p.version,
            "channel": p.channel,
            "sha256": p.sha256,
            "notices_sha256": p.notices_sha256,
            "notes": p.notes,
        })),
        "rollback_notice": state.rollback_notice,
        "last_check": state.last_check,
        "backups_count": backup_list.len(),
        "backups": backup_list.into_iter().take(6).map(|b| json!({
            "id": b.id,
            "reason": b.reason,
            "version": b.version,
            "created_at": b.created_at,
            "files_count": b.files.len(),
        })).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    fn temp_dir(name: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("vidgrab-upd-{name}-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn notices_digest_for_tests() -> (Vec<(&'static str, &'static [u8])>, String) {
        let files: Vec<(&'static str, &'static [u8])> = vec![
            ("FONTS_LICENSE.txt", b"OFL-1.1\n"),
            ("GPLv3.ffmpeg.txt", b"GPL-3.0\n"),
            ("LICENSE", b"MIT\n"),
            ("THIRD_PARTY.md", b"Notices\n"),
        ];
        let mut idx = String::new();
        for (name, body) in &files {
            idx.push_str(&format!("{:x}  {name}\n", Sha256::digest(body)));
        }
        let digest = format!("{:x}", Sha256::digest(idx.as_bytes()));
        (files, digest)
    }

    #[test]
    fn empty_compiled_update_configuration_uses_fallback() {
        const UNSET: &str = configured_or_default(None, "fallback");
        const EMPTY: &str = configured_or_default(Some(""), "fallback");
        const SET: &str = configured_or_default(Some("custom"), "fallback");
        assert_eq!(UNSET, "fallback");
        assert_eq!(EMPTY, "fallback");
        assert_eq!(SET, "custom");
    }

    #[test]
    fn default_manifest_matches_release_platform_filename() {
        if option_env!("VIDGRAB_UPDATE_MANIFEST_URL").is_none_or(str::is_empty) {
            assert!(DEFAULT_MANIFEST_URL.ends_with(&format!("/vidgrab-update-manifest-{}.json", current_platform())));
        }
    }

    fn available_check(version: &str) -> LastCheckStatus {
        LastCheckStatus {
            checked_at: 123,
            status: "update_available".into(),
            available_version: Some(version.into()),
            channel: "stable".into(),
            notes: Some("Release notes".into()),
            message: Some("Old message".into()),
        }
    }

    #[test]
    fn activation_clears_consumed_update_checks_and_preserves_newer_checks() {
        for (checked_version, expected_status) in [
            ("0.2.0", "up_to_date"),
            ("0.1.1", "up_to_date"),
            ("0.3.0", "update_available"),
            ("invalid", "update_available"),
        ] {
            let dir = temp_dir("activation-check");
            let bundle_dir = updates_dir(&dir).join("bundles/v0.2.0");
            fs::create_dir_all(&bundle_dir).unwrap();
            let engine_path = bundle_dir.join(engine_executable_name());
            fs::write(&engine_path, b"test engine").unwrap();
            let state = UpdateState {
                pending_bundle: Some(PendingBundle {
                    version: "0.2.0".into(), channel: "stable".into(),
                    engine_path, bundle_dir, sha256: "a".repeat(64),
                    notices_sha256: "b".repeat(64), notes: None,
                }),
                last_check: Some(available_check(checked_version)),
                ..UpdateState::default()
            };
            save_update_state(&dir, &state).unwrap();
            let activated = activate_pending_update(&dir).unwrap();
            let check = activated.last_check.unwrap();
            assert_eq!(check.status, expected_status);
            assert_eq!(check.checked_at, 123);
            if expected_status == "up_to_date" {
                assert_eq!(check.available_version.as_deref(), Some("0.2.0"));
                assert!(check.message.is_none());
            }
            assert_eq!(load_update_state(&dir).last_check.unwrap().status, expected_status);
            fs::remove_dir_all(dir).unwrap();
        }
    }

    #[test]
    fn verification_clears_a_persisted_stale_update_check() {
        let dir = temp_dir("verified-check");
        let state = UpdateState {
            active_bundle: Some(InstalledBundle {
                version: "0.2.0".into(), engine_path: dir.join(engine_executable_name()),
                bundle_dir: dir.clone(), sha256: "a".repeat(64), notices_sha256: "b".repeat(64),
            }),
            unverified_activation: true,
            last_check: Some(available_check("0.2.0")),
            ..UpdateState::default()
        };
        save_update_state(&dir, &state).unwrap();
        mark_active_verified(&dir);
        let verified = load_update_state(&dir);
        assert!(!verified.unverified_activation);
        assert_eq!(verified.last_check.unwrap().status, "up_to_date");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn canonical_signing_bytes_excludes_signature_and_sorts_keys() {
        let raw = br#"{"version":"0.2.0","signature":"deadbeef","channel":"stable","update_manifest_version":1}"#;
        let (_m, bytes) = canonical_manifest_signing_bytes(raw).unwrap_or_else(|_| {
            // Build full minimal manifest for full parse.
            let full = br#"{"version":"0.2.0","url":"https://example.com/a.tar","update_manifest_version":1,"third_party_notices_sha256":"0000000000000000000000000000000000000000000000000000000000000000","size":10,"signature":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sha256":"1111111111111111111111111111111111111111111111111111111111111111","protocol_min":1,"protocol_max":1,"platform":"linux-x86_64","channel":"stable"}"#;
            canonical_manifest_signing_bytes(full).unwrap()
        });
        let text = String::from_utf8(bytes).unwrap();
        assert!(!text.contains("signature"));
        assert!(text.starts_with(r#"{"channel":"stable","platform":"linux-x86_64""#));
    }

    #[test]
    fn offline_update_check_returns_graceful_status() {
        let dir = temp_dir("offline");
        let engine = dir.join("missing-engine");
        // Bind & immediately drop a listener to guarantee ECONNREFUSED on that port.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let url = format!("http://127.0.0.1:{port}/manifest.json");
        let snapshot = check_for_updates(&dir, &engine, Some(&url), Some("stable"));
        assert_eq!(snapshot["last_check"]["status"], "offline");
        assert!(snapshot["last_check"]["message"].as_str().unwrap().contains("unreachable"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn computes_deterministic_notices_digest() {
        let dir = temp_dir("notices");
        let (files, expected) = notices_digest_for_tests();
        for (name, body) in files {
            fs::write(dir.join(name), body).unwrap();
        }
        assert_eq!(compute_notices_digest(&dir).unwrap(), expected);
        fs::remove_file(dir.join("LICENSE")).unwrap();
        assert!(compute_notices_digest(&dir).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn rollback_restores_previous_bundle_and_records_redacted_notice() {
        let dir = temp_dir("rollback");
        let bad_bundle = updates_dir(&dir).join("bundles/v0.2.0-bad");
        fs::create_dir_all(&bad_bundle).unwrap();
        let mut state = UpdateState::default();
        state.active_bundle = Some(InstalledBundle {
            version: "0.2.0".to_owned(),
            engine_path: bad_bundle.join(engine_executable_name()),
            bundle_dir: bad_bundle.clone(),
            sha256: "a".repeat(64),
            notices_sha256: "b".repeat(64),
        });
        state.unverified_activation = true;
        save_update_state(&dir, &state).unwrap();

        let rolled = rollback_failed_activation(
            &dir,
            "https://user:secret@example.com/private crashed during --self-check",
        )
        .unwrap();
        assert!(rolled.active_bundle.is_none());
        assert!(!rolled.unverified_activation);
        let notice = rolled.rollback_notice.unwrap();
        assert!(notice.contains("v0.2.0") && notice.contains("v0.1.0"));
        assert!(!notice.contains("example.com") && !notice.contains("secret"));
        assert!(!bad_bundle.exists());
        let _ = fs::remove_dir_all(&dir);
    }
}
