use std::path::PathBuf;
use vidgrab_supervisor::{Config, Owner, protocol, transport};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut arguments = std::env::args_os().skip(1);
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--config")) {
        return Err("Use --config with an explicit trusted runtime configuration".into());
    }
    let path = PathBuf::from(arguments.next().ok_or("Missing configuration path")?);
    let remaining: Vec<_> = arguments.collect();
    let config = Config::load(&path)?;
    if remaining.len() == 2 && remaining[0] == "--request" {
        let operation = remaining[1].to_str().ok_or("Invalid diagnostic operation")?;
        if !["status", "activate", "quit", "quit_if_idle"].contains(&operation) {
            return Err("Unsupported local diagnostic operation".into());
        }
        let request = protocol::Request { spike_version: 1, id: "diagnostic".into(), op: operation.into(), params: Default::default() };
        request.validate(false).map_err(std::io::Error::other)?;
        println!("{}", transport::call(&config, &request)?);
        return Ok(());
    }
    if !remaining.is_empty() { return Err("Unexpected argument".into()); }
    match Owner::start(config.clone())? {
        Some(owner) => owner.run(|_| {}),
        None => {
            let request = protocol::Request { spike_version: 1, id: "activation".into(), op: "activate".into(), params: Default::default() };
            let _ = transport::call(&config, &request);
        }
    }
    Ok(())
}
