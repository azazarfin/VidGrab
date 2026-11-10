#![cfg(windows)]
use std::io;
use std::os::windows::{io::AsRawHandle, process::CommandExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::{Duration, Instant};
use windows_sys::Win32::{
    Foundation::*,
    System::{JobObjects::*, Threading::*},
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut nonce = [0; 8];
        getrandom::fill(&mut nonce).unwrap();
        let path = std::env::temp_dir().join(format!(
            "vidgrab activation বাংলা {:x}",
            u64::from_ne_bytes(nonce)
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::write(self.0.join("release"), b"");
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
struct KernelHandle(HANDLE);
impl Drop for KernelHandle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
fn wait_file(path: &Path) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !path.exists() {
        assert!(Instant::now() < deadline, "Missing {}", path.display());
        std::thread::sleep(Duration::from_millis(10));
    }
}
fn wait_child(child: &mut Child) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(status.success());
            return;
        }
        assert!(Instant::now() < deadline, "Activation helper did not exit");
        std::thread::sleep(Duration::from_millis(10));
    }
}
fn launch_in_job(limits: u32, succeeds: bool, remains_in_job: bool) {
    let root = Fixture::new();
    let job = KernelHandle(unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) });
    assert!(!job.0.is_null());
    let mut information: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    information.BasicLimitInformation.LimitFlags = limits;
    assert_ne!(
        unsafe {
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                std::ptr::from_ref(&information).cast(),
                std::mem::size_of_val(&information) as u32,
            )
        },
        0
    );
    let mut launcher = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "activation_job_launcher_helper", "--ignored"])
        .env("VIDGRAB_ACTIVATION_FIXTURE", &root.0)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .unwrap();
    wait_file(&root.0.join("launcher-ready"));
    assert_ne!(
        unsafe { AssignProcessToJobObject(job.0, launcher.as_raw_handle()) },
        0
    );
    std::fs::write(root.0.join("launch"), b"").unwrap();
    wait_file(&root.0.join("outcome"));
    let outcome = std::fs::read_to_string(root.0.join("outcome")).unwrap();
    wait_child(&mut launcher);
    if !succeeds {
        assert_eq!(outcome, "PermissionDenied");
        assert!(!root.0.join("target-ready").exists());
        return;
    }
    assert_eq!(outcome, "started", "{outcome}");
    wait_file(&root.0.join("target-ready"));
    let record: serde_json::Value =
        serde_json::from_slice(&std::fs::read(root.0.join("target-ready")).unwrap()).unwrap();
    let target = unsafe {
        OpenProcess(
            PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
            0,
            record["pid"].as_u64().unwrap() as u32,
        )
    };
    assert!(!target.is_null());
    let target = KernelHandle(target);
    let mut in_job = 0;
    assert_ne!(unsafe { IsProcessInJob(target.0, job.0, &mut in_job) }, 0);
    assert_eq!(in_job != 0, remains_in_job);
    drop(job);
    assert_eq!(
        unsafe { WaitForSingleObject(target.0, 100) },
        WAIT_TIMEOUT,
        "Closing the launcher's job must retain the companion"
    );
    std::fs::write(root.0.join("release"), b"").unwrap();
    assert_eq!(
        unsafe { WaitForSingleObject(target.0, 10000) },
        WAIT_OBJECT_0
    );
}

#[test]
fn activation_survives_a_non_breakaway_job_without_kill_on_close() {
    launch_in_job(0, true, true);
}
#[test]
fn activation_breaks_away_from_a_job_that_allows_it() {
    launch_in_job(
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK,
        true,
        false,
    );
}
#[test]
fn activation_uses_automatic_breakaway_when_the_job_requires_it() {
    launch_in_job(
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
        true,
        false,
    );
}
#[test]
fn activation_refuses_a_kill_on_close_job_without_breakaway() {
    launch_in_job(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, false, false);
}

#[test]
#[ignore = "Invoked only by an owned activation fixture"]
fn activation_job_launcher_helper() {
    let root = PathBuf::from(std::env::var_os("VIDGRAB_ACTIVATION_FIXTURE").unwrap());
    std::fs::write(root.join("launcher-ready"), b"").unwrap();
    wait_file(&root.join("launch"));
    let result = vidgrab_supervisor::windows::activate(
        &std::env::current_exe().unwrap(),
        "--exact",
        Path::new("activation_target_helper"),
    );
    let outcome = result
        .map(|_| "started".to_string())
        .unwrap_or_else(|error: io::Error| format!("{:?}", error.kind()));
    std::fs::write(root.join("outcome"), outcome).unwrap();
}
#[test]
fn activation_target_helper() {
    let Some(root) = std::env::var_os("VIDGRAB_ACTIVATION_FIXTURE") else {
        return;
    };
    let root = PathBuf::from(root);
    let mut in_job = 0;
    assert_ne!(
        unsafe { IsProcessInJob(GetCurrentProcess(), std::ptr::null_mut(), &mut in_job) },
        0
    );
    std::fs::write(
        root.join("target-ready"),
        serde_json::json!({"pid":std::process::id(),"in_job":in_job != 0}).to_string(),
    )
    .unwrap();
    wait_file(&root.join("release"));
}
