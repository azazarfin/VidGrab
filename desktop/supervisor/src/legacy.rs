//! Safe non-mutating legacy `config.json` importer and service migration guidance helpers.

use serde::Serialize;
use serde_json::{Map, Value};
use std::fs::{self, OpenOptions};
use std::io::Read;
use std::path::Path;

#[cfg(target_os = "linux")]
use std::os::unix::fs::OpenOptionsExt;
#[cfg(windows)]
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};

pub const MAX_LEGACY_CONFIG_BYTES: u64 = 256 * 1024;
const MAX_JS_SAFE_INT: u64 = 9_007_199_254_740_991;

#[derive(Clone, Debug, Serialize)]
pub struct LegacyConfigImport {
    pub source_path: String,
    pub patch: Map<String, Value>,
    pub imported_keys: Vec<String>,
    pub skipped_keys: Vec<String>,
    pub warnings: Vec<String>,
}

pub fn parse_legacy_config_bytes(source_path: &str, bytes: &[u8]) -> Result<LegacyConfigImport, String> {
    let raw: Value = serde_json::from_slice(bytes).map_err(|_| "Legacy config file is not valid JSON".to_owned())?;
    let obj = raw.as_object().ok_or("Legacy config must be a JSON object")?;

    let mut patch = Map::new();
    let mut imported_keys = Vec::new();
    let mut skipped_keys = Vec::new();
    let mut warnings = Vec::new();

    for (key, value) in obj {
        match key.as_str() {
            "custom_ytdlp_args" => {
                skipped_keys.push(key.clone());
                let non_empty = value.as_str().is_some_and(|s| !s.trim().is_empty());
                if non_empty {
                    warnings.push(
                        "Skipped 'custom_ytdlp_args': raw command-line arguments are disabled in the packaged VidGrab engine for safety.".to_owned(),
                    );
                } else {
                    warnings.push("Skipped legacy 'custom_ytdlp_args' field (not used in packaged VidGrab).".to_owned());
                }
            }
            "auto_update_ytdlp" => {
                skipped_keys.push(key.clone());
                warnings.push(
                    "Skipped 'auto_update_ytdlp': signed app/engine bundle updates replace runtime pip self-updates.".to_owned(),
                );
            }
            "download_dir" => {
                if let Some(s) = value.as_str().map(str::trim).filter(|s| !s.is_empty() && !s.contains('\0')) {
                    patch.insert(key.clone(), Value::String(s.to_owned()));
                    imported_keys.push(key.clone());
                } else {
                    skipped_keys.push(key.clone());
                    warnings.push("Skipped 'download_dir': must be a non-empty folder path.".to_owned());
                }
            }
            "default_format" | "default_quality" | "filename_template" | "sponsorblock_mode" | "postprocess_codec" => {
                if let Some(s) = value.as_str().filter(|s| !s.contains('\0')) {
                    patch.insert(key.clone(), Value::String(s.to_owned()));
                    imported_keys.push(key.clone());
                } else {
                    skipped_keys.push(key.clone());
                    warnings.push(format!("Skipped '{key}': expected a string value."));
                }
            }
            "sponsorblock_enabled" | "postprocess_normalize_audio" | "gpu_accelerated_conversion" | "transliterate_bangla_filenames" => {
                if let Some(b) = value.as_bool() {
                    patch.insert(key.clone(), Value::Bool(b));
                    imported_keys.push(key.clone());
                } else {
                    skipped_keys.push(key.clone());
                    warnings.push(format!("Skipped '{key}': expected a boolean value."));
                }
            }
            "max_concurrent" => {
                if let Some(n) = value.as_u64().filter(|n| (1..=10).contains(n)) {
                    patch.insert(key.clone(), Value::from(n));
                    imported_keys.push(key.clone());
                } else {
                    skipped_keys.push(key.clone());
                    warnings.push("Skipped 'max_concurrent': must be an integer between 1 and 10.".to_owned());
                }
            }
            "speed_limit" => {
                if let Some(n) = value.as_u64().filter(|n| *n <= MAX_JS_SAFE_INT) {
                    patch.insert(key.clone(), Value::from(n));
                    imported_keys.push(key.clone());
                } else {
                    skipped_keys.push(key.clone());
                    warnings.push("Skipped 'speed_limit': must be a non-negative integer.".to_owned());
                }
            }
            "download_threads" => {
                if let Some(n) = value.as_u64().filter(|n| (1..=32).contains(n)) {
                    patch.insert(key.clone(), Value::from(n));
                    imported_keys.push(key.clone());
                } else {
                    skipped_keys.push(key.clone());
                    warnings.push("Skipped 'download_threads': must be an integer between 1 and 32.".to_owned());
                }
            }
            "max_retries" => {
                if let Some(n) = value.as_u64().filter(|n| *n <= 10) {
                    patch.insert(key.clone(), Value::from(n));
                    imported_keys.push(key.clone());
                } else {
                    skipped_keys.push(key.clone());
                    warnings.push("Skipped 'max_retries': must be an integer between 0 and 10.".to_owned());
                }
            }
            "sponsorblock_categories" => {
                if let Some(items) = value.as_array() {
                    if items.iter().all(|v| v.as_str().is_some_and(|s| !s.is_empty() && !s.contains('\0'))) {
                        patch.insert(key.clone(), Value::Array(items.clone()));
                        imported_keys.push(key.clone());
                    } else {
                        skipped_keys.push(key.clone());
                        warnings.push("Skipped 'sponsorblock_categories': entries must be strings.".to_owned());
                    }
                } else {
                    skipped_keys.push(key.clone());
                    warnings.push("Skipped 'sponsorblock_categories': expected an array of strings.".to_owned());
                }
            }
            other => {
                skipped_keys.push(other.to_owned());
                warnings.push(format!("Skipped unrecognized legacy key '{other}'."));
            }
        }
    }

    Ok(LegacyConfigImport {
        source_path: source_path.to_owned(),
        patch,
        imported_keys,
        skipped_keys,
        warnings,
    })
}

pub fn read_legacy_config_file(path: &Path) -> Result<LegacyConfigImport, String> {
    let sym_meta = fs::symlink_metadata(path).map_err(|_| "Selected legacy config file does not exist".to_owned())?;
    if sym_meta.is_symlink() || !sym_meta.is_file() {
        return Err("Refused symlinked or non-regular legacy config file".to_owned());
    }
    #[cfg(target_os = "linux")]
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|e| e.to_string())?;
    #[cfg(windows)]
    let mut file = {
        use windows_sys::Win32::Storage::FileSystem::{FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT};
        let f = OpenOptions::new()
            .read(true)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .map_err(|e| e.to_string())?;
        if f.metadata().map_err(|e| e.to_string())?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err("Refused reparse-point legacy config file".to_owned());
        }
        f
    };
    #[cfg(not(any(target_os = "linux", windows)))]
    let mut file = OpenOptions::new().read(true).open(path).map_err(|e| e.to_string())?;

    let meta = file.metadata().map_err(|e| e.to_string())?;
    if !meta.is_file() || meta.len() > MAX_LEGACY_CONFIG_BYTES {
        return Err("Legacy config file exceeds the 256 KiB limit or is not a regular file".to_owned());
    }
    let mut buf = Vec::new();
    (&mut file)
        .take(MAX_LEGACY_CONFIG_BYTES + 1)
        .read_to_end(&mut buf)
        .map_err(|e| e.to_string())?;
    if buf.len() as u64 > MAX_LEGACY_CONFIG_BYTES {
        return Err("Legacy config file exceeds the 256 KiB limit".to_owned());
    }
    parse_legacy_config_bytes(&path.to_string_lossy(), &buf)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn maps_valid_keys_and_warns_on_removed_or_unsafe_legacy_keys() {
        let payload = br#"{
          "download_dir": "/tmp/LegacyMedia",
          "max_concurrent": 5,
          "default_format": "bestvideo+bestaudio",
          "custom_ytdlp_args": "--proxy socks5://127.0.0.1:9050",
          "auto_update_ytdlp": true,
          "transliterate_bangla_filenames": false,
          "unknown_field": 123
        }"#;
        let parsed = parse_legacy_config_bytes("/tmp/config.json", payload).unwrap();
        assert_eq!(parsed.patch["download_dir"], "/tmp/LegacyMedia");
        assert_eq!(parsed.patch["max_concurrent"], 5);
        assert_eq!(parsed.patch["default_format"], "bestvideo+bestaudio");
        assert_eq!(parsed.patch["transliterate_bangla_filenames"], false);
        assert!(!parsed.patch.contains_key("custom_ytdlp_args"));
        assert!(!parsed.patch.contains_key("auto_update_ytdlp"));
        assert!(parsed.skipped_keys.contains(&"custom_ytdlp_args".to_owned()));
        assert!(parsed.skipped_keys.contains(&"auto_update_ytdlp".to_owned()));
        assert!(parsed.warnings.iter().any(|w| w.contains("custom_ytdlp_args") && w.contains("disabled")));
        assert!(parsed.warnings.iter().any(|w| w.contains("auto_update_ytdlp")));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn refuses_symlinked_legacy_config_file() {
        let root = std::env::temp_dir().join(format!("vidgrab-legacy-sym-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
        fs::create_dir_all(&root).unwrap();
        let target = root.join("real.json");
        fs::write(&target, br#"{"max_concurrent":2}"#).unwrap();
        let link = root.join("config.json");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(read_legacy_config_file(&link).is_err());
        let _ = fs::remove_dir_all(&root);
    }
}
