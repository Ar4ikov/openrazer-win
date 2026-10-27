//! Finding, starting and stopping the daemon, and starting with Windows.
//!
//! The daemon is the Python half of openrazer-win: it owns the devices. This
//! program is only a client, so it has to be able to bring the daemon up --
//! without a console window -- from wherever it was installed: beside this
//! executable in a release, on `PATH` after `pip install`, or as a module.

use crate::rpc::{Client, Endpoint};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
/// Shared with the Python CLI's `autostart`, so there is only ever one entry.
const RUN_VALUE: &str = "OpenRazerWin";

/// How to launch the daemon on this machine, if it can be found at all.
pub fn daemon_command() -> Option<Command> {
    let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf));
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(dir) = &exe_dir {
        candidates.push(dir.join("openrazer-win.exe"));
    }
    candidates.extend(on_path("openrazer-win.exe"));
    for candidate in candidates {
        if candidate.is_file() {
            let mut command = Command::new(candidate);
            command.args(["daemon", "run"]);
            return Some(command);
        }
    }
    // A pip install without its Scripts directory on PATH: run the module.
    for python in on_path("pythonw.exe").into_iter().chain(on_path("python.exe")) {
        if python.is_file() {
            let mut command = Command::new(python);
            command.args(["-m", "openrazer_win.daemon.main"]);
            return Some(command);
        }
    }
    None
}

fn on_path(name: &str) -> Vec<PathBuf> {
    std::env::var_os("PATH")
        .map(|path| std::env::split_paths(&path).map(|dir| dir.join(name)).collect())
        .unwrap_or_default()
}

/// Whether a daemon answers right now.
pub fn is_running() -> bool {
    Client::connect()
        .and_then(|mut client| client.call("daemon.version", serde_json::json!({})))
        .is_ok()
}

/// Start the daemon detached and windowless, and wait until it answers.
pub fn start() -> Result<(), String> {
    if is_running() {
        return Ok(());
    }
    let mut command = daemon_command().ok_or_else(|| {
        "openrazer-win is not installed where this program can find it. Put \
         openrazer-win.exe next to it, or install it with pip."
            .to_string()
    })?;
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        command.creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
    }
    command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    command.spawn().map_err(|e| format!("could not start the daemon: {e}"))?;
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline {
        if is_running() {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(format!(
        "the daemon did not come up. Its log is at {}",
        crate::rpc::config_dir().join("daemon.log").display()
    ))
}

/// Ask the daemon to exit, and wait until it has.
pub fn stop() -> Result<(), String> {
    let mut client = Client::connect().map_err(|e| e.to_string())?;
    client.call("daemon.shutdown", serde_json::json!({})).map_err(|e| e.to_string())?;
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline && is_running() {
        std::thread::sleep(Duration::from_millis(200));
    }
    if Endpoint::read().is_some() && !is_running() {
        let _ = std::fs::remove_file(Endpoint::path());
    }
    Ok(())
}

pub fn restart() -> Result<(), String> {
    if is_running() {
        stop()?;
    }
    start()
}

/// The command line registered to run at logon: this program, in the tray.
fn autostart_command() -> Option<String> {
    let exe = std::env::current_exe().ok()?;
    Some(format!("\"{}\" --background", exe.display()))
}

pub fn autostart_enabled() -> bool {
    autostart_value().is_some()
}

pub fn autostart_value() -> Option<String> {
    let hkcu = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER);
    let key = hkcu.open_subkey(RUN_KEY).ok()?;
    key.get_value::<String, _>(RUN_VALUE).ok()
}

pub fn set_autostart(enabled: bool) -> Result<(), String> {
    let hkcu = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER);
    let (key, _) = hkcu.create_subkey(RUN_KEY).map_err(|e| e.to_string())?;
    if enabled {
        let command = autostart_command().ok_or("cannot tell where this program is")?;
        key.set_value(RUN_VALUE, &command).map_err(|e| e.to_string())
    } else {
        match key.delete_value(RUN_VALUE) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    }
}

/// Keep an enabled entry pointing at this executable.
///
/// The entry holds an absolute path. A new release unpacked somewhere else,
/// or an entry an older version wrote for the daemon directly, would
/// otherwise keep starting the wrong program at logon.
pub fn refresh_autostart() {
    if let (Some(current), Some(wanted)) = (autostart_value(), autostart_command()) {
        if current != wanted {
            let _ = set_autostart(true);
        }
    }
}
