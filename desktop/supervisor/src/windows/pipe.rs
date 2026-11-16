use super::security::{process_same_user, sid_text, Descriptor};
use super::{wide, Handle};
use sha2::{Digest, Sha256};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Storage::FileSystem::*;
use windows_sys::Win32::System::Pipes::*;

pub fn pipe_path(data: &Path) -> PathBuf {
    let data = data.canonicalize().unwrap_or_else(|_| data.to_path_buf());
    let text = data
        .to_string_lossy()
        .trim_start_matches(r"\\?\")
        .replace('/', r"\")
        .to_lowercase();
    let sid = sid_text().unwrap_or_default(); // Opening still requires a successfully constructed same-user DACL.
    PathBuf::from(format!(
        r"\\.\pipe\vidgrab-p02-{:x}",
        Sha256::digest(format!("{sid}\0{text}").as_bytes())
    ))
}
fn create(path: &Path, first: bool) -> io::Result<Handle> {
    let path = wide(path.as_os_str())?;
    let descriptor = Descriptor::private()?;
    let attributes = descriptor.attributes();
    Handle::new(unsafe {
        CreateNamedPipeW(
            path.as_ptr(),
            PIPE_ACCESS_DUPLEX
                | if first {
                    FILE_FLAG_FIRST_PIPE_INSTANCE
                } else {
                    0
                },
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS,
            16,
            65540,
            65540,
            2000,
            &attributes,
        )
    })
}
#[derive(Debug)]
pub struct PipeListener {
    path: PathBuf,
    handle: Handle,
}
impl PipeListener {
    pub fn bind(path: &Path) -> io::Result<Self> {
        Ok(Self {
            path: path.into(),
            handle: create(path, true)?,
        })
    }
    pub fn accept(&mut self) -> io::Result<(Pipe, ())> {
        if unsafe { ConnectNamedPipe(self.handle.0, null_mut()) } == 0 {
            let code = unsafe { GetLastError() };
            match code {
                ERROR_PIPE_CONNECTED => (),
                ERROR_PIPE_LISTENING => return Err(io::ErrorKind::WouldBlock.into()),
                ERROR_NO_DATA => {
                    unsafe {
                        DisconnectNamedPipe(self.handle.0);
                    }
                    return Err(io::ErrorKind::WouldBlock.into());
                }
                _ => return Err(io::Error::from_raw_os_error(code as i32)),
            }
        }
        let next = create(&self.path, false)?;
        let handle = std::mem::replace(&mut self.handle, next);
        Ok((Pipe::new(handle, true), ()))
    }
}
#[derive(Debug)]
pub struct Pipe {
    handle: Handle,
    server: bool,
    read_deadline: Instant,
    write_deadline: Instant,
}
impl Pipe {
    fn new(handle: Handle, server: bool) -> Self {
        Self {
            handle,
            server,
            read_deadline: Instant::now() + Duration::from_secs(4),
            write_deadline: Instant::now() + Duration::from_secs(2),
        }
    }
    pub fn connect(path: &Path) -> io::Result<Self> {
        if !path.to_string_lossy().starts_with(r"\\.\pipe\vidgrab-p02-") {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "Only local VidGrab pipes are permitted",
            ));
        }
        let path = wide(path.as_os_str())?;
        let deadline = Instant::now() + Duration::from_secs(2);
        let handle = loop {
            match Handle::new(unsafe {
                CreateFileW(
                    path.as_ptr(),
                    GENERIC_READ | GENERIC_WRITE,
                    0,
                    null(),
                    OPEN_EXISTING,
                    SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                    null_mut(),
                )
            }) {
                Ok(handle) => break handle,
                Err(error) if error.raw_os_error() == Some(ERROR_PIPE_BUSY as i32) => {
                    // The listener creates its next instance after accepting a
                    // client. Concurrent requests may arrive before it is ready.
                    // This wait happens before any request bytes are written.
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    if remaining.is_zero() {
                        return Err(io::Error::new(io::ErrorKind::TimedOut, "IPC connect deadline exceeded"));
                    }
                    let milliseconds = remaining.as_millis().max(1).min(u32::MAX as u128) as u32;
                    if unsafe { WaitNamedPipeW(path.as_ptr(), milliseconds) } == 0 {
                        let error = io::Error::last_os_error();
                        if error.raw_os_error() == Some(ERROR_SEM_TIMEOUT as i32) {
                            return Err(io::Error::new(io::ErrorKind::TimedOut, "IPC connect deadline exceeded"));
                        }
                        return Err(error);
                    }
                }
                Err(error) => return Err(error),
            }
        };
        let mode = PIPE_READMODE_BYTE | PIPE_NOWAIT;
        if unsafe { SetNamedPipeHandleState(handle.0, &mode, null(), null()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self::new(handle, false))
    }
    pub fn set_read_timeout(&mut self, value: Option<Duration>) -> io::Result<()> {
        self.read_deadline = Instant::now() + value.unwrap_or(Duration::from_secs(4));
        Ok(())
    }
    pub fn set_write_timeout(&mut self, value: Option<Duration>) -> io::Result<()> {
        self.write_deadline = Instant::now() + value.unwrap_or(Duration::from_secs(2));
        Ok(())
    }
}
pub fn same_user(pipe: &Pipe) -> io::Result<bool> {
    let mut pid = 0;
    let result = unsafe {
        if pipe.server {
            GetNamedPipeClientProcessId(pipe.handle.0, &mut pid)
        } else {
            GetNamedPipeServerProcessId(pipe.handle.0, &mut pid)
        }
    };
    if result == 0 {
        return Err(io::Error::last_os_error());
    }
    process_same_user(pid)
}
impl Read for Pipe {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        loop {
            let mut size = 0;
            if unsafe {
                ReadFile(
                    self.handle.0,
                    bytes.as_mut_ptr(),
                    bytes.len().min(u32::MAX as usize) as u32,
                    &mut size,
                    null_mut(),
                )
            } != 0
            {
                return Ok(size as usize);
            }
            let code = unsafe { GetLastError() };
            if code == ERROR_BROKEN_PIPE {
                return Ok(0);
            }
            if code != ERROR_NO_DATA {
                return Err(io::Error::from_raw_os_error(code as i32));
            }
            if Instant::now() >= self.read_deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "IPC read deadline exceeded",
                ));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}
impl Write for Pipe {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        loop {
            let mut size = 0;
            if unsafe {
                WriteFile(
                    self.handle.0,
                    bytes.as_ptr(),
                    bytes.len().min(u32::MAX as usize) as u32,
                    &mut size,
                    null_mut(),
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            if size != 0 {
                return Ok(size as usize);
            }
            if Instant::now() >= self.write_deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "IPC write deadline exceeded",
                ));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn listener() -> (PathBuf, PipeListener) {
        let unique = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let path = PathBuf::from(format!(r"\\.\pipe\vidgrab-p02-busy-test-{}-{unique}", std::process::id()));
        let mut listener = PipeListener::bind(&path).unwrap();
        assert_eq!(listener.accept().unwrap_err().kind(), io::ErrorKind::WouldBlock);
        (path, listener)
    }

    #[test]
    fn connect_waits_for_the_next_listener_before_any_write() {
        let (path, mut listener) = listener();
        let first = Pipe::connect(&path).unwrap();
        let second = std::thread::spawn(move || Pipe::connect(&path));
        std::thread::sleep(Duration::from_millis(200));
        let accepted = listener.accept().unwrap();
        let _ = listener.accept();
        let result = second.join().unwrap();
        assert!(result.is_ok(), "A concurrent client must wait for an available pipe: {result:?}");
        drop((result, first, accepted));
    }

    #[test]
    fn a_busy_pipe_connection_has_a_bounded_prewrite_deadline() {
        let (path, _listener) = listener();
        let _first = Pipe::connect(&path).unwrap();
        let started = Instant::now();
        let error = Pipe::connect(&path).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut, "The busy connection must wait: {error:?}");
        assert!(started.elapsed() < Duration::from_secs(3));
    }
}
