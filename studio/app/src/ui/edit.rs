//! What the controls show for one device, loaded from the daemon's state.
//!
//! The controls hold their own values so dragging a slider is smooth; they
//! are reloaded from the daemon whenever its saved state moves on, unless the
//! user touched something moments ago -- a reload then would snap a slider
//! back under their hand.

use crate::worker::{Command, Detail, Device, Snapshot};
use egui::Color32;
use serde_json::Value;
use std::collections::HashMap;
use std::time::{Duration, Instant};

pub struct Pending {
    pub key: String,
    pub command: Command,
    pub due: Instant,
}

pub const DEFAULT_COLOURS: [Color32; 3] = [
    Color32::from_rgb(0x44, 0xD6, 0x2C),
    Color32::from_rgb(0x00, 0xC8, 0xFF),
    Color32::from_rgb(0x8A, 0x3C, 0xFF),
];

#[derive(Clone, Debug)]
pub struct ZoneEdit {
    pub effect: String,
    pub colours: [Color32; 3],
    pub active: usize,
    pub speed: i64,
    pub direction: i64,
    pub brightness: Option<f32>,
}

impl Default for ZoneEdit {
    fn default() -> Self {
        ZoneEdit { effect: String::new(), colours: DEFAULT_COLOURS, active: 0, speed: 1, direction: 1, brightness: None }
    }
}

#[derive(Default)]
pub struct DeviceEdit {
    pub zone: Option<String>,
    pub zones: HashMap<String, ZoneEdit>,
    pub dpi: Option<(i64, i64)>,
    pub dpi_linked: bool,
    pub stages: Option<(i64, Vec<(i64, i64)>)>,
    pub poll_rate: Option<i64>,
    pub idle_minutes: Option<f32>,
    pub low_battery: Option<f32>,
    generation: u64,
    detail_seen: bool,
    touched: Option<Instant>,
}

impl DeviceEdit {
    /// Note that the user just changed something.
    pub fn touch(&mut self) {
        self.touched = Some(Instant::now());
    }

    fn recently_touched(&self) -> bool {
        self.touched.is_some_and(|t| t.elapsed() < Duration::from_millis(900))
    }

    pub fn refresh(&mut self, device: &Device, detail: &Detail, snap: &Snapshot) {
        let Some(caps) = detail.caps.as_ref() else { return };
        let first = !self.detail_seen;
        if !first && (snap.generation == self.generation || self.recently_touched()) {
            return;
        }
        self.detail_seen = true;
        self.generation = snap.generation;
        self.dpi_linked = self.dpi_linked || first;

        let saved = &snap.persistence[&device.serial];
        let zone_names: Vec<String> = caps["zones"].as_object().map(|z| z.keys().cloned().collect()).unwrap_or_default();
        if self.zone.as_ref().is_none_or(|z| !zone_names.contains(z)) {
            self.zone = order_zones(&zone_names).into_iter().next();
        }
        for zone in &zone_names {
            let state = &saved["zones"][zone];
            let edit = self.zones.entry(zone.clone()).or_default();
            edit.effect = state["effect"].as_str().unwrap_or_default().to_string();
            let ears = saved["zone_colours"].is_array().then(|| [rgb(&saved["zone_colours"], 0), rgb(&saved["zone_colours"], 3)]);
            let colours = if let Some(ears) = ears.filter(|_| matches!(zone.as_str(), "backlight" | "left" | "right")) {
                // The Bluetooth headset keeps each ear's colour at the device
                // level, and no effect per ear: an ear is lit or it is dark.
                // Reading the ear's own (absent) effect blanked the effect a
                // moment after it was chosen, taking the colour picker with it,
                // so an ear once turned off could not be lit again. Both ears
                // show the colour of whichever is lit, not always the left's.
                let colour = match zone.as_str() {
                    "left" => ears[0],
                    "right" => ears[1],
                    _ => ears.into_iter().find(|c| *c != Color32::BLACK).unwrap_or(Color32::BLACK),
                };
                edit.effect = if colour == Color32::BLACK { "none" } else { "static" }.to_string();
                Some([colour, edit.colours[1], edit.colours[2]])
            } else if state["colors"].is_array() {
                Some([rgb(&state["colors"], 0), rgb(&state["colors"], 3), rgb(&state["colors"], 6)])
            } else {
                None
            };
            if let Some(colours) = colours {
                edit.colours = colours;
            }
            if zone == "backlight" {
                if let Some(effect) = snap.software.get(&device.serial) {
                    edit.effect = effect.clone();
                }
            }
            edit.speed = state["speed"].as_i64().unwrap_or(1).clamp(1, 4);
            edit.direction = state["wave_dir"].as_i64().unwrap_or(1).clamp(1, 2);
            edit.brightness = detail
                .brightness
                .get(zone)
                .copied()
                .or_else(|| state["brightness"].as_f64())
                .map(|b| b as f32);
        }
        if first || detail.dpi.is_some() {
            self.dpi = detail.dpi.or(self.dpi);
        }
        self.stages = detail.dpi_stages.clone().or(self.stages.take());
        self.poll_rate = detail.poll_rate.or(self.poll_rate);
        self.idle_minutes = detail.idle_time.map(|s| s as f32 / 60.0).or(self.idle_minutes);
        self.low_battery = detail.low_battery.map(|v| v as f32).or(self.low_battery);
    }
}

fn rgb(flat: &Value, offset: usize) -> Color32 {
    let at = |i: usize| flat[offset + i].as_u64().unwrap_or(0).min(255) as u8;
    if flat[offset].is_null() {
        return DEFAULT_COLOURS[offset / 3 % 3];
    }
    Color32::from_rgb(at(0), at(1), at(2))
}

/// Give an effect that is chosen with black colours something to show.
///
/// A zone that was turned off reads back black, so choosing "Static" there
/// lit it black -- still dark.
pub fn light_up(edit: &mut ZoneEdit, count: usize) {
    for (slot, colour) in edit.colours.iter_mut().enumerate().take(count) {
        if *colour == Color32::BLACK {
            *colour = DEFAULT_COLOURS[slot];
        }
    }
}

/// The order zones are offered in: the whole device first.
pub fn order_zones(zones: &[String]) -> Vec<String> {
    const ORDER: [&str; 8] = ["backlight", "logo", "scroll", "left", "right", "charging", "fast_charging", "fully_charged"];
    let mut sorted: Vec<String> = zones.to_vec();
    sorted.sort_by_key(|z| ORDER.iter().position(|o| o == z).unwrap_or(ORDER.len()));
    sorted
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn headset(left: [u8; 3], right: [u8; 3]) -> (Device, Detail, Snapshot) {
        let device = Device { serial: "BLE1".into(), kind: "headset".into(), ..Default::default() };
        let zones = json!({ "backlight": { "effects": ["none", "static"] }, "left": { "effects": ["none", "static"] }, "right": { "effects": ["none", "static"] } });
        let detail = Detail { caps: Some(json!({ "zones": zones, "zone_colours": true })), ..Default::default() };
        let flat: Vec<u8> = left.iter().chain(right.iter()).copied().collect();
        let snap = Snapshot {
            persistence: json!({ "BLE1": { "zone_colours": flat, "zones": { "backlight": { "effect": "static", "colors": flat } } } }),
            generation: 1,
            ..Default::default()
        };
        (device, detail, snap)
    }

    #[test]
    fn an_ear_reads_as_lit_or_dark_from_its_colour() {
        let (device, detail, snap) = headset([0, 0, 0], [255, 0, 0]);
        let mut edit = DeviceEdit::default();
        edit.refresh(&device, &detail, &snap);
        assert_eq!(edit.zones["left"].effect, "none");
        assert_eq!(edit.zones["right"].effect, "static");
        assert_eq!(edit.zones["right"].colours[0], Color32::from_rgb(255, 0, 0));
        // Both ears: lit, in the colour of the one that is.
        assert_eq!(edit.zones["backlight"].effect, "static");
        assert_eq!(edit.zones["backlight"].colours[0], Color32::from_rgb(255, 0, 0));
    }

    #[test]
    fn both_ears_dark_read_as_off() {
        let (device, detail, snap) = headset([0, 0, 0], [0, 0, 0]);
        let mut edit = DeviceEdit::default();
        edit.refresh(&device, &detail, &snap);
        assert_eq!(edit.zones["backlight"].effect, "none");
    }

    #[test]
    fn an_ear_lit_again_keeps_its_effect_after_a_reload() {
        let (device, detail, mut snap) = headset([0, 0, 0], [255, 0, 0]);
        let mut edit = DeviceEdit::default();
        edit.refresh(&device, &detail, &snap);
        // What the daemon saves once the left ear is set to static green.
        snap.persistence["BLE1"]["zone_colours"] = json!([0x44, 0xD6, 0x2C, 255, 0, 0]);
        snap.generation += 1;
        edit.refresh(&device, &detail, &snap);
        assert_eq!(edit.zones["left"].effect, "static");
        assert_eq!(edit.zones["left"].colours[0], Color32::from_rgb(0x44, 0xD6, 0x2C));
    }

    #[test]
    fn lighting_up_replaces_only_black_colours_in_use() {
        let mut zone = ZoneEdit { colours: [Color32::BLACK, Color32::BLACK, Color32::RED], ..Default::default() };
        light_up(&mut zone, 1);
        assert_eq!(zone.colours, [DEFAULT_COLOURS[0], Color32::BLACK, Color32::RED]);
        light_up(&mut zone, 3);
        assert_eq!(zone.colours, [DEFAULT_COLOURS[0], DEFAULT_COLOURS[1], Color32::RED]);
    }
}
