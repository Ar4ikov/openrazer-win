//! Studio's own settings: how the window behaves, the macro library, and
//! which profile each app switches to.  The daemon keeps everything about
//! the devices themselves.

use crate::layout::Variant;
use openrazer_studio_core::i18n::Lang;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::PathBuf;

/// "When `exe` is in front, `serial` shows `profile`."
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct AppRule {
    pub exe: String,
    pub serial: String,
    pub profile: String,
}

/// A macro kept in the library, ready to put on any key.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct LibraryMacro {
    pub name: String,
    /// A binding as the daemon stores it: `{"type": "keys", "events": [...]}`, etc.
    pub action: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub language: Lang,
    pub close_to_tray: bool,
    pub check_updates: bool,
    /// Which colour each device is, by serial, for those that cannot say.
    pub colours: BTreeMap<String, String>,
    /// A keyboard layout chosen by hand, by serial, over the one worked out.
    pub key_layouts: BTreeMap<String, Variant>,
    pub auto_switch: bool,
    pub app_rules: Vec<AppRule>,
    /// What each device goes back to when no app with a rule is in front.
    pub default_profiles: BTreeMap<String, String>,
    pub macros: Vec<LibraryMacro>,
    /// Brightness before "Lights off", by serial and zone, to put back.
    pub lights_before: BTreeMap<String, BTreeMap<String, f64>>,
    pub recent_colours: Vec<[u8; 3]>,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            language: Lang::System,
            close_to_tray: true,
            check_updates: true,
            colours: BTreeMap::new(),
            key_layouts: BTreeMap::new(),
            auto_switch: true,
            app_rules: Vec::new(),
            default_profiles: BTreeMap::new(),
            macros: Vec::new(),
            lights_before: BTreeMap::new(),
            recent_colours: Vec::new(),
        }
    }
}

impl Settings {
    pub fn path() -> PathBuf {
        openrazer_studio_core::paths::studio_dir().join("settings.json")
    }

    /// The saved settings; a file that will not read is set aside, not lost.
    pub fn load() -> Settings {
        let path = Self::path();
        let Ok(text) = std::fs::read_to_string(&path) else { return Settings::default() };
        match serde_json::from_str(&text) {
            Ok(settings) => settings,
            Err(_) => {
                let _ = std::fs::rename(&path, path.with_extension("json.unreadable"));
                Settings::default()
            }
        }
    }

    pub fn save(&self) {
        if let Ok(text) = serde_json::to_string_pretty(self) {
            let _ = openrazer_studio_core::paths::write_atomic(&Self::path(), text.as_bytes());
        }
    }

    pub fn remember_colour(&mut self, colour: egui::Color32) {
        let rgb = [colour.r(), colour.g(), colour.b()];
        self.recent_colours.retain(|c| *c != rgb);
        self.recent_colours.insert(0, rgb);
        self.recent_colours.truncate(8);
    }

    /// The profile `serial` should show while `exe` is in front, if any.
    pub fn rule_for(&self, exe: &str, serial: &str) -> Option<&AppRule> {
        self.app_rules.iter().find(|r| r.exe.eq_ignore_ascii_case(exe) && r.serial == serial)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_older_or_partial_file_fills_in_the_rest() {
        let settings: Settings = serde_json::from_str(r#"{"close_to_tray": false, "language": "ru"}"#).unwrap();
        assert!(!settings.close_to_tray);
        assert_eq!(settings.language, Lang::Ru);
        assert!(settings.check_updates && settings.auto_switch);
    }

    #[test]
    fn recent_colours_keep_the_newest_eight_once_each() {
        let mut settings = Settings::default();
        for i in 0..10u8 {
            settings.remember_colour(egui::Color32::from_rgb(i, 0, 0));
        }
        settings.remember_colour(egui::Color32::from_rgb(5, 0, 0));
        assert_eq!(settings.recent_colours.len(), 8);
        assert_eq!(settings.recent_colours[0], [5, 0, 0]);
        assert_eq!(settings.recent_colours.iter().filter(|c| **c == [5, 0, 0]).count(), 1);
    }
}
