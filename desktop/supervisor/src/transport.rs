use crate::{protocol, Config};
use serde_json::Value;
use std::io::{self, Read, Write};
use std::net::TcpStream;
#[cfg(target_os = "linux")]
use std::os::fd::AsRawFd;
#[cfg(target_os = "linux")]
use std::os::unix::net::UnixStream;
#[cfg(windows)]
use crate::windows::Pipe as UnixStream;
#[cfg(windows)]
pub use crate::windows::same_user;
use std::path::Path;
use std::time::Duration;

#[cfg(target_os = "linux")]
pub fn same_user(stream: &UnixStream) -> io::Result<bool> {
    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SO_PEERCRED reads kernel-provided credentials for this connected socket.
    let result = unsafe {
        libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
            (&mut credentials as *mut libc::ucred).cast(), &mut length)
    };
    if result == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(credentials.uid == unsafe { libc::geteuid() })
}

pub fn call(config: &Config, request: &protocol::Request) -> io::Result<Value> {
    call_socket(&config.socket_path(), request)
}

pub fn call_socket(path: &Path, request: &protocol::Request) -> io::Result<Value> {
    let mut connection = UnixStream::connect(path)?;
    connection.set_read_timeout(Some(Duration::from_secs(4)))?;
    connection.set_write_timeout(Some(Duration::from_secs(2)))?;
    if !same_user(&connection)? {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "IPC belongs to another user"));
    }
    protocol::write_message(&mut connection, request)?;
    protocol::read_message(&mut connection)?.ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "No IPC response"))
}

pub fn engine_request(port: u16, token: &str, method: &str, path: &str, body: Option<Value>) -> io::Result<Value> {
    let address = format!("127.0.0.1:{port}").parse().unwrap();
    let mut connection = TcpStream::connect_timeout(&address, Duration::from_secs(2))?;
    connection.set_read_timeout(Some(Duration::from_secs(2)))?;
    connection.set_write_timeout(Some(Duration::from_secs(2)))?;
    let body = body.map(|value| value.to_string()).unwrap_or_default();
    write!(connection, "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}", body.len())?;
    let mut response = Vec::new();
    (&mut connection).take((protocol::MAX_MESSAGE + 8192) as u64).read_to_end(&mut response)?;
    let separator = response.windows(4).position(|part| part == b"\r\n\r\n")
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "Invalid engine HTTP response"))?;
    let headers = std::str::from_utf8(&response[..separator]).map_err(io::Error::other)?;
    if !headers.lines().next().unwrap_or_default().starts_with("HTTP/1.1 200 ") {
        return Err(io::Error::other("Engine rejected the request"));
    }
    let payload = &response[separator + 4..];
    if payload.len() > protocol::MAX_MESSAGE {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "Engine response exceeds the spike limit"));
    }
    let expected = headers.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("content-length").then(|| value.trim().parse::<usize>().ok()).flatten()
    });
    if expected != Some(payload.len()) {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "Incomplete engine response"));
    }
    serde_json::from_slice(payload).map_err(io::Error::other)
}
