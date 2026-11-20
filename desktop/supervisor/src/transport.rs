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

pub const DEFAULT_IPC_READ_TIMEOUT: Duration = Duration::from_secs(4);
pub const RESTART_IPC_READ_TIMEOUT: Duration = Duration::from_secs(20);
pub const PROBE_IPC_READ_TIMEOUT: Duration = Duration::from_secs(30);
pub const DEFAULT_ENGINE_READ_TIMEOUT: Duration = Duration::from_secs(2);
pub const PROBE_ENGINE_READ_TIMEOUT: Duration = Duration::from_secs(25);

pub fn call(config: &Config, request: &protocol::Request) -> io::Result<Value> {
    call_socket(&config.socket_path(), request)
}

pub fn ipc_read_timeout(request: &protocol::Request) -> Duration {
    if request.is_probe_request() {
        PROBE_IPC_READ_TIMEOUT
    } else if request.op == "restart_engine" {
        RESTART_IPC_READ_TIMEOUT
    } else {
        DEFAULT_IPC_READ_TIMEOUT
    }
}

pub fn call_socket(path: &Path, request: &protocol::Request) -> io::Result<Value> {
    call_socket_with_timeout(path, request, ipc_read_timeout(request))
}

pub fn call_socket_with_timeout(path: &Path, request: &protocol::Request, read_timeout: Duration) -> io::Result<Value> {
    let mut connection = UnixStream::connect(path)?;
    connection.set_read_timeout(Some(read_timeout))?;
    connection.set_write_timeout(Some(Duration::from_secs(2)))?;
    if !same_user(&connection)? {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "IPC belongs to another user"));
    }
    protocol::write_message(&mut connection, request)?;
    protocol::read_message(&mut connection)?.ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "No IPC response"))
}

pub fn engine_request(port: u16, token: &str, method: &str, path: &str, body: Option<Value>) -> io::Result<Value> {
    engine_request_with_timeout(port, token, method, path, body, DEFAULT_ENGINE_READ_TIMEOUT)
}

pub fn engine_request_with_timeout(port: u16, token: &str, method: &str, path: &str, body: Option<Value>, read_timeout: Duration) -> io::Result<Value> {
    let expected_id = body.as_ref().and_then(|value| value.get("request_id")).cloned();
    let address = format!("127.0.0.1:{port}").parse().unwrap();
    let mut connection = TcpStream::connect_timeout(&address, Duration::from_secs(2))?;
    connection.set_read_timeout(Some(read_timeout))?;
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

    #[test]
    fn slow_probe_survives_extended_engine_timeout_after_short_timeout_fails() {
        let probe_request = protocol::Request {
            spike_version: 1,
            id: "probe-slow".into(),
            op: "v1.request".into(),
            params: serde_json::json!({"protocol_version":1,"request_id":"probe-slow","operation":"probes.create","params":{"url":"https://fixture.invalid/media"}})
                .as_object().unwrap().clone(),
        };
        assert!(probe_request.is_probe_request());
        assert_eq!(ipc_read_timeout(&probe_request), PROBE_IPC_READ_TIMEOUT);
        assert!(PROBE_IPC_READ_TIMEOUT > PROBE_ENGINE_READ_TIMEOUT);
        let fast_request = protocol::Request {
            spike_version: 1,
            id: "fast-op".into(),
            op: "v1.request".into(),
            params: serde_json::json!({"protocol_version":1,"request_id":"fast-op","operation":"settings.get","params":{}})
                .as_object().unwrap().clone(),
        };
        assert!(!fast_request.is_probe_request());
        assert_eq!(ipc_read_timeout(&fast_request), DEFAULT_IPC_READ_TIMEOUT);

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            for _ in 0..2 {
                let (mut stream, _) = listener.accept().unwrap();
                stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                let mut request = Vec::new();
                loop {
                    let mut part = [0u8; 1024];
                    let received = stream.read(&mut part).unwrap();
                    if received == 0 { break; }
                    request.extend_from_slice(&part[..received]);
                    if let Some(separator) = request.windows(4).position(|part| part == b"\r\n\r\n") {
                        let headers = std::str::from_utf8(&request[..separator]).unwrap();
                        let length = headers.lines().find_map(|line| line.strip_prefix("Content-Length: ")).unwrap().parse::<usize>().unwrap();
                        if request.len() >= separator + 4 + length { break; }
                    }
                }
                std::thread::sleep(Duration::from_millis(220));
                let body = serde_json::json!({"protocol_version":1,"request_id":"probe-slow","ok":false,
                    "error":{"code":"UNSUPPORTED_MEDIA","request_id":"probe-slow","message":"Unsupported",
                        "retryable":false,"retry_at":null,"provenance":"local","details":{}}}).to_string();
                let _ = write!(stream, "HTTP/1.1 422 Unprocessable Entity\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
            }
        });
        let payload = Some(serde_json::Value::Object(probe_request.params.clone()));
        let timed_out = engine_request_with_timeout(port, "fixture-token", "POST", "/v1/requests", payload.clone(), Duration::from_millis(100));
        assert!(timed_out.is_err(), "Short timeout must expire while server delays");
        let succeeded = engine_request_with_timeout(port, "fixture-token", "POST", "/v1/requests", payload, PROBE_ENGINE_READ_TIMEOUT);
        server.join().unwrap();
        assert_eq!(succeeded.unwrap()["error"]["code"], "UNSUPPORTED_MEDIA");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn slow_probe_ipc_survives_extended_timeout_after_short_timeout_fails() {
        use std::os::unix::net::UnixListener;
        let socket_path = std::env::temp_dir().join(format!("vidgrab-probe-ipc-{}.sock", std::process::id()));
        let _ = std::fs::remove_file(&socket_path);
        let listener = UnixListener::bind(&socket_path).unwrap();
        let probe_request = protocol::Request {
            spike_version: 1,
            id: "ipc-slow".into(),
            op: "v1.request".into(),
            params: serde_json::json!({"protocol_version":1,"request_id":"ipc-slow","operation":"probes.create","params":{"url":"https://fixture.invalid/media"}})
                .as_object().unwrap().clone(),
        };
        let server = std::thread::spawn(move || {
            for _ in 0..2 {
                let (mut stream, _) = listener.accept().unwrap();
                let request: protocol::Request = protocol::read_message(&mut stream).unwrap().unwrap();
                std::thread::sleep(Duration::from_millis(220));
                let _ = protocol::write_message(&mut stream, &protocol::failure(Some(&request.id), "ENGINE_UNAVAILABLE"));
            }
        });
        let timed_out = call_socket_with_timeout(&socket_path, &probe_request, Duration::from_millis(100));
        assert!(timed_out.is_err());
        let succeeded = call_socket_with_timeout(&socket_path, &probe_request, ipc_read_timeout(&probe_request));
        server.join().unwrap();
        let _ = std::fs::remove_file(&socket_path);
        assert_eq!(succeeded.unwrap()["error"]["code"], "ENGINE_UNAVAILABLE");
    }

    #[test]
    fn restart_engine_uses_extended_ipc_read_timeout() {
        let restart_request = protocol::Request {
            spike_version: 1,
            id: "restart-1".into(),
            op: "restart_engine".into(),
            params: Default::default(),
        };
        assert!(!restart_request.is_probe_request());
        assert_eq!(ipc_read_timeout(&restart_request), RESTART_IPC_READ_TIMEOUT);
        assert!(RESTART_IPC_READ_TIMEOUT > Duration::from_secs(12));
    }
}
