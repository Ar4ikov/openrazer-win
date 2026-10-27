//! The device's own lighting effects: names, what they take, and the calls that set them.

use super::edit::ZoneEdit;
use crate::worker::{Command, Then};
use egui::Color32;
use openrazer_studio_core::t;
use serde_json::{json, Value};

/// Drawn by the daemon, frame by frame, rather than by the device.
pub const SOFTWARE: [&str; 4] = ["ripple", "ripple_random", "spectrum_soft", "wave_soft"];
/// Need a key press to place; pointless below this many LEDs.
const KEY_REACTIVE: [&str; 2] = ["ripple", "ripple_random"];
const MIN_RIPPLE_CELLS: i64 = 8;

pub const PALETTE: [Color32; 12] = [
    Color32::from_rgb(0x44, 0xD6, 0x2C),
    Color32::from_rgb(0x00, 0xFF, 0x80),
    Color32::from_rgb(0x00, 0xC8, 0xFF),
    Color32::from_rgb(0x00, 0x50, 0xFF),
    Color32::from_rgb(0x8A, 0x3C, 0xFF),
    Color32::from_rgb(0xFF, 0x2D, 0xC8),
    Color32::from_rgb(0xFF, 0x00, 0x40),
    Color32::from_rgb(0xFF, 0x30, 0x00),
    Color32::from_rgb(0xFF, 0x90, 0x00),
    Color32::from_rgb(0xFF, 0xE0, 0x00),
    Color32::from_rgb(0xFF, 0xFF, 0xFF),
    Color32::from_rgb(0xFF, 0xB4, 0x6E),
];

pub fn label(effect: &str) -> &'static str {
    match effect {
        "none" => t("Off"),
        "on" => t("On"),
        "static" => t("Static"),
        "spectrum" => t("Spectrum cycle"),
        "wave" => t("Wave"),
        "wheel" => t("Wheel"),
        "reactive" => t("Reactive"),
        "blinking" => t("Blinking"),
        "breath_random" => t("Breathing · random"),
        "breath_single" => t("Breathing"),
        "breath_dual" => t("Breathing · 2 colours"),
        "breath_triple" => t("Breathing · 3 colours"),
        "starlight_random" => t("Starlight · random"),
        "starlight_single" => t("Starlight"),
        "starlight_dual" => t("Starlight · 2 colours"),
        "ripple" => t("Ripple"),
        "ripple_random" => t("Ripple · random"),
        "spectrum_soft" => t("Smooth spectrum"),
        "wave_soft" => t("Smooth wave"),
        "custom" | "layers" => t("Per-key (Studio)"),
        _ => t("Other"),
    }
}

pub fn colour_count(effect: &str) -> usize {
    match effect {
        "static" | "blinking" | "reactive" | "breath_single" | "starlight_single" | "ripple" | "wave_soft" => 1,
        "breath_dual" | "starlight_dual" => 2,
        "breath_triple" => 3,
        _ => 0,
    }
}

pub fn speeds(effect: &str) -> Option<Vec<(i64, String)>> {
    match effect {
        "reactive" => Some(vec![(1, t("Short").into()), (2, t("Medium").into()), (3, t("Long").into()), (4, t("Longest").into())]),
        e if e.starts_with("starlight") => Some(vec![(1, t("Fast").into()), (2, t("Medium").into()), (3, t("Slow").into())]),
        _ => None,
    }
}

pub fn zone_label(zone: &str, kind: &str) -> &'static str {
    match (zone, kind) {
        ("backlight", "headset") => t("Both ears"),
        ("left", "headset") => t("Left ear"),
        ("right", "headset") => t("Right ear"),
        ("backlight", "mouse") => t("Whole mouse"),
        ("backlight", "mousemat") => t("Edge"),
        ("backlight", _) => t("Backlight"),
        ("logo", _) => t("Logo"),
        ("scroll", _) => t("Scroll wheel"),
        ("left", _) => t("Left side"),
        ("right", _) => t("Right side"),
        ("charging", _) => t("Charging"),
        ("fast_charging", _) => t("Fast charging"),
        ("fully_charged", _) => t("Charged"),
        _ => t("Zone"),
    }
}

fn rgb(c: Color32) -> [i64; 3] {
    [c.r() as i64, c.g() as i64, c.b() as i64]
}

/// The commands that show `edit` on `zone`.
pub fn apply_commands(serial: &str, zone: &str, edit: &ZoneEdit, software_running: bool) -> Vec<Command> {
    let effect = edit.effect.as_str();
    let [c1, c2, c3] = edit.colours.map(rgb);
    if SOFTWARE.contains(&effect) {
        let options = if colour_count(effect) > 0 { json!({ "colour": c1 }) } else { json!({}) };
        return vec![Command::Software { serial: serial.into(), effect: effect.into(), options }];
    }
    let mut commands = Vec::new();
    if software_running && zone == "backlight" {
        // Otherwise the host keeps drawing frames over the device's own effect.
        commands.push(Command::Software { serial: serial.into(), effect: "none".into(), options: json!({}) });
    }
    let flat = |parts: &[&[i64]]| parts.iter().flat_map(|p| p.iter().copied()).collect::<Vec<i64>>();
    let (method, args): (&str, Vec<i64>) = match effect {
        "none" => ("set_none", vec![]),
        "on" => ("set_on", vec![]),
        "static" => ("set_static", c1.to_vec()),
        "spectrum" => ("set_spectrum", vec![]),
        "wave" => ("set_wave", vec![edit.direction]),
        "wheel" => ("set_wheel", vec![edit.direction]),
        "reactive" => ("set_reactive", flat(&[&c1, &[edit.speed]])),
        "blinking" => ("set_blinking", c1.to_vec()),
        "breath_random" => ("set_breath_random", vec![]),
        "breath_single" => ("set_breath_single", c1.to_vec()),
        "breath_dual" => ("set_breath_dual", flat(&[&c1, &c2])),
        "breath_triple" => ("set_breath_triple", flat(&[&c1, &c2, &c3])),
        "starlight_random" => ("set_starlight_random", vec![edit.speed.min(3)]),
        "starlight_single" => ("set_starlight_single", flat(&[&c1, &[edit.speed.min(3)]])),
        "starlight_dual" => ("set_starlight_dual", flat(&[&c1, &c2, &[edit.speed.min(3)]])),
        _ => return commands,
    };
    commands.push(Command::Call { serial: serial.into(), method: method.into(), args: crate::worker::args(&args), zone: Some(zone.into()), then: Then::Lighting });
    commands
}

/// Effects `zone` offers, the daemon's live ones included where they make sense.
pub fn effects_for(caps: &Value, zone: &str) -> Vec<String> {
    let mut effects: Vec<String> = caps["zones"][zone]["effects"]
        .as_array()
        .map(|list| list.iter().filter_map(|e| e.as_str()).filter(|e| *e != "custom").map(str::to_string).collect())
        .unwrap_or_default();
    if zone == "backlight" && caps["software_effects"] == true {
        let cells = caps["matrix_dimensions"].as_array().map_or(1, |d| d.iter().filter_map(Value::as_i64).product());
        for effect in SOFTWARE {
            if KEY_REACTIVE.contains(&effect) && cells < MIN_RIPPLE_CELLS {
                continue;
            }
            if !effects.iter().any(|e| e == effect) {
                effects.push(effect.to_string());
            }
        }
    }
    effects
}

pub fn hex(c: Color32) -> String {
    format!("#{:02X}{:02X}{:02X}", c.r(), c.g(), c.b())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(commands: &[Command]) -> (String, Vec<Value>, Option<String>) {
        match commands.last() {
            Some(Command::Call { method, args, zone, .. }) => (method.clone(), args.clone(), zone.clone()),
            _ => panic!("expected a device call"),
        }
    }

    fn zone(effect: &str) -> ZoneEdit {
        ZoneEdit {
            effect: effect.into(),
            colours: [Color32::from_rgb(1, 2, 3), Color32::from_rgb(4, 5, 6), Color32::from_rgb(7, 8, 9)],
            active: 0,
            speed: 3,
            direction: 2,
            brightness: None,
        }
    }

    #[test]
    fn effects_become_the_daemon_s_methods_with_their_arguments() {
        let (method, args, zone_name) = call(&apply_commands("S", "logo", &zone("static"), false));
        assert_eq!((method.as_str(), zone_name.as_deref()), ("set_static", Some("logo")));
        assert_eq!(args, vec![json!(1), json!(2), json!(3)]);
        let (method, args, _) = call(&apply_commands("S", "backlight", &zone("reactive"), false));
        assert_eq!(method, "set_reactive");
        assert_eq!(args, vec![json!(1), json!(2), json!(3), json!(3)]);
        let (_, args, _) = call(&apply_commands("S", "backlight", &zone("wave"), false));
        assert_eq!(args, vec![json!(2)]);
    }

    #[test]
    fn a_hardware_effect_stops_a_live_one_first() {
        let commands = apply_commands("S", "backlight", &zone("spectrum"), true);
        assert!(matches!(&commands[0], Command::Software { effect, .. } if effect == "none"));
        assert_eq!(call(&commands).0, "set_spectrum");
    }

    #[test]
    fn colours_are_written_as_hex() {
        assert_eq!(hex(Color32::from_rgb(0x44, 0xD6, 0x2C)), "#44D62C");
        assert_eq!(hex(Color32::from_rgb(0, 5, 255)), "#0005FF");
    }
}
