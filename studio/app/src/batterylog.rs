//! The last battery level each device reported, kept across restarts (issue #30).
//!
//! A wireless mouse switched off behind its receiver reports 0%, which says
//! nothing about its battery. The last real reading is shown instead, marked
//! as such, until the mouse answers again.

use openrazer_studio_core::{paths, tf};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Reading {
    pub level: f64,
    /// When it was read, in seconds since 1970.
    pub at: u64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct BatteryLog {
    pub devices: BTreeMap<String, Reading>,
}

fn path() -> PathBuf {
    paths::studio_dir().join("battery.json")
}

pub fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

impl BatteryLog {
    pub fn load() -> BatteryLog {
        std::fs::read_to_string(path()).ok().and_then(|text| serde_json::from_str(&text).ok()).unwrap_or_default()
    }

    fn save(&self) {
        let _ = std::fs::create_dir_all(paths::studio_dir());
        if let Ok(text) = serde_json::to_string_pretty(self) {
            let _ = std::fs::write(path(), text);
        }
    }

    /// Take one reading in. Returns what to show: the reading itself, or the
    /// last real one with when it was taken, when this one is no reading.
    pub fn take(&mut self, serial: &str, level: Option<f64>, at: u64) -> (Option<f64>, Option<u64>) {
        match level {
            Some(level) if level > 0.0 => {
                let changed = self.devices.get(serial).is_none_or(|r| r.level.round() != level.round());
                self.devices.insert(serial.to_string(), Reading { level, at });
                if changed {
                    // Written when the percentage moves, not at every poll.
                    self.save();
                }
                (Some(level), None)
            }
            _ => match self.devices.get(serial) {
                Some(last) => (Some(last.level), Some(last.at)),
                None => (level, None),
            },
        }
    }
}

/// "5 min ago", "3 h ago", "2 days ago".
pub fn ago(at: u64, now: u64) -> String {
    let minutes = now.saturating_sub(at) / 60;
    if minutes < 60 {
        tf("{} min ago", &[&minutes.max(1)])
    } else if minutes < 48 * 60 {
        tf("{} h ago", &[&(minutes / 60)])
    } else {
        tf("{} days ago", &[&(minutes / (24 * 60))])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_from_a_switched_off_mouse_shows_the_last_real_level() {
        let mut log = BatteryLog::default();
        log.devices.insert("A".into(), Reading { level: 55.0, at: 100 });
        assert_eq!(log.take("A", Some(0.0), 900), (Some(55.0), Some(100)));
        assert_eq!(log.take("A", None, 900), (Some(55.0), Some(100)));
        // Never seen: nothing better to show than what it says.
        assert_eq!(log.take("B", Some(0.0), 900), (Some(0.0), None));
    }

    #[test]
    fn ages_read_as_people_say_them() {
        assert!(ago(0, 30).contains('1'));
        assert!(ago(0, 3 * 3600).contains('3'));
        assert!(ago(0, 3 * 86_400).contains('3'));
    }
}
