//! Home: a tile for every device -- its picture in its own light, who it is,
//! how it is connected -- with the few settings people change most.

use super::app::{draw_picture, kind_label, scroll, App};
use super::device::Tab;
use super::edit::light_up;
use super::effects::{self, PALETTE};
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, Color32, CornerRadius, Frame, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, BORDER_HI, SURFACE, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::widgets::{self, Button};
use openrazer_studio_core::{t, tf};
use serde_json::json;

/// Tiles are at least this wide; as many fit across as can.
const TILE_MIN: f32 = 320.0;
const STAGE_HEIGHT: f32 = 172.0;

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    let count = snap.devices.len();
    let subtitle = if count == 0 { String::new() } else { tf("{} device(s) connected", &[&count]) };
    widgets::page_header(ui, t("Home"), &subtitle, |ui| {
        if ui.add(Button::secondary(t("Look for devices")).icon(icon::REFRESH)).clicked() {
            app.send(Command::Rescan);
        }
        if count > 0 {
            let off = !app.settings.lights_before.is_empty();
            let (glyph, text) = if off { (icon::BULB, t("Lights back on")) } else { (icon::POWER, t("All lights off")) };
            if ui.add(Button::secondary(text).icon(glyph).min_width(150.0)).clicked() {
                lights(app, snap, off);
            }
        }
    });
    scroll(ui, "home", |ui| {
        notices(app, ui, snap);
        if snap.devices.is_empty() {
            widgets::plain_panel(ui, |ui| {
                widgets::empty_state(
                    ui,
                    t("No Razer devices found"),
                    t("Plug one in or switch a wireless one on. If Razer Synapse is running, close it: it keeps devices to itself."),
                    |ui| {
                        if ui.add(Button::primary(t("Look again")).icon(icon::REFRESH)).clicked() {
                            app.send(Command::Rescan);
                        }
                    },
                );
            });
            return;
        }
        let gap = theme::GAP;
        let width = ui.available_width();
        let columns = ((width + gap) / (TILE_MIN + gap)).floor().clamp(1.0, 4.0) as usize;
        let tile = ((width - gap * (columns as f32 - 1.0)) / columns as f32).floor();
        for (r, line) in snap.devices.chunks(columns).enumerate() {
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = gap;
                for device in line {
                    ui.allocate_ui_with_layout(vec2(tile, 0.0), egui::Layout::top_down(egui::Align::Min), |ui| {
                        ui.set_width(tile);
                        ui.spacing_mut().item_spacing.x = theme::SPACING;
                        card(app, ui, device, snap, egui::Id::new(("home-row", r, columns)));
                    });
                }
            });
        }
    });
}

fn notices(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    if !snap.synapse.is_empty() {
        let tx = app.tx.clone();
        widgets::banner(
            ui,
            theme::WARN,
            icon::WARNING,
            t("Razer Synapse is running"),
            t("Synapse talks to the same devices, and the two will keep overwriting each other's lighting and settings. Close it to let OpenRazer have them to itself."),
            |ui| {
                if ui.add(Button::primary(t("Close Synapse")).small()).clicked() {
                    let _ = tx.send(Command::CloseSynapse);
                }
                ui.label(widgets::faint(t("Its Windows services can only be stopped by an administrator, in Services.")));
            },
        );
    }
    if !snap.unsupported.is_empty() {
        let names: Vec<String> = snap.unsupported.iter().map(|(pid, name)| if name.is_empty() { format!("1532:{pid:04X}") } else { format!("{name} (1532:{pid:04X})") }).collect();
        widgets::banner(ui, theme::INFO, icon::INFO, t("Some Razer devices are not supported yet"), &names.join(", "), |ui| {
            widgets::link_button(ui, t("Ask for one to be added"), &format!("{}/issues/new?template=device-not-detected.md", openrazer_studio_core::install::REPOSITORY));
        });
    }
}

/// Turn every light off by its brightness -- effects and colours stay as they
/// are -- or put the brightness back.
fn lights(app: &mut App, snap: &Snapshot, back_on: bool) {
    if back_on {
        let before = std::mem::take(&mut app.settings.lights_before);
        for (serial, zones) in before {
            for (zone, level) in zones {
                app.send(brightness_call(&serial, &zone, level as f32));
                if let Some(edit) = app.edits.get_mut(&serial).and_then(|e| e.zones.get_mut(&zone)) {
                    edit.brightness = Some(level as f32);
                }
            }
        }
    } else {
        for device in &snap.devices {
            let Some(detail) = snap.details.get(&device.serial) else { continue };
            let zones = brightness_zones(detail.caps.as_ref());
            let mut saved = std::collections::BTreeMap::new();
            for zone in zones {
                let level = app.edits.get(&device.serial).and_then(|e| e.zones.get(&zone)).and_then(|z| z.brightness).map(f64::from).or_else(|| detail.brightness.get(&zone).copied()).unwrap_or(75.0);
                saved.insert(zone.clone(), level);
                app.send(brightness_call(&device.serial, &zone, 0.0));
                if let Some(edit) = app.edits.get_mut(&device.serial).and_then(|e| e.zones.get_mut(&zone)) {
                    edit.brightness = Some(0.0);
                }
            }
            if !saved.is_empty() {
                app.settings.lights_before.insert(device.serial.clone(), saved);
            }
        }
    }
    app.save_settings();
}

fn brightness_zones(caps: Option<&serde_json::Value>) -> Vec<String> {
    caps.and_then(|c| c["zones"].as_object()).map(|z| z.iter().filter(|(_, v)| v["brightness"] == true).map(|(k, _)| k.clone()).collect()).unwrap_or_default()
}

fn brightness_call(serial: &str, zone: &str, level: f32) -> Command {
    Command::Call { serial: serial.into(), method: "set_brightness".into(), args: vec![json!((level * 10.0).round() / 10.0)], zone: Some(zone.into()), then: Then::Nothing }
}

/// One device: its picture and state, and its quick settings.
fn card(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot, row: egui::Id) {
    let detail = snap.details.get(&device.serial).cloned().unwrap_or_default();
    let caps = detail.caps.clone();
    let serial = device.serial.clone();
    let hovered = ui.rect_contains_pointer(ui.max_rect().intersect(egui::Rect::from_min_size(ui.cursor().min, vec2(ui.available_width(), STAGE_HEIGHT + 90.0))));
    Frame::new()
        .fill(SURFACE)
        .stroke(Stroke::new(1.0_f32, if hovered { BORDER_HI } else { BORDER }))
        .corner_radius(CornerRadius::same(theme::RADIUS))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.spacing_mut().item_spacing.y = 0.0;
            // -- the device itself, in its own light: click to open it -------------------
            let mut open = false;
            Frame::new().inner_margin(Margin { left: 8, right: 8, top: 8, bottom: 0 }).show(ui, |ui| {
                super::app::stage_frame().show(ui, |ui| {
                    let stage = vec2(ui.available_width(), STAGE_HEIGHT);
                    let art = caps.as_ref().and_then(|caps| super::canvas::art_for(app, ui.ctx(), device, caps));
                    open = match (art, caps.as_ref()) {
                        (Some(art), Some(caps)) => {
                            let time = app.time();
                            let look = super::lighting::now_showing(app, &art, device, caps, snap, time);
                            let visible = |s: &crate::art::Shape| look.visible(s);
                            let picked = super::canvas::show(app, ui, &serial, &art, &look.colours, stage, super::canvas::View { visible: &visible, selected: &|_| false, labels: false });
                            if look.colours.iter().any(|c| *c != Color32::BLACK) {
                                ui.ctx().request_repaint_after(std::time::Duration::from_millis(50));
                            }
                            picked.any_click
                        }
                        _ => {
                            let (rect, response) = ui.allocate_exact_size(stage, Sense::click());
                            draw_picture(app, ui, device, rect.shrink(24.0));
                            response.clicked()
                        }
                    };
                });
            });
            if open {
                app.open_device(&serial, None);
            }

            // -- who it is ---------------------------------------------------------------
            Frame::new().inner_margin(Margin { left: 16, right: 16, top: 12, bottom: 12 }).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.spacing_mut().item_spacing.y = 4.0;
                ui.horizontal(|ui| {
                    let name_width = ui.available_width() - 82.0;
                    let name = widgets::galley_fit(ui, &super::app::title(device, snap), theme::bold(16.5), TEXT, name_width);
                    let (rect, response) = ui.allocate_exact_size(vec2(name_width, 24.0), Sense::click());
                    ui.painter().galley(egui::pos2(rect.left(), rect.center().y - name.size().y / 2.0), name, TEXT);
                    if response.on_hover_cursor(egui::CursorIcon::PointingHand).on_hover_text(&device.name).clicked() {
                        app.open_device(&serial, None);
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if let Some(level) = detail.battery {
                            super::widgets::battery(ui, level, detail.charging.unwrap_or(false));
                        }
                    });
                });
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 6.0;
                    let bluetooth = caps.as_ref().is_some_and(|c| c["protocol"] == "razer-ble");
                    let line = format!("{} · {}", kind_label(&device.kind), if bluetooth { "Bluetooth" } else { "USB" });
                    ui.label(RichText::new(line).size(12.5).color(TEXT_DIM));
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if detail.game_mode == Some(true) {
                            widgets::badge(ui, t("Game mode"), theme::WARN);
                        }
                        if let Some(effect) = snap.software.get(&serial) {
                            widgets::badge(ui, effects::label(effect), ACCENT);
                        }
                    });
                });
            });
            widgets::divider(ui);

            // -- quick settings -------------------------------------------------------
            Frame::new().inner_margin(Margin::symmetric(16, 12)).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.spacing_mut().item_spacing.y = 8.0;
                match caps.as_ref() {
                    Some(caps) => quick_settings(app, ui, device, &detail, caps, snap),
                    None => {
                        ui.add(egui::Spinner::new().color(TEXT_FAINT));
                    }
                }
                widgets::equalise(ui, row);
                ui.add_space(4.0);
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    let half = ((ui.available_width() - 8.0) / 2.0).floor();
                    let has_lights = caps.as_ref().and_then(|c| c["zones"].as_object()).is_some_and(|z| !z.is_empty());
                    if ui.add(Button::secondary(t("Lighting")).icon(icon::LIGHTING).width(half).enabled(has_lights)).clicked() {
                        app.open_device(&serial, Some(Tab::Lighting));
                    }
                    if ui.add(Button::secondary(t("All settings")).icon(icon::SETTINGS).width(half)).clicked() {
                        app.open_device(&serial, None);
                    }
                });
            });
        });
}

/// Effect, colour and brightness for the whole device, then DPI or game mode.
fn quick_settings(app: &mut App, ui: &mut egui::Ui, device: &Device, detail: &crate::worker::Detail, caps: &serde_json::Value, snap: &Snapshot) {
    let serial = device.serial.clone();
    let zones: Vec<String> = super::edit::order_zones(&caps["zones"].as_object().map(|z| z.keys().cloned().collect::<Vec<_>>()).unwrap_or_default());
    let software_running = snap.software.contains_key(&serial);
    let mut commands: Vec<Command> = Vec::new();

    if let Some(main) = zones.first().cloned() {
        let offered = effects::effects_for(caps, &main);
        let edit = app.edits.entry(serial.clone()).or_default();
        let current = edit.zones.get(&main).cloned().unwrap_or_default();
        let showing = if software_running { snap.software.get(&serial).cloned().unwrap_or_default() } else { current.effect.clone() };

        // Effect: every zone that has it gets it, so the device looks one piece.
        widgets::row(ui, t("Effect"), "", |ui| {
            let mut chosen: Option<String> = None;
            let shown = if showing.is_empty() { "—" } else { effects::label(&showing) };
            widgets::select(ui, ("home-effect", &serial), shown, ui.available_width(), |ui| {
                for effect in &offered {
                    if widgets::option(ui, *effect == showing, effects::label(effect)).clicked() {
                        chosen = Some(effect.clone());
                    }
                }
            });
            if let Some(effect) = chosen {
                for zone in &zones {
                    if !effects::effects_for(caps, zone).contains(&effect) {
                        continue;
                    }
                    let zone_edit = edit.zones.entry(zone.clone()).or_default();
                    zone_edit.effect = effect.clone();
                    light_up(zone_edit, effects::colour_count(&effect));
                    commands.extend(effects::apply_commands(&serial, zone, zone_edit, software_running));
                }
            }
        });

        // Colour: keep the effect if it takes one; otherwise go static.
        let takes_static = offered.iter().any(|e| e == "static");
        widgets::row(ui, t("Colour"), "", |ui| {
            ui.add_enabled_ui(takes_static, |ui| {
                ui.spacing_mut().item_spacing.x = 4.0;
                let mut colours: Vec<Color32> = PALETTE[..6].to_vec();
                for rgb in app.settings.recent_colours.iter().take(2).rev() {
                    let c = Color32::from_rgb(rgb[0], rgb[1], rgb[2]);
                    if !colours.contains(&c) {
                        colours.insert(0, c);
                    }
                }
                let fit = ((ui.available_width() + 4.0) / 26.0).floor().max(1.0) as usize;
                colours.truncate(fit.min(6));
                for colour in colours {
                    let selected = current.colours[0] == colour && effects::colour_count(&current.effect) > 0 && !software_running;
                    if super::widgets::dot(ui, colour, 22.0, selected).on_hover_text(effects::hex(colour)).clicked() {
                        for zone in &zones {
                            let offered = effects::effects_for(caps, zone);
                            let zone_edit = edit.zones.entry(zone.clone()).or_default();
                            if effects::colour_count(&zone_edit.effect) == 0 || software_running {
                                if !offered.iter().any(|e| e == "static") {
                                    continue;
                                }
                                zone_edit.effect = "static".into();
                            }
                            zone_edit.colours[0] = colour;
                            commands.extend(effects::apply_commands(&serial, zone, zone_edit, software_running));
                        }
                    }
                }
            });
        });
        if !commands.is_empty() {
            edit.touch();
        }
    }
    for command in commands.drain(..) {
        app.send(command);
    }

    // Brightness: one slider for every zone that has it.
    let lit = brightness_zones(Some(caps));
    if let Some(first) = lit.first().cloned() {
        let edit = app.edits.entry(serial.clone()).or_default();
        let mut value = edit.zones.get(&first).and_then(|z| z.brightness).unwrap_or(75.0) as f64;
        let mut changed = false;
        widgets::row(ui, t("Brightness"), "", |ui| {
            changed = widgets::slider_field(ui, ("home-brightness", &serial), &mut value, 0.0..=100.0, 1.0, 0, "%", 64.0);
        });
        if changed {
            edit.touch();
            for zone in &lit {
                edit.zones.entry(zone.clone()).or_default().brightness = Some(value as f32);
            }
            for zone in lit {
                app.debounce(format!("brightness:{serial}:{zone}"), brightness_call(&serial, &zone, value as f32), 120);
            }
        }
    }

    // Sensitivity: the mouse's own stages, or a few common values.
    if caps["dpi"] == true {
        let edit = app.edits.entry(serial.clone()).or_default();
        let current = edit.dpi.map(|d| d.0).unwrap_or(0);
        let mut call: Option<Command> = None;
        widgets::row(ui, "DPI", "", |ui| {
            let stages = edit.stages.clone().filter(|(_, s)| !s.is_empty());
            let shown = match &stages {
                Some((active, list)) => list.get((*active - 1).max(0) as usize).map(|(x, _)| tf("Stage {} · {}", &[active, x])).unwrap_or_default(),
                None => current.to_string(),
            };
            widgets::select(ui, ("home-dpi", &serial), shown, ui.available_width(), |ui| match &stages {
                Some((active, list)) => {
                    for (i, (x, y)) in list.iter().enumerate() {
                        let number = i as i64 + 1;
                        if widgets::option(ui, number == *active, &tf("Stage {} · {}", &[&number, x])).clicked() {
                            edit.stages = Some((number, list.clone()));
                            edit.dpi = Some((*x, *y));
                            let all: Vec<_> = list.iter().map(|(x, y)| json!([x, y])).collect();
                            call = Some(Command::Call { serial: serial.clone(), method: "set_dpi_stages".into(), args: vec![json!(number), json!(all)], zone: None, then: Then::Performance });
                        }
                    }
                }
                None => {
                    for preset in [400i64, 800, 1600, 3200] {
                        if widgets::option(ui, preset == current, &preset.to_string()).clicked() {
                            edit.dpi = Some((preset, preset));
                            call = Some(Command::Call { serial: serial.clone(), method: "set_dpi".into(), args: vec![json!(preset), json!(preset)], zone: None, then: Then::Performance });
                        }
                    }
                }
            });
        });
        if let Some(call) = call {
            edit.touch();
            app.send(call);
        }
    }

    if caps["game_mode"] == true {
        let mut on = detail.game_mode.unwrap_or(false);
        if widgets::toggle_row(ui, t("Game mode"), "", &mut on) {
            app.send(Command::Call { serial: serial.clone(), method: "set_game_mode".into(), args: vec![json!(on)], zone: None, then: Then::Keyboard });
        }
    }
}
