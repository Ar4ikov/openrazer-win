//! Finding, starting and stopping the daemon, and starting with Windows.
//!
//! The daemon is the Python half of openrazer-win: it owns the devices. This
//! program is only a client, so it has to be able to bring the daemon up --
//! without a console window -- from wherever it is: inside this executable in
//! a release, beside it, on `PATH` after `pip install`, or as a module.

use crate::rpc::{config_dir, Client, Endpoint};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
/// Shared with the Python CLI's `autostart`, so there is only ever one entry.
const RUN_VALUE: &str = "OpenRazerWin";

/// Where to download the daemon from, for the one case it cannot be found.
pub const RELEASES: &str = "https://github.com/Ar4ikov/openrazer-win/releases/latest";

/// The frozen daemon a release build carries, so the panel needs nothing else.
#[cfg(embedded_daemon)]
const EMBEDDED: Option<&[u8]> = Some(include_bytes!(concat!(env!("OUT_DIR"), "/openrazer-win.exe")));
#[cfg(not(embedded_daemon))]
const EMBEDDED: Option<&[u8]> = None;

/// Whether this build carries a daemon that looks like a Windows program.
pub fn embedded_daemon_ok() -> bool {
    EMBEDDED.is_none_or(|bytes| bytes.len() > 1024 * 1024 && bytes.starts_with(b"MZ"))
}

/// Write the embedded daemon out once per version, and return where it is.
fn unpack_embedded() -> Result<Option<PathBuf>, String> {
    let Some(bytes) = EMBEDDED else { return Ok(None) };
    let dir = config_dir().join("bin");
    let path = dir.join(format!("openrazer-win-{}.exe", env!("CARGO_PKG_VERSION")));
    if std::fs::metadata(&path).is_ok_and(|m| m.len() == bytes.len() as u64) {
        return Ok(Some(path));
    }
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    let partial = path.with_extension("exe.partial");
    std::fs::write(&partial, bytes).map_err(|e| format!("cannot write {}: {e}", partial.display()))?;
    std::fs::rename(&partial, &path).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    // Earlier versions' copies; one still running stays locked, which is fine.
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for entry in entries.flatten() {
            let other = entry.path();
            let name = other.file_name().and_then(|n| n.to_str()).unwrap_or_default();
            if other != path && name.starts_with("openrazer-win-") && name.ends_with(".exe") {
                let _ = std::fs::remove_file(&other);
            }
        }
    }
    Ok(Some(path))
}

/// Why the daemon could not be started.
#[derive(Clone, Debug)]
pub struct StartError {
    pub message: String,
    /// Nothing on this machine can run the daemon at all: the case for
    /// offering to install Python and openrazer-win.
    pub missing: bool,
}

impl std::fmt::Display for StartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

/// Every way this machine has of running the daemon, best first.
///
/// A copy beside this program wins -- someone put it there -- then the one
/// built into it, which matches its version, then whatever is installed. The
/// Python fallback only counts a Python that really has openrazer-win in it.
/// 1.4.0 took any python on PATH: on a laptop with Python 3.14 and no
/// openrazer-win the daemon died on its first import, before it could create
/// even the folder its log goes in, and the panel pointed at a log that did
/// not exist.
fn candidates() -> (Vec<Command>, Vec<String>) {
    let mut commands = Vec::new();
    let mut problems = Vec::new();
    let exe = |path: PathBuf| {
        let mut command = Command::new(path);
        command.args(["daemon", "run"]);
        command
    };
    let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf));
    if let Some(beside) = exe_dir.as_ref().map(|dir| dir.join("openrazer-win.exe")).filter(|p| p.is_file()) {
        commands.push(exe(beside));
    }
    match unpack_embedded() {
        Ok(Some(path)) => commands.push(exe(path)),
        Ok(None) => {}
        Err(problem) => problems.push(problem),
    }
    for installed in on_path("openrazer-win.exe") {
        if installed.is_file() {
            commands.push(exe(installed));
        }
    }
    for python in crate::setup::pythons_with_package() {
        let mut command = Command::new(python);
        command.args(["-m", "openrazer_win.daemon.main"]);
        commands.push(command);
    }
    (commands, problems)
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

/// Where the daemon's own output goes while it starts, before it has a log.
pub fn start_log_path() -> PathBuf {
    config_dir().join("daemon-start.log")
}

/// The last few lines of a file, if there is anything in it.
fn tail(path: &Path, lines: usize) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let kept: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    if kept.is_empty() {
        return None;
    }
    Some(kept[kept.len().saturating_sub(lines)..].join("\n"))
}

/// Start the daemon detached and windowless, and wait until it answers.
///
/// Each way of running it is tried in turn until one answers. Its output is
/// kept in `daemon-start.log`: if it dies before it can open its own log -- a
/// missing module, a blocked executable -- that is the only place the reason
/// ends up, and the error quotes it.
pub fn start() -> Result<(), StartError> {
    if is_running() {
        return Ok(());
    }
    let (commands, mut problems) = candidates();
    if commands.is_empty() {
        problems.push(
            "The daemon is not on this computer: no openrazer-win.exe beside this program or on \
             PATH, and no Python with openrazer-win installed in it."
                .to_string(),
        );
        return Err(StartError { message: problems.join("\n\n"), missing: true });
    }
    for command in commands {
        match try_start(command) {
            Ok(()) => return Ok(()),
            Err(problem) => problems.push(problem),
        }
    }
    Err(StartError { message: problems.join("\n\n"), missing: false })
}

fn try_start(mut command: Command) -> Result<(), String> {
    let dir = config_dir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    let output = std::fs::File::create(start_log_path()).ok();
    let errors = output.as_ref().and_then(|f| f.try_clone().ok());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        command.creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
    }
    command
        .stdin(Stdio::null())
        .stdout(output.map_or_else(Stdio::null, Stdio::from))
        .stderr(errors.map_or_else(Stdio::null, Stdio::from));
    let program = command.get_program().to_string_lossy().into_owned();
    let mut child = command.spawn().map_err(|e| format!("Could not start {program}: {e}"))?;

    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        if is_running() {
            return Ok(());
        }
        if let Ok(Some(status)) = child.try_wait() {
            // A daemon that steps aside for one already running exits too;
            // give that one a moment to answer before calling it a failure.
            std::thread::sleep(Duration::from_millis(500));
            if is_running() {
                return Ok(());
            }
            return Err(explain(&program, &format!("it exited ({status})")));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    let _ = child.kill();
    Err(explain(&program, "it did not answer within 30 seconds"))
}

fn explain(program: &str, what: &str) -> String {
    let mut message = format!("{program} did not start: {what}.");
    let log = config_dir().join("daemon.log");
    if let Some(output) = tail(&start_log_path(), 12) {
        message.push_str(&format!("\nIt said:\n{output}"));
    }
    if let Some(lines) = tail(&log, 6) {
        message.push_str(&format!("\nThe end of {}:\n{lines}", log.display()));
    }
    message
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

pub fn restart() -> Result<(), StartError> {
    if is_running() {
        stop().map_err(|message| StartError { message, missing: false })?;
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
