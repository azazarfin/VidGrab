use std::io;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

pub(crate) struct OwnedProcess {
    child: Child,
    group: i32,
}

impl OwnedProcess {
    pub fn spawn(
        executable: &std::path::Path,
        _args: &[std::ffi::OsString],
        log: std::fs::File,
    ) -> io::Result<Self> {
        let mut command = Command::new(executable);
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(log)
            .process_group(0);
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
        Ok(Self {
            group: child.id() as i32,
            child,
        })
    }
    pub fn id(&self) -> u32 {
        self.child.id()
    }
    pub fn take_input(&mut self) -> Box<dyn std::io::Write + Send> {
        Box::new(self.child.stdin.take().unwrap())
    }
    pub fn take_output(&mut self) -> Box<dyn std::io::Read + Send> {
        Box::new(self.child.stdout.take().unwrap())
    }
    pub fn has_exited(&mut self) -> io::Result<bool> {
        self.child.try_wait().map(|value| value.is_some())
    }

    fn stop(&mut self) {
        // The group is created for this child; it contains only owned descendants.
        unsafe {
            libc::kill(-self.group, libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_millis(1500);
        while Instant::now() < deadline {
            if self.child.try_wait().ok().flatten().is_some() {
                break;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        // A dead leader can leave FFmpeg/Deno descendants. Kill the group too.
        unsafe {
            libc::kill(-self.group, libc::SIGKILL);
        }
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
    fn drop(&mut self) {
        self.stop();
    }
}
