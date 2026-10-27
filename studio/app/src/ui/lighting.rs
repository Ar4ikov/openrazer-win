//! Lighting, two ways.
//!
//! Quick: pick a part of the device on its drawing, pick an effect, pick a
//! colour -- the device's own effects, the way most people want them.
//!
//! Studio: per-key lighting in layers. Each layer is one effect painted onto
//! the keys chosen for it; upper layers cover lower ones, and the ones that
//! react to typing let what is below show through. The daemon draws the
//! result, so it keeps running with this window closed.

use super::app::App;
use super::canvas::{self, View};
use super::edit::{light_up, order_zones};
use super::effects::{self, colour_count, effects_for, speeds, zone_label};
use crate::art::{Art, Region, Shape};
use crate::preview::{self, Kind, Layer};
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, Color32, CornerRadius, Frame, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::{t, tf, widgets};
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Default)]
pub struct LightingUi {
    pub studio: bool,
    stacks: HashMap<String, Stack>,
    hex: String,
}

/// The layers being edited for one device.
struct Stack {
    layers: Vec<Layer>,
    current: usize,
    generation: u64,
    /// Edited since it was sent to the daemon.
    dirty: bool,
    started: Instant,
    presses: Vec<(Instant, (u8, u8))>,
}

impl Default for Stack {
    fn default() -> Self {
        Stack { layers: Vec::new(), current: 0, generation: u64::MAX, dirty: false, started: Instant::now(), presses: Vec::new() }
    }
}

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    let Some(device) = app.device(snap).cloned() else {
        return super::app::empty_state(ui, t("No device chosen"), "", None::<fn(&mut egui::Ui)>);
    };
    let Some(caps) = snap.details.get(&device.serial).and_then(|d| d.caps.clone()) else {
        ui.add(egui::Spinner::new().color(ACCENT));
        return;
    };
    let per_key = caps["software_effects"] == true;

    ui.horizontal(|ui| {
        let mut studio = app.lighting.studio;
        let options = vec![(false, t("Quick").to_string()), (true, t("Studio · per key").to_string())];
        if widgets::segmented(ui, &options, &mut studio) {
            app.lighting.studio = studio;
        }
        ui.add_space(8.0);
        ui.label(widgets::faint(if app.lighting.studio { t("Effects in layers, painted onto the keys you choose.") } else { t("The device's own effects, zone by zone.") }));
    });
    ui.add_space(10.0);

    let Some(art) = canvas::art_for(app, ui.ctx(), &device, &caps) else {
        canvas::waiting(ui, vec2(ui.available_width(), 300.0));
        return;
    };
    if app.lighting.studio && !per_key {
        widgets::banner(ui, theme::INFO, icon::INFO, t("This device has no per-key lighting"), t("Its lights are set zone by zone, in Quick mode."), |ui| {
            if ui.button(t("Go to Quick mode")).clicked() {
                app.lighting.studio = false;
            }
        });
        ui.add_space(10.0);
    }
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        if app.lighting.studio && per_key {
            studio(app, ui, &device, &caps, snap, art);
        } else {
            quick(app, ui, &device, &caps, snap, art);
        }
        ui.add_space(16.0);
    });
}

// -- quick ---------------------------------------------------------------------------------

/// What a device is showing now, LED by LED, as far as Studio can tell.
pub struct Look {
    pub colours: Vec<Color32>,
    pub layers_running: bool,
    /// The backlight is drawn LED by LED rather than as one zone.
    pub cells_for_backlight: bool,
}

impl Look {
    /// Whether `shape` is how the device is drawn in this look.
    pub fn visible(&self, shape: &Shape) -> bool {
        match shape.region {
            Region::Zone("backlight") => !self.layers_running && !self.cells_for_backlight,
            Region::Zone(_) => true,
            Region::Cell(..) => self.layers_running || self.cells_for_backlight,
        }
    }
}

pub fn now_showing(app: &mut App, art: &Art, device: &Device, caps: &Value, snap: &Snapshot, time: f32) -> Look {
    let serial = &device.serial;
    let zones: Vec<String> = caps["zones"].as_object().map(|z| z.keys().cloned().collect()).unwrap_or_default();
    let software = snap.software.get(serial).cloned();
    let layers_running = software.as_deref() == Some("layers");
    let layers = if layers_running { stack_from(snap, serial).unwrap_or_default() } else { Vec::new() };
    let lit = preview::render_layers(&layers, time, &[]);
    let cells_for_backlight = zones.iter().any(|z| z == "backlight") && art.shapes.iter().any(|s| matches!(s.region, Region::Cell(..)));
    let edit = app.edits.entry(serial.clone()).or_default();
    // Where the device has addressable LEDs they are drawn one by one, so a
    // wave rolls across them.
    let zone_colour = |zone: &str, shape: &Shape, i: usize| {
        let state = edit.zones.get(zone).cloned().unwrap_or_default();
        let effect = if zone == "backlight" { software.clone().unwrap_or(state.effect.clone()) } else { state.effect.clone() };
        let level = state.brightness.unwrap_or(100.0) / 100.0;
        let x = (shape.bounds(art).center().x / art.w as f32).clamp(0.0, 1.0);
        preview::hardware(&effect, &state.colours, state.speed, state.direction, time, x, preview::twinkle_phase((i / 256) as u8, (i % 256) as u8)).gamma_multiply(0.25 + 0.75 * level)
    };
    let colours = art
        .shapes
        .iter()
        .enumerate()
        .map(|(i, shape)| match shape.region {
            Region::Cell(r, c) if layers_running => lit.get(&(r, c)).copied().unwrap_or(Color32::BLACK),
            Region::Cell(..) => zone_colour("backlight", shape, i),
            Region::Zone(z) => zone_colour(z, shape, i),
        })
        .collect();
    Look { colours, layers_running, cells_for_backlight }
}

fn quick(app: &mut App, ui: &mut egui::Ui, device: &Device, caps: &Value, snap: &Snapshot, art: Arc<Art>) {
    let zones = order_zones(&caps["zones"].as_object().map(|z| z.keys().cloned().collect::<Vec<_>>()).unwrap_or_default());
    if zones.is_empty() {
        ui.label(widgets::dim(t("This device has no lighting the port can control.")));
        return;
    }
    let serial = device.serial.clone();
    let software = snap.software.get(&serial).cloned();
    let layers_running = software.as_deref() == Some("layers");
    let time = app.time();

    let mut zone = app.edits.entry(serial.clone()).or_default().zone.clone().filter(|z| zones.contains(z)).unwrap_or_else(|| zones[0].clone());

    let look = now_showing(app, &art, device, caps, snap, time);
    let (colours, cells_for_backlight) = (look.colours.clone(), look.cells_for_backlight);
    let edit = app.edits.entry(serial.clone()).or_default();
    let animated = edit.zones.values().any(|z| preview::hardware_animated(&z.effect)) || software.is_some();

    let wide = ui.available_width() > 1000.0;
    let width = if wide { ui.available_width() * 0.5 } else { ui.available_width() };
    let canvas_size = vec2(width, (width * art.h as f32 / art.w as f32 + 40.0).clamp(220.0, 460.0));
    let mut sync: Option<super::edit::ZoneEdit> = None;
    let mut commands: Vec<Command> = Vec::new();
    let mut debounced: Vec<(String, Command, u64)> = Vec::new();

    let layout = if wide { egui::Layout::left_to_right(egui::Align::Min) } else { egui::Layout::top_down(egui::Align::Min) };
    ui.with_layout(layout, |ui| {
        // -- the device ----------------------------------------------------------------
        ui.vertical(|ui| {
            ui.set_width(canvas_size.x);
            Frame::new().fill(theme::WELL).stroke(Stroke::new(1.0_f32, BORDER)).corner_radius(CornerRadius::same(theme::RADIUS)).show(ui, |ui| {
                let zone_now = zone.clone();
                let visible = |s: &Shape| match s.region {
                    Region::Zone("backlight") => !layers_running && !cells_for_backlight,
                    Region::Zone(_) => true,
                    Region::Cell(..) => layers_running || cells_for_backlight,
                };
                let selected = |s: &Shape| match s.region {
                    Region::Zone(z) => z == zone_now && zones.len() > 1,
                    Region::Cell(..) => zone_now == "backlight" && zones.len() > 1,
                };
                let picked = canvas::show(app, ui, &serial, &art, &colours, canvas_size, View { visible: &visible, selected: &selected, labels: !art.photo });
                if let Some(index) = picked.clicked {
                    zone = match art.shapes[index].region {
                        Region::Zone(z) => z.to_string(),
                        Region::Cell(..) => "backlight".to_string(),
                    };
                    app.lighting.hex.clear();
                }
            });
            if zones.len() > 1 {
                ui.add_space(6.0);
                let options: Vec<(String, String)> = zones.iter().map(|z| (z.clone(), zone_label(z, &device.kind).to_string())).collect();
                if widgets::segmented(ui, &options, &mut zone) {
                    app.lighting.hex.clear();
                }
                ui.label(widgets::faint(t("Click a part of the device, or choose it here.")));
            }
        });
        if wide {
            ui.add_space(18.0);
        }

        // -- the controls ------------------------------------------------------------------
        ui.vertical(|ui| {
            ui.set_max_width(ui.available_width() - 4.0);
            let edit = app.edits.entry(serial.clone()).or_default();
            edit.zone = Some(zone.clone());
            let offered = effects_for(caps, &zone);
            let current = edit.zones.entry(zone.clone()).or_default();
            let running_here = zone == "backlight" && software.is_some();

            widgets::heading(ui, &tf("Effect · {}", &[&zone_label(&zone, &device.kind)]));
            if running_here {
                ui.label(widgets::faint(tf("Showing now: {}, drawn by the daemon.", &[&effects::label(software.as_deref().unwrap_or(""))])));
            }
            let mut preview_zone = current.clone();
            light_up(&mut preview_zone, 3);
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing = vec2(8.0, 8.0);
                for effect in &offered {
                    let selected = if running_here { software.as_deref() == Some(effect.as_str()) } else { current.effect == *effect };
                    let colours = if selected { current.colours } else { preview_zone.colours };
                    if super::widgets::effect_tile(ui, effect, &colours, selected, time).clicked() {
                        current.effect = effect.clone();
                        light_up(current, colour_count(effect));
                        commands.extend(effects::apply_commands(&serial, &zone, current, software.is_some()));
                    }
                }
            });
            if offered.is_empty() {
                ui.label(widgets::dim(t("Only brightness can be set here.")));
            }

            let effect = current.effect.clone();
            let count = colour_count(&effect);
            if count > 0 && !running_here || software.as_deref().is_some_and(|s| colour_count(s) > 0) && running_here {
                ui.add_space(10.0);
                widgets::heading(ui, if count > 1 { t("Colours") } else { t("Colour") });
                if count > 1 {
                    ui.horizontal(|ui| {
                        for slot in 0..count {
                            if widgets::swatch(ui, current.colours[slot], 34.0, current.active == slot).clicked() {
                                current.active = slot;
                                app.lighting.hex.clear();
                            }
                        }
                    });
                }
                let active = current.active.min(count.max(1) - 1);
                current.active = active;
                let mut colour = current.colours[active];
                if super::widgets::colour_chooser(ui, &mut colour, &mut app.lighting.hex, &app.settings.recent_colours) {
                    current.colours[active] = colour;
                    debounced.push((format!("light:{serial}:{zone}"), Command::Refresh, 0));
                }
            }

            let speed_options = speeds(&effect);
            let has_direction = matches!(effect.as_str(), "wave" | "wheel");
            if (speed_options.is_some() || has_direction) && !running_here {
                ui.add_space(10.0);
                if let Some(options) = &speed_options {
                    ui.horizontal(|ui| {
                        widgets::section(ui, if effect == "reactive" { t("Afterglow") } else { t("Speed") });
                        let mut speed = current.speed.min(options.len() as i64);
                        if widgets::segmented(ui, options, &mut speed) {
                            current.speed = speed;
                            commands.extend(effects::apply_commands(&serial, &zone, current, software.is_some()));
                        }
                    });
                }
                if has_direction {
                    ui.horizontal(|ui| {
                        widgets::section(ui, t("Direction"));
                        let options = vec![(1i64, format!("← {}", t("Left"))), (2i64, format!("{} →", t("Right")))];
                        if widgets::segmented(ui, &options, &mut current.direction) {
                            commands.extend(effects::apply_commands(&serial, &zone, current, software.is_some()));
                        }
                    });
                }
            }

            if caps["zones"][&zone]["brightness"] == true {
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    widgets::icon(ui, icon::BRIGHTNESS, 15.0, TEXT_DIM);
                    widgets::section(ui, t("Brightness"));
                    let mut value = current.brightness.unwrap_or(75.0);
                    ui.spacing_mut().slider_width = (ui.available_width() - 70.0).max(120.0);
                    let slider = ui.add(egui::Slider::new(&mut value, 0.0..=100.0).show_value(false));
                    ui.label(RichText::new(format!("{value:.0}%")).font(theme::bold(14.0)));
                    if slider.changed() {
                        current.brightness = Some(value);
                        debounced.push((
                            format!("brightness:{serial}:{zone}"),
                            Command::Call { serial: serial.clone(), method: "set_brightness".into(), args: vec![json!((value * 10.0).round() / 10.0)], zone: Some(zone.clone()), then: Then::Nothing },
                            120,
                        ));
                    }
                });
            }

            ui.add_space(14.0);
            ui.horizontal(|ui| {
                if widgets::icon_button(ui, icon::SYNC, t("Apply to all devices")).on_hover_text(t("Every device, every zone that has this effect")).clicked() {
                    sync = Some(current.clone());
                }
            });
        });
    });
    if let Some(look) = sync {
        sync_all(app, snap, &look);
    }

    if animated {
        ui.ctx().request_repaint_after(Duration::from_millis(33));
    }
    let edit = app.edits.entry(serial.clone()).or_default();
    let zone_now = edit.zones.get(&zone).cloned().unwrap_or_default();
    if !commands.is_empty() || !debounced.is_empty() {
        edit.touch();
    }
    if commands.iter().chain(debounced.iter().map(|d| &d.1)).any(|c| matches!(c, Command::Call { .. } | Command::Refresh)) && colour_count(&zone_now.effect) > 0 {
        app.settings.remember_colour(zone_now.colours[zone_now.active.min(2)]);
    }
    for command in commands {
        app.send(command);
    }
    for (key, command, delay) in debounced {
        if key.starts_with("light:") {
            for (i, command) in effects::apply_commands(&serial, &zone, &zone_now, software.is_some()).into_iter().enumerate() {
                app.debounce(format!("{key}:{i}"), command, 140);
            }
        } else {
            app.debounce(key, command, delay);
        }
    }
}

/// Give every device the effect and colours of `look`, wherever it is offered.
fn sync_all(app: &mut App, snap: &Snapshot, look: &super::edit::ZoneEdit) {
    let mut count = 0;
    for device in &snap.devices {
        let Some(caps) = snap.details.get(&device.serial).and_then(|d| d.caps.clone()) else { continue };
        let running = snap.software.contains_key(&device.serial);
        let zones: Vec<String> = caps["zones"].as_object().map(|z| z.keys().cloned().collect()).unwrap_or_default();
        for zone in zones {
            if !effects_for(&caps, &zone).contains(&look.effect) {
                continue;
            }
            let edit = app.edits.entry(device.serial.clone()).or_default();
            let target = edit.zones.entry(zone.clone()).or_default();
            target.effect = look.effect.clone();
            target.colours = look.colours;
            target.speed = look.speed;
            target.direction = look.direction;
            edit.touch();
            for command in effects::apply_commands(&device.serial, &zone, &look.clone(), running) {
                app.send(command);
            }
            count += 1;
        }
    }
    app.toast(tf("Applied to {} zone(s)", &[&count]), false);
}

// -- studio ---------------------------------------------------------------------------------

/// The layers the daemon is drawing, or last drew, for `serial`.
fn stack_from(snap: &Snapshot, serial: &str) -> Option<Vec<Layer>> {
    let options = if snap.software.get(serial).map(String::as_str) == Some("layers") {
        snap.software_options.get(serial).cloned()
    } else {
        let saved = &snap.persistence[serial]["software"];
        (saved["effect"] == "layers").then(|| saved["options"].clone())
    }?;
    serde_json::from_value(options["layers"].clone()).ok()
}

fn cells_of(art: &Art) -> Vec<(u8, u8)> {
    let mut cells: Vec<(u8, u8)> = art.shapes.iter().filter_map(|s| if let Region::Cell(r, c) = s.region { Some((r, c)) } else { None }).collect();
    cells.sort();
    cells.dedup();
    cells
}

/// Groups of keys to select at once, by the names on them.
fn groups(art: &Art) -> Vec<(&'static str, Vec<(u8, u8)>)> {
    let named = |names: &[&str]| -> Vec<(u8, u8)> {
        art.shapes.iter().filter(|s| names.contains(&s.label.as_str())).filter_map(|s| if let Region::Cell(r, c) = s.region { Some((r, c)) } else { None }).collect()
    };
    let letters: Vec<String> = ('A'..='Z').map(String::from).collect();
    let letters: Vec<&str> = letters.iter().map(String::as_str).collect();
    let fkeys: Vec<String> = (1..=12).map(|i| format!("F{i}")).collect();
    let fkeys: Vec<&str> = fkeys.iter().map(String::as_str).collect();
    let mut groups = vec![
        ("WASD", named(&["W", "A", "S", "D"])),
        (t("Arrows"), named(&["←", "↑", "→", "↓"])),
        (t("Letters"), named(&letters)),
        (t("Numbers"), art.shapes.iter().filter_map(|s| match s.region {
            Region::Cell(1, c) if (2..=11).contains(&c) && s.label.len() == 1 => Some((1, c)),
            _ => None,
        }).collect()),
        (t("F-keys"), named(&fkeys)),
        (t("Numpad"), art.shapes.iter().filter_map(|s| match s.region {
            Region::Cell(r, c) if c >= 18 && (1..=5).contains(&r) => Some((r, c)),
            _ => None,
        }).collect()),
        (t("Macro keys"), named(&["M1", "M2", "M3", "M4", "M5"])),
        (t("Modifiers"), named(&["Shift", "Ctrl", "Alt", "Win", "Fn", "Menu", "Caps", "Tab"])),
    ];
    groups.retain(|(_, cells)| !cells.is_empty());
    groups
}

/// The cell of the key labelled like the key just pressed in this window.
fn pressed_cells(ui: &egui::Ui, art: &Art) -> Vec<(u8, u8)> {
    let names: Vec<String> = ui.input(|i| {
        i.events
            .iter()
            .filter_map(|e| match e {
                egui::Event::Key { key, pressed: true, repeat: false, .. } => Some(*key),
                _ => None,
            })
            .map(|key| match key {
                egui::Key::Space => String::new(),
                egui::Key::Escape => "Esc".into(),
                egui::Key::Enter => "Enter".into(),
                egui::Key::Tab => "Tab".into(),
                egui::Key::Backspace => "Backspace".into(),
                egui::Key::ArrowLeft => "←".into(),
                egui::Key::ArrowRight => "→".into(),
                egui::Key::ArrowUp => "↑".into(),
                egui::Key::ArrowDown => "↓".into(),
                egui::Key::Minus => "-".into(),
                egui::Key::Equals => "=".into(),
                egui::Key::Comma => ",".into(),
                egui::Key::Period => ".".into(),
                egui::Key::Slash => "/".into(),
                egui::Key::Semicolon => ";".into(),
                other => {
                    let name = other.name();
                    name.strip_prefix("Num").map(str::to_string).unwrap_or_else(|| name.to_string())
                }
            })
            .collect()
    });
    art.shapes.iter().filter(|s| names.contains(&s.label)).filter_map(|s| if let Region::Cell(r, c) = s.region { Some((r, c)) } else { None }).collect()
}

fn studio(app: &mut App, ui: &mut egui::Ui, device: &Device, caps: &Value, snap: &Snapshot, art: Arc<Art>) {
    let serial = device.serial.clone();
    let all = cells_of(&art);
    let fallback = app.edits.get(&serial).and_then(|e| e.zones.get("backlight")).map(|z| z.colours[0]).unwrap_or(ACCENT);
    let stack = app.lighting.stacks.entry(serial.clone()).or_default();
    // Load what the daemon has, unless there are edits not yet sent to it.
    if stack.generation != snap.generation && !stack.dirty {
        stack.generation = snap.generation;
        match stack_from(snap, &serial) {
            Some(layers) if !layers.is_empty() => stack.layers = layers,
            _ if stack.layers.is_empty() => {
                let mut base = Layer::new(Kind::Static, all.clone(), fallback);
                base.name = t("Base").into();
                stack.layers = vec![base];
            }
            _ => {}
        }
        stack.current = stack.current.min(stack.layers.len().saturating_sub(1));
    }
    let running = snap.software.get(&serial).map(String::as_str) == Some("layers");
    let mut changed = false;

    // Typing in the window shows what reactive layers will do.
    let now = Instant::now();
    for cell in pressed_cells(ui, &art) {
        stack.presses.push((now, cell));
    }
    stack.presses.retain(|(at, _)| at.elapsed() < Duration::from_secs(2));
    let presses: Vec<(f32, (u8, u8))> = stack.presses.iter().map(|(at, cell)| (at.elapsed().as_secs_f32(), *cell)).collect();
    let time = stack.started.elapsed().as_secs_f32();
    let lit = preview::render_layers(&stack.layers, time, &presses);
    let colours: Vec<Color32> = art.shapes.iter().map(|s| if let Region::Cell(r, c) = s.region { lit.get(&(r, c)).copied().unwrap_or(Color32::BLACK) } else { Color32::BLACK }).collect();
    if preview::animated(&stack.layers) || !presses.is_empty() {
        ui.ctx().request_repaint_after(Duration::from_millis(33));
    }

    let wide = ui.available_width() > 1100.0;
    ui.horizontal_top(|ui| {
        // -- layers ------------------------------------------------------------------------
        ui.vertical(|ui| {
            ui.set_width(250.0);
            widgets::card(ui, |ui| {
                ui.horizontal(|ui| {
                    widgets::heading(ui, t("Layers"));
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.menu_button(RichText::new(format!("+ {}", t("Add"))).strong(), |ui| {
                            ui.set_min_width(260.0);
                            for kind in Kind::ALL {
                                let response = ui.add(egui::Button::new(RichText::new(kind.label()).strong()).frame(false)).on_hover_text(kind.hint());
                                if response.clicked() {
                                    let stack = app.lighting.stacks.get_mut(&serial).expect("made above");
                                    let mut layer = Layer::new(kind, Vec::new(), fallback);
                                    layer.name = kind.label().to_string();
                                    stack.layers.push(layer);
                                    stack.current = stack.layers.len() - 1;
                                    changed = true;
                                    ui.close_menu();
                                }
                            }
                        });
                    });
                });
                ui.label(widgets::faint(t("Top covers bottom. Pick a layer, then click keys to paint it on.")));
                ui.add_space(4.0);
                let stack = app.lighting.stacks.get_mut(&serial).expect("made above");
                let mut action: Option<(usize, &str)> = None;
                // Listed top first, as they stack.
                for index in (0..stack.layers.len()).rev() {
                    let layer = &mut stack.layers[index];
                    let selected = stack.current == index;
                    Frame::new()
                        .fill(if selected { theme::SURFACE_ACTIVE } else { theme::SURFACE_HI })
                        .stroke(Stroke::new(1.0_f32, if selected { ACCENT } else { BORDER }))
                        .corner_radius(CornerRadius::same(theme::RADIUS))
                        .inner_margin(Margin::symmetric(8, 6))
                        .show(ui, |ui| {
                            ui.set_width(ui.available_width());
                            ui.horizontal(|ui| {
                                let eye = if layer.enabled { icon::EYE } else { icon::EYE_OFF };
                                if widgets::ghost(ui, eye, t("Show or hide")).clicked() {
                                    layer.enabled = !layer.enabled;
                                    changed = true;
                                }
                                let (swatch, _) = ui.allocate_exact_size(vec2(14.0, 14.0), Sense::hover());
                                let colour = if layer.kind.colours() == 0 { preview::hue(time / 4.0) } else { layer.colour(0) };
                                ui.painter().rect_filled(swatch, CornerRadius::same(3), colour);
                                let name = if layer.name.is_empty() { layer.kind.label().to_string() } else { layer.name.clone() };
                                let label = ui.add(egui::Label::new(RichText::new(name).color(if layer.enabled { TEXT } else { TEXT_FAINT })).sense(Sense::click()).truncate());
                                if label.clicked() {
                                    action = Some((index, "select"));
                                }
                                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                    ui.label(widgets::faint(layer.cells.len().to_string()));
                                });
                            });
                            if selected {
                                ui.horizontal(|ui| {
                                    ui.spacing_mut().item_spacing.x = 2.0;
                                    if widgets::ghost(ui, icon::UP, t("Move up")).clicked() {
                                        action = Some((index, "up"));
                                    }
                                    if widgets::ghost(ui, icon::DOWN, t("Move down")).clicked() {
                                        action = Some((index, "down"));
                                    }
                                    if widgets::ghost(ui, icon::COPY, t("Duplicate")).clicked() {
                                        action = Some((index, "copy"));
                                    }
                                    if widgets::ghost(ui, icon::DELETE, t("Delete")).clicked() {
                                        action = Some((index, "delete"));
                                    }
                                });
                            }
                        });
                    ui.add_space(2.0);
                }
                if let Some((index, what)) = action {
                    match what {
                        "select" => stack.current = index,
                        "up" if index + 1 < stack.layers.len() => {
                            stack.layers.swap(index, index + 1);
                            stack.current = index + 1;
                            changed = true;
                        }
                        "down" if index > 0 => {
                            stack.layers.swap(index, index - 1);
                            stack.current = index - 1;
                            changed = true;
                        }
                        "copy" => {
                            let copy = stack.layers[index].clone();
                            stack.layers.insert(index + 1, copy);
                            stack.current = index + 1;
                            changed = true;
                        }
                        "delete" => {
                            stack.layers.remove(index);
                            stack.current = stack.current.min(stack.layers.len().saturating_sub(1));
                            changed = true;
                        }
                        _ => {}
                    }
                }
                if stack.layers.is_empty() {
                    ui.label(widgets::dim(t("No layers: the keys are dark. Add one above.")));
                }
            });
        });

        // -- the device ------------------------------------------------------------------------
        ui.vertical(|ui| {
            let canvas_width = if wide { ui.available_width() - 300.0 } else { ui.available_width() };
            ui.set_width(canvas_width);
            let stack = app.lighting.stacks.get_mut(&serial).expect("made above");
            let current: BTreeSet<(u8, u8)> = stack.layers.get(stack.current).map(|l| l.cells.iter().copied().collect()).unwrap_or_default();
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing = vec2(4.0, 4.0);
                let mut set: Option<Vec<(u8, u8)>> = None;
                if ui.button(t("All")).clicked() {
                    set = Some(all.clone());
                }
                if ui.button(t("None")).clicked() {
                    set = Some(Vec::new());
                }
                if ui.button(t("Invert")).clicked() {
                    set = Some(all.iter().copied().filter(|c| !current.contains(c)).collect());
                }
                for (name, cells) in groups(&art) {
                    if ui.button(name).on_hover_text(t("Add these keys to the layer")).clicked() {
                        let mut merged = current.clone();
                        merged.extend(cells);
                        set = Some(merged.into_iter().collect());
                    }
                }
                if let (Some(cells), Some(layer)) = (set, stack.layers.get_mut(stack.current)) {
                    layer.cells = cells;
                    changed = true;
                }
            });
            ui.add_space(4.0);
            let height = (canvas_width * art.h as f32 / art.w as f32 + 40.0).clamp(240.0, (ui.available_height() - 90.0).max(240.0));
            Frame::new().fill(theme::WELL).stroke(Stroke::new(1.0_f32, BORDER)).corner_radius(CornerRadius::same(theme::RADIUS)).show(ui, |ui| {
                let visible = |s: &Shape| matches!(s.region, Region::Cell(..));
                let selected = |s: &Shape| matches!(s.region, Region::Cell(r, c) if current.contains(&(r, c)));
                let picked = canvas::show(app, ui, &serial, &art, &colours, vec2(canvas_width, height), View { visible: &visible, selected: &selected, labels: !art.photo });
                let stack = app.lighting.stacks.get_mut(&serial).expect("made above");
                if let Some(layer) = stack.layers.get_mut(stack.current) {
                    if let Some(index) = picked.clicked {
                        if let Region::Cell(r, c) = art.shapes[index].region {
                            if let Some(at) = layer.cells.iter().position(|cell| *cell == (r, c)) {
                                layer.cells.remove(at);
                            } else if !picked.secondary {
                                layer.cells.push((r, c));
                            }
                            changed = true;
                        }
                    }
                    if let Some(swept) = picked.swept {
                        let remove = picked.modifiers.alt;
                        for index in swept {
                            if let Region::Cell(r, c) = art.shapes[index].region {
                                let at = layer.cells.iter().position(|cell| *cell == (r, c));
                                match (remove, at) {
                                    (true, Some(at)) => {
                                        layer.cells.remove(at);
                                    }
                                    (false, None) => layer.cells.push((r, c)),
                                    _ => {}
                                }
                            }
                        }
                        changed = true;
                    }
                }
            });
            ui.horizontal(|ui| {
                ui.label(widgets::faint(t("Click keys to add or remove them; drag to add many, Alt+drag to remove. Type here to try reactive layers.")));
            });
            let (dirty, empty) = app.lighting.stacks.get(&serial).map(|s| (s.dirty, s.layers.is_empty())).unwrap_or((false, true));
            ui.horizontal(|ui| {
                let (colour, text) = if dirty {
                    (theme::WARN, t("Applying…"))
                } else if running {
                    (ACCENT, t("On the device: the daemon keeps drawing it with this window closed"))
                } else {
                    (TEXT_FAINT, t("Not on the device yet: change anything to apply it"))
                };
                let (dot, _) = ui.allocate_exact_size(vec2(8.0, 8.0), Sense::hover());
                ui.painter().circle_filled(dot.center(), 4.0, colour);
                ui.label(RichText::new(text).size(12.5).color(TEXT_DIM));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if running && widgets::danger_button(ui, t("Turn per-key lighting off")).clicked() {
                        app.send(Command::Software { serial: serial.clone(), effect: "none".into(), options: json!({}) });
                        let edit = app.edits.entry(serial.clone()).or_default();
                        let back = edit.zones.get("backlight").cloned().unwrap_or_default();
                        let mut back = back;
                        if back.effect.is_empty() || back.effect == "custom" {
                            back.effect = "spectrum".into();
                        }
                        for command in effects::apply_commands(&serial, "backlight", &back, false) {
                            app.send(command);
                        }
                    } else if !running && !empty && widgets::primary_button(ui, t("Apply")).clicked() {
                        changed = true;
                    }
                });
            });
            if caps["type"] == "keyboard" {
                let mut variant = art.variant.unwrap_or(crate::layout::Variant::Ansi);
                ui.horizontal(|ui| {
                    ui.label(widgets::faint(t("Key layout")));
                    let options = vec![(crate::layout::Variant::Ansi, canvas::variant_label(crate::layout::Variant::Ansi).to_string()), (crate::layout::Variant::Iso, canvas::variant_label(crate::layout::Variant::Iso).to_string())];
                    if widgets::segmented(ui, &options, &mut variant) {
                        app.settings.key_layouts.insert(serial.clone(), variant);
                        app.save_settings();
                        app.arts.forget(&serial);
                    }
                });
            }
        });

        // -- the chosen layer -------------------------------------------------------------------
        if wide {
            ui.vertical(|ui| {
                ui.set_width(280.0);
                changed |= layer_panel(app, ui, &serial);
            });
        }
    });
    if !wide {
        ui.add_space(10.0);
        changed |= layer_panel(app, ui, &serial);
    }

    let stack = app.lighting.stacks.get_mut(&serial).expect("made above");
    if changed {
        stack.dirty = true;
        let layers = serde_json::to_value(&stack.layers).unwrap_or(Value::Null);
        app.debounce(format!("layers:{serial}"), Command::Software { serial: serial.clone(), effect: "layers".into(), options: json!({ "layers": layers }) }, 300);
    } else if stack.dirty && !app_pending(app, &serial) {
        // Sent: the daemon's copy is the one to load from again.
        if let Some(stack) = app.lighting.stacks.get_mut(&serial) {
            stack.dirty = false;
        }
    }
}

fn app_pending(app: &App, serial: &str) -> bool {
    app.is_pending(&format!("layers:{serial}"))
}

/// Settings of the chosen layer.  True if anything changed.
fn layer_panel(app: &mut App, ui: &mut egui::Ui, serial: &str) -> bool {
    let recent = app.settings.recent_colours.clone();
    let Some(stack) = app.lighting.stacks.get_mut(serial) else { return false };
    let Some(layer) = stack.layers.get_mut(stack.current) else { return false };
    let mut changed = false;
    let mut remember: Option<Color32> = None;
    widgets::card(ui, |ui| {
        widgets::heading(ui, t("Layer"));
        if ui.add(egui::TextEdit::singleline(&mut layer.name).hint_text(layer.kind.label()).desired_width(f32::INFINITY)).changed() {
            changed = true;
        }
        let mut kind = layer.kind;
        egui::ComboBox::from_id_salt(("kind", serial)).selected_text(kind.label()).width(ui.available_width() - 4.0).show_ui(ui, |ui| {
            for option in Kind::ALL {
                ui.selectable_value(&mut kind, option, option.label()).on_hover_text(option.hint());
            }
        });
        if kind != layer.kind {
            let first = layer.colour(0);
            *layer = Layer { kind, cells: std::mem::take(&mut layer.cells), name: std::mem::take(&mut layer.name), ..Layer::new(kind, Vec::new(), first) };
            changed = true;
        }
        ui.label(widgets::faint(layer.kind.hint()));
        if layer.kind.key_driven() {
            ui.label(widgets::faint(t("Lights up as you type anywhere, not just in this window.")));
        }

        let most = layer.kind.colours();
        if most > 0 {
            ui.add_space(4.0);
            widgets::section(ui, t("Colours"));
            ui.horizontal(|ui| {
                for (i, colour) in layer.colours.clone().iter().enumerate() {
                    let response = widgets::swatch(ui, *colour, 30.0, ui.data(|d| d.get_temp::<usize>(egui::Id::new(("slot", serial)))).unwrap_or(0) == i);
                    if response.clicked() {
                        ui.data_mut(|d| d.insert_temp(egui::Id::new(("slot", serial)), i));
                        app.lighting.hex.clear();
                    }
                    if response.secondary_clicked() && layer.colours.len() > 1 {
                        layer.colours.remove(i);
                        changed = true;
                    }
                }
                if layer.colours.len() < most && ui.button("+").on_hover_text(t("Another colour")).clicked() {
                    layer.colours.push(preview::hue(layer.colours.len() as f32 * 0.33));
                    changed = true;
                }
            });
            let slot = ui.data(|d| d.get_temp::<usize>(egui::Id::new(("slot", serial)))).unwrap_or(0).min(layer.colours.len().saturating_sub(1));
            if let Some(colour) = layer.colours.get_mut(slot) {
                if super::widgets::colour_chooser(ui, colour, &mut app.lighting.hex, &recent) {
                    remember = Some(*colour);
                    changed = true;
                }
            }
        }
        if layer.kind.has_speed() {
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                widgets::section(ui, t("Speed"));
                ui.spacing_mut().slider_width = (ui.available_width() - 50.0).max(80.0);
                if ui.add(egui::Slider::new(&mut layer.speed, 0.25..=3.0).step_by(0.25).show_value(false)).changed() {
                    changed = true;
                }
                ui.label(RichText::new(format!("{:.2}×", layer.speed)).size(12.5));
            });
        }
        if layer.kind.has_direction() {
            ui.horizontal(|ui| {
                widgets::section(ui, t("Direction"));
                let options = vec![(1u8, format!("{} →", t("Right"))), (2u8, format!("← {}", t("Left")))];
                if widgets::segmented(ui, &options, &mut layer.direction) {
                    changed = true;
                }
            });
        }
    });
    if let Some(colour) = remember {
        app.settings.remember_colour(colour);
    }
    changed
}
