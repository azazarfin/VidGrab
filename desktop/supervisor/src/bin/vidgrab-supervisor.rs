use std::path::PathBuf;
use vidgrab_supervisor::{protocol, transport, Config, Owner};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(windows)]
    {
        let args: Vec<_> = std::env::args_os().skip(1).collect();
        if args
            .first()
            .is_some_and(|arg| arg.to_string_lossy().starts_with("--installer-"))
        {
            use vidgrab_supervisor::installation::{application, UserDirs};
            if args.len() != 2 {
                return Err("Use an installer operation and an absolute destination".into());
            }
            let root = PathBuf::from(&args[1]);
            let dirs = UserDirs::from_env()?;
            match args[0].to_str() {
                Some("--installer-verify") => application::verify_installation(&root, &dirs)?,
                Some("--installer-check-destination") => {
                    application::verify_destination(&root, &dirs)?
                }
                Some("--installer-prepare-install") => application::prepare_install(&root, &dirs)?,
                Some("--installer-prepare-uninstall") => {
                    application::prepare_uninstall(&application::installer_layout(&root), &dirs)?
                }
                Some("--installer-record") => application::record_installation(&root, &dirs)?,
                _ => return Err("Unsupported installer operation".into()),
            }
            println!("{}", serde_json::json!({"prepared":true}));
            return Ok(());
        }
    }
    let mut arguments = std::env::args_os().skip(1);
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--config")) {
        return Err("Use --config with an explicit trusted runtime configuration".into());
    }
    let path = PathBuf::from(arguments.next().ok_or("Missing configuration path")?);
    let remaining: Vec<_> = arguments.collect();
    let config = Config::load(&path)?;
    if remaining.len() == 2 && remaining[0] == "--request" {
        let operation = remaining[1]
            .to_str()
            .ok_or("Invalid diagnostic operation")?;
        if !["status", "activate", "quit", "quit_if_idle"].contains(&operation) {
            return Err("Unsupported local diagnostic operation".into());
        }
        let request = protocol::Request {
            spike_version: 1,
            id: "diagnostic".into(),
            op: operation.into(),
            params: Default::default(),
        };
        request.validate(false).map_err(std::io::Error::other)?;
        println!("{}", transport::call(&config, &request)?);
        return Ok(());
    }
    if !remaining.is_empty() {
        return Err("Unexpected argument".into());
    }
    match Owner::start(config.clone())? {
        Some(owner) => owner.run(|_| {}),
        None => {
            let request = protocol::Request {
                spike_version: 1,
                id: "activation".into(),
                op: "activate".into(),
                params: Default::default(),
            };
            let _ = transport::call(&config, &request);
        }
    }
    Ok(())
}
