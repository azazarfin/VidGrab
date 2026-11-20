//! Pre-update database and settings backup snapshots with bounded retention.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(target_os = "linux")]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
#[cfg(windows)]
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};

pub const MAX_PRE_UPDATE_BACKUPS: usize = 3;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct BackupFileEntry {
    pub name: String,
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct BackupRecord {
    pub id: String,
    pub reason: String,
    pub version: String,
    pub created_at: u64,
    pub path: PathBuf,
    pub files: Vec<BackupFileEntry>,
}

fn check_no_symlink_or_reparse(path: &Path) -> io::Result<Option<fs::Metadata>> {
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
            Ok(Some(meta))
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(err) => Err(err),
    }
}

fn ensure_private_dir(path: &Path) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        check_no_symlink_or_reparse(parent)?;
    }
    match check_no_symlink_or_reparse(path)? {
        Some(meta) => {
            if !meta.is_dir() {
                return Err(io::Error::new(io::ErrorKind::AlreadyExists, "Expected directory in backups"));
            }
            #[cfg(target_os = "linux")]
            if meta.uid() != unsafe { libc::geteuid() } {
                return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Backup directory owned by another user"));
            }
        }
        None => {
            fs::create_dir_all(path)?;
            #[cfg(target_os = "linux")]
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        }
    }
    Ok(())
}

fn copy_and_hash_safe(src: &Path, dst: &Path) -> io::Result<Option<BackupFileEntry>> {
    let Some(meta) = check_no_symlink_or_reparse(src)? else {
        return Ok(None);
    };
    if !meta.is_file() {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "Backup source is not a regular file"));
    }
    check_no_symlink_or_reparse(dst)?;

    #[cfg(target_os = "linux")]
    let mut reader = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(src)?;
    #[cfg(windows)]
    let mut reader = {
        use windows_sys::Win32::Storage::FileSystem::{FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT};
        let f = OpenOptions::new().read(true).custom_flags(FILE_FLAG_OPEN_REPARSE_POINT).open(src)?;
        if f.metadata()?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Refused reparse-point backup source"));
        }
        f
    };
    #[cfg(not(any(target_os = "linux", windows)))]
    let mut reader = OpenOptions::new().read(true).open(src)?;

    #[cfg(target_os = "linux")]
    let mut writer = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(dst)?;
    #[cfg(not(target_os = "linux"))]
    let mut writer = OpenOptions::new().write(true).create_new(true).open(dst)?;

    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65_536];
    let mut total = 0u64;
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        writer.write_all(&buf[..n])?;
        hasher.update(&buf[..n]);
        total += n as u64;
    }
    writer.sync_all()?;
    let expected_hash = format!("{:x}", hasher.finalize());

    // Re-read written backup to confirm exact byte integrity on disk.
    let mut verify_reader = OpenOptions::new().read(true).open(dst)?;
    let mut verify_hasher = Sha256::new();
    let mut verify_total = 0u64;
    loop {
        let n = verify_reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        verify_hasher.update(&buf[..n]);
        verify_total += n as u64;
    }
    let actual_hash = format!("{:x}", verify_hasher.finalize());
    if verify_total != total || actual_hash != expected_hash {
        return Err(io::Error::other("Pre-update backup failed integrity verification"));
    }
    let name = dst.file_name().and_then(|s| s.to_str()).unwrap_or_default().to_owned();
    Ok(Some(BackupFileEntry { name, bytes: total, sha256: actual_hash }))
}

pub fn create_pre_update_backup(data_dir: &Path, from_version: &str, to_version: &str) -> io::Result<BackupRecord> {
    check_no_symlink_or_reparse(data_dir)?;
    let backups_dir = data_dir.join("backups");
    ensure_private_dir(&backups_dir)?;

    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let mut nonce = [0u8; 4];
    getrandom::fill(&mut nonce).map_err(io::Error::other)?;
    let suffix = u32::from_ne_bytes(nonce);
    let id = format!("pre-update-{now:010}-{suffix:08x}");
    let snapshot_dir = backups_dir.join(&id);
    ensure_private_dir(&snapshot_dir)?;

    let mut files = Vec::new();
    let result: io::Result<()> = (|| {
        for filename in ["vidgrab.db", "vidgrab.db-wal", "vidgrab.db-shm", "desktop-preferences.json", "connection.json"] {
            let src = data_dir.join(filename);
            let dst = snapshot_dir.join(filename);
            if let Some(entry) = copy_and_hash_safe(&src, &dst)? {
                files.push(entry);
            }
        }
        let record = BackupRecord {
            id: id.clone(),
            reason: format!("upgrade:{from_version}->{to_version}"),
            version: from_version.to_owned(),
            created_at: now,
            path: snapshot_dir.clone(),
            files: files.clone(),
        };
        let manifest_path = snapshot_dir.join("backup.json");
        let manifest_bytes = serde_json::to_vec_pretty(&record).map_err(io::Error::other)?;
        #[cfg(target_os = "linux")]
        let mut mf = OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(&manifest_path)?;
        #[cfg(not(target_os = "linux"))]
        let mut mf = OpenOptions::new().write(true).create_new(true).open(&manifest_path)?;
        mf.write_all(&manifest_bytes)?;
        mf.sync_all()?;
        Ok(())
    })();
    if let Err(err) = result {
        let _ = fs::remove_dir_all(&snapshot_dir);
        return Err(err);
    }

    prune_pre_update_backups(&backups_dir, MAX_PRE_UPDATE_BACKUPS)?;
    Ok(BackupRecord {
        id,
        reason: format!("upgrade:{from_version}->{to_version}"),
        version: from_version.to_owned(),
        created_at: now,
        path: snapshot_dir,
        files,
    })
}

fn prune_pre_update_backups(backups_dir: &Path, keep: usize) -> io::Result<()> {
    let mut dirs = Vec::new();
    for entry in fs::read_dir(backups_dir)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with("pre-update-") && entry.file_type().is_ok_and(|t| t.is_dir() && !t.is_symlink()) {
            dirs.push(entry.path());
        }
    }
    dirs.sort();
    while dirs.len() > keep {
        let oldest = dirs.remove(0);
        let _ = fs::remove_dir_all(oldest);
    }
    Ok(())
}

pub fn list_backups(data_dir: &Path) -> Vec<BackupRecord> {
    let backups_dir = data_dir.join("backups");
    let Ok(Some(meta)) = check_no_symlink_or_reparse(&backups_dir) else {
        return Vec::new();
    };
    if !meta.is_dir() {
        return Vec::new();
    }
    let mut records = Vec::new();
    let Ok(entries) = fs::read_dir(&backups_dir) else {
        return Vec::new();
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with("pre-update-") && entry.file_type().is_ok_and(|t| t.is_dir() && !t.is_symlink()) {
            let mf = path.join("backup.json");
            if let Ok(raw) = fs::read(&mf) {
                if let Ok(record) = serde_json::from_slice::<BackupRecord>(&raw) {
                    records.push(record);
                }
            }
        } else if name.starts_with("schema-v") && name.ends_with(".sqlite3") && entry.file_type().is_ok_and(|t| t.is_file()) {
            let bytes = entry.metadata().map(|m| m.len()).unwrap_or(0);
            let created_at = entry
                .metadata()
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            records.push(BackupRecord {
                id: name.clone(),
                reason: "schema-migration".to_owned(),
                version: name.split('-').nth(1).unwrap_or("unknown").to_owned(),
                created_at,
                path,
                files: vec![BackupFileEntry { name, bytes, sha256: String::new() }],
            });
        }
    }
    records.sort_by(|a, b| b.created_at.cmp(&a.created_at).then_with(|| b.id.cmp(&a.id)));
    records
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn creates_verified_pre_update_backup_and_enforces_retention() {
        let data_dir = std::env::temp_dir().join(format!("vidgrab-backup-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
        fs::create_dir_all(&data_dir).unwrap();
        fs::write(data_dir.join("vidgrab.db"), b"sqlite-fixture-data").unwrap();
        fs::write(data_dir.join("desktop-preferences.json"), b"{\"preferences_version\":1}").unwrap();

        for i in 0..5 {
            std::thread::sleep(std::time::Duration::from_millis(2));
            let record = create_pre_update_backup(&data_dir, &format!("0.1.{i}"), &format!("0.2.{i}")).unwrap();
            assert_eq!(record.files.len(), 2);
            assert!(record.files[0].sha256.len() == 64);
        }
        let remaining: Vec<_> = fs::read_dir(data_dir.join("backups"))
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().starts_with("pre-update-"))
            .collect();
        assert_eq!(remaining.len(), MAX_PRE_UPDATE_BACKUPS);
        assert_eq!(list_backups(&data_dir).len(), MAX_PRE_UPDATE_BACKUPS);
        let _ = fs::remove_dir_all(&data_dir);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn refuses_symlinked_database_before_backup() {
        let root = std::env::temp_dir().join(format!("vidgrab-backup-sym-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
        let data_dir = root.join("state");
        fs::create_dir_all(&data_dir).unwrap();
        let target = root.join("outside.db");
        fs::write(&target, b"secret").unwrap();
        std::os::unix::fs::symlink(&target, data_dir.join("vidgrab.db")).unwrap();
        assert!(create_pre_update_backup(&data_dir, "0.1.0", "0.2.0").is_err());
        let _ = fs::remove_dir_all(&root);
    }
}
