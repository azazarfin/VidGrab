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
    let expected_id = body.as_ref().and_then(|value| value.get("request_id")).cloned();
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
    if !headers.lines().next().unwrap_or_default().starts_with("HTTP/1.1 ") {
        return Err(io::Error::other("Invalid engine HTTP version"));
    }
    let status = headers.lines().next().unwrap_or_default().split_whitespace().nth(1)
        .and_then(|value| value.parse::<u16>().ok()).ok_or_else(|| io::Error::other("Invalid engine HTTP status"))?;
    if path != "/v1/requests" && status != 200 {
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
    let value: Value = serde_json::from_slice(payload).map_err(io::Error::other)?;
    if path == "/v1/requests" {
        let successful = (200..300).contains(&status);
        if !crate::contract::validate_response(&value) || value["ok"] != successful
            || value.get("request_id") != expected_id.as_ref() {
            return Err(io::Error::other("Invalid versioned engine response"));
        }
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versioned_http_conflicts_keep_their_typed_payload() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut request = Vec::new();
            loop {
                let mut part = [0u8; 1024];
                let received = stream.read(&mut part).unwrap();
                assert!(received > 0 && request.len() + received <= 8192);
                request.extend_from_slice(&part[..received]);
                if let Some(separator) = request.windows(4).position(|part| part == b"\r\n\r\n") {
                    let headers = std::str::from_utf8(&request[..separator]).unwrap();
                    let length = headers.lines().find_map(|line| line.strip_prefix("Content-Length: ")).unwrap().parse::<usize>().unwrap();
                    if request.len() >= separator + 4 + length { break; }
                }
            }
            let body = serde_json::json!({"protocol_version":1,"request_id":"contract","ok":false,
                "error":{"code":"REVISION_CONFLICT","request_id":"contract","message":"Reload settings",
                    "retryable":false,"retry_at":null,"provenance":"local","details":{}}}).to_string();
            write!(stream, "HTTP/1.1 409 Conflict\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
        });
        let response = engine_request(port, "fixture-token", "POST", "/v1/requests",
            Some(serde_json::json!({"protocol_version":1,"request_id":"contract","operation":"settings.get","params":{}})));
        server.join().unwrap();
        assert!(response.is_ok(), "A settings conflict must retain its typed result: {response:?}");
        assert_eq!(response.unwrap()["error"]["code"], "REVISION_CONFLICT");
    }
}
