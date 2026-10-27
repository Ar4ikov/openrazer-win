//! Where things are kept.

use std::path::PathBuf;

/// `%LOCALAPPDATA%\openrazer-win`: the daemon's settings, profiles, macros
/// and logs.  Shared with the classic control panel and with pip installs.
pub fn data_dir() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(|home| PathBuf::from(home).join(".config")))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("openrazer-win")
}

/// Studio's own settings, inside the shared folder.
pub fn studio_dir() -> PathBuf {
    data_dir().join("studio")
}

/// `%LOCALAPPDATA%\Programs\OpenRazer Studio`: installed per user, so that
/// installing, repairing and removing never asks for administrator rights.
pub fn default_install_dir() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
    base.join("Programs").join(crate::install::APP_NAME)
}

/// Write `bytes` to `path` without ever leaving half a file behind.
pub fn write_atomic(path: &std::path::Path, bytes: &[u8]) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let partial = path.with_extension("partial");
    std::fs::write(&partial, bytes)?;
    std::fs::rename(&partial, path)
}
