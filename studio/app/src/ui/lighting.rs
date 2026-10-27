//! Lighting, two ways, as a tab of the device page.
//!
//! Quick effects: pick a part of the device on its drawing, pick an effect,
//! pick a colour -- the device's own effects, the way most people want them.
//!
//! Studio: per-key lighting in layers. Each layer is one effect painted onto
//! the keys chosen for it; upper layers cover lower ones, and the ones that
//! react to typing let what is below show through. The daemon draws the
//! result, so it keeps running with this window closed.

use super::app::{scroll, stage_frame, App};
use super::canvas::{self, View};
use super::edit::{light_up, order_zones};
use super::effects::{self, colour_count, effects_for, speeds, zone_label};
use crate::art::{Art, Region, Shape};
use crate::preview::{self, Kind, Layer};
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, Align, Color32, CornerRadius, Layout, RichText, Sense};
use openrazer_studio_core::theme::{self, icon, ACCENT, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::widgets::{self, Button, IconButton};
use openrazer_studio_core::{t, tf};
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// The width of the layer list and of the layer's settings beside the canvas.
const LAYERS_WIDTH: f32 = 240.0;
const PROPERTIES_WIDTH: f32 = 284.0;

#[derive(Default)]
pub struct LightingUi {
    pub studio: bool,
    stacks: HashMap<String, Stack>,
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
    /// Which of the chosen layer's colours is being edited.
    slot: usize,
}

impl Default for Stack {
    fn default() -> Self {
        Stack { layers: Vec::new(), current: 0, generation: u64::MAX, dirty: false, started: Instant::now(), presses: Vec::new(), slot: 0 }
    }
}

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot, device: &Device) {
    let Some(caps) = snap.details.get(&device.serial).and_then(|d| d.caps.clone()) else {
        ui.add(egui::Spinner::new().color(ACCENT));
        return;
    };
    let per_key = caps["software_effects"] == true;

    // The two ways, and what the chosen one is for.
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 12.0;
        let mut studio = app.lighting.studio;
        let options = vec![(false, t("Quick effects").to_string()), (true, t("Studio · per key").to_string())];
        if widgets::segmented(ui, &options, &mut studio) {
            app.lighting.studio = studio;
        }
        let hint = if app.lighting.studio { t("Effects in layers, painted onto the keys you choose.") } else { t("The device's own effects, zone by zone.") };
        ui.label(RichText::new(hint).size(13.0).color(TEXT_DIM));
    });
    ui.add_space(theme::GAP);

    let Some(art) = canvas::art_for(app, ui.ctx(), device, &caps) else {
        widgets::plain_panel(ui, |ui| canvas::waiting(ui, vec2(ui.available_width(), 300.0)));
        return;
    };
    scroll(ui, "lighting", |ui| {
        if app.lighting.studio && !per_key {
            widgets::banner(ui, theme::INFO, icon::INFO, t("This device has no per-key lighting"), t("Its lights are set zone by zone, in Quick effects."), |ui| {
                if ui.add(Button::secondary(t("Go to Quick effects")).small()).clicked() {
                    app.lighting.studio = false;
                }
            });
        }
        if app.lighting.studio && per_key {
            studio(app, ui, device, &caps, snap, art);
        } else {
            quick(app, ui, device, &caps, snap, art);
        }
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
        widgets::plain_panel(ui, |ui| widgets::empty_state(ui, t("This device has no lighting the port can control."), "", |_| {}));
        return;
    }
    let serial = device.serial.clone();
    let software = snap.software.get(&serial).cloned();
    let layers_running = software.as_deref() == Some("layers");
    let time = app.time();

    let mut zone = app.edits.entry(serial.clone()).or_default().zone.clone().filter(|z| zones.contains(z)).unwrap_or_else(|| zones[0].clone());

    let look = now_showing(app, &art, device, caps, snap, time);
    let (colours, cells_for_backlight) = (look.colours.clone(), look.cells_for_backlight);
    let animated = app.edits.entry(serial.clone()).or_default().zones.values().any(|z| preview::hardware_animated(&z.effect)) || software.is_some();

    let mut sync: Option<super::edit::ZoneEdit> = None;
    let mut commands: Vec<Command> = Vec::new();
    let mut debounced: Vec<(String, Command, u64)> = Vec::new();

    widgets::columns(ui, 0.56, 900.0, |ui, column| {
        if column == 0 {
            // -- the device ----------------------------------------------------------------
            widgets::panel(ui, t("Device"), |ui| {
                stage_frame().show(ui, |ui| {
                    let width = ui.available_width();
                    // Lower when the panels stack, so the effects are not far below.
                    let tallest = if width > 600.0 { 240.0 } else { 300.0 };
                    let height = (width * art.h as f32 / art.w as f32 + 40.0).clamp(200.0, tallest);
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
                    let picked = canvas::show(app, ui, &serial, &art, &colours, vec2(width, height), View { visible: &visible, selected: &selected, labels: !art.photo });
                    if let Some(index) = picked.clicked {
                        zone = match art.shapes[index].region {
                            Region::Zone(z) => z.to_string(),
                            Region::Cell(..) => "backlight".to_string(),
                        };
                    }
                });
                if zones.len() > 1 {
                    ui.add_space(4.0);
                    widgets::row(ui, t("Part"), t("Click a part of the device, or choose it here."), |ui| {
                        let options: Vec<(String, String)> = zones.iter().map(|z| (z.clone(), zone_label(z, &device.kind).to_string())).collect();
                        let widest = options.iter().map(|(_, l)| widgets::galley(ui, l, theme::body(13.5), TEXT, 0.0).size().x).fold(0.0_f32, f32::max);
                        if (widest + 28.0).max(52.0) * options.len() as f32 + 4.0 <= ui.available_width() {
                            widgets::segmented(ui, &options, &mut zone);
                        } else {
                            let mut chosen = None;
                            widgets::select(ui, ("zone", &serial), zone_label(&zone, &device.kind), ui.available_width().min(240.0), |ui| {
                                for (value, label) in &options {
                                    if widgets::option(ui, *value == zone, label).clicked() {
                                        chosen = Some(value.clone());
                                    }
                                }
                            });
                            if let Some(chosen) = chosen {
                                zone = chosen;
                            }
                        }
                    });
                }
            });

            // -- the effect ----------------------------------------------------------------
            let edit = app.edits.entry(serial.clone()).or_default();
            edit.zone = Some(zone.clone());
            let offered = effects_for(caps, &zone);
            let current = edit.zones.entry(zone.clone()).or_default();
            let running_here = zone == "backlight" && software.is_some();
            widgets::panel(ui, &tf("Effect · {}", &[&zone_label(&zone, &device.kind)]), |ui| {
                if running_here {
                    widgets::help(ui, &tf("Showing now: {}, drawn by the daemon.", &[&effects::label(software.as_deref().unwrap_or(""))]));
                }
                if offered.is_empty() {
                    ui.label(widgets::dim(t("Only brightness can be set here.")));
                    return;
                }
                let mut preview_zone = current.clone();
                light_up(&mut preview_zone, 3);
                let shown = current.effect.clone();
                let clicked = super::widgets::effect_grid(
                    ui,
                    &offered,
                    |effect| if effect == shown { current.colours } else { preview_zone.colours },
                    |effect| if running_here { software.as_deref() == Some(effect) } else { shown == effect },
                    time,
                );
                if let Some(effect) = clicked {
                    current.effect = effect.clone();
                    light_up(current, colour_count(&effect));
                    commands.extend(effects::apply_commands(&serial, &zone, current, software.is_some()));
                }
            });
            return;
        }

        let edit = app.edits.entry(serial.clone()).or_default();
        let current = edit.zones.entry(zone.clone()).or_default();
        let running_here = zone == "backlight" && software.is_some();
        let effect = current.effect.clone();

        // -- how it runs: always the same three rows, greyed where they do not apply --
        widgets::panel(ui, t("Adjustments"), |ui| {
            widgets::row(ui, t("Brightness"), "", |ui| {
                if caps["zones"][&zone]["brightness"] == true {
                    let mut value = current.brightness.unwrap_or(75.0) as f64;
                    if widgets::slider_field(ui, ("brightness", &serial, &zone), &mut value, 0.0..=100.0, 1.0, 0, "%", 72.0) {
                        current.brightness = Some(value as f32);
                        debounced.push((
                            format!("brightness:{serial}:{zone}"),
                            Command::Call { serial: serial.clone(), method: "set_brightness".into(), args: vec![json!((value * 10.0).round() / 10.0)], zone: Some(zone.clone()), then: Then::Nothing },
                            120,
                        ));
                    }
                } else {
                    ui.label(widgets::faint(t("Not adjustable on this part")));
                }
            });
            widgets::divider(ui);
            let speed_label = if effect == "reactive" { t("Afterglow") } else { t("Speed") };
            widgets::row(ui, speed_label, "", |ui| match speeds(&effect).filter(|_| !running_here) {
                Some(options) => {
                    let mut speed = current.speed.min(options.len() as i64);
                    if widgets::segmented(ui, &options, &mut speed) {
                        current.speed = speed;
                        commands.extend(effects::apply_commands(&serial, &zone, current, software.is_some()));
                    }
                }
                None => {
                    ui.label(widgets::faint(t("Not used by this effect")));
                }
            });
            widgets::divider(ui);
            widgets::row(ui, t("Direction"), "", |ui| {
                if matches!(effect.as_str(), "wave" | "wheel") && !running_here {
                    let options = vec![(1i64, format!("← {}", t("Left"))), (2i64, format!("{} →", t("Right")))];
                    if widgets::segmented(ui, &options, &mut current.direction) {
                        commands.extend(effects::apply_commands(&serial, &zone, current, software.is_some()));
                    }
                } else {
                    ui.label(widgets::faint(t("Not used by this effect")));
                }
            });
            widgets::divider(ui);
            widgets::row(ui, t("Every device"), t("The same effect and colours wherever it is offered."), |ui| {
                if ui.add(Button::secondary(t("Apply to all devices")).icon(icon::SYNC)).clicked() {
                    sync = Some(current.clone());
                }
            });
        });

        // -- colours ---------------------------------------------------------------------
        let count = colour_count(&effect);
        widgets::panel(ui, if count > 1 { t("Colours") } else { t("Colour") }, |ui| {
            if count == 0 {
                widgets::help(ui, t("This effect picks its own colours."));
            }
            if count > 1 {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 10.0;
                    for slot in 0..count {
                        if widgets::swatch(ui, current.colours[slot], 32.0, current.active == slot).on_hover_text(tf("Colour {}", &[&(slot + 1)])).clicked() {
                            current.active = slot;
                        }
                    }
                    ui.label(widgets::faint(t("Pick which colour to change.")));
                });
                ui.add_space(4.0);
            }
            let active = current.active.min(count.max(1) - 1);
            current.active = active;
            let mut colour = current.colours[active];
            ui.add_enabled_ui(count > 0, |ui| {
                if super::widgets::colour_chooser(ui, &format!("quick-{serial}-{zone}-{active}"), &mut colour, &app.settings.recent_colours) {
                    current.colours[active] = colour;
                    debounced.push((format!("light:{serial}:{zone}"), Command::Refresh, 0));
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
    edit.zone = Some(zone.clone());
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
    // Typing a layer's name is not playing the keyboard.
    if ui.ctx().wants_keyboard_input() {
        return Vec::new();
    }
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

    // Layers on the left, the keys in the middle, the chosen layer on the
    // right; with less room, the chosen layer goes under the list.
    let gap = theme::GAP;
    let width = ui.available_width();
    let three = width >= LAYERS_WIDTH + PROPERTIES_WIDTH + 2.0 * gap + 440.0;
    let side = if three { LAYERS_WIDTH } else { LAYERS_WIDTH + 30.0 };
    let middle = if three { width - LAYERS_WIDTH - PROPERTIES_WIDTH - 2.0 * gap } else { width - side - gap };
    ui.horizontal_top(|ui| {
        ui.spacing_mut().item_spacing.x = gap;
        ui.allocate_ui_with_layout(vec2(side, 0.0), Layout::top_down(Align::Min), |ui| {
            ui.set_width(side);
            ui.spacing_mut().item_spacing = vec2(theme::SPACING, gap);
            changed |= layers_panel(app, ui, &serial, fallback, time);
            if !three {
                changed |= layer_panel(app, ui, &serial);
            }
        });
        ui.allocate_ui_with_layout(vec2(middle, 0.0), Layout::top_down(Align::Min), |ui| {
            ui.set_width(middle);
            ui.spacing_mut().item_spacing.x = theme::SPACING;
            changed |= keys_panel(app, ui, &serial, caps, &art, &all, &colours, running);
        });
        if three {
            ui.allocate_ui_with_layout(vec2(PROPERTIES_WIDTH, 0.0), Layout::top_down(Align::Min), |ui| {
                ui.set_width(PROPERTIES_WIDTH);
                ui.spacing_mut().item_spacing.x = theme::SPACING;
                changed |= layer_panel(app, ui, &serial);
            });
        }
    });

    let pending = app.is_pending(&format!("layers:{serial}"));
    let stack = app.lighting.stacks.get_mut(&serial).expect("made above");
    if changed {
        stack.dirty = true;
        let layers = serde_json::to_value(&stack.layers).unwrap_or(Value::Null);
        app.debounce(format!("layers:{serial}"), Command::Software { serial: serial.clone(), effect: "layers".into(), options: json!({ "layers": layers }) }, 300);
    } else if stack.dirty && !pending {
        // Sent: the daemon's copy is the one to load from again.
        stack.dirty = false;
    }
}

/// The list of layers, top first, with buttons to add, move, copy and delete.
/// True if the layers changed.
fn layers_panel(app: &mut App, ui: &mut egui::Ui, serial: &str, fallback: Color32, time: f32) -> bool {
    let mut changed = false;
    let mut added: Option<Kind> = None;
    let stack = app.lighting.stacks.get_mut(serial).expect("made above");
    widgets::panel_with(
        ui,
        t("Layers"),
        |ui| {
            widgets::menu_button(ui, icon::ADD, t("Add"), |ui| {
                for kind in Kind::ALL {
                    if widgets::option(ui, false, kind.label()).on_hover_text(kind.hint()).clicked() {
                        added = Some(kind);
                        ui.close_menu();
                    }
                }
            });
        },
        |ui| {
            ui.spacing_mut().item_spacing.y = 4.0;
            let mut action: Option<(usize, &str)> = None;
            if stack.layers.is_empty() {
                widgets::help(ui, t("No layers: the keys are dark. Add one above."));
            }
            // Listed top first, as they stack.
            for index in (0..stack.layers.len()).rev() {
                let selected = stack.current == index;
                let layer = &mut stack.layers[index];
                let (row, _) = widgets::list_row(ui, selected, 40.0, |ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    let eye = if layer.enabled { icon::EYE } else { icon::EYE_OFF };
                    if ui.add(IconButton::new(eye, t("Show or hide")).size(24.0)).clicked() {
                        layer.enabled = !layer.enabled;
                        changed = true;
                    }
                    let (swatch, _) = ui.allocate_exact_size(vec2(14.0, 14.0), Sense::hover());
                    let colour = if layer.kind.colours() == 0 { preview::hue(time / 4.0) } else { layer.colour(0) };
                    ui.painter().rect_filled(swatch, CornerRadius::same(3), colour);
                    let count = layer.cells.len().to_string();
                    let name = if layer.name.is_empty() { layer.kind.label().to_string() } else { layer.name.clone() };
                    let room = ui.available_width() - 30.0;
                    let text = widgets::galley_fit(ui, &name, theme::body(13.5), if layer.enabled { TEXT } else { TEXT_FAINT }, room);
                    let (rect, _) = ui.allocate_exact_size(vec2(room, 20.0), Sense::hover());
                    ui.painter().galley(egui::pos2(rect.left(), rect.center().y - text.size().y / 2.0), text, TEXT);
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.label(RichText::new(count).size(12.0).color(TEXT_FAINT)).on_hover_text(t("Keys in this layer"));
                    });
                });
                if row.clicked() {
                    action = Some((index, "select"));
                }
            }
            ui.add_space(4.0);
            // What to do with the chosen layer: always the same four buttons.
            let current = stack.current;
            let count = stack.layers.len();
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 6.0;
                if ui.add(IconButton::new(icon::UP, t("Move up")).size(30.0).enabled(current + 1 < count)).clicked() {
                    action = Some((current, "up"));
                }
                if ui.add(IconButton::new(icon::DOWN, t("Move down")).size(30.0).enabled(current > 0 && count > 0)).clicked() {
                    action = Some((current, "down"));
                }
                if ui.add(IconButton::new(icon::COPY, t("Duplicate")).size(30.0).enabled(count > 0)).clicked() {
                    action = Some((current, "copy"));
                }
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.add(IconButton::new(icon::DELETE, t("Delete")).danger().size(30.0).enabled(count > 0)).clicked() {
                        action = Some((current, "delete"));
                    }
                });
            });
            widgets::help(ui, t("Top covers bottom. Pick a layer, then click keys to paint it on."));
            if let Some((index, what)) = action {
                match what {
                    "select" => {
                        stack.current = index;
                        stack.slot = 0;
                    }
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
                    "copy" if index < stack.layers.len() => {
                        let copy = stack.layers[index].clone();
                        stack.layers.insert(index + 1, copy);
                        stack.current = index + 1;
                        changed = true;
                    }
                    "delete" if index < stack.layers.len() => {
                        stack.layers.remove(index);
                        stack.current = stack.current.min(stack.layers.len().saturating_sub(1));
                        changed = true;
                    }
                    _ => {}
                }
            }
        },
    );
    if let Some(kind) = added {
        let stack = app.lighting.stacks.get_mut(serial).expect("made above");
        let mut layer = Layer::new(kind, Vec::new(), fallback);
        layer.name = kind.label().to_string();
        stack.layers.push(layer);
        stack.current = stack.layers.len() - 1;
        stack.slot = 0;
        changed = true;
    }
    changed
}

/// The keyboard to paint the chosen layer onto, a selection toolbar above it
/// and whether it is on the device below. True if the layers changed.
#[allow(clippy::too_many_arguments)]
fn keys_panel(app: &mut App, ui: &mut egui::Ui, serial: &str, caps: &Value, art: &Arc<Art>, all: &[(u8, u8)], colours: &[Color32], running: bool) -> bool {
    let mut changed = false;
    let mut turn_off = false;
    let in_layer = app.lighting.stacks.get(serial).and_then(|s| s.layers.get(s.current)).map_or(0, |l| l.cells.len());
    widgets::panel_with(ui, t("Keys"), |ui| {
        widgets::chip(ui, &tf("{} keys", &[&in_layer])).on_hover_text(t("Keys in this layer"));
    }, |ui| {
        let stack = app.lighting.stacks.get_mut(serial).expect("made above");
        let current: BTreeSet<(u8, u8)> = stack.layers.get(stack.current).map(|l| l.cells.iter().copied().collect()).unwrap_or_default();
        let has_layer = stack.layers.get(stack.current).is_some();
        // -- selection toolbar ---------------------------------------------------------
        let mut set: Option<Vec<(u8, u8)>> = None;
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
            ui.add_enabled_ui(has_layer, |ui| {
                if ui.add(Button::ghost(t("All")).icon(icon::SELECT_ALL).small()).clicked() {
                    set = Some(all.to_vec());
                }
                if ui.add(Button::ghost(t("None")).icon(icon::CLEAR).small()).clicked() {
                    set = Some(Vec::new());
                }
                if ui.add(Button::ghost(t("Invert")).icon(icon::SYNC).small()).clicked() {
                    set = Some(all.iter().copied().filter(|c| !current.contains(c)).collect());
                }
                widgets::menu_button(ui, icon::ADD, t("Groups"), |ui| {
                    for (name, cells) in groups(art) {
                        if widgets::option(ui, false, name).on_hover_text(t("Add these keys to the layer")).clicked() {
                            let mut merged = current.clone();
                            merged.extend(cells);
                            set = Some(merged.into_iter().collect());
                            ui.close_menu();
                        }
                    }
                });
            });
        });
        if let (Some(cells), Some(layer)) = (set, stack.layers.get_mut(stack.current)) {
            layer.cells = cells;
            changed = true;
        }

        // -- the canvas ------------------------------------------------------------------
        stage_frame().show(ui, |ui| {
            let width = ui.available_width();
            let height = (width * art.h as f32 / art.w as f32 + 40.0).clamp(220.0, 420.0);
            let visible = |s: &Shape| matches!(s.region, Region::Cell(..));
            let selected = |s: &Shape| matches!(s.region, Region::Cell(r, c) if current.contains(&(r, c)));
            let picked = canvas::show(app, ui, serial, art, colours, vec2(width, height), View { visible: &visible, selected: &selected, labels: !art.photo });
            let stack = app.lighting.stacks.get_mut(serial).expect("made above");
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
        widgets::help(ui, t("Click keys to add or remove them; drag to add many, Alt+drag to remove. Type here to try reactive layers."));
        widgets::divider(ui);

        // -- on the device? ----------------------------------------------------------------
        // The state on one line, what to do about it on the next: a long
        // translation of either never runs into the other.
        let (dirty, empty) = app.lighting.stacks.get(serial).map(|s| (s.dirty, s.layers.is_empty())).unwrap_or((false, true));
        let (colour, text) = if dirty {
            (theme::WARN, t("Applying…"))
        } else if running {
            (ACCENT, t("On the device: the daemon keeps drawing it with this window closed"))
        } else {
            (TEXT_FAINT, t("Not on the device yet: change anything to apply it"))
        };
        ui.horizontal_top(|ui| {
            let (dot, _) = ui.allocate_exact_size(vec2(8.0, 18.0), Sense::hover());
            ui.painter().circle_filled(dot.center(), 4.0, colour);
            ui.add(egui::Label::new(RichText::new(text).size(12.5).color(TEXT_DIM)).wrap());
        });
        if running {
            turn_off = ui.add(Button::danger(t("Turn per-key lighting off")).icon(icon::POWER).small()).clicked();
        } else if ui.add(Button::primary(t("Apply")).icon(icon::CHECK).small().min_width(110.0).enabled(!empty)).clicked() {
            changed = true;
        }
        if caps["type"] == "keyboard" {
            widgets::divider(ui);
            let mut variant = art.variant.unwrap_or(crate::layout::Variant::Ansi);
            widgets::row(ui, t("Key layout"), t("The shape of Enter and left Shift."), |ui| {
                let options = vec![(crate::layout::Variant::Ansi, canvas::variant_label(crate::layout::Variant::Ansi).to_string()), (crate::layout::Variant::Iso, canvas::variant_label(crate::layout::Variant::Iso).to_string())];
                if widgets::segmented(ui, &options, &mut variant) {
                    app.settings.key_layouts.insert(serial.to_string(), variant);
                    app.save_settings();
                    app.arts.forget(serial);
                }
            });
        }
    });
    if turn_off {
        app.send(Command::Software { serial: serial.to_string(), effect: "none".into(), options: json!({}) });
        let edit = app.edits.entry(serial.to_string()).or_default();
        let mut back = edit.zones.get("backlight").cloned().unwrap_or_default();
        if back.effect.is_empty() || back.effect == "custom" || back.effect == "layers" {
            back.effect = "spectrum".into();
        }
        for command in effects::apply_commands(serial, "backlight", &back, false) {
            app.send(command);
        }
    }
    changed
}

/// Settings of the chosen layer.  True if anything changed.
fn layer_panel(app: &mut App, ui: &mut egui::Ui, serial: &str) -> bool {
    let recent = app.settings.recent_colours.clone();
    let Some(stack) = app.lighting.stacks.get_mut(serial) else { return false };
    let slot_now = stack.slot;
    let Some(layer) = stack.layers.get_mut(stack.current) else {
        widgets::panel(ui, t("Layer"), |ui| widgets::help(ui, t("No layers: the keys are dark. Add one above.")));
        return false;
    };
    let mut changed = false;
    let mut remember: Option<Color32> = None;
    let mut slot = slot_now.min(layer.colours.len().saturating_sub(1));
    widgets::panel(ui, t("Layer"), |ui| {
        ui.spacing_mut().item_spacing.y = 12.0;
        widgets::stacked(ui, t("Name"), |ui| {
            if widgets::text_field(ui, &mut layer.name, layer.kind.label(), ui.available_width()).changed() {
                changed = true;
            }
        });
        let mut kind = layer.kind;
        widgets::stacked(ui, t("Effect"), |ui| {
            widgets::select(ui, ("kind", serial), kind.label(), ui.available_width(), |ui| {
                for option in Kind::ALL {
                    if widgets::option(ui, option == kind, option.label()).on_hover_text(option.hint()).clicked() {
                        kind = option;
                    }
                }
            });
            widgets::help(ui, layer.kind.hint());
            if layer.kind.key_driven() {
                widgets::help(ui, t("Lights up as you type anywhere, not just in this window."));
            }
        });
        if kind != layer.kind {
            let first = layer.colour(0);
            *layer = Layer { kind, cells: std::mem::take(&mut layer.cells), name: std::mem::take(&mut layer.name), ..Layer::new(kind, Vec::new(), first) };
            slot = 0;
            changed = true;
        }
        // Speed and direction are always here, greyed where the effect has none.
        widgets::stacked(ui, t("Speed"), |ui| {
            ui.add_enabled_ui(layer.kind.has_speed(), |ui| {
                let mut speed = layer.speed as f64;
                if widgets::slider_field(ui, ("layer-speed", serial), &mut speed, 0.25..=3.0, 0.25, 2, "×", 76.0) {
                    layer.speed = speed as f32;
                    changed = true;
                }
            });
        });
        widgets::stacked(ui, t("Direction"), |ui| {
            ui.add_enabled_ui(layer.kind.has_direction(), |ui| {
                let options = vec![(1u8, format!("{} →", t("Right"))), (2u8, format!("← {}", t("Left")))];
                if widgets::segmented(ui, &options, &mut layer.direction) {
                    changed = true;
                }
            });
        });
        widgets::divider(ui);
        let most = layer.kind.colours();
        widgets::stacked(ui, if most > 1 { t("Colours") } else { t("Colour") }, |ui| {
            if most == 0 {
                widgets::help(ui, t("This effect picks its own colours."));
                return;
            }
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 10.0;
                let mut remove: Option<usize> = None;
                for (i, colour) in layer.colours.iter().enumerate() {
                    let response = widgets::swatch(ui, *colour, 28.0, slot == i).on_hover_text(t("Click to edit; right-click to remove"));
                    if response.clicked() {
                        slot = i;
                    }
                    if response.secondary_clicked() && layer.colours.len() > 1 {
                        remove = Some(i);
                    }
                }
                if let Some(i) = remove {
                    layer.colours.remove(i);
                    slot = slot.min(layer.colours.len() - 1);
                    changed = true;
                }
                if layer.colours.len() < most && ui.add(IconButton::new(icon::ADD, t("Another colour")).size(28.0)).clicked() {
                    layer.colours.push(preview::hue(layer.colours.len() as f32 * 0.33));
                    slot = layer.colours.len() - 1;
                    changed = true;
                }
            });
            if let Some(colour) = layer.colours.get_mut(slot) {
                if super::widgets::colour_chooser(ui, &format!("layer-{serial}-{slot}"), colour, &recent) {
                    remember = Some(*colour);
                    changed = true;
                }
            }
        });
    });
    stack.slot = slot;
    if let Some(colour) = remember {
        app.settings.remember_colour(colour);
    }
    changed
}
