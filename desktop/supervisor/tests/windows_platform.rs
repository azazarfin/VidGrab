#![cfg(windows)]
use std::io::{Read, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant};
use vidgrab_supervisor::windows::{self, Pipe, PipeListener};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0u8; 8];
        getrandom::fill(&mut random).unwrap();
        let path =
            std::env::temp_dir().join(format!("vidgrab pipe বাংলা {:x}", u64::from_ne_bytes(random)));
        windows::private_directory(&path).unwrap();
        Self(path)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn pipe_has_one_owner_and_kernel_verified_same_user_peers() {
    let root = Fixture::new();
    let path = windows::pipe_path(&root.0);
    let mut server = PipeListener::bind(&path).unwrap();
    assert!(
        PipeListener::bind(&path).is_err(),
        "A second owner must not replace the pipe"
    );
    assert_eq!(
        server.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    let mut client = Pipe::connect(&path).unwrap();
    let (mut peer, _) = server.accept().unwrap();
    assert!(windows::same_user(&client).unwrap());
    assert!(windows::same_user(&peer).unwrap());
    client.write_all(b"hello").unwrap();
    let mut bytes = [0; 5];
    peer.read_exact(&mut bytes).unwrap();
    assert_eq!(&bytes, b"hello");
    peer.write_all(b"reply").unwrap();
    drop(peer);
    client.read_exact(&mut bytes).unwrap();
    assert_eq!(
        &bytes, b"reply",
        "Closing the server must preserve its queued response"
    );
}

#[test]
fn incomplete_pipe_frame_obeys_the_request_deadline() {
    let root = Fixture::new();
    let path = windows::pipe_path(&root.0);
    let mut server = PipeListener::bind(&path).unwrap();
    let mut client = Pipe::connect(&path).unwrap();
    let (mut peer, _) = server.accept().unwrap();
    peer.set_read_timeout(Some(Duration::from_millis(80)))
        .unwrap();
    client.write_all(&[1]).unwrap();
    let start = Instant::now();
    let error = peer.read_exact(&mut [0; 4]).unwrap_err();
    assert_eq!(error.kind(), std::io::ErrorKind::TimedOut);
    assert!(start.elapsed() < Duration::from_secs(1));
}

#[test]
fn private_directories_reject_reparse_points() {
    let root = Fixture::new();
    let target = root.0.join("target");
    windows::private_directory(&target).unwrap();
    let alias = root.0.join("alias");
    use std::os::windows::process::CommandExt;
    let result = std::process::Command::new("cmd.exe")
        .args(["/d", "/c"])
        .raw_arg(format!(
            "mklink /J \"{}\" \"{}\"",
            alias.display(),
            target.display()
        ))
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(
        windows::private_directory(&alias).unwrap_err().kind(),
        std::io::ErrorKind::PermissionDenied
    );
    std::fs::remove_dir(alias).unwrap();
}

#[test]
fn private_file_has_the_account_sid_even_in_an_elevated_session() {
    let root = Fixture::new();
    let file = root.0.join("owned.json");
    windows::create_private_file(&file)
        .unwrap()
        .write_all(b"private")
        .unwrap();
    windows::check_owned(&file).unwrap();
    assert!(windows::is_private(&file).unwrap());
}

#[test]
fn owner_lock_prevents_two_supervisors_and_releases_without_starting_the_engine() {
    use vidgrab_supervisor::{Config, Owner};
    let root = Fixture::new();
    let config = Config {
        app: root.0.join("absent-app.exe"),
        engine: root.0.join("absent-engine.exe"),
        data_dir: root.0.join("state"),
        download_dir: root.0.join("media"),
        chrome_origin: String::new(),
        firefox_id: String::new(),
    };
    let first = Owner::start(config.clone()).unwrap().unwrap();
    assert!(Owner::start(config.clone()).unwrap().is_none());
    drop(first);
    assert!(Owner::start(config).unwrap().is_some());
}
