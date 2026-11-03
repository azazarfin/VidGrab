use std::path::PathBuf;
use vidgrab_supervisor::{Config, Owner, protocol, transport};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut arguments = std::env::args_os().skip(1);
    if arguments.next().as_deref() != Some(std::ffi::OsStr::new("--config")) {
        return Err("Use --config with an explicit trusted runtime configuration".into());
    }
    let path = PathBuf::from(arguments.next().ok_or("Missing configuration path")?);
    if arguments.next().is_some() { return Err("Unexpected argument".into()); }
    let config = Config::load(&path)?;
    match Owner::start(config.clone())? {
        Some(owner) => owner.run(|_| {}),
        None => {
            let request = protocol::Request { spike_version: 1, id: "activation".into(), op: "activate".into(), params: Default::default() };
            let _ = transport::call(&config, &request);
        }
    }
    Ok(())
}
