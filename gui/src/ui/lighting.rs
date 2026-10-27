//! Lighting: zone, effect, colours, speed, direction and brightness.

use super::app::App;
use super::edit::{light_up, order_zones, ZoneEdit};
use super::theme::{self, TEXT_DIM, TEXT_FAINT};
use super::widgets;
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, Color32, RichText};
use serde_json::{json, Value};

/// Drawn on the host, frame by frame, rather than by the device.
const SOFTWARE: [&str; 4] = ["ripple", "ripple_random", "spectrum_soft", "wave_soft"];
/// Need a key press to place; pointless below this many LEDs.
const KEY_REACTIVE: [&str; 2] = ["ripple", "ripple_random"];
const MIN_RIPPLE_CELLS: i64 = 8;

const PALETTE: [Color32; 12] = [
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

pub fn label(effect: &str) -> &str {
    match effect {
        "none" => "Off",
        "on" => "On",
        "static" => "Static",
        "spectrum" => "Spectrum",
        "wave" => "Wave",
        "wheel" => "Wheel",
        "reactive" => "Reactive",
        "blinking" => "Blinking",
        "breath_random" => "Breathing ∙ random",
        "breath_single" => "Breathing",
        "breath_dual" => "Breathing ∙ 2",
        "breath_triple" => "Breathing ∙ 3",
        "starlight_random" => "Starlight ∙ random",
        "starlight_single" => "Starlight",
        "starlight_dual" => "Starlight ∙ 2",
        "ripple" => "Ripple",
        "ripple_random" => "Ripple ∙ random",
        "spectrum_soft" => "Smooth spectrum",
        "wave_soft" => "Smooth wave",
        "custom" => "Custom",
        other => other,
    }
}

fn colour_count(effect: &str) -> usize {
    match effect {
        "static" | "blinking" | "reactive" | "breath_single" | "starlight_single" | "ripple" | "wave_soft" => 1,
        "breath_dual" | "starlight_dual" => 2,
        "breath_triple" => 3,
        _ => 0,
    }
}

fn speeds(effect: &str) -> Option<Vec<(i64, String)>> {
    match effect {
        "reactive" => Some(vec![(1, "Short".into()), (2, "Medium".into()), (3, "Long".into()), (4, "Longest".into())]),
        e if e.starts_with("starlight") => Some(vec![(1, "Fast".into()), (2, "Medium".into()), (3, "Slow".into())]),
        _ => None,
    }
}

fn zone_label(zone: &str, kind: &str) -> String {
    match (zone, kind) {
        ("backlight", "headset") => "Both ears".into(),
        ("left", "headset") => "Left ear".into(),
        ("right", "headset") => "Right ear".into(),
        ("backlight", _) => "Backlight".into(),
        ("logo", _) => "Logo".into(),
        ("scroll", _) => "Scroll wheel".into(),
        ("left", _) => "Left side".into(),
        ("right", _) => "Right side".into(),
        ("charging", _) => "Charging".into(),
        ("fast_charging", _) => "Fast charging".into(),
        ("fully_charged", _) => "Charged".into(),
        (other, _) => other.into(),
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
    commands.push(Command::Call {
        serial: serial.into(),
        method: method.into(),
        args: crate::worker::args(&args),
        zone: Some(zone.into()),
        then: Then::Lighting,
    });
    commands
}

fn effects_for(caps: &Value, zone: &str) -> Vec<String> {
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

pub fn page(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(caps) = snap.details.get(&device.serial).and_then(|d| d.caps.clone()) else {
        ui.add(egui::Spinner::new());
        return;
    };
    let zones = order_zones(&caps["zones"].as_object().map(|z| z.keys().cloned().collect::<Vec<_>>()).unwrap_or_default());
    if zones.is_empty() {
        ui.label(widgets::dim("This device has no lighting the port can control."));
        return;
    }
    let software_running = snap.software.contains_key(&device.serial);
    let serial = device.serial.clone();
    let mut commands: Vec<Command> = Vec::new();
    let mut debounced: Vec<(String, Command, u64)> = Vec::new();

    let edit = app.edits.entry(serial.clone()).or_default();
    let mut zone = edit.zone.clone().unwrap_or_else(|| zones[0].clone());
    if !zones.contains(&zone) {
        zone = zones[0].clone();
    }

    // -- effect --------------------------------------------------------------
    widgets::card(ui, |ui| {
        ui.horizontal(|ui| {
            ui.label(RichText::new("Effect").font(theme::bold(16.0)));
            if zones.len() > 1 {
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    // Laid out right to left, so listed backwards to read forwards.
                    let options: Vec<(String, String)> = zones.iter().rev().map(|z| (z.clone(), zone_label(z, &device.kind))).collect();
                    if widgets::segmented(ui, &options, &mut zone) {
                        edit.zone = Some(zone.clone());
                    }
                });
            }
        });
        edit.zone = Some(zone.clone());
        let effects = effects_for(&caps, &zone);
        let current = edit.zones.entry(zone.clone()).or_default();
        ui.add_space(4.0);
        if effects.is_empty() {
            ui.label(widgets::dim("Only brightness can be set here."));
        }
        // What choosing an effect will show, dark colours lit up as they will be.
        let mut preview = current.clone();
        light_up(&mut preview, 3);
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = vec2(10.0, 10.0);
            for effect in &effects {
                let selected = current.effect == *effect;
                let colours = if selected { &current.colours } else { &preview.colours };
                let response = widgets::effect_tile(ui, effect, label(effect), colours, selected);
                let response = if SOFTWARE.contains(&effect.as_str()) {
                    response.on_hover_text("Drawn by the daemon, frame by frame; it lasts while the daemon runs")
                } else {
                    response
                };
                if response.clicked() {
                    current.effect = effect.clone();
                    light_up(current, colour_count(effect));
                    commands.extend(apply_commands(&serial, &zone, current, software_running));
                }
            }
        });
    });

    let current = edit.zones.entry(zone.clone()).or_default();
    let effect = current.effect.clone();

    // -- colours ---------------------------------------------------------------
    let count = colour_count(&effect);
    if count > 0 {
        ui.add_space(12.0);
        widgets::card(ui, |ui| {
            ui.label(RichText::new(if count == 1 { "Colour" } else { "Colours" }).font(theme::bold(16.0)));
            ui.add_space(2.0);
            let mut changed = false;
            ui.horizontal_top(|ui| {
                ui.vertical(|ui| {
                    ui.horizontal(|ui| {
                        for slot in 0..count {
                            if widgets::swatch(ui, current.colours[slot], 46.0, count > 1 && current.active == slot).clicked() {
                                current.active = slot;
                                edit.hex.clear();
                            }
                        }
                    });
                    let active = current.active.min(count - 1);
                    current.active = active;
                    ui.add_space(6.0);
                    let mut colour = current.colours[active];
                    if egui::widgets::color_picker::color_picker_color32(ui, &mut colour, egui::color_picker::Alpha::Opaque) {
                        current.colours[active] = colour;
                        changed = true;
                    }
                });
                ui.add_space(18.0);
                ui.vertical(|ui| {
                    widgets::section(ui, "Presets");
                    egui::Grid::new("palette").spacing(vec2(8.0, 8.0)).show(ui, |ui| {
                        for (i, colour) in PALETTE.iter().enumerate() {
                            let active = current.active.min(count - 1);
                            if widgets::swatch(ui, *colour, 30.0, current.colours[active] == *colour).clicked() {
                                current.colours[active] = *colour;
                                changed = true;
                            }
                            if i % 6 == 5 {
                                ui.end_row();
                            }
                        }
                    });
                    ui.add_space(8.0);
                    widgets::section(ui, "Hex");
                    let active = current.active.min(count - 1);
                    let c = current.colours[active];
                    if edit.hex.is_empty() {
                        edit.hex = format!("#{:02X}{:02X}{:02X}", c.r(), c.g(), c.b());
                    }
                    let response = ui.add(egui::TextEdit::singleline(&mut edit.hex).desired_width(110.0).font(egui::TextStyle::Monospace));
                    if response.lost_focus() || response.changed() {
                        if let Some(parsed) = parse_hex(&edit.hex) {
                            if parsed != c {
                                current.colours[active] = parsed;
                                changed = true;
                            }
                        }
                    }
                    if !response.has_focus() {
                        edit.hex = format!("#{:02X}{:02X}{:02X}", current.colours[active].r(), current.colours[active].g(), current.colours[active].b());
                    }
                });
            });
            if changed {
                debounced.push((format!("light:{serial}:{zone}"), Command::Refresh, 0));
            }
        });
    }

    // -- speed and direction -----------------------------------------------------
    let speed_options = speeds(&effect);
    let has_direction = matches!(effect.as_str(), "wave" | "wheel");
    if speed_options.is_some() || has_direction {
        ui.add_space(12.0);
        widgets::card(ui, |ui| {
            if let Some(options) = &speed_options {
                ui.horizontal(|ui| {
                    ui.label(RichText::new(if effect == "reactive" { "Afterglow" } else { "Speed" }).font(theme::bold(16.0)));
                    ui.add_space(12.0);
                    let mut speed = current.speed.min(options.len() as i64);
                    if widgets::segmented(ui, options, &mut speed) {
                        current.speed = speed;
                        commands.extend(apply_commands(&serial, &zone, current, software_running));
                    }
                });
            }
            if has_direction {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("Direction").font(theme::bold(16.0)));
                    ui.add_space(12.0);
                    let options = vec![(1i64, "← Left".to_string()), (2i64, "Right →".to_string())];
                    if widgets::segmented(ui, &options, &mut current.direction) {
                        commands.extend(apply_commands(&serial, &zone, current, software_running));
                    }
                });
            }
        });
    }

    // -- brightness ---------------------------------------------------------------
    if caps["zones"][&zone]["brightness"] == true {
        ui.add_space(12.0);
        widgets::card(ui, |ui| {
            ui.horizontal(|ui| {
                widgets::icon(ui, theme::icon::BRIGHTNESS, 16.0, TEXT_DIM);
                ui.label(RichText::new("Brightness").font(theme::bold(16.0)));
                let mut value = current.brightness.unwrap_or(75.0);
                ui.add_space(16.0);
                ui.spacing_mut().slider_width = (ui.available_width() - 70.0).max(120.0);
                let slider = ui.add(egui::Slider::new(&mut value, 0.0..=100.0).show_value(false));
                ui.label(RichText::new(format!("{value:.0}%")).font(theme::bold(15.0)));
                if slider.changed() {
                    current.brightness = Some(value);
                    debounced.push((
                        format!("brightness:{serial}:{zone}"),
                        Command::Call {
                            serial: serial.clone(),
                            method: "set_brightness".into(),
                            args: vec![json!((value * 10.0).round() / 10.0)],
                            zone: Some(zone.clone()),
                            then: Then::Nothing,
                        },
                        120,
                    ));
                }
            });
        });
    }

    if software_running {
        ui.add_space(10.0);
        ui.label(RichText::new("A live effect is being drawn by the daemon; it lasts as long as the daemon runs.").size(12.5).color(TEXT_FAINT));
    }

    // Colour changes go through the debouncer as a fresh apply of the whole zone.
    let zone_now = edit.zones.get(&zone).cloned().unwrap_or_default();
    let touched = !commands.is_empty() || !debounced.is_empty();
    if touched {
        edit.touch();
    }
    for command in commands {
        app.send(command);
    }
    for (key, command, delay) in debounced {
        if key.starts_with("light:") {
            for (i, command) in apply_commands(&serial, &zone, &zone_now, software_running).into_iter().enumerate() {
                app.debounce(format!("{key}:{i}"), command, 140);
            }
        } else {
            app.debounce(key, command, delay);
        }
    }
}

fn parse_hex(text: &str) -> Option<Color32> {
    let hex = text.trim().trim_start_matches('#');
    if hex.len() != 6 {
        return None;
    }
    let value = u32::from_str_radix(hex, 16).ok()?;
    Some(Color32::from_rgb((value >> 16) as u8, (value >> 8) as u8, value as u8))
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
        let (method, args, _) = call(&apply_commands("S", "backlight", &zone("breath_triple"), false));
        assert_eq!(method, "set_breath_triple");
        assert_eq!(args.len(), 9);
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
    fn live_effects_go_to_the_effect_engine() {
        let commands = apply_commands("S", "backlight", &zone("ripple"), false);
        assert!(matches!(&commands[0], Command::Software { effect, options, .. } if effect == "ripple" && options["colour"] == json!([1, 2, 3])));
    }

    #[test]
    fn hex_colours_parse() {
        assert_eq!(parse_hex("#44D62C"), Some(Color32::from_rgb(0x44, 0xD6, 0x2C)));
        assert_eq!(parse_hex("44d62c"), Some(Color32::from_rgb(0x44, 0xD6, 0x2C)));
        assert_eq!(parse_hex("#44D6"), None);
    }
}
