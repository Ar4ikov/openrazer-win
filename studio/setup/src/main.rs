//! OpenRazer Studio's installer: installs, updates, repairs and removes it.
//!
//! The same program does all four, so the copy kept in the install folder --
//! which Apps & features runs for "Modify" and "Uninstall" -- can always put
//! back a missing or damaged file without downloading anything.
//!
//! Without arguments it opens its window. For scripts:
//!
//! ```text
//! setup --install [--dir PATH] [--no-start-menu] [--desktop] [--no-autostart] [--path] [--launch] --silent
//! setup --repair --silent
//! setup --uninstall [--remove-settings] [--remove-all-data] --silent
//! ```

#![windows_subsystem = "windows"]

mod ops;
mod ui;

use openrazer_studio_core::install::{self, Options};
use std::path::PathBuf;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Action {
    Install,
    Repair,
    Uninstall,
    Maintenance,
}

pub struct Args {
    pub action: Option<Action>,
    pub silent: bool,
    pub dir: Option<PathBuf>,
    pub options: Options,
    pub options_given: bool,
    pub launch: bool,
    pub removal: ops::Removal,
    /// This is the temporary copy that removes the installed one.
    pub from_temp: bool,
}

fn parse(args: &[String]) -> Args {
    let mut parsed = Args {
        action: None,
        silent: false,
        dir: None,
        options: Options::default(),
        options_given: false,
        launch: false,
        removal: ops::Removal::default(),
        from_temp: false,
    };
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--install" => parsed.action = Some(Action::Install),
            "--repair" => parsed.action = Some(Action::Repair),
            "--uninstall" => parsed.action = Some(Action::Uninstall),
            "--maintenance" => parsed.action = Some(Action::Maintenance),
            "--silent" | "/S" | "/s" | "--quiet" => parsed.silent = true,
            "--dir" => parsed.dir = iter.next().map(PathBuf::from),
            "--no-start-menu" => {
                parsed.options.start_menu = false;
                parsed.options_given = true;
            }
            "--desktop" => {
                parsed.options.desktop = true;
                parsed.options_given = true;
            }
            "--no-autostart" => {
                parsed.options.autostart = false;
                parsed.options_given = true;
            }
            "--path" => {
                parsed.options.path = true;
                parsed.options_given = true;
            }
            "--launch" => parsed.launch = true,
            "--remove-settings" => parsed.removal.settings = true,
            "--remove-all-data" => parsed.removal.all_data = true,
            "--from-temp" => parsed.from_temp = true,
            _ => {}
        }
    }
    parsed
}

/// Progress for a run without a window: a log in the temporary folder, and the console if there is one.
struct Log(std::sync::Mutex<std::fs::File>);

impl ops::Progress for Log {
    fn step(&self, text: String, _fraction: f32) {
        self.line(&text);
    }
    fn note(&self, text: String) {
        self.line(&format!("  - {text}"));
    }
}

impl Log {
    fn path() -> PathBuf {
        std::env::temp_dir().join("openrazer-studio-setup.log")
    }
    fn open() -> Log {
        let file = std::fs::OpenOptions::new().create(true).append(true).open(Self::path()).or_else(|_| std::fs::File::create(std::env::temp_dir().join("openrazer-setup.log"))).expect("the temporary folder is writable");
        Log(std::sync::Mutex::new(file))
    }
    fn line(&self, text: &str) {
        use std::io::Write;
        println!("{text}");
        let _ = writeln!(self.0.lock().unwrap(), "{text}");
    }
}

fn attach_console() {
    #[cfg(windows)]
    unsafe {
        windows_sys::Win32::System::Console::AttachConsole(windows_sys::Win32::System::Console::ATTACH_PARENT_PROCESS);
    }
}

/// Removing the installed copy from inside it cannot delete the running
/// installer, so a copy in the temporary folder does the work instead.
fn hand_over_to_temp(args: &[String]) -> bool {
    let Ok(me) = std::env::current_exe() else { return false };
    let Some(dir) = ops::target_dir(None) else { return false };
    if !me.parent().is_some_and(|parent| install::same_path(parent, &dir)) {
        return false;
    }
    let copy = std::env::temp_dir().join(format!("openrazer-studio-uninstall-{}.exe", std::process::id()));
    if std::fs::copy(&me, &copy).is_err() {
        return false;
    }
    let mut forwarded: Vec<String> = args.to_vec();
    forwarded.push("--from-temp".into());
    forwarded.push("--dir".into());
    forwarded.push(dir.display().to_string());
    std::process::Command::new(&copy).args(&forwarded).spawn().is_ok()
}

fn main() -> eframe::Result<()> {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let args = parse(&raw);
    let settings = openrazer_studio_core::paths::studio_dir().join("settings.json");
    let language = std::fs::read_to_string(settings).ok().and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok()).and_then(|v| serde_json::from_value(v["language"].clone()).ok()).unwrap_or_default();
    openrazer_studio_core::i18n::set(language);

    if raw.iter().any(|a| a == "--version") {
        attach_console();
        println!("openrazer-studio-setup {}", ops::VERSION);
        return Ok(());
    }
    if raw.iter().any(|a| a == "--self-test") {
        std::process::exit(if ops::has_payload() { 0 } else { 3 });
    }
    if args.action == Some(Action::Uninstall) && !args.from_temp && hand_over_to_temp(&raw) {
        return Ok(());
    }
    if args.silent {
        attach_console();
        std::process::exit(silent(&args));
    }
    ui::run(args)
}

/// Do what the arguments ask without a window; the exit code says how it went.
fn silent(args: &Args) -> i32 {
    let log = Log::open();
    log.line(&format!("OpenRazer Studio setup {} — {:?}", ops::VERSION, args.action));
    let result = match args.action.unwrap_or(Action::Install) {
        Action::Install => {
            let dir = args.dir.clone().unwrap_or_else(openrazer_studio_core::paths::default_install_dir);
            let options = if args.options_given { args.options.clone() } else { install::Manifest::read(&dir).map(|m| m.options).unwrap_or_default() };
            let result = ops::install(&dir, &options, false, &log);
            if result.is_ok() && args.launch {
                let _ = std::process::Command::new(dir.join(install::APP_EXE)).spawn();
            }
            result
        }
        Action::Repair | Action::Maintenance => match ops::target_dir(args.dir.clone()) {
            Some(dir) => {
                let options = install::Manifest::read(&dir).map(|m| m.options).unwrap_or_default();
                ops::install(&dir, &options, true, &log)
            }
            None => {
                log.line("OpenRazer Studio is not installed");
                return 2;
            }
        },
        Action::Uninstall => match ops::target_dir(args.dir.clone()) {
            Some(dir) => {
                let result = ops::uninstall(&dir, args.removal, &log);
                if args.from_temp {
                    if let Ok(me) = std::env::current_exe() {
                        ops::delete_after_exit(&me);
                    }
                }
                result
            }
            None => {
                log.line("OpenRazer Studio is not installed");
                return 2;
            }
        },
    };
    match result {
        Ok(()) => {
            log.line("OK");
            0
        }
        Err(error) => {
            log.line(&format!("FAILED: {error}"));
            1
        }
    }
}
