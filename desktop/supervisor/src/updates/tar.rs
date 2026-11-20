//! Pure-Rust symlink- and traversal-safe POSIX ustar `.tar` extractor.
//! Rejects symlinks, hardlinks, special files, absolute paths, and `..` segments.

use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};

#[cfg(target_os = "linux")]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
#[cfg(windows)]
use std::os::windows::fs::MetadataExt;

const MAX_TAR_METADATA_BYTES: usize = 64 * 1024;

fn parse_octal(field: &[u8]) -> io::Result<u64> {
    let text = field
        .split(|b| *b == 0)
        .next()
        .and_then(|raw| std::str::from_utf8(raw).ok())
        .map(str::trim)
        .unwrap_or("");
    if text.is_empty() {
        return Ok(0);
    }
    u64::from_str_radix(text, 8)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Invalid tar octal header field"))
}

fn cstr(field: &[u8]) -> io::Result<String> {
    let raw = field.split(|b| *b == 0).next().unwrap_or(field);
    std::str::from_utf8(raw)
        .map(str::to_owned)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Archive entry name must be valid UTF-8"))
}

fn verify_checksum(header: &[u8; 512]) -> io::Result<()> {
    let expected = parse_octal(&header[148..156])?;
    let mut sum: u32 = 0;
    for (idx, &byte) in header.iter().enumerate() {
        let b = if (148..156).contains(&idx) { b' ' } else { byte };
        sum += u32::from(b);
    }
    if u64::from(sum) != expected {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "Invalid tar header checksum"));
    }
    Ok(())
}

fn sanitize_rel_path(raw: &str) -> io::Result<PathBuf> {
    if raw.is_empty() || raw.contains('\\') || raw.starts_with('/') {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "Refused unsafe archive path"));
    }
    let trimmed = raw.trim_start_matches("./").trim_end_matches('/');
    if trimmed.is_empty() || trimmed == "." {
        return Ok(PathBuf::new());
    }
    let mut clean = PathBuf::new();
    for component in Path::new(trimmed).components() {
        match component {
            Component::Normal(part) => {
                if part.is_empty() || part == "." || part == ".." {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "Refused invalid archive path component"));
                }
                clean.push(part);
            }
            _ => return Err(io::Error::new(io::ErrorKind::InvalidData, "Refused absolute or traversal archive path")),
        }
    }
    Ok(clean)
}

fn ensure_safe_dir(base: &Path, rel: &Path) -> io::Result<PathBuf> {
    let mut cur = base.to_path_buf();
    for comp in rel.components() {
        cur.push(comp);
        match fs::symlink_metadata(&cur) {
            Ok(meta) => {
                if meta.is_symlink() || !meta.is_dir() {
                    return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Refused symlink or non-directory in destination"));
                }
                #[cfg(windows)]
                {
                    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
                    if meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Refused reparse point directory in destination"));
                    }
                }
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => fs::create_dir(&cur)?,
            Err(err) => return Err(err),
        }
    }
    Ok(cur)
}

fn skip_exact(reader: &mut impl Read, mut count: usize) -> io::Result<()> {
    let mut buf = [0u8; 65_536];
    while count > 0 {
        let to_read = count.min(buf.len());
        reader.read_exact(&mut buf[..to_read])?;
        count -= to_read;
    }
    Ok(())
}

fn skip_tar_padding(reader: &mut impl Read, size: usize) -> io::Result<()> {
    let rem = size % 512;
    if rem != 0 {
        let mut pad = [0u8; 512];
        reader.read_exact(&mut pad[..512 - rem])?;
    }
    Ok(())
}

fn write_regular_file_streaming(dest: &Path, reader: &mut impl Read, size: usize, mode: u64) -> io::Result<()> {
    if let Ok(meta) = fs::symlink_metadata(dest) {
        if meta.is_symlink() {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Refused symlink target"));
        }
        #[cfg(windows)]
        {
            use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
            if meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Refused reparse point target"));
            }
        }
        return Err(io::Error::new(io::ErrorKind::AlreadyExists, "Duplicate archive entry"));
    }
    #[cfg(target_os = "linux")]
    let mut file = {
        let unix_mode = if mode & 0o100 != 0 { 0o755 } else { 0o644 };
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(unix_mode)
            .custom_flags(libc::O_NOFOLLOW)
            .open(dest)?
    };
    #[cfg(windows)]
    let mut file = {
        let _ = mode;
        OpenOptions::new().write(true).create_new(true).open(dest)?
    };
    #[cfg(not(any(target_os = "linux", windows)))]
    let mut file = {
        let _ = mode;
        OpenOptions::new().write(true).create_new(true).open(dest)?
    };
    let res = (|| -> io::Result<()> {
        let mut remaining = size;
        let mut buf = [0u8; 65_536];
        while remaining > 0 {
            let to_read = remaining.min(buf.len());
            reader.read_exact(&mut buf[..to_read])?;
            file.write_all(&buf[..to_read])?;
            remaining -= to_read;
        }
        file.sync_all()?;
        #[cfg(target_os = "linux")]
        {
            let unix_mode = if mode & 0o100 != 0 { 0o755 } else { 0o644 };
            file.set_permissions(fs::Permissions::from_mode(unix_mode))?;
        }
        Ok(())
    })();
    if res.is_err() {
        drop(file);
        let _ = fs::remove_file(dest);
    }
    res
}

pub fn extract_tar_file(archive: &Path, destination: &Path) -> io::Result<()> {
    let mut reader = OpenOptions::new().read(true).open(archive)?;
    if fs::symlink_metadata(destination).is_ok() {
        return Err(io::Error::new(io::ErrorKind::AlreadyExists, "Tar extraction target must be a fresh directory"));
    }
    fs::create_dir_all(destination)?;
    let res = (|| -> io::Result<()> {
        let mut long_name: Option<String> = None;
        let mut pax_path: Option<String> = None;

        loop {
            let mut header = [0u8; 512];
            let mut read_total = 0;
            while read_total < 512 {
                let n = reader.read(&mut header[read_total..])?;
                if n == 0 {
                    break;
                }
                read_total += n;
            }
            if read_total == 0 {
                break;
            }
            if read_total != 512 {
                return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "Truncated tar header"));
            }
            if header.iter().all(|&b| b == 0) {
                break;
            }
            verify_checksum(&header)?;

            let size = usize::try_from(parse_octal(&header[124..136])?)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Archive entry exceeds size limit"))?;
            if size > 512 * 1024 * 1024 {
                return Err(io::Error::new(io::ErrorKind::InvalidData, "Archive entry exceeds 512 MiB limit"));
            }
            let mode = parse_octal(&header[100..108])?;
            let typeflag = header[156];

            if typeflag == b'L' || typeflag == b'x' {
                if size > MAX_TAR_METADATA_BYTES {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "Tar metadata header exceeds size limit"));
                }
                let mut payload = vec![0u8; size];
                reader.read_exact(&mut payload)?;
                skip_tar_padding(&mut reader, size)?;
                if typeflag == b'L' {
                    let text = std::str::from_utf8(&payload)
                        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Invalid GNU long name"))?
                        .trim_end_matches('\0')
                        .to_owned();
                    long_name = Some(text);
                } else {
                    // Parse PAX path override if present.
                    let text = std::str::from_utf8(&payload).unwrap_or("");
                    for line in text.lines() {
                        if let Some((_, kv)) = line.split_once(' ') {
                            if let Some(val) = kv.strip_prefix("path=") {
                                pax_path = Some(val.to_owned());
                            }
                        }
                    }
                }
                continue;
            }
            if typeflag == b'g' {
                // Skip global extended headers without allocating.
                skip_exact(&mut reader, size)?;
                skip_tar_padding(&mut reader, size)?;
                continue;
            }

            let name = cstr(&header[0..100])?;
            let prefix = cstr(&header[345..500])?;
            let raw_path = if let Some(p) = pax_path.take() {
                let _ = long_name.take();
                p
            } else if let Some(l) = long_name.take() {
                l
            } else if prefix.is_empty() {
                name
            } else {
                format!("{prefix}/{name}")
            };
            let rel = sanitize_rel_path(&raw_path)?;

            match typeflag {
                b'5' => {
                    if size > 0 {
                        skip_exact(&mut reader, size)?;
                        skip_tar_padding(&mut reader, size)?;
                    }
                    if !rel.as_os_str().is_empty() {
                        ensure_safe_dir(destination, &rel)?;
                    }
                }
                b'0' | 0 => {
                    if rel.as_os_str().is_empty() {
                        return Err(io::Error::new(io::ErrorKind::InvalidData, "Refused unnamed tar regular file"));
                    }
                    if let Some(parent) = rel.parent() {
                        if !parent.as_os_str().is_empty() {
                            ensure_safe_dir(destination, parent)?;
                        }
                    }
                    write_regular_file_streaming(&destination.join(&rel), &mut reader, size, mode)?;
                    skip_tar_padding(&mut reader, size)?;
                }
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("Refused unsupported or link tar entry type '{}'", char::from(typeflag)),
                    ));
                }
            }
        }
        Ok(())
    })();
    if res.is_err() {
        let _ = fs::remove_dir_all(destination);
    }
    res
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    fn tar_entry(path: &str, typeflag: u8, mode: u32, body: &[u8]) -> Vec<u8> {
        let mut hdr = [0u8; 512];
        let name_bytes = path.as_bytes();
        hdr[..name_bytes.len()].copy_from_slice(name_bytes);
        hdr[100..108].copy_from_slice(format!("{mode:07o}\0").as_bytes());
        hdr[108..116].copy_from_slice(b"0000000\0");
        hdr[116..124].copy_from_slice(b"0000000\0");
        hdr[124..136].copy_from_slice(format!("{:011o}\0", body.len()).as_bytes());
        hdr[136..148].copy_from_slice(b"00000000000\0");
        hdr[148..156].copy_from_slice(b"        ");
        hdr[156] = typeflag;
        hdr[257..263].copy_from_slice(b"ustar\0");
        hdr[263..265].copy_from_slice(b"00");
        let cksum: u32 = hdr.iter().map(|&b| u32::from(b)).sum();
        hdr[148..156].copy_from_slice(format!("{cksum:06o}\0 ").as_bytes());
        let mut out = hdr.to_vec();
        out.extend_from_slice(body);
        let rem = body.len() % 512;
        if rem != 0 {
            out.extend(std::iter::repeat_n(0u8, 512 - rem));
        }
        out
    }

    #[test]
    fn extracts_regular_files_and_rejects_symlinks_or_traversal() {
        let root = std::env::temp_dir().join(format!("vidgrab-tar-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
        fs::create_dir_all(&root).unwrap();

        let good_tar = root.join("good.tar");
        let mut bytes = Vec::new();
        bytes.extend(tar_entry("engine/", b'5', 0o755, b""));
        bytes.extend(tar_entry("engine/vidgrab-engine", b'0', 0o755, b"#!/bin/sh\necho ok\n"));
        bytes.extend(vec![0u8; 1024]);
        fs::write(&good_tar, bytes).unwrap();

        let out_good = root.join("out-good");
        extract_tar_file(&good_tar, &out_good).unwrap();
        assert_eq!(fs::read(out_good.join("engine/vidgrab-engine")).unwrap(), b"#!/bin/sh\necho ok\n");

        // Symlink entry type b'2' must fail.
        let sym_tar = root.join("sym.tar");
        let mut sym = Vec::new();
        sym.extend(tar_entry("link", b'2', 0o777, b""));
        sym.extend(vec![0u8; 1024]);
        fs::write(&sym_tar, sym).unwrap();
        assert!(extract_tar_file(&sym_tar, &root.join("out-sym")).is_err());

        // Traversal entry must fail.
        let trav_tar = root.join("trav.tar");
        let mut trav = Vec::new();
        trav.extend(tar_entry("../escape", b'0', 0o644, b"bad"));
        trav.extend(vec![0u8; 1024]);
        fs::write(&trav_tar, trav).unwrap();
        assert!(extract_tar_file(&trav_tar, &root.join("out-trav")).is_err());

        let _ = fs::remove_dir_all(&root);
    }
}
