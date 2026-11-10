use std::ffi::{c_void, OsStr};
use std::io;
use std::os::windows::io::FromRawHandle;
use std::os::windows::{ffi::OsStrExt, fs::MetadataExt};
use std::path::Path;
use std::ptr::null_mut;
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Security::Authorization::*;
use windows_sys::Win32::Security::*;
use windows_sys::Win32::Storage::FileSystem::{CreateDirectoryW, FILE_ATTRIBUTE_REPARSE_POINT};
use windows_sys::Win32::System::SystemServices::ACCESS_ALLOWED_ACE_TYPE;
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, OpenProcess, OpenProcessToken, PROCESS_QUERY_LIMITED_INFORMATION,
};

#[derive(Debug)]
pub(crate) struct Handle(pub HANDLE);
impl Handle {
    pub fn new(raw: HANDLE) -> io::Result<Self> {
        if raw.is_null() || raw == INVALID_HANDLE_VALUE {
            Err(io::Error::last_os_error())
        } else {
            Ok(Self(raw))
        }
    }
    pub fn into_raw(self) -> HANDLE {
        let raw = self.0;
        std::mem::forget(self);
        raw
    }
}
// An owned kernel handle can move to the owner thread; it is never inherited.
unsafe impl Send for Handle {}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

pub(crate) fn wide(value: &OsStr) -> io::Result<Vec<u16>> {
    let mut value: Vec<_> = value.encode_wide().collect();
    if value.contains(&0) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "NUL in a Windows path",
        ));
    }
    value.push(0);
    Ok(value)
}

pub(crate) fn user_sid(process: HANDLE) -> io::Result<Vec<u8>> {
    let mut token = null_mut();
    if unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let token = Handle::new(token)?;
    let mut size = 0;
    unsafe {
        GetTokenInformation(token.0, TokenUser, null_mut(), 0, &mut size);
    }
    if size == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut storage = vec![0usize; (size as usize).div_ceil(std::mem::size_of::<usize>())];
    if unsafe {
        GetTokenInformation(
            token.0,
            TokenUser,
            storage.as_mut_ptr().cast(),
            size,
            &mut size,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let token_user = unsafe { &*storage.as_ptr().cast::<TOKEN_USER>() };
    let length = unsafe { GetLengthSid(token_user.User.Sid) };
    let mut sid = vec![0u8; length as usize];
    if unsafe { CopySid(length, sid.as_mut_ptr().cast(), token_user.User.Sid) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(sid)
}
pub(crate) fn current_sid() -> io::Result<Vec<u8>> {
    user_sid(unsafe { GetCurrentProcess() })
}
pub(crate) fn process_same_user(pid: u32) -> io::Result<bool> {
    let process = Handle::new(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) })?;
    let peer = user_sid(process.0)?;
    let owner = current_sid()?;
    Ok(unsafe { EqualSid(peer.as_ptr() as *mut c_void, owner.as_ptr() as *mut c_void) } != 0)
}
pub(crate) fn sid_text() -> io::Result<String> {
    let sid = current_sid()?;
    let mut text = null_mut();
    if unsafe { ConvertSidToStringSidW(sid.as_ptr() as *mut c_void, &mut text) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut length = 0;
    unsafe {
        while *text.add(length) != 0 {
            length += 1;
        }
    }
    let value = String::from_utf16(unsafe { std::slice::from_raw_parts(text, length) })
        .map_err(io::Error::other);
    unsafe {
        LocalFree(text.cast());
    }
    value
}

pub(crate) struct Descriptor(PSECURITY_DESCRIPTOR);
impl Descriptor {
    pub fn private() -> io::Result<Self> {
        let sid = sid_text()?;
        let text = wide(OsStr::new(&format!("O:{sid}D:P(A;OICI;GA;;;{sid})")))?;
        let mut descriptor = null_mut();
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                text.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(Self(descriptor))
    }
    pub fn attributes(&self) -> SECURITY_ATTRIBUTES {
        SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: self.0,
            bInheritHandle: 0,
        }
    }
}
impl Drop for Descriptor {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.0);
        }
    }
}

pub fn create_private_file(path: &Path) -> io::Result<std::fs::File> {
    use windows_sys::Win32::Storage::FileSystem::*;
    check_directory(
        path.parent()
            .ok_or_else(|| io::Error::other("Missing private file directory"))?,
    )?;
    let descriptor = Descriptor::private()?;
    let attributes = descriptor.attributes();
    let path = wide(path.as_os_str())?;
    let handle = Handle::new(unsafe {
        CreateFileW(
            path.as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            &attributes,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            null_mut(),
        )
    })?;
    Ok(unsafe { std::fs::File::from_raw_handle(handle.into_raw()) })
}

struct FileSecurity {
    descriptor: PSECURITY_DESCRIPTOR,
    owner: PSID,
    dacl: *mut ACL,
}
impl FileSecurity {
    fn read(path: &Path) -> io::Result<Self> {
        let path = wide(path.as_os_str())?;
        let mut descriptor = null_mut();
        let mut owner = null_mut();
        let mut dacl = null_mut();
        let code = unsafe {
            GetNamedSecurityInfoW(
                path.as_ptr(),
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                null_mut(),
                &mut dacl,
                null_mut(),
                &mut descriptor,
            )
        };
        if code != 0 {
            return Err(io::Error::from_raw_os_error(code as i32));
        }
        Ok(Self {
            descriptor,
            owner,
            dacl,
        })
    }
    fn own(&self) -> io::Result<bool> {
        let sid = current_sid()?;
        Ok(unsafe { EqualSid(self.owner, sid.as_ptr() as *mut c_void) } != 0)
    }
}
impl Drop for FileSecurity {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.descriptor);
        }
    }
}
pub fn check_owned(path: &Path) -> io::Result<()> {
    if FileSecurity::read(path)?.own()? {
        Ok(())
    } else {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "File belongs to another Windows user",
        ))
    }
}
pub fn is_private(path: &Path) -> io::Result<bool> {
    let security = FileSecurity::read(path)?;
    if !security.own()? || security.dacl.is_null() {
        return Ok(false);
    }
    let mut control = 0;
    let mut revision = 0;
    if unsafe { GetSecurityDescriptorControl(security.descriptor, &mut control, &mut revision) }
        == 0
    {
        return Err(io::Error::last_os_error());
    }
    if control & SE_DACL_PROTECTED == 0 {
        return Ok(false);
    }
    let own = current_sid()?;
    for index in 0..unsafe { (*security.dacl).AceCount } as u32 {
        let mut ace = null_mut();
        if unsafe { GetAce(security.dacl, index, &mut ace) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let header = unsafe { &*ace.cast::<ACE_HEADER>() };
        if header.AceType != ACCESS_ALLOWED_ACE_TYPE as u8
            || (header.AceSize as usize) < std::mem::size_of::<ACCESS_ALLOWED_ACE>()
        {
            return Ok(false);
        }
        let allowed = unsafe { &*ace.cast::<ACCESS_ALLOWED_ACE>() };
        let sid = std::ptr::addr_of!(allowed.SidStart) as PSID;
        if unsafe { EqualSid(sid, own.as_ptr() as *mut c_void) } == 0 {
            return Ok(false);
        }
    }
    Ok(unsafe { (*security.dacl).AceCount } > 0)
}
pub fn check_directory(path: &Path) -> io::Result<()> {
    for parent in path.ancestors() {
        match std::fs::symlink_metadata(parent) {
            Ok(metadata)
                if !metadata.is_dir()
                    || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 =>
            {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "Private directories must not be reparse points or files",
                ))
            }
            Ok(_) => (),
            Err(error) if error.kind() == io::ErrorKind::NotFound => (),
            Err(error) => return Err(error),
        }
    }
    Ok(())
}
pub fn private_directory(path: &Path) -> io::Result<()> {
    check_directory(path)?;
    if path.exists() {
        if !is_private(path)? {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "Directory must be private and owned by this Windows user",
            ));
        }
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let path_w = wide(path.as_os_str())?;
    let descriptor = Descriptor::private()?;
    let attributes = descriptor.attributes();
    if unsafe { CreateDirectoryW(path_w.as_ptr(), &attributes) } == 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(ERROR_ALREADY_EXISTS as i32) {
            return Err(error);
        }
    }
    if !is_private(path)? {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Private directory creation failed",
        ));
    }
    Ok(())
}
