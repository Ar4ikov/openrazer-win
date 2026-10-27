//! Performance, power and the device's details.

use super::app::{chip, kind_label, App};
use super::theme::{self, ACCENT, TEXT_DIM, TEXT_FAINT};
use super::widgets;
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, RichText};
use serde_json::json;

const DPI_PRESETS: [i64; 8] = [400, 800, 1200, 1600, 3200, 6400, 12800, 25600];

pub fn performance(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(caps) = snap.details.get(&device.serial).and_then(|d| d.caps.clone()) else { return };
    let serial = device.serial.clone();
    let max_dpi = caps["max_dpi"].as_i64().unwrap_or(16000).max(800);
    let mut send: Vec<Command> = Vec::new();
    let mut later: Vec<(String, Command)> = Vec::new();
    let edit = app.edits.entry(serial.clone()).or_default();

    if caps["dpi"] == true {
        widgets::card(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new("Sensitivity").font(theme::bold(16.0)));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let mut separate = !edit.dpi_linked;
                    if widgets::toggle(ui, &mut separate).changed() {
                        edit.dpi_linked = !separate;
                    }
                    ui.label(widgets::dim("Separate X and Y"));
                });
            });
            let (mut x, mut y) = edit.dpi.unwrap_or((800, 800));
            ui.horizontal(|ui| {
                ui.label(RichText::new(format!("{x}")).font(theme::bold(38.0)).color(ACCENT));
                if !edit.dpi_linked && y != x {
                    ui.label(RichText::new(format!("× {y}")).font(theme::bold(26.0)).color(TEXT_DIM));
                }
                ui.label(RichText::new("DPI").size(16.0).color(TEXT_DIM));
            });
            let mut changed = false;
            let width = ui.available_width() - 20.0;
            ui.spacing_mut().slider_width = width.max(200.0);
            if edit.dpi_linked {
                changed |= ui.add(egui::Slider::new(&mut x, 100..=max_dpi).step_by(50.0).show_value(false)).changed();
                y = x;
            } else {
                ui.label(widgets::dim("Horizontal"));
                changed |= ui.add(egui::Slider::new(&mut x, 100..=max_dpi).step_by(50.0).show_value(false)).changed();
                ui.label(widgets::dim("Vertical"));
                changed |= ui.add(egui::Slider::new(&mut y, 100..=max_dpi).step_by(50.0).show_value(false)).changed();
            }
            ui.add_space(4.0);
            ui.horizontal_wrapped(|ui| {
                for preset in DPI_PRESETS.iter().filter(|p| **p <= max_dpi) {
                    let selected = x == *preset && y == *preset;
                    let text = RichText::new(preset.to_string()).size(13.0);
                    if ui.add(egui::Button::new(if selected { text.color(ACCENT) } else { text }).selected(selected)).clicked() {
                        x = *preset;
                        y = *preset;
                        changed = true;
                    }
                }
            });
            if changed {
                edit.dpi = Some((x, y));
                later.push((format!("dpi:{serial}"), Command::Call { serial: serial.clone(), method: "set_dpi".into(), args: vec![json!(x), json!(y)], zone: None, then: Then::Nothing }));
            }
        });
    }

    if caps["dpi_stages"] == true {
        if let Some((mut active, mut stages)) = edit.stages.clone() {
            ui.add_space(12.0);
            widgets::card(ui, |ui| {
                ui.label(RichText::new("Sensitivity stages").font(theme::bold(16.0)));
                ui.label(widgets::dim("What the DPI button on the mouse cycles through."));
                let mut remove = None;
                for (i, stage) in stages.iter_mut().enumerate() {
                    ui.horizontal(|ui| {
                        let number = i as i64 + 1;
                        if ui.radio(active == number, format!("Stage {number}")).clicked() {
                            active = number;
                        }
                        ui.add_space(12.0);
                        ui.add(egui::DragValue::new(&mut stage.0).range(100..=max_dpi).speed(25.0).suffix(" DPI"));
                        if !edit.dpi_linked {
                            ui.add(egui::DragValue::new(&mut stage.1).range(100..=max_dpi).speed(25.0).suffix(" Y"));
                        } else {
                            stage.1 = stage.0;
                        }
                        if ui.add(egui::Button::new(RichText::new(theme::icon::DELETE).font(theme::icon_font(12.0)).color(TEXT_DIM)).frame(false)).on_hover_text("Remove").clicked() {
                            remove = Some(i);
                        }
                    });
                }
                if let Some(i) = remove {
                    if stages.len() > 1 {
                        stages.remove(i);
                        active = active.min(stages.len() as i64);
                    }
                }
                ui.horizontal(|ui| {
                    if stages.len() < 5 && widgets::icon_button(ui, theme::icon::ADD, "Add a stage").clicked() {
                        let last = stages.last().copied().unwrap_or((800, 800));
                        stages.push(((last.0 * 2).min(max_dpi), (last.1 * 2).min(max_dpi)));
                    }
                    if widgets::primary_button(ui, "Apply stages").clicked() {
                        let list: Vec<_> = stages.iter().map(|(x, y)| json!([x, y])).collect();
                        send.push(Command::Call { serial: serial.clone(), method: "set_dpi_stages".into(), args: vec![json!(active), json!(list)], zone: None, then: Then::Performance });
                    }
                });
            });
            edit.stages = Some((active, stages));
        }
    }

    if caps["poll_rate"] == true {
        ui.add_space(12.0);
        widgets::card(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new("Polling rate").font(theme::bold(16.0)));
                ui.add_space(16.0);
                let rates: Vec<(i64, String)> = caps["supported_poll_rates"]
                    .as_array()
                    .map(|r| r.iter().filter_map(|v| v.as_i64()).map(|v| (v, format!("{v} Hz"))).collect())
                    .unwrap_or_default();
                let mut rate = edit.poll_rate.unwrap_or(0);
                if widgets::segmented(ui, &rates, &mut rate) {
                    edit.poll_rate = Some(rate);
                    send.push(Command::Call { serial: serial.clone(), method: "set_poll_rate".into(), args: vec![json!(rate)], zone: None, then: Then::Performance });
                }
            });
            ui.label(RichText::new("How often the mouse reports to the PC. Higher is smoother and costs battery on wireless mice.").size(12.5).color(TEXT_FAINT));
        });
    }

    if !send.is_empty() || !later.is_empty() {
        edit.touch();
    }
    for command in send {
        app.send(command);
    }
    for (key, command) in later {
        app.debounce(key, command, 150);
    }
}

pub fn power(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(detail) = snap.details.get(&device.serial) else { return };
    let Some(caps) = detail.caps.clone() else { return };
    let serial = device.serial.clone();
    let mut later: Vec<(String, Command)> = Vec::new();
    let edit = app.edits.entry(serial.clone()).or_default();

    if caps["battery"] == true {
        widgets::card(ui, |ui| {
            ui.label(RichText::new("Battery").font(theme::bold(16.0)));
            match detail.battery {
                Some(level) => {
                    let charging = detail.charging.unwrap_or(false);
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(format!("{level:.0}%")).font(theme::bold(40.0)).color(if level < 20.0 { theme::DANGER } else { ACCENT }));
                        ui.label(widgets::dim(if charging { "charging" } else { "on battery" }));
                    });
                    let bar = egui::ProgressBar::new((level / 100.0) as f32).desired_height(8.0).fill(if level < 20.0 { theme::DANGER } else { ACCENT });
                    ui.add(bar);
                }
                None => {
                    ui.label(widgets::dim("Not reported yet."));
                }
            }
        });
    }
    if caps["idle_time"] == true {
        ui.add_space(12.0);
        widgets::card(ui, |ui| {
            ui.label(RichText::new("Sleep").font(theme::bold(16.0)));
            let mut minutes = edit.idle_minutes.unwrap_or(5.0);
            ui.horizontal(|ui| {
                ui.label(widgets::dim("Turn off after"));
                let response = ui.add(egui::Slider::new(&mut minutes, 1.0..=15.0).step_by(0.5).show_value(false));
                ui.label(RichText::new(format!("{minutes:.1} min")).font(theme::bold(14.0)));
                if response.changed() {
                    edit.idle_minutes = Some(minutes);
                    later.push((format!("idle:{serial}"), Command::Call { serial: serial.clone(), method: "set_idle_time".into(), args: vec![json!((minutes * 60.0) as i64)], zone: None, then: Then::Power }));
                }
            });
        });
    }
    if caps["battery"] == true && detail.low_battery.is_some() {
        ui.add_space(12.0);
        widgets::card(ui, |ui| {
            ui.label(RichText::new("Low battery warning").font(theme::bold(16.0)));
            let mut threshold = edit.low_battery.unwrap_or(10.0);
            ui.horizontal(|ui| {
                ui.label(widgets::dim("Warn below"));
                let response = ui.add(egui::Slider::new(&mut threshold, 5.0..=100.0).step_by(1.0).show_value(false));
                ui.label(RichText::new(format!("{threshold:.0}%")).font(theme::bold(14.0)));
                if response.changed() {
                    edit.low_battery = Some(threshold);
                    later.push((format!("low:{serial}"), Command::Call { serial: serial.clone(), method: "set_low_battery_threshold".into(), args: vec![json!(threshold)], zone: None, then: Then::Nothing }));
                }
            });
        });
    }
    if !later.is_empty() {
        edit.touch();
    }
    for (key, command) in later {
        app.debounce(key, command, 250);
    }
}

pub fn info(_app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let caps = snap.details.get(&device.serial).and_then(|d| d.caps.clone()).unwrap_or_default();
    widgets::card(ui, |ui| {
        egui::Grid::new("details").num_columns(2).spacing(vec2(28.0, 10.0)).show(ui, |ui| {
            let mut row = |name: &str, value: String| {
                ui.label(widgets::dim(name));
                ui.label(value);
                ui.end_row();
            };
            row("Name", device.name.clone());
            row("Type", kind_label(&device.kind).to_string());
            row("USB id", format!("1532:{:04X}", device.pid));
            row("Serial", device.serial.clone());
            row("Firmware", device.firmware.clone().unwrap_or_else(|| "—".into()));
            if let Some(dims) = caps["matrix_dimensions"].as_array() {
                if dims.len() == 2 {
                    row("LED matrix", format!("{} × {}", dims[0], dims[1]));
                }
            }
            if let Some(transport) = caps["transport"]["path"].as_str() {
                row("Connected through", if caps["protocol"] == "razer-ble" { format!("Bluetooth LE ({transport})") } else { "USB".into() });
            }
        });
    });
    ui.add_space(12.0);
    widgets::card(ui, |ui| {
        ui.label(RichText::new("What it can do").font(theme::bold(16.0)));
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
            let flags = [
                ("dpi", "DPI"),
                ("dpi_stages", "DPI stages"),
                ("poll_rate", "Polling rate"),
                ("battery", "Battery"),
                ("idle_time", "Sleep timer"),
                ("game_mode", "Game mode"),
                ("dedicated_macro_keys", "Macro keys"),
                ("custom_frame", "Per-key lighting"),
                ("software_effects", "Live effects"),
                ("scroll_mode", "Scroll modes"),
            ];
            for (key, name) in flags {
                if caps[key] == true {
                    chip(ui, name);
                }
            }
            if let Some(zones) = caps["zones"].as_object() {
                for (zone, info) in zones {
                    let effects = info["effects"].as_array().map_or(0, Vec::len);
                    let mut name = zone.replace('_', " ");
                    if let Some(first) = name.get_mut(0..1) {
                        first.make_ascii_uppercase();
                    }
                    chip(ui, &format!("{name} lighting: {effects} effects"));
                }
            }
        });
    });
}
