//! P02 ownership spike with private Linux and Windows kernel boundaries.
#[cfg(not(any(target_os = "linux", windows)))]
compile_error!("The P02 native lifecycle spike supports Linux and Windows");

mod engine;
#[cfg(target_os = "linux")]
mod linux_process;
#[cfg(windows)]
pub mod windows;
pub mod installation;
pub mod protocol;
pub mod transport;

use engine::Engine;
use serde::Deserialize;
use std::fs::{self, File, TryLockError};
#[cfg(target_os = "linux")]
use std::fs::OpenOptions;
use std::io;
#[cfg(target_os = "linux")]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
#[cfg(target_os = "linux")]
use std::os::unix::net::UnixListener as Listener;
#[cfg(windows)]
use windows::PipeListener as Listener;
use std::path::{Path, PathBuf};
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};
use std::time::Duration;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub app: PathBuf,
    pub engine: PathBuf,
    pub data_dir: PathBuf,
    pub download_dir: PathBuf,
    pub chrome_origin: String,
    pub firefox_id: String,
}

impl Config {
    pub fn load(path: &Path) -> io::Result<Self> {
        let config: Self = serde_json::from_slice(&fs::read(path)?).map_err(io::Error::other)?;
        if [&config.app, &config.engine, &config.data_dir, &config.download_dir].iter().any(|path| !path.is_absolute()) {
            return Err(io::Error::other("Companion paths must be absolute"));
        }
        Ok(config)
    }
    pub fn ipc_dir(&self) -> PathBuf { self.data_dir.join("ipc") }
    pub fn socket_path(&self) -> PathBuf {
        #[cfg(target_os = "linux")] { self.ipc_dir().join("control.sock") }
        #[cfg(windows)] { windows::pipe_path(&self.data_dir) }
    }
}

#[derive(Clone, Copy)]
pub enum UiEvent { Activate, Stopped }

pub struct Owner {
    _lock: File,
    config: Config,
    listener: Listener,
    engine: Engine,
    stop: Arc<AtomicBool>,
}

impl Owner {
    pub fn start(config: Config) -> io::Result<Option<Self>> {
        fs::create_dir_all(&config.data_dir)?;
        let ipc = config.ipc_dir();
        #[cfg(target_os = "linux")]
        let lock = {
            match fs::create_dir(&ipc) {
                Ok(()) => fs::set_permissions(&ipc, fs::Permissions::from_mode(0o700))?,
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                    let metadata = fs::symlink_metadata(&ipc)?;
                    if !metadata.is_dir() || metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o777 != 0o700 {
                        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "IPC directory must be private and owned by this user"));
                    }
                }
                Err(error) => return Err(error),
            }
            OpenOptions::new().create(true).truncate(false).read(true).write(true).mode(0o600).open(ipc.join("owner.lock"))?
        };
        #[cfg(windows)]
        let lock = { windows::private_directory(&ipc)?; windows::open_lock(&ipc.join("owner.lock"))? };
        match lock.try_lock() {
            Ok(()) => (),
            Err(TryLockError::WouldBlock) => return Ok(None),
            Err(TryLockError::Error(error)) => return Err(error),
        }
        let socket = config.socket_path();
        #[cfg(target_os = "linux")]
        if socket.exists() { fs::remove_file(&socket)?; }
        let listener = Listener::bind(&socket)?;
        #[cfg(target_os = "linux")]
        {
            fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
            listener.set_nonblocking(true)?;
            // Orphaned descendants become reappable children of this Linux owner.
            if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1) } == -1 { return Err(io::Error::last_os_error()); }
        }
        Ok(Some(Self { _lock: lock, config, listener, engine: Engine::new(), stop: Arc::new(AtomicBool::new(false)) }))
    }

    pub fn stop_handle(&self) -> Arc<AtomicBool> { self.stop.clone() }

    pub fn run(mut self, notify: impl Fn(UiEvent)) {
        while !self.stop.load(Ordering::Acquire) {
            self.engine.tick(&self.config);
            match self.listener.accept() {
                Ok((mut connection, _)) => {
                    let result = (|| -> io::Result<()> {
                        connection.set_read_timeout(Some(Duration::from_secs(2)))?;
                        connection.set_write_timeout(Some(Duration::from_secs(2)))?;
                        if !transport::same_user(&connection)? { return Ok(()); }
                        let request: protocol::Request = match protocol::read_message(&mut connection)? {
                            Some(value) => value, None => return Ok(()),
                        };
                        let response = if let Err(code) = request.validate(false) {
                            protocol::failure(Some(&request.id), code)
                        } else if request.op == "quit" {
                            self.stop.store(true, Ordering::Release);
                            protocol::success(&request, self.engine.snapshot())
                        } else if request.op == "quit_if_idle" {
                            match self.engine.request("status", &Default::default()) {
                                Ok(data) if data.get("downloads_active").and_then(serde_json::Value::as_bool) == Some(false) => {
                                    self.stop.store(true, Ordering::Release);
                                    protocol::success(&request, data)
                                }
                                Ok(_) => protocol::failure(Some(&request.id), "DOWNLOADS_ACTIVE"),
                                Err(_) => protocol::failure(Some(&request.id), "ENGINE_UNAVAILABLE"),
                            }
                        } else if request.op == "activate" {
                            notify(UiEvent::Activate);
                            protocol::success(&request, self.engine.snapshot())
                        } else if request.op == "status" && self.engine.state != "ready" {
                            protocol::success(&request, self.engine.snapshot())
                        } else {
                            match self.engine.request(&request.op, &request.params) {
                                Ok(data) => protocol::success(&request, data),
                                Err(_) => protocol::failure(Some(&request.id), "ENGINE_UNAVAILABLE"),
                            }
                        };
                        protocol::write_message(&mut connection, &response)
                    })();
                    if let Err(error) = result { eprintln!("Companion IPC failed: {error}"); }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => std::thread::sleep(Duration::from_millis(25)),
                Err(error) => { eprintln!("Companion listener failed: {error}"); break; }
            }
        }
        self.engine = Engine::new(); // Drop and reap the owned group before releasing the lock.
        #[cfg(target_os = "linux")]
        let _ = fs::remove_file(self.config.socket_path());
        drop(self.listener);
        drop(self._lock);
        notify(UiEvent::Stopped);
    }
}
