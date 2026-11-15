use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{self, Read, Write};

pub const MAX_MESSAGE: usize = 65_536;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub spike_version: u16,
    pub id: String,
    pub op: String,
    #[serde(default)]
    pub params: serde_json::Map<String, Value>,
}

impl Request {
    pub fn validate(&self, browser: bool) -> Result<(), &'static str> {
        if self.spike_version != 1 {
            return Err("INCOMPATIBLE_VERSION");
        }
        if self.id.is_empty() || self.id.len() > 256 {
            return Err("BAD_REQUEST");
        }
        match self.op.as_str() {
            "status" | "activate" if self.params.is_empty() => Ok(()),
            "quit" | "quit_if_idle" if !browser && self.params.is_empty() => Ok(()),
            "fixture.download" => {
                let url = self.params.get("url").and_then(Value::as_str).ok_or("BAD_REQUEST")?;
                let local = url.strip_prefix("http://127.0.0.1:").ok_or("BAD_REQUEST")?;
                let (port, path) = local.split_once('/').ok_or("BAD_REQUEST")?;
                let port = port.parse::<u16>().map_err(|_| "BAD_REQUEST")?;
                if self.params.len() != 1 || port == 0 || path != "fixture.wav" || url.len() > 2048 {
                    return Err("BAD_REQUEST");
                }
                Ok(())
            }
            "v1.request" => {
                let value = Value::Object(self.params.clone());
                crate::contract::validate_request(&value)?;
                if value["request_id"] != self.id { return Err("BAD_REQUEST"); }
                Ok(())
            }
            "status" | "activate" | "quit" | "quit_if_idle" => Err("BAD_REQUEST"),
            _ => Err("UNSUPPORTED_OPERATION"),
        }
    }
}

pub fn read_message<T: serde::de::DeserializeOwned>(reader: &mut impl Read) -> io::Result<Option<T>> {
    let mut header = [0_u8; 4];
    if reader.read(&mut header[..1])? == 0 {
        return Ok(None);
    }
    reader.read_exact(&mut header[1..])?;
    let size = u32::from_ne_bytes(header) as usize;
    if size == 0 || size > MAX_MESSAGE {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "Native message exceeds the spike limit"));
    }
    let mut payload = vec![0; size];
    reader.read_exact(&mut payload)?;
    serde_json::from_slice(&payload).map(Some).map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

pub fn write_message(writer: &mut impl Write, message: &impl Serialize) -> io::Result<()> {
    let payload = serde_json::to_vec(message)?;
    if payload.is_empty() || payload.len() > MAX_MESSAGE {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "Response exceeds the spike limit"));
    }
    writer.write_all(&(payload.len() as u32).to_ne_bytes())?;
    writer.write_all(&payload)?;
    writer.flush()
}

pub fn success(request: &Request, data: Value) -> Value {
    json!({"spike_version": 1, "id": request.id, "ok": true, "data": data})
}

pub fn failure(id: Option<&str>, code: &str) -> Value {
    let message = match code {
        "ORIGIN_REJECTED" => "This browser extension is not allowed to connect",
        "INCOMPATIBLE_VERSION" => "The companion and extension spike versions differ",
        "APP_INCOMPATIBLE" => "Update VidGrab and the browser extension to compatible versions",
        "BAD_REQUEST" => "The request does not match the supported companion contract",
        "UNSUPPORTED_OPERATION" => "This operation is unavailable in the lifecycle spike",
        "APP_MISSING" => "The installed companion could not be launched",
        "APP_UNAVAILABLE" => "The companion did not become ready before the deadline",
        "ENGINE_UNAVAILABLE" => "The engine is unavailable; reopen the app to try again",
        "DOWNLOADS_ACTIVE" => "Finish active downloads before removing VidGrab",
        "BROWSER_DISCONNECTED" => "Browser access is disabled; use Connect browsers in VidGrab",
        "BROWSER_CONNECTION_UNAVAILABLE" => "The browser connection settings could not be read; open VidGrab to check them",
        "REQUEST_OUTCOME_UNKNOWN" => "The request may have been accepted; check the queue before trying again",
        "BAD_FRAME" => "The native message is incomplete, oversized or invalid JSON",
        _ => "The companion request was rejected",
    };
    json!({"spike_version": 1, "id": id, "ok": false, "error": {"code": code, "message": message}})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idle_removal_shutdown_is_available_only_to_the_local_desktop() {
        let request = Request { spike_version: 1, id: "remove".into(), op: "quit_if_idle".into(), params: Default::default() };
        assert_eq!(request.validate(false), Ok(()));
        assert!(request.validate(true).is_err(), "Browser requests cannot shut down the app for removal");
    }

    #[test]
    fn active_downloads_have_an_actionable_removal_error() {
        assert_eq!(failure(Some("remove"), "DOWNLOADS_ACTIVE")["error"]["message"],
            "Finish active downloads before removing VidGrab");
    }

    #[test]
    fn versioned_control_is_available_to_browser_and_desktop() {
        let request = Request { spike_version: 1, id: "contract".into(), op: "v1.request".into(),
            params: json!({"protocol_version":1,"request_id":"contract","operation":"settings.get","params":{}}).as_object().unwrap().clone() };
        assert_eq!(request.validate(true), Ok(()));
        assert_eq!(request.validate(false), Ok(()));
    }

    #[test]
    fn versioned_settings_validate_before_owner_activation() {
        let request = Request { spike_version: 1, id: "contract".into(), op: "v1.request".into(),
            params: json!({"protocol_version":1,"request_id":"contract","operation":"settings.patch","idempotency_key":"stable",
                "params":{"expected_revision":true,"patch":{"max_concurrent":4}}}).as_object().unwrap().clone() };
        assert_eq!(request.validate(true), Err("BAD_REQUEST"));
    }

    #[test]
    fn versioned_incompatibility_has_an_upgrade_message() {
        let request = Request { spike_version: 1, id: "contract".into(), op: "v1.request".into(),
            params: json!({"protocol_version":2,"request_id":"contract","operation":"settings.get","params":{}}).as_object().unwrap().clone() };
        assert_eq!(request.validate(true), Err("APP_INCOMPATIBLE"));
        assert!(failure(Some("contract"), "APP_INCOMPATIBLE")["error"]["message"].as_str().unwrap().contains("Update"));
    }

    #[test]
    fn versioned_payload_reserves_space_for_native_compatibility_framing() {
        let request = Request { spike_version: 1, id: "contract".into(), op: "v1.request".into(),
            params: json!({"protocol_version":1,"request_id":"contract","operation":"settings.patch","idempotency_key":"stable",
                "params":{"expected_revision":0,"patch":{"default_format":"a".repeat(64900)}}}).as_object().unwrap().clone() };
        assert_eq!(request.validate(true), Err("MESSAGE_TOO_LARGE"));
    }
}
