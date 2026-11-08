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
            "quit" if !browser && self.params.is_empty() => Ok(()),
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
            "status" | "activate" | "quit" => Err("BAD_REQUEST"),
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
        "UNSUPPORTED_OPERATION" => "This operation is unavailable in the lifecycle spike",
        "APP_MISSING" => "The installed companion could not be launched",
        "APP_UNAVAILABLE" => "The companion did not become ready before the deadline",
        "ENGINE_UNAVAILABLE" => "The engine is unavailable; reopen the app to try again",
        "BROWSER_DISCONNECTED" => "Browser access is disabled; use Connect browsers in VidGrab",
        "BROWSER_CONNECTION_UNAVAILABLE" => "The browser connection settings could not be read; open VidGrab to check them",
        "REQUEST_OUTCOME_UNKNOWN" => "The request may have been accepted; check the queue before trying again",
        "BAD_FRAME" => "The native message is incomplete, oversized or invalid JSON",
        _ => "The companion request was rejected",
    };
    json!({"spike_version": 1, "id": id, "ok": false, "error": {"code": code, "message": message}})
}
