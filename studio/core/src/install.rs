//! What an installation of Studio is, and where Windows is told about it.
//!
//! Everything is per user: the program lives under `%LOCALAPPDATA%\Programs`,
//! its entry in Apps & features under `HKEY_CURRENT_USER`. No step of
//! installing, repairing or removing it needs administrator rights, so none
//! of them can half-fail on a refused prompt.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const APP_NAME: &str = "OpenRazer Studio";
pub const PUBLISHER: &str = "openrazer-win project";
pub const APP_EXE: &str = "openrazer-studio.exe";
/// The daemon and command line, installed beside the app.
pub const DAEMON_EXE: &str = "openrazer-win.exe";
/// A copy of the installer kept in the install folder: Apps & features runs
/// it to repair or remove Studio, and it carries everything it needs to put
/// back a file that has gone missing, without a download.
pub const SETUP_EXE: &str = "setup.exe";
/// What was installed, and how, in the install folder.
pub const MANIFEST: &str = "install.json";

const UNINSTALL_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenRazerStudio";
const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
/// Shared with the classic control panel and the command line's `autostart`,
/// so that only one of them ever starts at sign-in.
pub const RUN_VALUE: &str = "OpenRazerWin";

pub const RELEASES: &str = "https://github.com/Ar4ikov/openrazer-win/releases/latest";
pub const REPOSITORY: &str = "https://github.com/Ar4ikov/openrazer-win";
pub const COMMUNITY: &str = "https://t.me/RazerRussianCommunity";

/// The choices made when Studio was installed, kept so a repair or an update
/// makes the same ones.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Options {
    pub start_menu: bool,
    pub desktop: bool,
    pub autostart: bool,
    /// Put the install folder on the user's PATH, for `openrazer-win` in a terminal.
    pub path: bool,
}

impl Default for Options {
    fn default() -> Self {
        Options { start_menu: true, desktop: false, autostart: true, path: false }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct Manifest {
    pub version: String,
    pub installed: String,
    pub options: Options,
    /// Every file put in the install folder, relative to it, with its size.
    pub files: Vec<(String, u64)>,
}

impl Manifest {
    pub fn read(dir: &Path) -> Option<Manifest> {
        let text = std::fs::read_to_string(dir.join(MANIFEST)).ok()?;
        serde_json::from_str(&text).ok()
    }
}

/// An installation Windows knows about.
#[derive(Clone, Debug)]
pub struct Installed {
    pub dir: PathBuf,
    pub version: String,
}

impl Installed {
    pub fn app(&self) -> PathBuf {
        self.dir.join(APP_EXE)
    }
    pub fn setup(&self) -> PathBuf {
        self.dir.join(SETUP_EXE)
    }
}

fn hkcu() -> winreg::RegKey {
    winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
}

/// The installation recorded in Apps & features, if any.
pub fn installed() -> Option<Installed> {
    let key = hkcu().open_subkey(UNINSTALL_KEY).ok()?;
    let dir: String = key.get_value("InstallLocation").ok()?;
    let version: String = key.get_value("DisplayVersion").unwrap_or_default();
    Some(Installed { dir: PathBuf::from(dir), version })
}

/// Whether the program running now is the installed one.
pub fn running_installed() -> Option<Installed> {
    let installed = installed()?;
    let exe = std::env::current_exe().ok()?;
    let here = exe.parent()?;
    same_path(here, &installed.dir).then_some(installed)
}

pub fn same_path(a: &Path, b: &Path) -> bool {
    let canon = |p: &Path| std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    canon(a).as_os_str().eq_ignore_ascii_case(canon(b).as_os_str())
}

/// Tell Apps & features about an installation in `dir`.
pub fn register(dir: &Path, version: &str, size_kb: u32) -> std::io::Result<()> {
    let (key, _) = hkcu().create_subkey(UNINSTALL_KEY)?;
    let quoted = |name: &str| format!("\"{}\"", dir.join(name).display());
    key.set_value("DisplayName", &APP_NAME)?;
    key.set_value("DisplayVersion", &version)?;
    key.set_value("Publisher", &PUBLISHER)?;
    key.set_value("DisplayIcon", &format!("{},0", dir.join(APP_EXE).display()))?;
    key.set_value("InstallLocation", &dir.display().to_string())?;
    key.set_value("InstallDate", &today())?;
    key.set_value("UninstallString", &format!("{} --uninstall", quoted(SETUP_EXE)))?;
    key.set_value("QuietUninstallString", &format!("{} --uninstall --silent", quoted(SETUP_EXE)))?;
    // "Modify" opens the installer's own page: repair, or remove.
    key.set_value("ModifyPath", &format!("{} --maintenance", quoted(SETUP_EXE)))?;
    key.set_value("URLInfoAbout", &REPOSITORY)?;
    key.set_value("URLUpdateInfo", &RELEASES)?;
    key.set_value("HelpLink", &format!("{REPOSITORY}/issues"))?;
    key.set_value("Comments", &"Razer device control without Synapse")?;
    key.set_value("EstimatedSize", &size_kb)?;
    key.set_value("NoModify", &0u32)?;
    key.set_value("NoRepair", &0u32)?;
    Ok(())
}

pub fn unregister() -> std::io::Result<()> {
    match hkcu().delete_subkey_all(UNINSTALL_KEY) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
        _ => Ok(()),
    }
}

fn today() -> String {
    // YYYYMMDD, which is what Apps & features expects, from the system clock.
    let days = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() / 86_400).unwrap_or(0) as i64;
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}{m:02}{d:02}")
}

/// Days since 1970-01-01 to a calendar date (Howard Hinnant's algorithm).
pub fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

// -- starting with Windows ------------------------------------------------------

pub fn autostart_value() -> Option<String> {
    let key = hkcu().open_subkey(RUN_KEY).ok()?;
    key.get_value::<String, _>(RUN_VALUE).ok()
}

/// The command that starts `app` in the tray at sign-in.
pub fn autostart_command(app: &Path) -> String {
    format!("\"{}\" --background", app.display())
}

pub fn set_autostart(command: Option<&str>) -> Result<(), String> {
    let (key, _) = hkcu().create_subkey(RUN_KEY).map_err(|e| e.to_string())?;
    match command {
        Some(command) => key.set_value(RUN_VALUE, &command).map_err(|e| e.to_string()),
        None => match key.delete_value(RUN_VALUE) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.to_string()),
            _ => Ok(()),
        },
    }
}

/// Whether the sign-in entry starts something inside `dir`.
pub fn autostart_points_into(dir: &Path) -> bool {
    autostart_value().is_some_and(|value| {
        let target = value.trim().trim_start_matches('"');
        let target = target.split('"').next().unwrap_or(target);
        Path::new(target).parent().is_some_and(|parent| same_path(parent, dir))
    })
}

// -- the user's PATH -------------------------------------------------------------

fn path_entries() -> Vec<String> {
    hkcu()
        .open_subkey("Environment")
        .ok()
        .and_then(|key| key.get_value::<String, _>("Path").ok())
        .map(|path| path.split(';').filter(|p| !p.is_empty()).map(str::to_string).collect())
        .unwrap_or_default()
}

pub fn on_user_path(dir: &Path) -> bool {
    path_entries().iter().any(|entry| same_path(Path::new(entry), dir))
}

/// Add `dir` to, or take it off, the user's PATH.
pub fn set_on_user_path(dir: &Path, wanted: bool) -> std::io::Result<()> {
    let mut entries = path_entries();
    let present = entries.iter().any(|entry| same_path(Path::new(entry), dir));
    if present == wanted {
        return Ok(());
    }
    if wanted {
        entries.push(dir.display().to_string());
    } else {
        entries.retain(|entry| !same_path(Path::new(entry), dir));
    }
    let (key, _) = hkcu().create_subkey("Environment")?;
    // REG_EXPAND_SZ, as Windows itself writes it, so %VARIABLES% keep working.
    let value = winreg::RegValue {
        bytes: entries.join(";").encode_utf16().chain(std::iter::once(0)).flat_map(|u| u.to_le_bytes()).collect(),
        vtype: winreg::enums::RegType::REG_EXPAND_SZ,
    };
    key.set_raw_value("Path", &value)?;
    crate::process::announce_environment_change();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_are_counted_from_1970() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(20_753), (2026, 10, 27));
        assert_eq!(civil_from_days(11_016), (2000, 2, 29));
    }

    #[test]
    fn options_default_to_a_start_menu_entry_and_starting_with_windows() {
        let options: Options = serde_json::from_str("{}").unwrap();
        assert!(options.start_menu && options.autostart && !options.desktop && !options.path);
    }
}
