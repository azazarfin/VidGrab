//! Windows kernel boundaries for private IPC and owned engine descendants.
mod pipe;
pub(crate) mod process;
pub(crate) mod registry;
mod security;
pub use pipe::{pipe_path, same_user, Pipe, PipeListener};
pub use process::activate;
pub use security::{
    check_directory, check_owned, create_private_file, is_private, private_directory,
};
pub(crate) use security::{wide, Handle};
use std::fs::{File, OpenOptions};
use std::io;
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::path::Path;
use windows_sys::Win32::Storage::FileSystem::{
    MoveFileExW, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT,
    MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
};

pub(crate) fn open_lock(path: &Path) -> io::Result<File> {
    check_directory(
        path.parent()
            .ok_or_else(|| io::Error::other("Missing lock directory"))?,
    )?;
    match create_private_file(path) {
        Ok(file) => return Ok(file),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error),
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)?;
    if file.metadata()?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || !file.metadata()?.is_file()
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Owner lock must be a regular file",
        ));
    }
    check_owned(path)?;
    Ok(file)
}
pub(crate) fn replace(source: &Path, destination: &Path) -> io::Result<()> {
    let source = wide(source.as_os_str())?;
    let destination = wide(destination.as_os_str())?;
    if unsafe {
        MoveFileExW(
            source.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
