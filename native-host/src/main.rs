use std::io;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
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
            let mut command = Command::new(config.app());
            command.arg(config.launch_arg()).arg(path).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
            // A new POSIX session keeps browser-owned process-group termination separate.
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() == -1 { return Err(io::Error::last_os_error()); }
                    Ok(())
                });
            }
            if command.spawn().is_err() { return protocol::failure(Some(&request.id), "APP_MISSING"); }
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

    #[test]
    fn disconnected_packaged_host_rejects_before_launching_or_contacting_the_owner() {
        let root = std::env::temp_dir().join(format!("vidgrab-native-disconnected-{}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        let path = root.join("host-config.json");
        let config: HostConfig = serde_json::from_value(json!({"package_version":1,
            "app":root.join("missing-app"), "data_dir":root.join("state"), "download_dir":root.join("media"),
            "chrome_origin":CHROME_ORIGIN, "firefox_id":FIREFOX_ID})).unwrap();
        std::fs::write(root.join("browser-connection.json"), br#"{"connection_version":1,"enabled":false}"#).unwrap();
        let request = protocol::Request { spike_version:1, id:"disconnected".into(), op:"status".into(), params:Default::default() };
        let response = bridge(&config, &path, &request);
        std::fs::remove_dir_all(&root).unwrap();
        assert_eq!(response["id"], "disconnected");
        assert_eq!(response["error"]["code"], "BROWSER_DISCONNECTED");
    }
}
