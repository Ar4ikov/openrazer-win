//! Installing, repairing and removing Studio: the work itself, without a window.
//!
//! Every step says what it is doing through `progress`, and every step that
//! finds something to fix says so in the report, so the window -- or the log
//! of a silent run -- can tell the user exactly what was done.

use openrazer_studio_core::install::{self, Manifest, Options, APP_EXE, APP_NAME, DAEMON_EXE, MANIFEST, SETUP_EXE};
use openrazer_studio_core::{paths, process, t, tf};
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(payload)]
const APP: Option<&[u8]> = Some(include_bytes!(concat!(env!("OUT_DIR"), "/openrazer-studio.exe")));
#[cfg(not(payload))]
const APP: Option<&[u8]> = None;
#[cfg(payload)]
const DAEMON: Option<&[u8]> = Some(include_bytes!(concat!(env!("OUT_DIR"), "/openrazer-win.exe")));
#[cfg(not(payload))]
const DAEMON: Option<&[u8]> = None;
const LICENSE: &str = include_str!("../../../LICENSE");

/// What a run of the installer reports as it goes.
pub trait Progress: Sync {
    fn step(&self, text: String, fraction: f32);
    fn note(&self, text: String);
}

/// The files this installer carries, by the name they get in the install folder.
/// A file to install: its name in the install folder, and what goes in it.
type File = (&'static str, std::borrow::Cow<'static, [u8]>);

fn payload() -> Result<Vec<File>, String> {
    let (Some(app), Some(daemon)) = (APP, DAEMON) else {
        return Err(t("This copy of the installer was built without the program inside it. Download the installer from the releases page.").to_string());
    };
    let me = std::env::current_exe().map_err(|e| e.to_string())?;
    let setup = std::fs::read(&me).map_err(|e| format!("cannot read {}: {e}", me.display()))?;
    Ok(vec![
        (APP_EXE, std::borrow::Cow::Borrowed(app)),
        (DAEMON_EXE, std::borrow::Cow::Borrowed(daemon)),
        (SETUP_EXE, std::borrow::Cow::Owned(setup)),
        ("LICENSE.txt", std::borrow::Cow::Borrowed(LICENSE.as_bytes())),
    ])
}

pub fn has_payload() -> bool {
    APP.is_some() && DAEMON.is_some()
}

// -- where things go -------------------------------------------------------------------

#[cfg(windows)]
fn known_folder(id: &windows_sys::core::GUID) -> Option<PathBuf> {
    use windows_sys::Win32::System::Com::CoTaskMemFree;
    use windows_sys::Win32::UI::Shell::SHGetKnownFolderPath;
    unsafe {
        let mut raw: windows_sys::core::PWSTR = std::ptr::null_mut();
        if SHGetKnownFolderPath(id, 0, std::ptr::null_mut(), &mut raw) != 0 || raw.is_null() {
            return None;
        }
        let len = (0..).take_while(|&i| *raw.add(i) != 0).count();
        let path = String::from_utf16_lossy(std::slice::from_raw_parts(raw, len));
        CoTaskMemFree(raw as _);
        Some(PathBuf::from(path))
    }
}

fn start_menu() -> PathBuf {
    #[cfg(windows)]
    if let Some(dir) = known_folder(&windows_sys::Win32::UI::Shell::FOLDERID_Programs) {
        return dir.join(APP_NAME);
    }
    PathBuf::from(std::env::var_os("APPDATA").unwrap_or_default()).join(r"Microsoft\Windows\Start Menu\Programs").join(APP_NAME)
}

fn desktop() -> PathBuf {
    #[cfg(windows)]
    if let Some(dir) = known_folder(&windows_sys::Win32::UI::Shell::FOLDERID_Desktop) {
        return dir;
    }
    PathBuf::from(std::env::var_os("USERPROFILE").unwrap_or_default()).join("Desktop")
}

fn desktop_link() -> PathBuf {
    desktop().join(format!("{APP_NAME}.lnk"))
}

fn shortcut(target: &Path, arguments: Option<&str>, link: &Path, description: &str) -> Result<(), String> {
    if let Some(dir) = link.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let mut lnk = mslnk::ShellLink::new(target).map_err(|e| e.to_string())?;
    lnk.set_arguments(arguments.map(str::to_string));
    lnk.set_name(Some(description.to_string()));
    lnk.set_working_dir(target.parent().map(|p| p.display().to_string()));
    lnk.set_icon_location(Some(target.with_file_name(APP_EXE).display().to_string()));
    lnk.create_lnk(link).map_err(|e| e.to_string())
}

// -- installing -------------------------------------------------------------------------

/// Write `bytes` to `path` unless it is already exactly that. True if written.
fn put(path: &Path, bytes: &[u8]) -> Result<bool, String> {
    if std::fs::read(path).is_ok_and(|existing| existing == bytes) {
        return Ok(false);
    }
    let partial = path.with_extension("partial");
    std::fs::write(&partial, bytes).map_err(|e| format!("cannot write {}: {e}", partial.display()))?;
    // A file still held by a process that would not stop: try a few times.
    let mut last = None;
    for _ in 0..10 {
        match std::fs::rename(&partial, path) {
            Ok(()) => return Ok(true),
            Err(e) => last = Some(e),
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    let _ = std::fs::remove_file(&partial);
    Err(format!("cannot replace {}: {}", path.display(), last.map(|e| e.to_string()).unwrap_or_default()))
}

/// Install into `dir`, or bring an installation there back to how it should be.
///
/// Installing and repairing are the same work: every file compared with the
/// one carried here and rewritten if it differs, then the Start menu, Apps &
/// features, starting with Windows and PATH set as `options` say. A repair
/// also looks over the settings files and the daemon's endpoint.
pub fn install(dir: &Path, options: &Options, repair: bool, progress: &dyn Progress) -> Result<(), String> {
    let files = payload()?;
    progress.step(t("Closing OpenRazer Studio and the daemon…").into(), 0.05);
    let stuck = process::stop_everything_in(dir);
    if !stuck.is_empty() {
        progress.note(tf("Could not close: {}", &[&stuck.join(", ")]));
    }
    // A daemon from somewhere else (pip, the classic panel) would be the one
    // Studio talks to; stop it too, so this version's starts in its place.
    if openrazer_studio_core::rpc::shutdown_daemon(Duration::from_secs(10)) {
        progress.note(t("Stopped the running daemon; Studio starts this version's").into());
    }

    progress.step(tf("Copying files to {}…", &[&dir.display()]), 0.15);
    std::fs::create_dir_all(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    let me = std::env::current_exe().ok();
    let mut listed = Vec::new();
    let total = files.len() as f32;
    for (i, (name, bytes)) in files.iter().enumerate() {
        let path = dir.join(name);
        listed.push((name.to_string(), bytes.len() as u64));
        // The installer cannot overwrite itself; it is the installed copy already.
        if *name == SETUP_EXE && me.as_ref().is_some_and(|me| install::same_path(me, &path)) {
            continue;
        }
        let was_there = path.exists();
        if put(&path, bytes)? && repair {
            progress.note(if was_there { tf("Replaced a damaged {}", &[name]) } else { tf("Put back the missing {}", &[name]) });
        }
        progress.step(tf("Copying files to {}…", &[&dir.display()]), 0.15 + 0.55 * (i as f32 + 1.0) / total);
    }
    let (y, m, d) = install::civil_from_days(days());
    let manifest = Manifest { version: VERSION.into(), installed: format!("{y:04}-{m:02}-{d:02}"), options: options.clone(), files: listed.clone() };
    paths::write_atomic(&dir.join(MANIFEST), serde_json::to_string_pretty(&manifest).unwrap_or_default().as_bytes()).map_err(|e| e.to_string())?;

    progress.step(t("Telling Windows about it…").into(), 0.75);
    let size_kb = (listed.iter().map(|(_, size)| size).sum::<u64>() / 1024) as u32;
    install::register(dir, VERSION, size_kb).map_err(|e| tf("cannot register in Apps & features: {}", &[&e]))?;

    progress.step(t("Making shortcuts…").into(), 0.82);
    let app = dir.join(APP_EXE);
    let menu = start_menu();
    if options.start_menu {
        shortcut(&app, None, &menu.join(format!("{APP_NAME}.lnk")), t("Razer device control"))?;
        shortcut(&dir.join(SETUP_EXE), Some("--maintenance"), &menu.join(format!("{}.lnk", t("Repair or remove OpenRazer Studio"))), t("Repair or remove OpenRazer Studio"))?;
    } else if menu.exists() {
        let _ = std::fs::remove_dir_all(&menu);
    }
    if options.desktop {
        shortcut(&app, None, &desktop_link(), t("Razer device control"))?;
    } else {
        let _ = std::fs::remove_file(desktop_link());
    }

    progress.step(t("Starting with Windows…").into(), 0.88);
    if options.autostart {
        install::set_autostart(Some(&install::autostart_command(&app)))?;
    } else if install::autostart_points_into(dir) {
        install::set_autostart(None)?;
    }
    install::set_on_user_path(dir, options.path).map_err(|e| e.to_string())?;

    if repair {
        progress.step(t("Checking settings files…").into(), 0.93);
        check_settings(progress);
    }
    progress.step(t("Done").into(), 1.0);
    Ok(())
}

fn days() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| (d.as_secs() / 86_400) as i64).unwrap_or(0)
}

/// Set aside settings files that no longer read, and a stale endpoint.
///
/// Everything a file held is kept, renamed beside it: the daemon and Studio
/// start afresh rather than refusing to, and nothing is lost for good.
pub fn check_settings(progress: &dyn Progress) {
    let data = paths::data_dir();
    let files = [
        data.join("persistence.json"),
        data.join("profiles.json"),
        data.join("macros.json"),
        data.join("gui.json"),
        paths::studio_dir().join("settings.json"),
    ];
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    for file in files {
        let Ok(text) = std::fs::read_to_string(&file) else { continue };
        if serde_json::from_str::<serde_json::Value>(&text).is_err() {
            let aside = file.with_extension(format!("json.broken-{stamp}"));
            if std::fs::rename(&file, &aside).is_ok() {
                progress.note(tf("{} did not read and was set aside as {}", &[&file.display(), &aside.file_name().unwrap_or_default().to_string_lossy()]));
            }
        }
    }
    let endpoint = openrazer_studio_core::rpc::Endpoint::path();
    if endpoint.exists() && openrazer_studio_core::rpc::Client::connect().is_err() && std::fs::remove_file(&endpoint).is_ok() {
        progress.note(t("Removed a leftover note of a daemon that is no longer running").into());
    }
}

// -- removing -----------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Removal {
    /// Studio's own settings and macro library.
    pub settings: bool,
    /// Everything in `%LOCALAPPDATA%\openrazer-win`: device settings,
    /// profiles, key bindings, logs -- shared with the classic panel and pip.
    pub all_data: bool,
}

pub fn uninstall(dir: &Path, removal: Removal, progress: &dyn Progress) -> Result<(), String> {
    progress.step(t("Closing OpenRazer Studio and the daemon…").into(), 0.1);
    let stuck = process::stop_everything_in(dir);
    if !stuck.is_empty() {
        return Err(tf("These are still running and could not be closed: {}. Close them and try again.", &[&stuck.join(", ")]));
    }
    progress.step(t("Removing shortcuts and settings in Windows…").into(), 0.3);
    if install::autostart_points_into(dir) {
        install::set_autostart(None)?;
        progress.note(t("Studio no longer starts with Windows").into());
    }
    let _ = install::set_on_user_path(dir, false);
    let menu = start_menu();
    if menu.exists() {
        let _ = std::fs::remove_dir_all(&menu);
    }
    let _ = std::fs::remove_file(desktop_link());
    install::unregister().map_err(|e| e.to_string())?;

    progress.step(tf("Deleting {}…", &[&dir.display()]), 0.55);
    let manifest = Manifest::read(dir).unwrap_or_default();
    let mut names: Vec<String> = manifest.files.iter().map(|(name, _)| name.clone()).collect();
    for known in [APP_EXE, DAEMON_EXE, SETUP_EXE, "LICENSE.txt", MANIFEST] {
        if !names.iter().any(|n| n == known) {
            names.push(known.to_string());
        }
    }
    let me = std::env::current_exe().ok();
    for name in names {
        let path = dir.join(&name);
        if me.as_ref().is_some_and(|me| install::same_path(me, &path)) {
            continue; // removed after this process exits
        }
        if path.exists() {
            std::fs::remove_file(&path).map_err(|e| tf("cannot delete {}: {}", &[&path.display(), &e]))?;
        }
    }
    for leftover in std::fs::read_dir(dir).into_iter().flatten().flatten() {
        let name = leftover.file_name().to_string_lossy().to_string();
        if name.ends_with(".partial") {
            let _ = std::fs::remove_file(leftover.path());
        }
    }
    // Only an empty folder goes: anything else in it was put there by someone else.
    if std::fs::read_dir(dir).map(|mut d| d.next().is_none()).unwrap_or(false) {
        let _ = std::fs::remove_dir(dir);
    }

    if removal.all_data {
        progress.step(t("Deleting your settings…").into(), 0.85);
        let data = paths::data_dir();
        if data.exists() {
            std::fs::remove_dir_all(&data).map_err(|e| tf("cannot delete {}: {}", &[&data.display(), &e]))?;
            progress.note(tf("Deleted {}", &[&data.display()]));
        }
    } else if removal.settings {
        progress.step(t("Deleting your settings…").into(), 0.85);
        let studio = paths::studio_dir();
        if studio.exists() {
            let _ = std::fs::remove_dir_all(&studio);
            progress.note(tf("Deleted {}", &[&studio.display()]));
        }
    }
    progress.step(t("Done").into(), 1.0);
    Ok(())
}

/// Have Windows delete `file` once this process has exited.
///
/// The installer that removes Studio is a copy of itself in the temporary
/// folder; it cannot delete its own running file, so a hidden command waits a
/// moment and deletes it.
pub fn delete_after_exit(file: &Path) {
    let mut command = std::process::Command::new("cmd.exe");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // Verbatim: cmd does not read the backslash-escaped quotes that
        // ordinary argument quoting would put round the path.
        command.raw_arg(format!("/d /c ping 127.0.0.1 -n 4 > nul & del /f /q \"{}\"", file.display()));
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let _ = command.spawn();
}

/// The install folder to repair or remove: the installer's own, when it is
/// the copy kept there, otherwise the one Apps & features knows about.
pub fn target_dir(explicit: Option<PathBuf>) -> Option<PathBuf> {
    if explicit.is_some() {
        return explicit;
    }
    let me = std::env::current_exe().ok()?;
    let here = me.parent()?.to_path_buf();
    if here.join(MANIFEST).exists() && here.join(APP_EXE).exists() {
        return Some(here);
    }
    install::installed().map(|i| i.dir)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Quiet;
    impl Progress for Quiet {
        fn step(&self, _: String, _: f32) {}
        fn note(&self, _: String) {}
    }

    #[test]
    fn a_file_already_right_is_left_alone() {
        let dir = std::env::temp_dir().join(format!("openrazer-setup-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("a.bin");
        assert!(put(&file, b"one").unwrap());
        assert!(!put(&file, b"one").unwrap());
        assert!(put(&file, b"two").unwrap());
        assert_eq!(std::fs::read(&file).unwrap(), b"two");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn without_a_payload_install_says_so() {
        if has_payload() {
            return;
        }
        let dir = std::env::temp_dir().join("openrazer-setup-nothing");
        assert!(install(&dir, &Options::default(), false, &Quiet).is_err());
        assert!(!dir.exists());
    }
}
