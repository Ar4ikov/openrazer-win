//! Getting the daemon onto a machine that has nothing: Python, then the package.
//!
//! The release build carries a daemon inside it, so this is the fallback --
//! for a build without one, or a daemon that will not run. The panel offers to
//! fetch the official Python installer from python.org and opens it, then
//! installs openrazer-win into that Python with pip.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// The Python the daemon is tested with and the installer offered for it.
pub const PYTHON_VERSION: &str = "3.13.17";
/// What openrazer-win needs at the least.
const MIN_PYTHON: (u32, u32) = (3, 9);

pub fn installer_url() -> String {
    format!("https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-amd64.exe")
}

fn hidden(command: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    command.stdin(Stdio::null())
}

/// Run `command` to completion, windowless, giving up after `limit`.
fn run(command: &mut Command, limit: Duration) -> Option<(bool, String)> {
    let mut child = hidden(command).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().ok()?;
    let deadline = Instant::now() + limit;
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            let mut out = String::new();
            if let Some(mut stdout) = child.stdout.take() {
                let _ = stdout.read_to_string(&mut out);
            }
            if let Some(mut stderr) = child.stderr.take() {
                let _ = stderr.read_to_string(&mut out);
            }
            return Some((status.success(), out));
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            return None;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn on_path(name: &str) -> Vec<PathBuf> {
    std::env::var_os("PATH")
        .map(|path| std::env::split_paths(&path).map(|dir| dir.join(name)).collect())
        .unwrap_or_default()
}

/// Every python.exe worth asking, including fresh installs this process's
/// PATH does not know about yet.
///
/// None is judged by where it lives. "python.exe" in WindowsApps is the
/// Microsoft Store stub on a machine without Python -- but it is also how the
/// Python 3.14 install manager puts a perfectly real Python on PATH. Each one
/// is asked to run instead (see `new_enough`), which the stub cannot.
fn python_candidates() -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = on_path("python.exe");
    let mut roots = Vec::new();
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        roots.push(PathBuf::from(local).join("Programs").join("Python"));
    }
    for var in ["ProgramFiles", "ProgramW6432"] {
        if let Some(dir) = std::env::var_os(var) {
            roots.push(PathBuf::from(dir));
        }
    }
    for root in roots {
        if let Ok(entries) = std::fs::read_dir(&root) {
            let mut dirs: Vec<PathBuf> = entries
                .flatten()
                .map(|e| e.path())
                .filter(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("Python3")))
                .collect();
            dirs.sort();
            dirs.reverse(); // newest first
            found.extend(dirs.into_iter().map(|d| d.join("python.exe")));
        }
    }
    // The py launcher knows about every registered install.
    for launcher in on_path("py.exe").into_iter().filter(|p| p.is_file()) {
        if let Some((true, out)) = run(Command::new(launcher).args(["-3", "-c", "import sys; print(sys.executable)"]), Duration::from_secs(10)) {
            if let Some(line) = out.lines().next() {
                found.push(PathBuf::from(line.trim()));
            }
        }
    }
    let mut unique: Vec<PathBuf> = Vec::new();
    for path in found {
        if path.is_file() && !unique.iter().any(|p| p.as_os_str().eq_ignore_ascii_case(path.as_os_str())) {
            unique.push(path);
        }
    }
    unique
}

fn new_enough(python: &Path) -> bool {
    let probe = format!("import sys; print(sys.version_info[:2] >= {MIN_PYTHON:?})");
    matches!(run(Command::new(python).args(["-c", &probe]), Duration::from_secs(15)), Some((true, out)) if out.trim() == "True")
}

/// A Python this machine has that can run openrazer-win.
pub fn find_python() -> Option<PathBuf> {
    python_candidates().into_iter().find(|p| new_enough(p))
}

/// Whether `python` has openrazer-win installed.
pub fn has_package(python: &Path) -> bool {
    matches!(run(Command::new(python).args(["-c", "import openrazer_win.daemon.main"]), Duration::from_secs(20)), Some((true, _)))
}

/// Pythons that can run the daemon right now, windowless where possible.
pub fn pythons_with_package() -> Vec<PathBuf> {
    python_candidates()
        .into_iter()
        .filter(|p| has_package(p))
        .map(|p| {
            let windowless = p.with_file_name("pythonw.exe");
            if windowless.is_file() {
                windowless
            } else {
                p
            }
        })
        .collect()
}

/// Download the Python installer, reporting progress as a fraction.
pub fn download_installer(progress: &dyn Fn(f32)) -> Result<PathBuf, String> {
    let url = installer_url();
    let target = std::env::temp_dir().join(format!("python-{PYTHON_VERSION}-amd64.exe"));
    let response = ureq::get(&url).timeout(Duration::from_secs(60)).call().map_err(|e| format!("could not download {url}: {e}"))?;
    let total: f32 = response.header("Content-Length").and_then(|v| v.parse().ok()).unwrap_or(30_000_000.0);
    let partial = target.with_extension("exe.partial");
    let mut file = std::fs::File::create(&partial).map_err(|e| format!("cannot write {}: {e}", partial.display()))?;
    let mut reader = response.into_reader();
    let mut buffer = vec![0u8; 256 * 1024];
    let mut done = 0usize;
    loop {
        let read = reader.read(&mut buffer).map_err(|e| format!("the download broke off: {e}"))?;
        if read == 0 {
            break;
        }
        file.write_all(&buffer[..read]).map_err(|e| e.to_string())?;
        done += read;
        progress((done as f32 / total).min(1.0));
    }
    drop(file);
    if done < 1024 * 1024 {
        return Err(format!("the download from {url} was too small to be the installer"));
    }
    std::fs::rename(&partial, &target).map_err(|e| e.to_string())?;
    Ok(target)
}

/// Open the installer and wait for the person to finish with it.
///
/// "Add python.exe to PATH" comes ticked, so the Python is found again later;
/// everything else is left to them.
pub fn run_installer(installer: &Path) -> Result<(), String> {
    let mut command = Command::new(installer);
    command.args(["PrependPath=1", "Include_launcher=1", "Include_test=0"]);
    let status = match command.status() {
        Ok(status) => status,
        // ERROR_ELEVATION_REQUIRED: let the shell do the asking, without waiting.
        Err(e) if e.raw_os_error() == Some(740) => {
            let _ = Command::new("explorer.exe").arg(installer).spawn();
            return Err("The installer needs administrator rights and was opened separately. When it is done, press “Install openrazer-win”.".into());
        }
        Err(e) => return Err(format!("could not open the installer: {e}")),
    };
    match status.code() {
        Some(0) => Ok(()),
        Some(1602) => Err("The Python installation was cancelled.".into()),
        Some(code) => Err(format!("The Python installer stopped with code {code}.")),
        None => Err("The Python installer stopped.".into()),
    }
}

/// Install openrazer-win -- this version, with Bluetooth support -- with pip.
pub fn install_package(python: &Path) -> Result<(), String> {
    let log = crate::rpc::config_dir().join("pip-install.log");
    let _ = std::fs::create_dir_all(crate::rpc::config_dir());
    let version = env!("CARGO_PKG_VERSION");
    // This panel's own version first; then the newest, for a build whose
    // version is not on PyPI; then without the Bluetooth extra, whose WinRT
    // wheels can lag behind a brand-new Python -- USB devices need nothing.
    let specs = [
        format!("openrazer-win[ble]=={version}"),
        "openrazer-win[ble]".to_string(),
        format!("openrazer-win=={version}"),
        "openrazer-win".to_string(),
    ];
    let mut transcript = String::new();
    for spec in specs.iter().map(String::as_str) {
        let attempt = run(
            Command::new(python).args(["-m", "pip", "install", "--upgrade", "--disable-pip-version-check", spec]),
            Duration::from_secs(600),
        );
        match attempt {
            Some((true, out)) => {
                transcript.push_str(&out);
                let _ = std::fs::write(&log, &transcript);
                return Ok(());
            }
            Some((false, out)) => transcript.push_str(&out),
            None => transcript.push_str("pip took longer than ten minutes\n"),
        }
    }
    let _ = std::fs::write(&log, &transcript);
    let last: Vec<&str> = transcript.lines().filter(|l| !l.trim().is_empty()).collect();
    Err(format!(
        "pip could not install openrazer-win:\n{}\n\nThe whole output is in {}",
        last[last.len().saturating_sub(6)..].join("\n"),
        log.display()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_program_that_is_not_python_is_not_new_enough() {
        // What the Store stub amounts to: something called python that
        // cannot run the version check.
        let not_python = std::env::var_os("ComSpec").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("cmd.exe"));
        assert!(!new_enough(&not_python));
    }

    #[test]
    fn the_installer_is_the_official_one() {
        assert!(installer_url().starts_with("https://www.python.org/ftp/python/"));
        assert!(installer_url().ends_with(&format!("python-{PYTHON_VERSION}-amd64.exe")));
    }

    /// Talks to the network and installs into the Python named by
    /// OPENRAZER_TEST_PYTHON; run by hand with `cargo test -- --ignored`.
    #[test]
    #[ignore]
    fn download_and_install_for_real() {
        let installer = download_installer(&|_| {}).expect("the installer downloads");
        assert!(std::fs::metadata(&installer).unwrap().len() > 10 * 1024 * 1024);
        let _ = std::fs::remove_file(&installer);
        let python = PathBuf::from(std::env::var_os("OPENRAZER_TEST_PYTHON").expect("OPENRAZER_TEST_PYTHON"));
        assert!(new_enough(&python));
        assert!(!has_package(&python));
        install_package(&python).expect("pip installs openrazer-win");
        assert!(has_package(&python));
    }
}
