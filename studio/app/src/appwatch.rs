//! Switching profiles when a game or app comes to the front.
//!
//! Runs on its own thread: the window is often hidden in the tray, and a
//! hidden window draws no frames, so nothing on the UI thread would notice.

use crate::settings::Settings;
use crate::worker::Command;
use std::collections::HashMap;
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// The rules, shared with the window that edits them.
pub type Rules = Arc<Mutex<Settings>>;

pub fn spawn(rules: Rules, tx: Sender<Command>, serials: Arc<Mutex<Vec<String>>>) {
    std::thread::Builder::new()
        .name("app-watch".into())
        .spawn(move || {
            let mut last_exe = String::new();
            // What each device was last switched to, and whether a rule did it.
            let mut applied: HashMap<String, (String, bool)> = HashMap::new();
            loop {
                std::thread::sleep(Duration::from_millis(1000));
                let Some(exe) = openrazer_studio_core::process::foreground_exe() else { continue };
                if exe == last_exe || exe == openrazer_studio_core::install::APP_EXE {
                    continue;
                }
                last_exe = exe.clone();
                let settings = rules.lock().unwrap().clone();
                if !settings.auto_switch {
                    continue;
                }
                for serial in serials.lock().unwrap().clone() {
                    let want = match settings.rule_for(&exe, &serial) {
                        Some(rule) => Some((rule.profile.clone(), true)),
                        // Leaving a ruled app: back to the default, if there is one.
                        None if applied.get(&serial).is_some_and(|(_, by_rule)| *by_rule) => {
                            settings.default_profiles.get(&serial).map(|p| (p.clone(), false))
                        }
                        None => None,
                    };
                    let Some((profile, by_rule)) = want else { continue };
                    if applied.get(&serial).is_some_and(|(p, _)| *p == profile) {
                        applied.insert(serial.clone(), (profile, by_rule));
                        continue;
                    }
                    let _ = tx.send(Command::Profile { action: "load", serial: serial.clone(), name: profile.clone(), quiet: true });
                    applied.insert(serial, (profile, by_rule));
                }
            }
        })
        .ok();
}
