use std::io;
#[cfg(target_os = "linux")]
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
#[cfg(target_os = "linux")]
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use vidgrab_supervisor::{installation::HostConfig, protocol, transport};

fn bridge(config: &HostConfig, path: &PathBuf, request: &protocol::Request) -> serde_json::Value {
    match config.browser_enabled(path) {
        Ok(true) => (),
        Ok(false) => return protocol::failure(Some(&request.id), "BROWSER_DISCONNECTED"),
        Err(_) => return protocol::failure(Some(&request.id), "BROWSER_CONNECTION_UNAVAILABLE"),
    }
    let handshake = protocol::Request { spike_version: 1, id: "native-handshake".into(), op: "status".into(), params: Default::default() };
    let connected = match transport::call_socket(&config.socket_path(), &handshake) {
        Ok(response) => Some(response),
        Err(_) => {
            #[cfg(target_os = "linux")]
            let mut command = Command::new(config.app());
            #[cfg(target_os = "linux")]
            command.arg(config.launch_arg()).arg(path).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
            // A new POSIX session keeps browser-owned process-group termination separate.
            #[cfg(target_os = "linux")]
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() == -1 { return Err(io::Error::last_os_error()); }
                    Ok(())
                });
            }
            #[cfg(target_os = "linux")]
            if command.spawn().is_err() { return protocol::failure(Some(&request.id), "APP_MISSING"); }
            #[cfg(windows)]
            if let Err(error) = vidgrab_supervisor::windows::activate(config.app(), config.launch_arg(), path) {
                let code = if error.kind() == io::ErrorKind::NotFound { "APP_MISSING" } else { "APP_UNAVAILABLE" };
                return protocol::failure(Some(&request.id), code);
            }
            let deadline = Instant::now() + Duration::from_secs(15);
            let mut connected = None;
            while Instant::now() < deadline {
                if let Ok(response) = transport::call_socket(&config.socket_path(), &handshake) { connected = Some(response); break; }
                std::thread::sleep(Duration::from_millis(50));
            }
            connected
        }
    };
    let Some(mut response) = connected else { return protocol::failure(Some(&request.id), "APP_UNAVAILABLE"); };
    response["id"] = serde_json::json!(request.id);
    if request.op == "status" || response["ok"] != true { return response; }
    if response["data"]["state"] != "ready" && request.op == "fixture.download" {
        return protocol::failure(Some(&request.id), "ENGINE_UNAVAILABLE");
    }
    // After forwarding a mutation, its outcome can be unknown. Never replay it.
    transport::call_socket(&config.socket_path(), request).unwrap_or_else(|_| protocol::failure(Some(&request.id), "REQUEST_OUTCOME_UNKNOWN"))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut arguments: Vec<_> = std::env::args_os().skip(1).collect();
    let config_path = if arguments.first().is_some_and(|arg| arg == "--config") {
        if arguments.len() < 3 { return Err("Missing isolated-test configuration/caller".into()); }
        let path = PathBuf::from(arguments.remove(1));
        arguments.remove(0);
        path
    } else {
        std::env::current_exe()?.parent().ok_or("Missing install directory")?.join("host-config.json")
    };
    let config = HostConfig::load(&config_path)?;
    let chrome = arguments.first().is_some_and(|arg| arg == config.chrome_origin());
    let firefox = arguments.len() >= 2 && PathBuf::from(&arguments[0]).is_absolute() && arguments[1] == config.firefox_id();
    let mut output = io::stdout().lock();
    if !chrome && !firefox {
        protocol::write_message(&mut output, &protocol::failure(None, "ORIGIN_REJECTED"))?;
        return Ok(());
    }
    let mut input = io::stdin().lock();
    loop {
        let request: protocol::Request = match protocol::read_message(&mut input) {
            Ok(Some(value)) => value,
            Ok(None) => break,
            Err(_) => {
                protocol::write_message(&mut output, &protocol::failure(None, "BAD_FRAME"))?;
                break;
            }
        };
        let response = match request.validate(true) {
            Ok(()) => bridge(&config, &config_path, &request),
            Err(code) => protocol::failure(Some(&request.id), code),
        };
        protocol::write_message(&mut output, &response)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use vidgrab_supervisor::installation::{CHROME_ORIGIN, FIREFOX_ID};

    #[cfg(windows)]
    #[test]
    fn windows_lost_mutation_reply_is_forwarded_once_without_relaunch() {
        use vidgrab_supervisor::windows::PipeListener;
        let mut nonce = [0u8; 8];
        fixture_nonce(&mut nonce);
        let root = std::env::temp_dir().join(format!("vidgrab-lost-reply-{:x}", u64::from_ne_bytes(nonce)));
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir(root.join("state")).unwrap();
        let config: HostConfig = serde_json::from_value(json!({
            "app":root.join("must-not-launch.exe"), "engine":root.join("missing-engine.exe"),
            "data_dir":root.join("state"), "download_dir":root.join("media"),
            "chrome_origin":CHROME_ORIGIN, "firefox_id":FIREFOX_ID
        })).unwrap();
        let mut listener = PipeListener::bind(&config.socket_path()).unwrap();
        let server = std::thread::spawn(move || {
            let mut operations = Vec::new();
            for _ in 0..2 {
                let deadline = Instant::now() + Duration::from_secs(8);
                let mut connection = loop {
                    match listener.accept() {
                        Ok((connection, _)) => break connection,
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                            assert!(Instant::now() < deadline);
                            std::thread::sleep(Duration::from_millis(5));
                        }
                        Err(error) => panic!("{error}"),
                    }
                };
                let request: protocol::Request = protocol::read_message(&mut connection).unwrap().unwrap();
                operations.push(request.op.clone());
                if request.op == "status" {
                    protocol::write_message(&mut connection, &protocol::success(&request, json!({"state":"ready"}))).unwrap();
                }
                // The fake owner accepts the mutation and loses its reply.
            }
            operations
        });
        let request = protocol::Request { spike_version:1, id:"lost-reply".into(), op:"fixture.download".into(),
            params:json!({"url":"http://127.0.0.1:8080/fixture.wav"}).as_object().unwrap().clone() };
        let response = bridge(&config, &root.join("host-config.json"), &request);
        assert_eq!(response["error"]["code"], "REQUEST_OUTCOME_UNKNOWN");
        assert_eq!(server.join().unwrap(), ["status", "fixture.download"]);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(windows)]
    fn fixture_nonce(bytes: &mut [u8;8]) {
        // No extra dependency: a process ID and monotonic counter own test paths.
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        *bytes = (((std::process::id() as u64) << 32) | NEXT.fetch_add(1, Ordering::Relaxed)).to_ne_bytes();
    }

    #[test]
    fn disconnected_packaged_host_rejects_before_launching_or_contacting_the_owner() {
        let root = std::env::temp_dir().join(format!("vidgrab-native-disconnected-{}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        let path = root.join("host-config.json");
        let config: HostConfig = serde_json::from_value(json!({"package_version":1,
            "app":root.join("missing-app"), "data_dir":root.join("state"), "download_dir":root.join("media"),
            "chrome_origin":CHROME_ORIGIN, "firefox_id":FIREFOX_ID})).unwrap();
        let preference = root.join("browser-connection.json");
        #[cfg(target_os = "linux")]
        std::fs::write(&preference, br#"{"connection_version":1,"enabled":false}"#).unwrap();
        #[cfg(windows)]
        {
            use std::io::Write;
            vidgrab_supervisor::windows::create_private_file(&preference).unwrap()
                .write_all(br#"{"connection_version":1,"enabled":false}"#).unwrap();
        }
        let request = protocol::Request { spike_version:1, id:"disconnected".into(), op:"status".into(), params:Default::default() };
        let response = bridge(&config, &path, &request);
        std::fs::remove_dir_all(&root).unwrap();
        assert_eq!(response["id"], "disconnected");
        assert_eq!(response["error"]["code"], "BROWSER_DISCONNECTED");
    }
}
