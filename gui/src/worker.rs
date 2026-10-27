//! Everything that talks to the daemon, off the UI thread.
//!
//! The window must never wait on the daemon: a Bluetooth call can take
//! seconds. So the UI sends commands down a channel, one worker thread runs
//! them against the daemon in order, and what comes back lands in a shared
//! snapshot the UI reads every frame.

use crate::rpc::{Client, RpcError};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// How often the device list and daemon state are refreshed.
const POLL: Duration = Duration::from_millis(2000);
/// Battery levels change slowly, and reading them wakes wireless devices.
const BATTERY_POLL: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, Default)]
pub enum DaemonState {
    #[default]
    Connecting,
    NotRunning(String),
    Running(Value),
}

#[derive(Clone, Debug, Default)]
pub struct Device {
    pub serial: String,
    pub name: String,
    pub kind: String,
    pub pid: u16,
    pub image: Option<String>,
    /// A picture per colour it is sold in, when it cannot say which it is.
    pub images: Vec<(String, String)>,
    pub firmware: Option<String>,
}

#[derive(Clone, Debug, Default)]
pub struct Detail {
    pub caps: Option<Value>,
    pub battery: Option<f64>,
    pub charging: Option<bool>,
    pub dpi: Option<(i64, i64)>,
    pub dpi_stages: Option<(i64, Vec<(i64, i64)>)>,
    pub poll_rate: Option<i64>,
    pub idle_time: Option<i64>,
    pub low_battery: Option<f64>,
    pub brightness: HashMap<String, f64>,
    pub profiles: Vec<Value>,
    pub macros: Option<Value>,
}

#[derive(Clone, Debug)]
pub struct Toast {
    pub text: String,
    pub error: bool,
    pub at: Instant,
}

#[derive(Clone, Debug, Default)]
pub struct Snapshot {
    pub daemon: DaemonState,
    pub devices: Vec<Device>,
    pub details: HashMap<String, Detail>,
    /// The daemon's saved lighting state, keyed by serial.
    pub persistence: Value,
    pub keys: Value,
    /// Host-rendered effects running, serial -> effect name.
    pub software: HashMap<String, String>,
    pub toast: Option<Toast>,
    /// Bumped whenever the saved state changes, so the UI can reload from it.
    pub generation: u64,
    pub busy: bool,
    /// Why the daemon could not be started, until it is.
    pub start_error: Option<crate::daemon::StartError>,
    /// A Python that could run the daemon once openrazer-win is in it.
    pub python: Option<std::path::PathBuf>,
    /// Installing Python or the package: what is happening, and how far.
    pub setup: Option<(String, Option<f32>)>,
}

pub enum Command {
    Refresh,
    Rescan,
    LoadDevice(String),
    Call { serial: String, method: String, args: Vec<Value>, zone: Option<String>, then: Then },
    Software { serial: String, effect: String, options: Value },
    Profile { action: &'static str, serial: String, name: String },
    MacroSet { serial: String, key: String, action: Value },
    MacroDelete { serial: String, key: String },
    MacroRecordStart { serial: String, key: String },
    MacroRecordStop { serial: String },
    MacroPlay { serial: String, key: String },
    StartDaemon,
    StopDaemon,
    RestartDaemon,
    /// Fetch and open the Python installer, then install openrazer-win.
    InstallPython,
    /// Install openrazer-win into the Python already here.
    InstallPackage,
}

/// What to re-read after a call succeeds.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Then {
    Nothing,
    Lighting,
    Performance,
    Power,
}

pub type Shared = Arc<Mutex<Snapshot>>;

pub fn spawn(shared: Shared, repaint: impl Fn() + Send + 'static) -> Sender<Command> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::Builder::new()
        .name("daemon".into())
        .spawn(move || Worker { shared, client: None, battery_at: HashMap::new(), repaint: Box::new(repaint) }.run(rx))
        .expect("the worker thread starts");
    tx
}

struct Worker {
    shared: Shared,
    client: Option<Client>,
    battery_at: HashMap<String, Instant>,
    repaint: Box<dyn Fn() + Send>,
}

impl Worker {
    fn run(mut self, rx: Receiver<Command>) {
        let mut last_poll = Instant::now() - POLL;
        loop {
            let wait = POLL.saturating_sub(last_poll.elapsed());
            match rx.recv_timeout(wait) {
                Ok(command) => {
                    self.set_busy(true);
                    self.handle(command);
                    self.set_busy(false);
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => return,
            }
            if last_poll.elapsed() >= POLL {
                self.poll();
                last_poll = Instant::now();
            }
            (self.repaint)();
        }
    }

    fn set_busy(&self, busy: bool) {
        self.shared.lock().unwrap().busy = busy;
        (self.repaint)();
    }

    fn toast(&self, text: impl Into<String>, error: bool) {
        self.shared.lock().unwrap().toast = Some(Toast { text: text.into(), error, at: Instant::now() });
    }

    /// A connected client, connecting first if need be.
    fn client(&mut self) -> Result<&mut Client, RpcError> {
        if self.client.is_none() {
            self.client = Some(Client::connect()?);
        }
        Ok(self.client.as_mut().unwrap())
    }

    /// Run `f` against the daemon, dropping the connection if it broke.
    fn with<T>(&mut self, f: impl FnOnce(&mut Client) -> Result<T, RpcError>) -> Result<T, RpcError> {
        let result = match self.client() {
            Ok(client) => f(client),
            Err(error) => Err(error),
        };
        if let Err(RpcError::Unavailable(_)) = &result {
            self.client = None;
        }
        result
    }

    fn poll(&mut self) {
        let version = self.with(|c| c.call("daemon.version", json!({})));
        let info = match version {
            Ok(info) => info,
            Err(error) => {
                let mut shared = self.shared.lock().unwrap();
                shared.daemon = DaemonState::NotRunning(error.to_string());
                shared.devices.clear();
                return;
            }
        };
        let devices = self.with(|c| c.call("devices.list", json!({}))).unwrap_or(Value::Null);
        let keys = self.with(|c| c.call("keys.status", json!({}))).unwrap_or(Value::Null);
        let effects = self.with(|c| c.call("effect.status", json!({}))).unwrap_or(Value::Null);
        let devices: Vec<Device> = devices
            .as_array()
            .map(|rows| rows.iter().map(parse_device).collect())
            .unwrap_or_default();
        let software = effects
            .get("active")
            .and_then(Value::as_object)
            .map(|active| {
                active
                    .iter()
                    .filter_map(|(serial, effect)| Some((serial.clone(), effect.as_str()?.to_string())))
                    .collect()
            })
            .unwrap_or_default();

        let new: Vec<String> = {
            let mut shared = self.shared.lock().unwrap();
            let first = !matches!(shared.daemon, DaemonState::Running(_));
            shared.daemon = DaemonState::Running(info);
            shared.keys = keys;
            shared.software = software;
            let new = devices
                .iter()
                .filter(|d| first || !shared.details.contains_key(&d.serial))
                .map(|d| d.serial.clone())
                .collect();
            shared.devices = devices;
            new
        };
        if !new.is_empty() {
            self.load_persistence();
        }
        for serial in new {
            self.load_device(&serial);
        }
        self.poll_batteries();
    }

    fn poll_batteries(&mut self) {
        let due: Vec<String> = {
            let shared = self.shared.lock().unwrap();
            shared
                .devices
                .iter()
                .filter(|d| {
                    shared.details.get(&d.serial).and_then(|x| x.caps.as_ref()).is_some_and(|caps| caps["battery"] == true)
                })
                .map(|d| d.serial.clone())
                .filter(|serial| self.battery_at.get(serial).is_none_or(|at| at.elapsed() >= BATTERY_POLL))
                .collect()
        };
        for serial in due {
            self.battery_at.insert(serial.clone(), Instant::now());
            self.read_power(&serial, false);
        }
    }

    fn load_persistence(&mut self) {
        if let Ok(state) = self.with(|c| c.call("daemon.persistence", json!({}))) {
            let mut shared = self.shared.lock().unwrap();
            shared.persistence = state;
            shared.generation += 1;
        }
    }

    fn load_device(&mut self, serial: &str) {
        let caps = match self.with(|c| c.call("device.capabilities", json!({ "serial": serial }))) {
            Ok(caps) => caps,
            Err(_) => return,
        };
        self.update(serial, |d| d.caps = Some(caps.clone()));
        self.read_lighting(serial, &caps);
        self.read_performance(serial, &caps);
        self.read_power(serial, true);
        self.read_profiles(serial);
        self.read_macros(serial, &caps);
        self.battery_at.insert(serial.to_string(), Instant::now());
    }

    fn update(&self, serial: &str, f: impl FnOnce(&mut Detail)) {
        let mut shared = self.shared.lock().unwrap();
        f(shared.details.entry(serial.to_string()).or_default());
    }

    fn read_lighting(&mut self, serial: &str, caps: &Value) {
        let zones: Vec<String> = caps["zones"]
            .as_object()
            .map(|zones| zones.iter().filter(|(_, z)| z["brightness"] == true).map(|(name, _)| name.clone()).collect())
            .unwrap_or_default();
        for zone in zones {
            if let Ok(level) = self.with(|c| c.device(serial, "get_brightness", vec![], Some(&zone))) {
                if let Some(level) = level.as_f64() {
                    self.update(serial, |d| {
                        d.brightness.insert(zone.clone(), level);
                    });
                }
            }
        }
    }

    fn read_performance(&mut self, serial: &str, caps: &Value) {
        if caps["dpi"] == true {
            if let Ok(dpi) = self.with(|c| c.device(serial, "get_dpi", vec![], None)) {
                let pair = (dpi[0].as_i64().unwrap_or(0), dpi[1].as_i64().unwrap_or(0));
                self.update(serial, |d| d.dpi = Some(pair));
            }
        }
        if caps["dpi_stages"] == true {
            if let Ok(stages) = self.with(|c| c.device(serial, "get_dpi_stages", vec![], None)) {
                let active = stages[0].as_i64().unwrap_or(1);
                let list = stages[1]
                    .as_array()
                    .map(|rows| rows.iter().map(|s| (s[0].as_i64().unwrap_or(0), s[1].as_i64().unwrap_or(0))).collect())
                    .unwrap_or_default();
                self.update(serial, |d| d.dpi_stages = Some((active, list)));
            }
        }
        if caps["poll_rate"] == true {
            if let Ok(rate) = self.with(|c| c.device(serial, "get_poll_rate", vec![], None)) {
                self.update(serial, |d| d.poll_rate = rate.as_i64());
            }
        }
    }

    fn read_power(&mut self, serial: &str, settings: bool) {
        let caps = self.shared.lock().unwrap().details.get(serial).and_then(|d| d.caps.clone());
        let Some(caps) = caps else { return };
        if caps["battery"] == true {
            if let Ok(level) = self.with(|c| c.device(serial, "get_battery_level", vec![], None)) {
                self.update(serial, |d| d.battery = level.as_f64());
            }
            if let Ok(charging) = self.with(|c| c.device(serial, "is_charging", vec![], None)) {
                self.update(serial, |d| d.charging = charging.as_bool());
            }
            if settings {
                if let Ok(threshold) = self.with(|c| c.device(serial, "get_low_battery_threshold", vec![], None)) {
                    self.update(serial, |d| d.low_battery = threshold.as_f64());
                }
            }
        }
        if settings && caps["idle_time"] == true {
            if let Ok(idle) = self.with(|c| c.device(serial, "get_idle_time", vec![], None)) {
                self.update(serial, |d| d.idle_time = idle.as_i64());
            }
        }
    }

    fn read_profiles(&mut self, serial: &str) {
        if let Ok(list) = self.with(|c| c.call("profile.list", json!({ "serial": serial }))) {
            let rows = list.as_array().cloned().unwrap_or_default();
            self.update(serial, |d| d.profiles = rows);
        }
    }

    fn read_macros(&mut self, serial: &str, caps: &Value) {
        if caps["type"] != "keyboard" && caps["dedicated_macro_keys"] != true {
            return;
        }
        if let Ok(info) = self.with(|c| c.call("macro.list", json!({ "serial": serial }))) {
            self.update(serial, |d| d.macros = Some(info));
        }
    }

    fn report<T>(&self, result: &Result<T, RpcError>, success: Option<String>) {
        match result {
            Ok(_) => {
                if let Some(text) = success {
                    self.toast(text, false);
                }
            }
            Err(error) => self.toast(error.to_string(), true),
        }
    }

    fn handle(&mut self, command: Command) {
        match command {
            Command::Refresh => self.poll(),
            Command::Rescan => {
                let result = self.with(|c| c.call("daemon.rescan", json!({})));
                self.report(&result, Some("Rescanned".into()));
                self.poll();
            }
            Command::LoadDevice(serial) => {
                self.load_persistence();
                self.load_device(&serial);
            }
            Command::Call { serial, method, args, zone, then } => {
                let result = self.with(|c| c.device(&serial, &method, args, zone.as_deref()));
                self.report(&result, None);
                if result.is_ok() {
                    let caps = self.shared.lock().unwrap().details.get(&serial).and_then(|d| d.caps.clone());
                    match (then, caps) {
                        (Then::Lighting, Some(caps)) => {
                            self.load_persistence();
                            self.read_lighting(&serial, &caps);
                        }
                        (Then::Performance, Some(caps)) => self.read_performance(&serial, &caps),
                        (Then::Power, _) => self.read_power(&serial, true),
                        _ => {}
                    }
                }
            }
            Command::Software { serial, effect, options } => {
                let result = self.with(|c| {
                    c.call("effect.set", json!({ "serial": serial, "effect": effect, "options": options }))
                });
                self.report(&result, None);
            }
            Command::Profile { action, serial, name } => {
                let method = format!("profile.{action}");
                let result = self.with(|c| c.call(&method, json!({ "serial": serial, "name": name })));
                let verb = match action {
                    "save" => "saved",
                    "load" => "applied",
                    _ => "deleted",
                };
                self.report(&result, Some(format!("Profile “{name}” {verb}")));
                self.read_profiles(&serial);
                if action == "load" {
                    self.load_persistence();
                    let caps = self.shared.lock().unwrap().details.get(&serial).and_then(|d| d.caps.clone());
                    if let Some(caps) = caps {
                        self.read_lighting(&serial, &caps);
                    }
                }
            }
            Command::MacroSet { serial, key, action } => {
                let result = self.with(|c| c.call("macro.set", json!({ "serial": serial, "key": key, "action": action })));
                self.report(&result, Some(format!("{key} saved")));
                self.reload_macros(&serial);
            }
            Command::MacroDelete { serial, key } => {
                let result = self.with(|c| c.call("macro.delete", json!({ "serial": serial, "key": key })));
                self.report(&result, Some(format!("{key} is back to its default")));
                self.reload_macros(&serial);
            }
            Command::MacroRecordStart { serial, key } => {
                let result = self.with(|c| c.call("macro.record_start", json!({ "serial": serial, "key": key })));
                self.report(&result, Some(format!("Recording {key} — type the macro, then press Stop")));
                self.reload_macros(&serial);
            }
            Command::MacroRecordStop { serial } => {
                let result = self.with(|c| c.call("macro.record_stop", json!({})));
                match &result {
                    Ok(Value::Null) => self.toast("Nothing was recorded; the key is back to its default", false),
                    Ok(saved) => {
                        let count = saved["events"].as_array().map_or(0, Vec::len);
                        self.toast(format!("Recorded {count} key events"), false)
                    }
                    Err(error) => self.toast(error.to_string(), true),
                }
                self.reload_macros(&serial);
            }
            Command::MacroPlay { serial, key } => {
                let result = self.with(|c| c.call("macro.play", json!({ "serial": serial, "key": key })));
                self.report(&result, None);
            }
            Command::StartDaemon => self.start_daemon(false),
            Command::StopDaemon => {
                let result = crate::daemon::stop();
                self.client = None;
                match result {
                    Ok(()) => self.toast("Daemon stopped", false),
                    Err(error) => self.toast(error, true),
                }
                self.poll();
            }
            Command::RestartDaemon => self.start_daemon(true),
            Command::InstallPython => {
                let result = self.install_python().and_then(|python| self.install_package(&python));
                self.finish_setup(result);
            }
            Command::InstallPackage => {
                let result = match crate::setup::find_python() {
                    Some(python) => self.install_package(&python),
                    None => Err("No Python 3.9 or newer was found.".to_string()),
                };
                self.finish_setup(result);
            }
        }
    }

    fn start_daemon(&mut self, restart: bool) {
        self.client = None;
        let result = if restart { crate::daemon::restart() } else { crate::daemon::start() };
        self.client = None;
        match result {
            Ok(()) => {
                let mut shared = self.shared.lock().unwrap();
                shared.start_error = None;
                if restart {
                    shared.details.clear();
                }
                drop(shared);
                self.toast(if restart { "Daemon restarted" } else { "Daemon started" }, false);
            }
            Err(error) => {
                let python = if error.missing { crate::setup::find_python() } else { None };
                let mut shared = self.shared.lock().unwrap();
                shared.python = python;
                shared.start_error = Some(error);
                drop(shared);
                self.toast("The daemon could not be started", true);
            }
        }
        self.poll();
    }

    fn stage(&self, text: impl Into<String>, progress: Option<f32>) {
        self.shared.lock().unwrap().setup = Some((text.into(), progress));
        (self.repaint)();
    }

    fn install_python(&mut self) -> Result<std::path::PathBuf, String> {
        let version = crate::setup::PYTHON_VERSION;
        self.stage(format!("Downloading Python {version} from python.org…"), Some(0.0));
        let shared = self.shared.clone();
        let repaint = &self.repaint;
        let installer = crate::setup::download_installer(&|fraction| {
            if let Some(stage) = shared.lock().unwrap().setup.as_mut() {
                stage.1 = Some(fraction);
            }
            repaint();
        })?;
        self.stage(format!("Finish the Python {version} installer; this continues once it closes."), None);
        crate::setup::run_installer(&installer)?;
        let _ = std::fs::remove_file(&installer);
        crate::setup::find_python().ok_or_else(|| "Python was installed, but cannot be found; start this program again.".to_string())
    }

    fn install_package(&mut self, python: &std::path::Path) -> Result<(), String> {
        self.stage("Installing openrazer-win with pip…", None);
        crate::setup::install_package(python)
    }

    fn finish_setup(&mut self, result: Result<(), String>) {
        self.shared.lock().unwrap().setup = None;
        match result {
            Ok(()) => self.start_daemon(false),
            Err(message) => {
                let python = crate::setup::find_python();
                let mut shared = self.shared.lock().unwrap();
                shared.python = python;
                shared.start_error = Some(crate::daemon::StartError { message, missing: true });
            }
        }
    }

    fn reload_macros(&mut self, serial: &str) {
        if let Ok(info) = self.with(|c| c.call("macro.list", json!({ "serial": serial }))) {
            self.update(serial, |d| d.macros = Some(info));
        }
        if let Ok(keys) = self.with(|c| c.call("keys.status", json!({}))) {
            self.shared.lock().unwrap().keys = keys;
        }
    }
}

fn parse_device(row: &Value) -> Device {
    Device {
        serial: row["serial"].as_str().unwrap_or_default().to_string(),
        name: row["name"].as_str().unwrap_or("Razer device").to_string(),
        kind: row["type"].as_str().unwrap_or("device").to_string(),
        pid: row["pid"].as_u64().unwrap_or(0) as u16,
        image: row["image"].as_str().filter(|s| !s.is_empty()).map(str::to_string),
        images: row["images"]
            .as_object()
            .map(|images| images.iter().filter_map(|(colour, url)| Some((colour.clone(), url.as_str()?.to_string()))).collect())
            .unwrap_or_default(),
        firmware: row["firmware"].as_str().map(str::to_string),
    }
}

/// Positional and keyword arguments for `device.call`, built up readably.
pub fn args(values: &[i64]) -> Vec<Value> {
    values.iter().map(|v| json!(v)).collect()
}

#[allow(dead_code)]
pub fn object(pairs: &[(&str, Value)]) -> Value {
    let mut map = Map::new();
    for (key, value) in pairs {
        map.insert((*key).to_string(), value.clone());
    }
    Value::Object(map)
}
