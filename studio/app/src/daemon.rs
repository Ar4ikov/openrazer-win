//! Finding, starting and stopping the daemon, and starting with Windows.
//!
//! The daemon is the Python half of openrazer-win: it owns the devices. This
//! program is only a client, so it has to be able to bring the daemon up --
//! without a console window -- from wherever it is: beside this executable
//! (which is where the installer puts it), on `PATH`, or as a Python module.

use openrazer_studio_core::install;
use openrazer_studio_core::paths::data_dir as config_dir;
use openrazer_studio_core::rpc::{Client, Endpoint};
use openrazer_studio_core::t;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Why the daemon could not be started.
#[derive(Clone, Debug)]
pub struct StartError {
    pub message: String,
    /// Nothing on this machine can run the daemon at all: the case for
    /// offering to install Python and openrazer-win, or to repair Studio.
    pub missing: bool,
}

impl std::fmt::Display for StartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

/// Every way this machine has of running the daemon, best first: the copy
/// installed beside this program, then whatever is on PATH, then a Python
/// that really has openrazer-win in it.
fn candidates() -> Vec<Command> {
    let mut commands = Vec::new();
    let exe = |path: PathBuf| {
        let mut command = Command::new(path);
        command.args(["daemon", "run"]);
        command
    };
    let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf));
    if let Some(beside) = exe_dir.as_ref().map(|dir| dir.join(install::DAEMON_EXE)).filter(|p| p.is_file()) {
        commands.push(exe(beside));
    }
    for installed in on_path(install::DAEMON_EXE) {
        if installed.is_file() {
            commands.push(exe(installed));
        }
    }
    for python in crate::pysetup::pythons_with_package() {
        let mut command = Command::new(python);
        command.args(["-m", "openrazer_win.daemon.main"]);
        commands.push(command);
    }
    commands
}

fn on_path(name: &str) -> Vec<PathBuf> {
    std::env::var_os("PATH").map(|path| std::env::split_paths(&path).map(|dir| dir.join(name)).collect()).unwrap_or_default()
}

/// Whether a daemon answers right now.
pub fn is_running() -> bool {
    Client::connect().and_then(|mut client| client.call("daemon.version", serde_json::json!({}))).is_ok()
}

/// Where the daemon's own output goes while it starts, before it has a log.
pub fn start_log_path() -> PathBuf {
    config_dir().join("daemon-start.log")
}

pub fn log_path() -> PathBuf {
    config_dir().join("daemon.log")
}

/// The last few lines of a file, if there is anything in it.
pub fn tail(path: &Path, lines: usize) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let kept: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    if kept.is_empty() {
        return None;
    }
    Some(kept[kept.len().saturating_sub(lines)..].join("\n"))
}

/// Start the daemon windowless, and wait until it answers.
///
/// Each way of running it is tried in turn until one answers. Its output is
/// kept in `daemon-start.log`: if it dies before it can open its own log -- a
/// missing module, a blocked executable -- that is the only place the reason
/// ends up, and the error quotes it.
pub fn start() -> Result<(), StartError> {
    if is_running() {
        return Ok(());
    }
    let commands = candidates();
    if commands.is_empty() {
        return Err(StartError {
            message: t("The daemon is not on this computer: there is no openrazer-win.exe beside this program or on PATH, and no Python with openrazer-win installed in it.").to_string(),
            missing: true,
        });
    }
    let mut problems = Vec::new();
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
        // A console of its own that has no window -- not no console at all.
        // The frozen daemon is a one-file build whose bootloader runs the real
        // program as a child: with DETACHED_PROCESS that child found no console
        // to inherit and Windows opened a new, visible one for it, and closing
        // that window killed the daemon. A windowless console is inherited.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        command.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    }
    command.stdin(Stdio::null()).stdout(output.map_or_else(Stdio::null, Stdio::from)).stderr(errors.map_or_else(Stdio::null, Stdio::from));
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
    if let Some(output) = tail(&start_log_path(), 12) {
        message.push_str(&format!("\nIt said:\n{output}"));
    }
    if let Some(lines) = tail(&log_path(), 6) {
        message.push_str(&format!("\nThe end of {}:\n{lines}", log_path().display()));
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

// -- starting with Windows -----------------------------------------------------

fn this_exe() -> Option<PathBuf> {
    std::env::current_exe().ok()
}

/// Whether Studio, rather than the classic panel or the command line, starts
/// at sign-in.
pub fn autostart_enabled() -> bool {
    let Some(exe) = this_exe() else { return false };
    exe.parent().is_some_and(install::autostart_points_into) && install::autostart_value().is_some_and(|v| v.to_lowercase().contains(install::APP_EXE))
}

/// Something else -- the classic panel, or the command line -- starts at sign-in.
pub fn autostart_taken() -> Option<String> {
    let value = install::autostart_value()?;
    (!autostart_enabled()).then_some(value)
}

pub fn set_autostart(enabled: bool) -> Result<(), String> {
    if enabled {
        let exe = this_exe().ok_or_else(|| "cannot tell where this program is".to_string())?;
        install::set_autostart(Some(&install::autostart_command(&exe)))
    } else if autostart_enabled() {
        install::set_autostart(None)
    } else {
        Ok(())
    }
}

/// Keep an entry that starts Studio pointing at this copy of it.
///
/// Only an entry for Studio is touched: one the classic panel or the command
/// line wrote is theirs, until the user turns this on in Settings.
pub fn refresh_autostart() {
    let (Some(value), Some(exe)) = (install::autostart_value(), this_exe()) else { return };
    if value.to_lowercase().contains(install::APP_EXE) && !value.contains(&exe.display().to_string()) {
        let _ = install::set_autostart(Some(&install::autostart_command(&exe)));
    }
}
