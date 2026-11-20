//! Exact HKCU native-host registrations; foreign values and registry links are refused.
use super::wide;
use crate::installation::{ManifestState, HOST_NAME};
use std::ffi::OsStr;
use std::io;
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};
use windows_sys::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND};
use windows_sys::Win32::System::Registry::*;

pub(crate) struct Key(pub(crate) HKEY);
impl Drop for Key {
    fn drop(&mut self) {
        unsafe {
            RegCloseKey(self.0);
        }
    }
}
pub(crate) fn checked(code: u32) -> io::Result<()> {
    if code == 0 {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(code as i32))
    }
}
pub(crate) fn missing(code: u32) -> bool {
    code == ERROR_FILE_NOT_FOUND || code == ERROR_PATH_NOT_FOUND
}
pub(crate) fn open(path: &str, view: u32, access: u32) -> io::Result<Option<Key>> {
    let name = wide(OsStr::new(path))?;
    let mut key = null_mut();
    let code = unsafe {
        RegOpenKeyExW(
            HKEY_CURRENT_USER,
            name.as_ptr(),
            REG_OPTION_OPEN_LINK,
            access | view,
            &mut key,
        )
    };
    if missing(code) {
        return Ok(None);
    }
    checked(code)?;
    Ok(Some(Key(key)))
}
pub(crate) fn reject_links(path: &str, view: u32) -> io::Result<()> {
    let mut prefix = String::new();
    let link = wide(OsStr::new("SymbolicLinkValue"))?;
    for part in path.split('\\') {
        if !prefix.is_empty() {
            prefix.push('\\');
        }
        prefix.push_str(part);
        if let Some(key) = open(&prefix, view, KEY_QUERY_VALUE)? {
            let mut size = 0;
            let code = unsafe {
                RegQueryValueExW(
                    key.0,
                    link.as_ptr(),
                    null(),
                    null_mut(),
                    null_mut(),
                    &mut size,
                )
            };
            if code == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "Browser registration must not traverse a registry link",
                ));
            }
            if !missing(code) {
                checked(code)?;
            }
        }
    }
    Ok(())
}
fn counts(key: &Key) -> io::Result<(u32, u32)> {
    let mut children = 0;
    let mut values = 0;
    checked(unsafe {
        RegQueryInfoKeyW(
            key.0,
            null_mut(),
            null_mut(),
            null(),
            &mut children,
            null_mut(),
            null_mut(),
            &mut values,
            null_mut(),
            null_mut(),
            null_mut(),
            null_mut(),
        )
    })?;
    Ok((children, values))
}
fn read(path: &str, view: u32, expected: &Path) -> io::Result<ManifestState> {
    reject_links(path, view)?;
    let Some(key) = open(path, view, KEY_READ)? else {
        return Ok(ManifestState::Missing);
    };
    let (children, values) = counts(&key)?;
    if children != 0 || values > 1 {
        return Ok(ManifestState::Conflict);
    }
    let mut kind = 0;
    let mut size = 0;
    let code = unsafe { RegQueryValueExW(key.0, null(), null(), &mut kind, null_mut(), &mut size) };
    if missing(code) {
        return Ok(if values == 0 {
            ManifestState::Missing
        } else {
            ManifestState::Conflict
        });
    }
    checked(code)?;
    if kind != REG_SZ || size == 0 || size > 65536 || size % 2 != 0 {
        return Ok(ManifestState::Conflict);
    }
    let mut text = vec![0u16; size as usize / 2];
    checked(unsafe {
        RegQueryValueExW(
            key.0,
            null(),
            null(),
            &mut kind,
            text.as_mut_ptr().cast(),
            &mut size,
        )
    })?;
    if kind != REG_SZ
        || size as usize != text.len() * 2
        || text.last() != Some(&0)
        || text[..text.len() - 1].contains(&0)
    {
        return Ok(ManifestState::Conflict);
    }
    let path = String::from_utf16(&text[..text.len() - 1]).map_err(io::Error::other)?;
    Ok(if PathBuf::from(path) == expected {
        ManifestState::Registered
    } else {
        ManifestState::Conflict
    })
}
fn write(path: &str, view: u32, expected: &Path) -> io::Result<()> {
    let path = wide(OsStr::new(path))?;
    let value = wide(expected.as_os_str())?;
    let mut key = null_mut();
    checked(unsafe {
        RegCreateKeyExW(
            HKEY_CURRENT_USER,
            path.as_ptr(),
            0,
            null(),
            REG_OPTION_NON_VOLATILE,
            KEY_READ | KEY_WRITE | view,
            null(),
            &mut key,
            null_mut(),
        )
    })?;
    let key = Key(key);
    checked(unsafe {
        RegSetValueExW(
            key.0,
            null(),
            0,
            REG_SZ,
            value.as_ptr().cast(),
            (value.len() * 2) as u32,
        )
    })?;
    checked(unsafe { RegFlushKey(key.0) })
}
struct Registrations {
    keys: [String; 2],
    paths: [PathBuf; 2],
}
impl Registrations {
    fn new(manifests: &[(PathBuf, bool)]) -> Self {
        Self {
            keys: [
                format!(r"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"),
                format!(r"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}"),
            ],
            paths: [manifests[0].0.clone(), manifests[1].0.clone()],
        }
    }
    fn states(&self) -> io::Result<[ManifestState; 2]> {
        let mut states = [ManifestState::Missing, ManifestState::Missing];
        for (index, state) in states.iter_mut().enumerate() {
            let a = read(&self.keys[index], KEY_WOW64_32KEY, &self.paths[index])?;
            let b = read(&self.keys[index], KEY_WOW64_64KEY, &self.paths[index])?;
            *state = if a == ManifestState::Conflict || b == ManifestState::Conflict {
                ManifestState::Conflict
            } else if a == ManifestState::Registered && b == ManifestState::Registered {
                ManifestState::Registered
            } else {
                ManifestState::Missing
            };
        }
        Ok(states)
    }
    fn preflight(&self) -> io::Result<()> {
        if self.states()?.contains(&ManifestState::Conflict) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "An existing browser registry value belongs to another installation",
            ));
        }
        Ok(())
    }
    fn publish(&self) -> io::Result<()> {
        self.preflight()?;
        for index in 0..2 {
            for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY] {
                write(&self.keys[index], view, &self.paths[index])?;
            }
        }
        Ok(())
    }
    fn remove_owned(&self) -> io::Result<()> {
        for index in 0..2 {
            for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY] {
                if read(&self.keys[index], view, &self.paths[index])? != ManifestState::Registered {
                    continue;
                }
                if let Some(key) = open(&self.keys[index], view, KEY_READ | KEY_WRITE)? {
                    checked(unsafe { RegDeleteValueW(key.0, null()) })?;
                    checked(unsafe { RegFlushKey(key.0) })?;
                    if counts(&key)? == (0, 0) {
                        drop(key);
                        let name = wide(OsStr::new(&self.keys[index]))?;
                        let code =
                            unsafe { RegDeleteKeyExW(HKEY_CURRENT_USER, name.as_ptr(), view, 0) };
                        if !missing(code) {
                            checked(code)?;
                        }
                    }
                }
            }
        }
        Ok(())
    }
}
pub(crate) fn preflight(manifests: &[(PathBuf, bool)]) -> io::Result<()> {
    Registrations::new(manifests).preflight()
}
pub(crate) fn publish(manifests: &[(PathBuf, bool)]) -> io::Result<()> {
    Registrations::new(manifests).publish()
}
pub(crate) fn remove_owned(manifests: &[(PathBuf, bool)]) -> io::Result<()> {
    Registrations::new(manifests).remove_owned()
}
pub(crate) fn states(manifests: &[(PathBuf, bool)]) -> io::Result<[ManifestState; 2]> {
    Registrations::new(manifests).states()
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fixture {
        root: String,
        registrations: Registrations,
    }
    impl Fixture {
        fn new() -> Self {
            let mut nonce = [0u8; 8];
            getrandom::fill(&mut nonce).unwrap();
            let root = format!(r"Software\VidGrabP02Tests\{:x}", u64::from_ne_bytes(nonce));
            let registrations = Registrations {
                keys: [format!(r"{root}\Chrome"), format!(r"{root}\Firefox")],
                paths: [
                    PathBuf::from(r"E:\fixture space বাংলা\chrome.json"),
                    PathBuf::from(r"E:\fixture space বাংলা\firefox.json"),
                ],
            };
            Self {
                root,
                registrations,
            }
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let root = wide(OsStr::new(&self.root)).unwrap();
            unsafe {
                RegDeleteTreeW(HKEY_CURRENT_USER, root.as_ptr());
            }
        }
    }
    #[test]
    fn registry_round_trip_covers_both_views_and_removes_only_owned_values() {
        let fixture = Fixture::new();
        let r = &fixture.registrations;
        assert_eq!(
            r.states().unwrap(),
            [ManifestState::Missing, ManifestState::Missing]
        );
        r.publish().unwrap();
        assert_eq!(
            r.states().unwrap(),
            [ManifestState::Registered, ManifestState::Registered]
        );
        r.remove_owned().unwrap();
        assert_eq!(
            r.states().unwrap(),
            [ManifestState::Missing, ManifestState::Missing]
        );
    }
    #[test]
    fn foreign_registry_value_blocks_all_writes_and_survives_removal() {
        let fixture = Fixture::new();
        let r = &fixture.registrations;
        let foreign = Path::new(r"E:\foreign.json");
        write(&r.keys[0], KEY_WOW64_32KEY, foreign).unwrap();
        assert_eq!(
            r.publish().unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            read(&r.keys[1], KEY_WOW64_64KEY, &r.paths[1]).unwrap(),
            ManifestState::Missing
        );
        r.remove_owned().unwrap();
        assert_eq!(
            read(&r.keys[0], KEY_WOW64_32KEY, foreign).unwrap(),
            ManifestState::Registered
        );
    }
}
