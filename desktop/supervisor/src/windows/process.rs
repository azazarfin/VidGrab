use super::{wide, Handle};
use std::ffi::OsString;
use std::fs::File;
use std::io;
use std::os::windows::io::{AsRawHandle, FromRawHandle};
use std::path::Path;
use std::ptr::{null, null_mut};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
use windows_sys::Win32::System::JobObjects::*;
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::*;

struct Attributes {
    storage: Vec<usize>,
    initialized: bool,
}
impl Attributes {
    fn new(handles: &[HANDLE]) -> io::Result<Self> {
        let mut size = 0;
        unsafe {
            InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut size);
        }
        if size == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut value = Self {
            storage: vec![0; (size as usize).div_ceil(std::mem::size_of::<usize>())],
            initialized: false,
        };
        if unsafe { InitializeProcThreadAttributeList(value.pointer(), 1, 0, &mut size) } == 0 {
            return Err(io::Error::last_os_error());
        }
        value.initialized = true;
        if unsafe {
            UpdateProcThreadAttribute(
                value.pointer(),
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr().cast(),
                std::mem::size_of_val(handles),
                null_mut(),
                null(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(value)
    }
    fn pointer(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        self.storage.as_mut_ptr().cast()
    }
}
impl Drop for Attributes {
    fn drop(&mut self) {
        if self.initialized {
            unsafe {
                DeleteProcThreadAttributeList(self.pointer());
            }
        }
    }
}
fn anonymous_pipe(parent_reads: bool) -> io::Result<(Handle, Handle)> {
    let mut read = null_mut();
    let mut write = null_mut();
    let attributes = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: null_mut(),
        bInheritHandle: 1,
    };
    if unsafe { CreatePipe(&mut read, &mut write, &attributes, 0) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let read = Handle::new(read)?;
    let write = Handle::new(write)?;
    let parent = if parent_reads { read.0 } else { write.0 };
    if unsafe { SetHandleInformation(parent, HANDLE_FLAG_INHERIT, 0) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok((read, write))
}
fn quoted_argument(value: &std::ffi::OsStr) -> io::Result<Vec<u16>> {
    let raw = wide(value)?;
    let mut result = vec![b'"' as u16];
    let mut slashes = 0;
    for &character in &raw[..raw.len() - 1] {
        if character == b'\\' as u16 {
            slashes += 1;
            continue;
        }
        if character == b'"' as u16 {
            result.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2 + 1));
        } else {
            result.extend(std::iter::repeat_n(b'\\' as u16, slashes));
        }
        slashes = 0;
        result.push(character);
    }
    result.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2));
    result.push(b'"' as u16);
    Ok(result)
}

fn activation_job_flags() -> io::Result<u32> {
    let mut in_job = 0;
    if unsafe { IsProcessInJob(GetCurrentProcess(), null_mut(), &mut in_job) } == 0 {
        return Err(io::Error::last_os_error());
    }
    if in_job == 0 {
        return Ok(0);
    }
    let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe {
        QueryInformationJobObject(
            null_mut(),
            JobObjectExtendedLimitInformation,
            std::ptr::from_mut(&mut limits).cast(),
            std::mem::size_of_val(&limits) as u32,
            null_mut(),
        )
    } == 0 {
        return Err(io::Error::last_os_error());
    }
    let flags = limits.BasicLimitInformation.LimitFlags;
    if flags & JOB_OBJECT_LIMIT_BREAKAWAY_OK != 0 {
        return Ok(CREATE_BREAKAWAY_FROM_JOB);
    }
    if flags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK != 0 {
        return Ok(0);
    }
    // Task Scheduler can supply a job without breakaway permission or
    // kill-on-close. Inheriting that job does not couple app exit to the host.
    if flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "The launcher's kill-on-close job does not permit companion breakaway",
        ));
    }
    Ok(0)
}

pub fn activate(executable: &Path, launch_arg: &str, configuration: &Path) -> io::Result<()> {
    use windows_sys::Win32::Storage::FileSystem::*;
    let job_flags = activation_job_flags()?;
    let nul = wide(std::ffi::OsStr::new("NUL"))?;
    let security = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: null_mut(),
        bInheritHandle: 1,
    };
    let nul = Handle::new(unsafe {
        CreateFileW(
            nul.as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            &security,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            null_mut(),
        )
    })?;
    let inherited = [nul.0];
    let mut attributes = Attributes::new(&inherited)?;
    let mut startup: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = nul.0;
    startup.StartupInfo.hStdOutput = nul.0;
    startup.StartupInfo.hStdError = nul.0;
    startup.lpAttributeList = attributes.pointer();
    let program = wide(executable.as_os_str())?;
    let mut command = quoted_argument(executable.as_os_str())?;
    for argument in [std::ffi::OsStr::new(launch_arg), configuration.as_os_str()] {
        command.push(b' ' as u16);
        command.extend(quoted_argument(argument)?);
    }
    command.push(0);
    let mut information: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    // Rust Command currently inherits every inheritable handle. The explicit
    // list prevents browser pipes and unrelated job handles reaching the app.
    if unsafe {
        CreateProcessW(
            program.as_ptr(),
            command.as_mut_ptr(),
            null(),
            null(),
            1,
            EXTENDED_STARTUPINFO_PRESENT
                | job_flags
                | CREATE_NEW_PROCESS_GROUP
                | DETACHED_PROCESS,
            null(),
            null(),
            &startup.StartupInfo,
            &mut information,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    drop(Handle::new(information.hThread)?);
    drop(Handle::new(information.hProcess)?);
    Ok(())
}

pub(crate) struct OwnedProcess {
    process: Handle,
    job: Handle,
    pid: u32,
    input: Option<File>,
    output: Option<File>,
    stopped: bool,
}
impl OwnedProcess {
    pub fn spawn(executable: &Path, arguments: &[OsString], log: File) -> io::Result<Self> {
        let job = Handle::new(unsafe { CreateJobObjectW(null(), null()) })?;
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if unsafe {
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                std::ptr::from_ref(&limits).cast(),
                std::mem::size_of_val(&limits) as u32,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let (child_input, parent_input) = anonymous_pipe(false)?;
        let (parent_output, child_output) = anonymous_pipe(true)?;
        let mut stderr = null_mut();
        let current = unsafe { GetCurrentProcess() };
        if unsafe {
            DuplicateHandle(
                current,
                log.as_raw_handle(),
                current,
                &mut stderr,
                0,
                1,
                DUPLICATE_SAME_ACCESS,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let stderr = Handle::new(stderr)?;
        let inherited = [child_input.0, child_output.0, stderr.0];
        let mut attributes = Attributes::new(&inherited)?;
        let mut startup: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
        startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = child_input.0;
        startup.StartupInfo.hStdOutput = child_output.0;
        startup.StartupInfo.hStdError = stderr.0;
        startup.lpAttributeList = attributes.pointer();
        let executable_w = wide(executable.as_os_str())?;
        let mut command = quoted_argument(executable.as_os_str())?;
        for argument in arguments {
            command.push(b' ' as u16);
            command.extend(quoted_argument(argument)?);
        }
        command.push(0);
        let mut information: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
        if unsafe {
            CreateProcessW(
                executable_w.as_ptr(),
                command.as_mut_ptr(),
                null(),
                null(),
                1,
                CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
                null(),
                null(),
                &startup.StartupInfo,
                &mut information,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let process = Handle::new(information.hProcess)?;
        let thread = Handle::new(information.hThread)?;
        // The engine cannot execute or create a descendant until it belongs to
        // our non-inheritable kill-on-close job. Assignment failure is fatal.
        if unsafe { AssignProcessToJobObject(job.0, process.0) } == 0 {
            let error = io::Error::last_os_error();
            unsafe {
                TerminateProcess(process.0, 1);
                WaitForSingleObject(process.0, 3000);
            }
            return Err(error);
        }
        let owned = Self {
            process,
            job,
            pid: information.dwProcessId,
            input: Some(unsafe { File::from_raw_handle(parent_input.into_raw()) }),
            output: Some(unsafe { File::from_raw_handle(parent_output.into_raw()) }),
            stopped: false,
        };
        if unsafe { ResumeThread(thread.0) } == u32::MAX {
            return Err(io::Error::last_os_error());
        }
        Ok(owned)
    }
    pub fn id(&self) -> u32 {
        self.pid
    }
    pub fn take_input(&mut self) -> Box<dyn io::Write + Send> {
        Box::new(self.input.take().unwrap())
    }
    pub fn take_output(&mut self) -> Box<dyn io::Read + Send> {
        Box::new(self.output.take().unwrap())
    }
    pub fn has_exited(&mut self) -> io::Result<bool> {
        match unsafe { WaitForSingleObject(self.process.0, 0) } {
            WAIT_OBJECT_0 => Ok(true),
            WAIT_TIMEOUT => Ok(false),
            _ => Err(io::Error::last_os_error()),
        }
    }
    fn stop(&mut self) -> io::Result<()> {
        if self.stopped {
            return Ok(());
        }
        if unsafe { TerminateJobObject(self.job.0, 1) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let mut accounting: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION =
                unsafe { std::mem::zeroed() };
            if unsafe {
                QueryInformationJobObject(
                    self.job.0,
                    JobObjectBasicAccountingInformation,
                    std::ptr::from_mut(&mut accounting).cast(),
                    std::mem::size_of_val(&accounting) as u32,
                    null_mut(),
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            if accounting.ActiveProcesses == 0 {
                self.stopped = true;
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "Owned job descendants did not exit",
                ));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for OwnedProcess {
    fn drop(&mut self) {
        if let Err(error) = self.stop() {
            eprintln!("Engine job cleanup failed: {error}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::process::{Child, Command, Stdio};
    fn powershell() -> std::path::PathBuf {
        std::path::PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32/WindowsPowerShell/v1.0/powershell.exe")
    }
    fn launch(log: File) -> OwnedProcess {
        let script = r#"$child=[Diagnostics.Process]::Start($env:SystemRoot+'\System32\WindowsPowerShell\v1.0\powershell.exe','-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 300"');[Console]::Out.WriteLine($child.Id);[Console]::Out.Flush();Start-Sleep -Seconds 300"#;
        OwnedProcess::spawn(
            &powershell(),
            &[
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
                script.into(),
            ],
            log,
        )
        .unwrap()
    }
    fn descendant(process: &mut OwnedProcess) -> u32 {
        let output = process.take_output();
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let mut line = String::new();
            let value = BufReader::new(output).read_line(&mut line).map(|_| line);
            let _ = sender.send(value);
        });
        receiver
            .recv_timeout(Duration::from_secs(15))
            .unwrap()
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }
    fn pid_handle(pid: u32) -> Handle {
        Handle::new(unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) }).unwrap()
    }
    fn assert_dead(handle: &Handle) {
        assert_eq!(
            unsafe { WaitForSingleObject(handle.0, 5000) },
            WAIT_OBJECT_0
        );
    }
    #[test]
    fn dropping_engine_job_terminates_the_engine_and_its_descendant() {
        let mut process = launch(File::options().write(true).open("NUL").unwrap());
        let descendant = pid_handle(descendant(&mut process));
        let engine = pid_handle(process.id());
        drop(process);
        assert_dead(&engine);
        assert_dead(&descendant);
    }
    #[test]
    #[ignore = "Launched only by the owner-death test with its private marker"]
    fn owner_crash_helper() {
        let Some(marker) = std::env::var_os("VIDGRAB_JOB_TEST_MARKER") else {
            return;
        };
        let mut process = launch(File::options().write(true).open("NUL").unwrap());
        let child = descendant(&mut process);
        let mut file = File::create(marker).unwrap();
        writeln!(file, "{} {child}", process.id()).unwrap();
        file.sync_all().unwrap();
        std::thread::sleep(Duration::from_secs(300));
        drop(process);
    }
    struct ChildGuard(Child);
    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    #[test]
    fn abrupt_owner_exit_closes_the_non_inherited_job_and_kills_descendants() {
        let mut nonce = [0u8; 8];
        getrandom::fill(&mut nonce).unwrap();
        let marker =
            std::env::temp_dir().join(format!("vidgrab-job-{:x}.txt", u64::from_ne_bytes(nonce)));
        let mut owner = ChildGuard(
            Command::new(std::env::current_exe().unwrap())
                .args([
                    "--ignored",
                    "--exact",
                    "windows::process::tests::owner_crash_helper",
                    "--nocapture",
                ])
                .env("VIDGRAB_JOB_TEST_MARKER", &marker)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let deadline = Instant::now() + Duration::from_secs(20);
        let ids = loop {
            if let Ok(value) = std::fs::read_to_string(&marker) {
                let ids: Vec<u32> = value
                    .split_whitespace()
                    .filter_map(|v| v.parse().ok())
                    .collect();
                if ids.len() == 2 {
                    break ids;
                }
            }
            assert!(Instant::now() < deadline, "Owned helper failed to start");
            std::thread::sleep(Duration::from_millis(25));
        };
        let engine = pid_handle(ids[0]);
        let child = pid_handle(ids[1]);
        owner.0.kill().unwrap();
        owner.0.wait().unwrap();
        assert_dead(&engine);
        assert_dead(&child);
        std::fs::remove_file(marker).unwrap();
    }
}
