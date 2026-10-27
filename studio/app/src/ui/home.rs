//! Home: every device at a glance, with the settings people change most.

use super::app::{draw_picture, kind_label, short_name, App, Page};
use super::edit::light_up;
use super::effects::{self, PALETTE};
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, Color32, CornerRadius, Frame, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, SURFACE, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::{t, tf, widgets};
use serde_json::json;

const CARD_WIDTH: f32 = 420.0;

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        notices(app, ui, snap);
        if snap.devices.is_empty() {
            let tx = app.tx.clone();
            super::app::empty_state(
                ui,
                t("No Razer devices found"),
                t("Plug one in or switch a wireless one on. If Razer Synapse is running, close it: it keeps devices to itself."),
                Some(move |ui: &mut egui::Ui| {
                    if widgets::icon_button(ui, icon::REFRESH, t("Look again")).clicked() {
                        let _ = tx.send(Command::Rescan);
                    }
                }),
            );
            return;
        }
        ui.horizontal(|ui| {
            widgets::heading(ui, &tf("Your devices · {}", &[&snap.devices.len()]));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if widgets::icon_button(ui, icon::REFRESH, t("Look for devices")).clicked() {
                    app.send(Command::Rescan);
                }
                let off = !app.settings.lights_before.is_empty();
                let (glyph, text) = if off { (icon::BULB, t("Lights back on")) } else { (icon::POWER, t("All lights off")) };
                if widgets::icon_button(ui, glyph, text).clicked() {
                    lights(app, snap, off);
                }
            });
        });
        ui.add_space(4.0);
        let columns = ((ui.available_width() + 16.0) / (CARD_WIDTH + 16.0)).floor().max(1.0) as usize;
        let width = ((ui.available_width() - 16.0 * (columns as f32 - 1.0)) / columns as f32).max(320.0);
        for row in snap.devices.chunks(columns) {
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = 16.0;
                for device in row {
                    ui.allocate_ui_with_layout(vec2(width, 0.0), egui::Layout::top_down(egui::Align::Min), |ui| {
                        ui.set_width(width);
                        card(app, ui, device, snap);
                    });
                }
            });
            ui.add_space(6.0);
        }
        ui.add_space(20.0);
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
                ui.horizontal(|ui| {
                    if widgets::primary_button(ui, t("Close Synapse")).clicked() {
                        let _ = tx.send(Command::CloseSynapse);
                    }
                    ui.label(widgets::faint(t("Its Windows services can only be stopped by an administrator, in Services.")));
                });
            },
        );
        ui.add_space(12.0);
    }
    if !snap.unsupported.is_empty() {
        let names: Vec<String> = snap.unsupported.iter().map(|(pid, name)| if name.is_empty() { format!("1532:{pid:04X}") } else { format!("{name} (1532:{pid:04X})") }).collect();
        widgets::banner(ui, theme::INFO, icon::INFO, t("Some Razer devices are not supported yet"), &names.join(", "), |ui| {
            widgets::link(ui, t("Ask for one to be added"), &format!("{}/issues/new?template=device-not-detected.md", openrazer_studio_core::install::REPOSITORY));
        });
        ui.add_space(12.0);
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
fn card(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let detail = snap.details.get(&device.serial).cloned().unwrap_or_default();
    let caps = detail.caps.clone();
    let serial = device.serial.clone();
    Frame::new()
        .fill(SURFACE.gamma_multiply(0.94))
        .stroke(Stroke::new(1.0_f32, BORDER))
        .corner_radius(CornerRadius::same(18))
        .shadow(theme::shadow())
        .inner_margin(Margin::same(18))
        .show(ui, |ui| {
        ui.set_width(ui.available_width());
        // -- the device itself, in its own light ---------------------------------------
        let stage = vec2(ui.available_width(), 190.0);
        let art = caps.as_ref().and_then(|caps| super::canvas::art_for(app, ui.ctx(), device, caps));
        let clicked = match (art, caps.as_ref()) {
            (Some(art), Some(caps)) => {
                let time = app.time();
                let look = super::lighting::now_showing(app, &art, device, caps, snap, time);
                let visible = |s: &crate::art::Shape| look.visible(s);
                let picked = super::canvas::show(app, ui, &serial, &art, &look.colours, stage, super::canvas::View { visible: &visible, selected: &|_| false, labels: false });
                if look.colours.iter().any(|c| *c != egui::Color32::BLACK) {
                    ui.ctx().request_repaint_after(std::time::Duration::from_millis(50));
                }
                picked.clicked.is_some() || picked.hovered.is_some() && ui.input(|i| i.pointer.primary_clicked())
            }
            _ => {
                let (rect, response) = ui.allocate_exact_size(stage, Sense::click());
                draw_picture(app, ui, device, rect.shrink(20.0));
                response.clicked()
            }
        };
        if clicked {
            app.open(Page::Lighting, &serial);
        }
        ui.add_space(4.0);
        // -- who it is ------------------------------------------------------------------
        ui.label(RichText::new(short_name(&device.name)).font(theme::heavy(20.0)));
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 6.0;
            ui.label(RichText::new(kind_label(&device.kind)).size(12.5).color(TEXT_DIM));
            if let Some(level) = detail.battery {
                ui.label(RichText::new("·").color(TEXT_FAINT));
                super::widgets::battery(ui, level, detail.charging.unwrap_or(false));
            }
            if let Some(effect) = snap.software.get(&serial) {
                widgets::badge(ui, effects::label(effect), ACCENT);
            }
            if detail.game_mode == Some(true) {
                widgets::badge(ui, t("Game mode"), theme::WARN);
            }
            if caps.as_ref().is_some_and(|c| c["protocol"] == "razer-ble") {
                widgets::badge(ui, "Bluetooth", theme::INFO);
            }
        });
        let Some(caps) = caps else {
            ui.add(egui::Spinner::new().color(TEXT_FAINT));
            return;
        };
        ui.add_space(6.0);

        // -- lighting ---------------------------------------------------------------
        let zones: Vec<String> = super::edit::order_zones(&caps["zones"].as_object().map(|z| z.keys().cloned().collect::<Vec<_>>()).unwrap_or_default());
        if let Some(main) = zones.first().cloned() {
            let software_running = snap.software.contains_key(&serial);
            let quick = effects::quick_effects(&caps, &main);
            let edit = app.edits.entry(serial.clone()).or_default();
            let current = edit.zones.get(&main).cloned().unwrap_or_default();
            let mut commands: Vec<Command> = Vec::new();
            ui.horizontal(|ui| {
                widgets::section(ui, t("Lighting"));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let effect = if software_running { snap.software.get(&serial).cloned().unwrap_or_default() } else { current.effect.clone() };
                    if !effect.is_empty() {
                        ui.label(widgets::faint(effects::label(&effect)));
                    }
                });
            });
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing = vec2(5.0, 5.0);
                for effect in &quick {
                    let selected = current.effect == *effect && !software_running;
                    let text = RichText::new(effects::label(effect)).size(12.5);
                    let button = egui::Button::new(if selected { text.color(theme::ON_ACCENT).strong() } else { text.color(TEXT_DIM) }).fill(if selected { ACCENT } else { theme::SURFACE_HI }).corner_radius(CornerRadius::same(255));
                    if ui.add(button).clicked() {
                        // Every zone that has the effect gets it, so the device looks one piece.
                        for zone in &zones {
                            if !effects::effects_for(&caps, zone).contains(effect) {
                                continue;
                            }
                            let zone_edit = edit.zones.entry(zone.clone()).or_default();
                            zone_edit.effect = effect.clone();
                            light_up(zone_edit, effects::colour_count(effect));
                            commands.extend(effects::apply_commands(&serial, zone, zone_edit, software_running));
                        }
                    }
                }
            });
            if effects::effects_for(&caps, &main).iter().any(|e| e == "static") {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 6.0;
                    let mut colours: Vec<Color32> = PALETTE[..8].to_vec();
                    for rgb in app.settings.recent_colours.iter().take(2) {
                        colours.insert(0, Color32::from_rgb(rgb[0], rgb[1], rgb[2]));
                    }
                    colours.truncate(9);
                    for colour in colours {
                        let selected = current.colours[0] == colour && effects::colour_count(&current.effect) > 0 && !software_running;
                        if super::widgets::dot(ui, colour, 22.0, selected).on_hover_text(effects::hex(colour)).clicked() {
                            for zone in &zones {
                                let offered = effects::effects_for(&caps, zone);
                                let zone_edit = edit.zones.entry(zone.clone()).or_default();
                                // Keep the effect if it takes a colour; otherwise go static.
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
            }
            if !commands.is_empty() {
                edit.touch();
                for command in commands {
                    app.send(command);
                }
            }

            // Brightness: one slider for every zone that has it.
            let lit = brightness_zones(Some(&caps));
            if let Some(first) = lit.first().cloned() {
                let edit = app.edits.entry(serial.clone()).or_default();
                let mut value = edit.zones.get(&first).and_then(|z| z.brightness).unwrap_or(75.0);
                let mut changed = false;
                ui.horizontal(|ui| {
                    widgets::icon(ui, icon::BRIGHTNESS, 14.0, TEXT_DIM);
                    ui.spacing_mut().slider_width = (ui.available_width() - 60.0).max(120.0);
                    changed = ui.add(egui::Slider::new(&mut value, 0.0..=100.0).show_value(false)).changed();
                    ui.label(RichText::new(format!("{value:.0}%")).font(theme::bold(13.0)));
                });
                if changed {
                    edit.touch();
                    for zone in &lit {
                        edit.zones.entry(zone.clone()).or_default().brightness = Some(value);
                    }
                    for zone in lit {
                        app.debounce(format!("brightness:{serial}:{zone}"), brightness_call(&serial, &zone, value), 120);
                    }
                }
            }
        }

        // -- sensitivity ----------------------------------------------------------------
        if caps["dpi"] == true {
            ui.add_space(2.0);
            let tx = app.tx.clone();
            ui.horizontal(|ui| {
                widgets::section(ui, "DPI");
                ui.add_space(6.0);
                let edit = app.edits.entry(serial.clone()).or_default();
                let current = edit.dpi.map(|d| d.0).unwrap_or(0);
                ui.spacing_mut().item_spacing.x = 4.0;
                if let Some((active, stages)) = edit.stages.clone().filter(|(_, s)| !s.is_empty()) {
                    for (i, (x, _)) in stages.iter().enumerate() {
                        let number = i as i64 + 1;
                        let selected = number == active;
                        let text = RichText::new(x.to_string()).size(12.5);
                        if ui.add(egui::Button::new(if selected { text.color(theme::ON_ACCENT).strong() } else { text.color(TEXT_DIM) }).fill(if selected { ACCENT } else { theme::SURFACE_HI }).corner_radius(CornerRadius::same(255))).clicked() {
                            edit.stages = Some((number, stages.clone()));
                            edit.dpi = Some(stages[i]);
                            edit.touch();
                            let list: Vec<_> = stages.iter().map(|(x, y)| json!([x, y])).collect();
                            let _ = tx.send(Command::Call { serial: serial.clone(), method: "set_dpi_stages".into(), args: vec![json!(number), json!(list)], zone: None, then: Then::Performance });
                        }
                    }
                } else {
                    for preset in [400i64, 800, 1600, 3200] {
                        let selected = current == preset;
                        let text = RichText::new(preset.to_string()).size(12.5);
                        if ui.add(egui::Button::new(if selected { text.color(theme::ON_ACCENT).strong() } else { text.color(TEXT_DIM) }).fill(if selected { ACCENT } else { theme::SURFACE_HI }).corner_radius(CornerRadius::same(255))).clicked() {
                            edit.dpi = Some((preset, preset));
                            edit.touch();
                            let _ = tx.send(Command::Call { serial: serial.clone(), method: "set_dpi".into(), args: vec![json!(preset), json!(preset)], zone: None, then: Then::Performance });
                        }
                    }
                }
            });
        }

        // -- game mode ---------------------------------------------------------------------
        if caps["game_mode"] == true {
            let mut on = detail.game_mode.unwrap_or(false);
            if widgets::toggle_row(ui, t("Game mode"), t("Holds back the Windows key, so it cannot drop you out of a game."), &mut on) {
                app.send(Command::Call { serial: serial.clone(), method: "set_game_mode".into(), args: vec![json!(on)], zone: None, then: Then::Keyboard });
            }
        }

        ui.add_space(2.0);
        ui.horizontal(|ui| {
            if widgets::icon_button(ui, icon::LIGHTING, t("Lighting")).clicked() {
                app.open(Page::Lighting, &serial);
            }
            if widgets::icon_button(ui, icon::SETTINGS, t("All settings")).clicked() {
                app.open(Page::Devices, &serial);
            }
        });
    });
}
