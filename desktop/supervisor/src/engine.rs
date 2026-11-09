use crate::{transport, Config};
use serde::Deserialize;
use serde_json::{json, Value};
use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
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

struct OwnedProcess {
    child: Child,
    group: i32,
}

impl OwnedProcess {
    fn stop(&mut self) {
        // The group is created for this child; it contains only owned descendants.
        unsafe { libc::kill(-self.group, libc::SIGTERM); }
        let deadline = Instant::now() + Duration::from_millis(1500);
        while Instant::now() < deadline {
            if self.child.try_wait().ok().flatten().is_some() {
                break;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        // A dead leader can leave FFmpeg/Deno descendants. Kill the group too.
        unsafe { libc::kill(-self.group, libc::SIGKILL); }
        let _ = self.child.wait();
        let deadline = Instant::now() + Duration::from_millis(500);
        while Instant::now() < deadline {
            let result = unsafe { libc::waitpid(-self.group, std::ptr::null_mut(), libc::WNOHANG) };
            if result < 0 {
                break;
            }
            if result == 0 {
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
}

impl Drop for OwnedProcess {
    fn drop(&mut self) { self.stop(); }
}

pub(crate) struct Engine {
    process: Option<OwnedProcess>,
    token: String,
    port: u16,
    pub generation: u8,
    pub state: &'static str,
    next_restart: Option<Instant>,
    components: Value,
}

impl Engine {
    pub fn new() -> Self {
        Self { process: None, token: String::new(), port: 0, generation: 0,
            state: "starting", next_restart: Some(Instant::now()), components: Value::Null }
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
        let mut command = Command::new(&config.engine);
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(log).process_group(0);
        // Only async-signal-safe syscalls run in the child before exec.
        unsafe {
            command.pre_exec(|| {
                let parent = libc::getppid();
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) == -1 {
                    return Err(io::Error::last_os_error());
                }
                if libc::getppid() != parent || parent == 1 {
                    return Err(io::Error::other("Supervisor exited during engine launch"));
                }
                Ok(())
            });
        }
        let child = command.spawn()?;
        let mut owned = OwnedProcess { group: child.id() as i32, child };
        let bootstrap = json!({"token": self.token, "data_dir": config.data_dir, "download_dir": config.download_dir});
        let mut input = owned.child.stdin.take().unwrap();
        writeln!(input, "{bootstrap}")?;
        drop(input);
        let output = owned.child.stdout.take().unwrap();
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
        if line.len() > 4096 || ready.spike_version != 1 || ready.pid != owned.child.id() || ready.port == 0 {
            return Err(io::Error::other("Invalid engine readiness message"));
        }
        transport::engine_request(ready.port, &self.token, "GET", "/health", None)?;
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

    pub fn tick(&mut self, config: &Config) {
        if self.process.as_mut().is_some_and(|process| !matches!(process.child.try_wait(), Ok(None))) {
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
            "engine_pid": self.process.as_ref().map(|process| process.child.id()),
            "engine_generation": self.generation, "components": self.components})
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
            _ => Err(io::Error::other("Unsupported engine operation")),
        }
    }
}
