//! A report to attach when asking for help: versions, devices, logs.
//!
//! Plain text, written where the user chooses, so they can read every word
//! of it before sending it anywhere. Serial numbers are shortened.

use crate::worker::{DaemonState, Snapshot};
use std::fmt::Write;

fn mask(serial: &str) -> String {
    let keep: String = serial.chars().take(4).collect();
    format!("{keep}…")
}

pub fn build(snap: &Snapshot) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "OpenRazer Studio {}", env!("CARGO_PKG_VERSION"));
    if let Some(installed) = openrazer_studio_core::install::installed() {
        let _ = writeln!(out, "Installed: {} in {}", installed.version, installed.dir.display());
    }
    let _ = writeln!(out, "Windows: {}", std::env::var("OS").unwrap_or_default());
    match &snap.daemon {
        DaemonState::Running(info) => {
            let _ = writeln!(out, "Daemon: {} (device data {}), backend {}, state {}", info["version"], info["upstream"], info["backend"], info["state"]);
        }
        DaemonState::NotRunning(reason) => {
            let _ = writeln!(out, "Daemon: not running ({reason})");
        }
        DaemonState::Connecting => {
            let _ = writeln!(out, "Daemon: connecting");
        }
    }
    if !snap.synapse.is_empty() {
        let names: Vec<&str> = snap.synapse.iter().map(|(_, n)| n.as_str()).collect();
        let _ = writeln!(out, "Razer Synapse running: {}", names.join(", "));
    }
    let _ = writeln!(out, "\n== Devices ({})", snap.devices.len());
    for device in &snap.devices {
        let _ = writeln!(out, "- {} [{}] 1532:{:04X} serial {} firmware {}", device.name, device.kind, device.pid, mask(&device.serial), device.firmware.as_deref().unwrap_or("?"));
        if let Some(caps) = snap.details.get(&device.serial).and_then(|d| d.caps.as_ref()) {
            let mut caps = caps.clone();
            caps["transport"]["path"] = serde_json::Value::String("…".into());
            let _ = writeln!(out, "  {}", serde_json::to_string(&caps).unwrap_or_default());
        }
        if let Some(effect) = snap.software.get(&device.serial) {
            let _ = writeln!(out, "  live effect: {effect}");
        }
    }
    for (pid, product) in &snap.unsupported {
        let _ = writeln!(out, "- not supported: 1532:{pid:04X} {product}");
    }
    let _ = writeln!(out, "\n== Keys\n{}", serde_json::to_string(&snap.keys).unwrap_or_default());
    for (name, path) in [("daemon.log", crate::daemon::log_path()), ("daemon-start.log", crate::daemon::start_log_path())] {
        let _ = writeln!(out, "\n== {name} (last 150 lines)");
        let _ = writeln!(out, "{}", crate::daemon::tail(&path, 150).unwrap_or_else(|| "(empty)".into()));
    }
    // Serials can turn up in the logs too.
    for device in &snap.devices {
        if device.serial.len() > 4 {
            out = out.replace(&device.serial, &mask(&device.serial));
        }
    }
    out
}
