use crate::{transport, Config};
use serde::Deserialize;
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::sync::mpsc;
use std::time::{Duration, Instant};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Ready {
    spike_version: u16,
    port: u16,
    pid: u32,
    #[serde(default)]
    components: Value,
}

#[cfg(target_os = "linux")]
use crate::linux_process::OwnedProcess;
#[cfg(windows)]
use crate::windows::process::OwnedProcess;

pub(crate) struct Engine {
    process: Option<OwnedProcess>,
    token: String,
    port: u16,
    pub generation: u8,
    pub state: &'static str,
    next_restart: Option<Instant>,
    components: Value,
    protocol_range: Option<(u16, u16)>,
}

impl Engine {
    pub fn new() -> Self {
        Self { process: None, token: String::new(), port: 0, generation: 0,
            state: "starting", next_restart: Some(Instant::now()), components: Value::Null, protocol_range: None }
    }

    fn launch(&mut self, config: &Config) -> io::Result<()> {
        self.generation += 1;
        self.state = "starting";
        let mut random = [0_u8; 32];
        getrandom::fill(&mut random).map_err(io::Error::other)?;
        self.token = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let logs = config.data_dir.join("logs");
        std::fs::create_dir_all(&logs)?;
        let log = OpenOptions::new().create(true).append(true).open(logs.join("engine.log"))?;
        let mut owned = OwnedProcess::spawn(&config.engine, &[], log)?;
        let bootstrap = json!({"token": self.token, "data_dir": config.data_dir, "download_dir": config.download_dir});
        let mut input = owned.take_input();
        writeln!(input, "{bootstrap}")?;
        drop(input);
        let output = owned.take_output();
        let (sender, receiver) = mpsc::sync_channel(1);
        let reader = std::thread::spawn(move || {
            let mut line = Vec::new();
            let result = BufReader::new(output).take(4097).read_until(b'\n', &mut line).map(|_| line);
            let _ = sender.send(result);
        });
        let line = receiver.recv_timeout(Duration::from_secs(12))
            .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "Engine readiness deadline exceeded"))??;
        let _ = reader.join();
        let ready: Ready = serde_json::from_slice(&line).map_err(io::Error::other)?;
        if line.len() > 4096 || ready.spike_version != 1 || ready.pid != owned.id() || ready.port == 0 {
            return Err(io::Error::other("Invalid engine readiness message"));
        }
        transport::engine_request(ready.port, &self.token, "GET", "/health", None)?;
        // Preserve legacy readiness framing. An old engine remains usable for
        // lifecycle fixtures, but cannot receive versioned control mutations.
        self.protocol_range = transport::engine_request(ready.port, &self.token, "POST", "/v1/requests",
            Some(json!({"protocol_version":1,"request_id":"engine-negotiation","operation":"capabilities.get","params":{}})))
            .ok().filter(|reply| reply["ok"] == true).and_then(|reply| {
                let minimum = u16::try_from(reply["data"]["protocol_min"].as_u64()?).ok()?;
                let maximum = u16::try_from(reply["data"]["protocol_max"].as_u64()?).ok()?;
                (minimum > 0 && minimum <= maximum).then_some((minimum, maximum))
            });
        self.port = ready.port;
        self.components = ready.components;
        self.process = Some(owned);
        self.next_restart = None;
        self.state = "ready";
        Ok(())
    }

    fn schedule_restart(&mut self) {
        self.process.take();
        if self.generation >= 4 {
            self.state = "failed";
            self.next_restart = None;
        } else {
            self.state = "recovering";
            self.next_restart = Some(Instant::now() + Duration::from_millis(250 * (1 << self.generation.saturating_sub(1))));
        }
    }

    pub fn restart_now(&mut self, config: &Config) -> io::Result<()> {
        self.process.take();
        self.generation = 0;
        self.state = "starting";
        self.next_restart = None;
        self.protocol_range = None;
        if let Err(error) = self.launch(config) {
            self.schedule_restart();
            return Err(error);
        }
        Ok(())
    }

    pub fn tick(&mut self, config: &Config) {
        if self.process.as_mut().is_some_and(|process| !matches!(process.has_exited(), Ok(false))) {
            self.schedule_restart();
        }
        if self.next_restart.is_some_and(|deadline| Instant::now() >= deadline) {
            if let Err(error) = self.launch(config) {
                eprintln!("Engine start failed: {error}");
                self.schedule_restart();
            }
        }
    }

    pub fn snapshot(&self) -> Value {
        json!({"state": self.state, "supervisor_pid": std::process::id(),
            "engine_pid": self.process.as_ref().map(|process| process.id()),
            "engine_generation": self.generation, "components": self.components,
            "protocol_min": self.protocol_range.map(|range| range.0), "protocol_max": self.protocol_range.map(|range| range.1)})
    }

    pub fn supports_protocol(&self, version: u16) -> bool {
        self.protocol_range.is_some_and(|(minimum, maximum)| (minimum..=maximum).contains(&version))
    }

    pub fn request(&self, operation: &str, params: &serde_json::Map<String, Value>) -> io::Result<Value> {
        if self.state != "ready" { return Err(io::Error::other("Engine is not ready")); }
        match operation {
            "status" => {
                let mut snapshot = self.snapshot();
                let response = transport::engine_request(self.port, &self.token, "GET", "/spike/status", None)?;
                snapshot["jobs"] = response["jobs"].clone();
                snapshot["downloads_active"] = response["downloads_active"].clone();
                Ok(snapshot)
            }
            "fixture.download" => transport::engine_request(self.port, &self.token, "POST", "/spike/fixture", Some(Value::Object(params.clone()))),
            "v1.request" => {
                let timeout = if params.get("operation").and_then(Value::as_str) == Some("probes.create") {
                    transport::PROBE_ENGINE_READ_TIMEOUT
                } else {
                    transport::DEFAULT_ENGINE_READ_TIMEOUT
                };
                transport::engine_request_with_timeout(self.port, &self.token, "POST", "/v1/requests", Some(Value::Object(params.clone())), timeout)
            }
            _ => Err(io::Error::other("Unsupported engine operation")),
        }
    }
}
