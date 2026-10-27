//! One device in full: a header with its picture and the profile picker, tabs
//! under it -- performance, lighting, power, profiles, keys, about -- and the
//! settings of the chosen tab in titled panels.

use super::app::{draw_picture, kind_label, scroll, App};
use crate::settings::AppRule;
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, Align, CornerRadius, Frame, Layout, Margin, Rect, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, SURFACE_HI, TEXT, TEXT_DIM};
use openrazer_studio_core::widgets::{self, Button, IconButton};
use openrazer_studio_core::{t, tf};
use serde_json::json;

const DPI_PRESETS: [i64; 8] = [400, 800, 1200, 1600, 3200, 6400, 12800, 25600];
/// Below this width the panels of a tab stack instead of standing side by side.
pub const TWO_COLUMNS: f32 = 820.0;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Tab {
    #[default]
    Performance,
    Lighting,
    Power,
    Profiles,
    Keys,
    About,
}

impl Tab {
    fn label(self) -> &'static str {
        match self {
            Tab::Performance => t("Performance"),
            Tab::Lighting => t("Lighting"),
            Tab::Power => t("Power"),
            Tab::Profiles => t("Profiles"),
            Tab::Keys => t("Keys"),
            Tab::About => t("About"),
        }
    }

    pub fn from_name(name: &str) -> Tab {
        match name {
            "lighting" => Tab::Lighting,
            "power" => Tab::Power,
            "profiles" => Tab::Profiles,
            "keys" => Tab::Keys,
            "about" => Tab::About,
            _ => Tab::Performance,
        }
    }
}

#[derive(Default)]
pub struct DeviceUi {
    pub tab: Tab,
    pub profile_name: String,
    /// The profile an app is being chosen for.
    pub linking: Option<String>,
    pub app_name: String,
    apps: Vec<String>,
}

fn tabs_for(device: &Device, snap: &Snapshot) -> Vec<Tab> {
    let detail = snap.details.get(&device.serial);
    let caps = detail.and_then(|d| d.caps.as_ref());
    let mut tabs = vec![];
    if let Some(caps) = caps {
        if caps["dpi"] == true || caps["poll_rate"] == true {
            tabs.push(Tab::Performance);
        }
        if caps["zones"].as_object().is_some_and(|z| !z.is_empty()) {
            tabs.push(Tab::Lighting);
        }
        if caps["battery"] == true || caps["idle_time"] == true {
            tabs.push(Tab::Power);
        }
    }
    tabs.push(Tab::Profiles);
    if detail.and_then(|d| d.macros.as_ref()).is_some_and(|m| m["keys"].as_array().is_some_and(|k| !k.is_empty())) {
        tabs.push(Tab::Keys);
    }
    tabs.push(Tab::About);
    tabs
}

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    let Some(device) = app.device(snap).cloned() else {
        widgets::page_header(ui, t("Devices"), "", |_| {});
        widgets::plain_panel(ui, |ui| widgets::empty_state(ui, t("No device chosen"), t("Plug one in or switch a wireless one on."), |_| {}));
        return;
    };
    header(app, ui, &device, snap);
    let tabs = tabs_for(&device, snap);
    if !tabs.contains(&app.device_ui.tab) {
        // Ask for Lighting on a device whose zones are still loading, and get it once they are.
        if !(app.device_ui.tab == Tab::Lighting && snap.details.get(&device.serial).and_then(|d| d.caps.as_ref()).is_none()) {
            app.device_ui.tab = tabs[0];
        }
    }
    let options: Vec<(Tab, &str)> = tabs.iter().map(|tab| (*tab, tab.label())).collect();
    widgets::tabs(ui, &options, &mut app.device_ui.tab);
    ui.add_space(theme::GAP);
    if app.device_ui.tab == Tab::Lighting {
        return super::lighting::page(app, ui, snap, &device);
    }
    scroll(ui, "device-tab", |ui| match app.device_ui.tab {
        Tab::Performance => performance(app, ui, &device, snap),
        Tab::Power => power(app, ui, &device, snap),
        Tab::Profiles => profiles(app, ui, &device, snap),
        Tab::Keys => super::macros::device_keys(app, ui, &device, snap),
        Tab::About => about(app, ui, &device, snap),
        Tab::Lighting => {}
    });
    link_app_dialog(app, ui);
}

/// The device's picture, name and state, and the profile picker on the right.
fn header(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let detail = snap.details.get(&device.serial);
    let caps = detail.and_then(|d| d.caps.as_ref());
    let width = ui.available_width();
    // One height for every device, whether it has a battery or badges to show.
    ui.allocate_ui_with_layout(vec2(width, 96.0), Layout::left_to_right(Align::Center), |ui| {
        ui.set_min_size(vec2(width, 96.0));
        ui.spacing_mut().item_spacing.x = theme::GAP;
        let (rect, _) = ui.allocate_exact_size(vec2(132.0, 84.0), Sense::hover());
        ui.painter().add(super::app::stage_frame().paint(rect));
        draw_picture(app, ui, device, rect.shrink(8.0));
        let text_width = (ui.available_width() - 236.0).max(200.0);
        ui.allocate_ui_with_layout(vec2(text_width, 88.0), Layout::top_down(Align::Min), |ui| {
            ui.set_min_height(88.0);
            ui.spacing_mut().item_spacing = vec2(theme::SPACING, 4.0);
            ui.add_space(2.0);
            let name = widgets::galley_fit(ui, &super::app::title(device, snap).to_uppercase(), theme::heavy(22.0), TEXT, text_width);
            let (rect, response) = ui.allocate_exact_size(vec2(text_width, 30.0), Sense::hover());
            ui.painter().galley(egui::pos2(rect.left(), rect.center().y - name.size().y / 2.0), name, TEXT);
            response.on_hover_text(&device.name);
            let bluetooth = caps.is_some_and(|c| c["protocol"] == "razer-ble");
            let mut facts = vec![kind_label(&device.kind).to_string(), if bluetooth { "Bluetooth".into() } else { "USB".into() }, format!("1532:{:04X}", device.pid)];
            if let Some(firmware) = &device.firmware {
                facts.push(tf("Firmware {}", &[firmware]));
            }
            ui.add(egui::Label::new(RichText::new(facts.join("  ·  ")).size(13.0).color(TEXT_DIM)).truncate());
            ui.allocate_ui_with_layout(vec2(text_width, 22.0), Layout::left_to_right(Align::Center), |ui| {
                ui.set_min_height(22.0);
                ui.spacing_mut().item_spacing.x = 8.0;
                if let Some(level) = detail.and_then(|d| d.battery) {
                    super::widgets::battery(ui, level, detail.and_then(|d| d.charging).unwrap_or(false));
                }
                if let Some(effect) = snap.software.get(&device.serial) {
                    widgets::badge(ui, super::effects::label(effect), ACCENT);
                }
                if detail.and_then(|d| d.game_mode) == Some(true) {
                    widgets::badge(ui, t("Game mode"), theme::WARN);
                }
            });
        });
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            ui.allocate_ui_with_layout(vec2(220.0, 60.0), Layout::top_down(Align::Min), |ui| {
                ui.spacing_mut().item_spacing.y = 6.0;
                widgets::section_title(ui, t("Profile"));
                profile_switcher(app, ui, device, snap);
            });
        });
    });
    ui.add_space(4.0);
}

// -- performance ----------------------------------------------------------------------

fn performance(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(caps) = snap.details.get(&device.serial).and_then(|d| d.caps.clone()) else { return };
    let serial = device.serial.clone();
    let max_dpi = caps["max_dpi"].as_i64().unwrap_or(16000).max(800);
    let mut send: Vec<Command> = Vec::new();
    let mut later: Vec<(String, Command)> = Vec::new();
    let edit = app.edits.entry(serial.clone()).or_default();
    let has_dpi = caps["dpi"] == true;
    let has_side = caps["dpi_stages"] == true && edit.stages.is_some() || caps["poll_rate"] == true;

    let two = has_dpi && has_side;
    widgets::columns(ui, if two { 0.56 } else { 1.0 }, if two { TWO_COLUMNS } else { f32::INFINITY }, |ui, column| {
        let side = has_side && if two { column == 1 } else { column == 0 };
        if column == 0 && has_dpi {
            widgets::panel(ui, t("Sensitivity"), |ui| {
                let (x, y) = edit.dpi.unwrap_or((800, 800));
                let (mut fx, mut fy) = (x as f64, y as f64);
                let mut changed = false;
                widgets::row(ui, t("Sensitivity"), t("Dots per inch: the higher, the further the pointer goes."), |ui| {
                    changed |= widgets::slider_field(ui, ("dpi-x", &serial), &mut fx, 100.0..=max_dpi as f64, 50.0, 0, "DPI", 104.0);
                });
                widgets::divider(ui);
                let mut separate = !edit.dpi_linked;
                if widgets::toggle_row(ui, t("Separate X and Y"), t("Set the vertical sensitivity on its own."), &mut separate) {
                    edit.dpi_linked = !separate;
                    if !separate {
                        fy = fx;
                        changed = true;
                    }
                }
                widgets::divider(ui);
                widgets::row(ui, t("Vertical"), "", |ui| {
                    ui.add_enabled_ui(!edit.dpi_linked, |ui| {
                        let mut shown = if edit.dpi_linked { fx } else { fy };
                        if widgets::slider_field(ui, ("dpi-y", &serial), &mut shown, 100.0..=max_dpi as f64, 50.0, 0, "DPI", 104.0) {
                            fy = shown;
                            changed = true;
                        }
                    });
                });
                widgets::divider(ui);
                widgets::row(ui, t("Presets"), "", |ui| {
                    ui.horizontal_wrapped(|ui| {
                        ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
                        for preset in DPI_PRESETS.iter().filter(|p| **p <= max_dpi) {
                            if ui.add(Button::secondary(&preset.to_string()).small().min_width(58.0)).clicked() {
                                fx = *preset as f64;
                                fy = *preset as f64;
                                changed = true;
                            }
                        }
                    });
                });
                if edit.dpi_linked {
                    fy = fx;
                }
                if changed {
                    let (x, y) = (fx.round() as i64, fy.round() as i64);
                    edit.dpi = Some((x, y));
                    later.push((format!("dpi:{serial}"), Command::Call { serial: serial.clone(), method: "set_dpi".into(), args: vec![json!(x), json!(y)], zone: None, then: Then::Nothing }));
                }
            });
        }
        if side {
            if caps["dpi_stages"] == true {
                if let Some((mut active, mut stages)) = edit.stages.clone() {
                    let mut apply = false;
                    let mut add = false;
                    let can_add = stages.len() < 5;
                    widgets::panel_with(
                        ui,
                        t("Sensitivity stages"),
                        |ui| {
                            add = ui.add(Button::ghost(t("Add a stage")).icon(icon::ADD).small().enabled(can_add)).clicked();
                        },
                        |ui| {
                            widgets::help(ui, t("What the DPI button on the mouse steps through. Click a stage to use it now."));
                            ui.add_space(2.0);
                            let mut remove = None;
                            let total = stages.len();
                            ui.spacing_mut().item_spacing.y = 6.0;
                            for (i, stage) in stages.iter_mut().enumerate() {
                                let number = i as i64 + 1;
                                let (row, _) = widgets::list_row(ui, active == number, 44.0, |ui| {
                                    ui.label(RichText::new(tf("Stage {}", &[&number])).font(theme::bold(13.5)).color(TEXT));
                                    if active == number {
                                        widgets::badge(ui, t("In use"), ACCENT);
                                    }
                                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                                        ui.spacing_mut().item_spacing.x = 6.0;
                                        if ui.add(IconButton::new(icon::CLOSE, t("Remove")).danger().enabled(total > 1)).clicked() {
                                            remove = Some(i);
                                        }
                                        let mut v = stage.0 as f64;
                                        if !edit.dpi_linked {
                                            let mut vy = stage.1 as f64;
                                            if widgets::number_field(ui, ("stage-y", &serial, i), &mut vy, 100.0..=max_dpi as f64, 0, "Y", 88.0) {
                                                stage.1 = vy.round() as i64;
                                            }
                                        }
                                        if widgets::number_field(ui, ("stage-x", &serial, i), &mut v, 100.0..=max_dpi as f64, 0, if edit.dpi_linked { "DPI" } else { "X" }, 96.0) {
                                            stage.0 = v.round() as i64;
                                        }
                                        if edit.dpi_linked {
                                            stage.1 = stage.0;
                                        }
                                    });
                                });
                                if row.clicked() {
                                    active = number;
                                    apply = true;
                                }
                            }
                            if let Some(i) = remove {
                                if stages.len() > 1 {
                                    stages.remove(i);
                                    active = active.min(stages.len() as i64);
                                }
                            }
                            ui.add_space(6.0);
                            ui.horizontal(|ui| {
                                if ui.add(Button::primary(t("Apply stages"))).clicked() {
                                    apply = true;
                                }
                                ui.label(widgets::faint(t("Sent to the mouse, which keeps them.")));
                            });
                        },
                    );
                    if add && stages.len() < 5 {
                        let last = stages.last().copied().unwrap_or((800, 800));
                        stages.push(((last.0 * 2).min(max_dpi), (last.1 * 2).min(max_dpi)));
                    }
                    if apply {
                        let list: Vec<_> = stages.iter().map(|(x, y)| json!([x, y])).collect();
                        send.push(Command::Call { serial: serial.clone(), method: "set_dpi_stages".into(), args: vec![json!(active), json!(list)], zone: None, then: Then::Performance });
                    }
                    edit.stages = Some((active, stages));
                }
            }
            if caps["poll_rate"] == true {
                widgets::panel(ui, t("Polling rate"), |ui| {
                    let rates: Vec<(i64, String)> = caps["supported_poll_rates"].as_array().map(|r| r.iter().filter_map(|v| v.as_i64()).map(|v| (v, format!("{v} Hz"))).collect()).unwrap_or_default();
                    let mut rate = edit.poll_rate.unwrap_or(0);
                    widgets::row(ui, t("Polling rate"), t("How often the mouse reports to the PC. Higher is smoother; on a wireless mouse it costs battery."), |ui| {
                        if widgets::segmented(ui, &rates, &mut rate) {
                            edit.poll_rate = Some(rate);
                            send.push(Command::Call { serial: serial.clone(), method: "set_poll_rate".into(), args: vec![json!(rate)], zone: None, then: Then::Performance });
                        }
                    });
                });
            }
        }
    });

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

// -- power ------------------------------------------------------------------------------

fn power(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(detail) = snap.details.get(&device.serial) else { return };
    let Some(caps) = detail.caps.clone() else { return };
    let serial = device.serial.clone();
    let mut later: Vec<(String, Command)> = Vec::new();
    let edit = app.edits.entry(serial.clone()).or_default();
    let has_battery = caps["battery"] == true;
    let has_saving = caps["idle_time"] == true || has_battery && detail.low_battery.is_some();

    let two = has_battery && has_saving;
    widgets::columns(ui, if two { 0.42 } else { 1.0 }, if two { TWO_COLUMNS } else { f32::INFINITY }, |ui, column| {
        let saving_here = has_saving && if two { column == 1 } else { column == 0 };
        if column == 0 && has_battery {
            widgets::panel(ui, t("Battery"), |ui| match detail.battery {
                Some(level) => {
                    let charging = detail.charging.unwrap_or(false);
                    let colour = if level < 20.0 { theme::DANGER } else { ACCENT };
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(format!("{level:.0}%")).font(theme::heavy(40.0)).color(colour));
                        ui.vertical(|ui| {
                            ui.add_space(12.0);
                            ui.label(RichText::new(if charging { t("charging") } else { t("on battery") }).color(TEXT_DIM));
                        });
                    });
                    let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width(), 8.0), Sense::hover());
                    ui.painter().rect_filled(rect, CornerRadius::same(4), SURFACE_HI);
                    ui.painter().rect_filled(Rect::from_min_size(rect.min, vec2(rect.width() * (level / 100.0).clamp(0.0, 1.0) as f32, rect.height())), CornerRadius::same(4), colour);
                }
                None => {
                    ui.label(widgets::dim(t("Not reported yet.")));
                }
            });
        }
        if saving_here {
            widgets::panel(ui, t("Power saving"), |ui| {
                let mut first = true;
                if caps["idle_time"] == true {
                    first = false;
                    let mut minutes = edit.idle_minutes.unwrap_or(5.0) as f64;
                    widgets::row(ui, t("Turn off after"), t("Minutes without use before the device sleeps."), |ui| {
                        if widgets::slider_field(ui, ("idle", &serial), &mut minutes, 1.0..=15.0, 0.5, 1, t("min"), 88.0) {
                            edit.idle_minutes = Some(minutes as f32);
                            later.push((format!("idle:{serial}"), Command::Call { serial: serial.clone(), method: "set_idle_time".into(), args: vec![json!((minutes * 60.0).round() as i64)], zone: None, then: Then::Power }));
                        }
                    });
                }
                if has_battery && detail.low_battery.is_some() {
                    if !first {
                        widgets::divider(ui);
                    }
                    let mut threshold = edit.low_battery.unwrap_or(10.0) as f64;
                    widgets::row(ui, t("Low battery warning"), t("The device warns you when the battery falls below this."), |ui| {
                        if widgets::slider_field(ui, ("low", &serial), &mut threshold, 5.0..=100.0, 1.0, 0, "%", 88.0) {
                            edit.low_battery = Some(threshold as f32);
                            later.push((format!("low:{serial}"), Command::Call { serial: serial.clone(), method: "set_low_battery_threshold".into(), args: vec![json!(threshold.round())], zone: None, then: Then::Nothing }));
                        }
                    });
                }
            });
        }
    });
    if !later.is_empty() {
        edit.touch();
    }
    for (key, command) in later {
        app.debounce(key, command, 250);
    }
}

// -- profiles -------------------------------------------------------------------------------

/// The profile picker in the device's header. It applies a saved profile;
/// with none saved it says so, at the same size.
pub fn profile_switcher(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let profiles = snap.details.get(&device.serial).map(|d| d.profiles.clone()).unwrap_or_default();
    let names: Vec<String> = profiles.iter().filter_map(|p| p["name"].as_str().map(str::to_string)).collect();
    let mut chosen: Option<String> = None;
    let width = ui.available_width();
    ui.add_enabled_ui(!names.is_empty(), |ui| {
        let shown = if names.is_empty() { t("No profiles saved") } else { t("Apply a profile…") };
        widgets::select(ui, ("profile", &device.serial), RichText::new(shown).color(TEXT_DIM), width, |ui| {
            for name in &names {
                if widgets::option(ui, false, name).clicked() {
                    chosen = Some(name.clone());
                }
            }
        });
    });
    if let Some(name) = chosen {
        app.send(Command::Profile { action: "load", serial: device.serial.clone(), name, quiet: false });
    }
}

fn profiles(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let serial = device.serial.clone();
    let profiles = snap.details.get(&serial).map(|d| d.profiles.clone()).unwrap_or_default();

    widgets::columns(ui, 0.4, TWO_COLUMNS, |ui, column| {
        if column == 0 {
            widgets::panel(ui, t("Save what is showing now"), |ui| {
                widgets::help(ui, t("Every zone's effect, colours and brightness, and per-key Studio lighting. Up to ten per device."));
                ui.add_space(4.0);
                ui.horizontal(|ui| {
                    let name = app.device_ui.profile_name.trim().to_string();
                    let save_width = 136.0;
                    let field = widgets::text_field(ui, &mut app.device_ui.profile_name, t("Profile name, e.g. Night"), ui.available_width() - save_width - theme::SPACING);
                    let enter = field.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                    let save = ui.add(Button::primary(t("Save")).icon(icon::SAVE).width(save_width).enabled(!name.is_empty()));
                    if (save.clicked() || enter) && !name.is_empty() {
                        app.send(Command::Profile { action: "save", serial: serial.clone(), name, quiet: false });
                        app.device_ui.profile_name.clear();
                    }
                });
            });
            widgets::panel(ui, t("Switching by app"), |ui| {
                let mut auto = app.settings.auto_switch;
                if widgets::toggle_row(ui, t("Switch profiles by app"), t("Link a profile to a game or app, and it is applied whenever that app is in front."), &mut auto) {
                    app.settings.auto_switch = auto;
                    app.save_settings();
                }
            });
            return;
        }
        let count = profiles.len().to_string();
        widgets::panel_with(
            ui,
            t("Saved profiles"),
            |ui| {
                widgets::chip(ui, &count);
            },
            |ui| {
                if profiles.is_empty() {
                    widgets::empty_state(ui, t("None yet."), t("Save what the device shows now, and come back to it in one click."), |_| {});
                }
                for profile in &profiles {
                    profile_row(app, ui, &serial, profile);
                }
            },
        );
    });
}

fn profile_row(app: &mut App, ui: &mut egui::Ui, serial: &str, profile: &serde_json::Value) {
    let name = profile["name"].as_str().unwrap_or_default().to_string();
    let saved = profile["saved"].as_str().unwrap_or_default();
    let is_default = app.settings.default_profiles.get(serial) == Some(&name);
    Frame::new().fill(SURFACE_HI).stroke(Stroke::new(1.0_f32, BORDER)).corner_radius(CornerRadius::same(theme::RADIUS_SM)).inner_margin(Margin::symmetric(14, 10)).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.spacing_mut().item_spacing.y = 6.0;
        ui.horizontal(|ui| {
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 2.0;
                ui.horizontal(|ui| {
                    ui.label(RichText::new(&name).font(theme::bold(14.5)).color(TEXT));
                    if is_default {
                        widgets::badge(ui, t("Default"), ACCENT);
                    }
                });
                let when = saved.split('.').next().unwrap_or(saved).replace('T', " ");
                ui.label(widgets::faint(tf("saved {}", &[&when])));
            });
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.spacing_mut().item_spacing.x = 6.0;
                if ui.add(IconButton::new(icon::DELETE, t("Delete")).danger().size(32.0)).clicked() {
                    app.send(Command::Profile { action: "delete", serial: serial.to_string(), name: name.clone(), quiet: false });
                    app.settings.app_rules.retain(|r| !(r.serial == serial && r.profile == name));
                    if is_default {
                        app.settings.default_profiles.remove(serial);
                    }
                    app.save_settings();
                }
                let star = if is_default { icon::STAR_FILLED } else { icon::STAR };
                let hint = if is_default { t("Not the default") } else { t("Make default: the profile to go back to when no linked app is in front") };
                if ui.add(IconButton::new(star, hint).on(is_default).size(32.0)).clicked() {
                    if is_default {
                        app.settings.default_profiles.remove(serial);
                    } else {
                        app.settings.default_profiles.insert(serial.to_string(), name.clone());
                    }
                    app.save_settings();
                }
                if ui.add(Button::secondary(t("Apply")).icon(icon::CHECK).min_width(110.0)).clicked() {
                    app.send(Command::Profile { action: "load", serial: serial.to_string(), name: name.clone(), quiet: false });
                }
            });
        });
        // The apps that switch to it.
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
            let linked: Vec<String> = app.settings.app_rules.iter().filter(|r| r.serial == serial && r.profile == name).map(|r| r.exe.clone()).collect();
            ui.label(widgets::faint(if linked.is_empty() { t("No app linked") } else { t("Linked apps:") }));
            for exe in &linked {
                Frame::new().fill(theme::WELL).stroke(Stroke::new(1.0_f32, BORDER)).corner_radius(CornerRadius::same(4)).inner_margin(Margin { left: 8, right: 2, top: 1, bottom: 1 }).show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 4.0;
                        widgets::icon(ui, icon::APP, 11.0, TEXT_DIM);
                        ui.label(RichText::new(exe).size(12.5).color(TEXT));
                        if ui.add(IconButton::new(icon::CLOSE, t("Unlink")).size(20.0)).clicked() {
                            app.settings.app_rules.retain(|r| !(r.serial == serial && r.profile == name && &r.exe == exe));
                            app.save_settings();
                        }
                    });
                });
            }
            if ui.add(Button::ghost(t("Link an app")).icon(icon::LINK).small()).clicked() {
                app.device_ui.linking = Some(name.clone());
                app.device_ui.apps = openrazer_studio_core::process::windowed_apps();
                app.device_ui.app_name.clear();
            }
        });
    });
}

/// Choosing the app a profile follows -- one on screen now, a typed name, or
/// a file -- in a dialog over the page.
fn link_app_dialog(app: &mut App, ui: &mut egui::Ui) {
    let Some(profile) = app.device_ui.linking.clone() else { return };
    let Some(serial) = app.selected.clone() else { return };
    let mut chosen: Option<String> = None;
    let mut cancel = false;
    let frame = Frame::new().fill(theme::SURFACE).stroke(Stroke::new(1.0_f32, theme::BORDER_HI)).corner_radius(CornerRadius::same(theme::RADIUS)).shadow(theme::shadow()).inner_margin(Margin::same(20));
    let modal = egui::Modal::new(egui::Id::new("link-app")).frame(frame).show(ui.ctx(), |ui| {
        ui.set_width(500.0);
        ui.spacing_mut().item_spacing.y = 10.0;
        ui.label(RichText::new(tf("Link an app to “{}”", &[&profile])).font(theme::bold(17.0)).color(TEXT));
        widgets::help(ui, t("Whenever this app is in front, the profile is applied."));
        widgets::section_title(ui, t("Apps open now"));
        egui::ScrollArea::vertical().max_height(150.0).show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
                let apps: Vec<String> = app.device_ui.apps.iter().filter(|exe| exe.as_str() != openrazer_studio_core::install::APP_EXE && exe.as_str() != "explorer.exe").cloned().collect();
                if apps.is_empty() {
                    ui.label(widgets::faint(t("None found")));
                }
                for exe in apps {
                    if ui.add(Button::secondary(&exe).icon(icon::APP).small()).clicked() {
                        chosen = Some(exe);
                    }
                }
            });
        });
        widgets::section_title(ui, t("Or by its file name"));
        ui.horizontal(|ui| {
            widgets::text_field(ui, &mut app.device_ui.app_name, "game.exe", ui.available_width() - 200.0);
            let name = app.device_ui.app_name.trim().to_lowercase();
            if ui.add(Button::secondary(t("Add")).width(84.0).enabled(!name.is_empty())).clicked() {
                chosen = Some(name);
            }
            if ui.add(Button::secondary(t("Browse")).icon(icon::FOLDER).width(100.0)).clicked() {
                if let Some(path) = rfd::FileDialog::new().add_filter(t("Programs"), &["exe"]).set_title(t("Which program switches to this profile?")).pick_file() {
                    chosen = path.file_name().map(|n| n.to_string_lossy().to_lowercase());
                }
            }
        });
        ui.add_space(4.0);
        widgets::divider(ui);
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            if ui.add(Button::secondary(t("Cancel")).min_width(96.0)).clicked() {
                cancel = true;
            }
        });
    });
    if modal.should_close() {
        cancel = true;
    }
    if let Some(exe) = chosen {
        // One profile per app and device: a new link replaces an old one.
        app.settings.app_rules.retain(|r| !(r.serial == serial && r.exe.eq_ignore_ascii_case(&exe)));
        app.settings.app_rules.push(AppRule { exe, serial: serial.clone(), profile });
        app.device_ui.linking = None;
        app.save_settings();
    } else if cancel {
        app.device_ui.linking = None;
    }
}

// -- about --------------------------------------------------------------------------------------

fn about(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let caps = snap.details.get(&device.serial).and_then(|d| d.caps.clone()).unwrap_or_default();
    widgets::columns(ui, 0.55, TWO_COLUMNS, |ui, column| {
        if column == 0 {
            widgets::panel(ui, t("Details"), |ui| {
                if device.images.len() > 1 {
                    // Nothing the device reports tells its colours apart.
                    let mut colour = app.settings.colours.get(&device.serial).cloned().unwrap_or_else(|| {
                        let default = device.image.as_ref();
                        device.images.iter().find(|(_, url)| Some(url) == default).map_or_else(|| device.images[0].0.clone(), |(c, _)| c.clone())
                    });
                    let mut chosen = None;
                    widgets::row(ui, t("Colour"), t("Which picture to show: the device cannot tell."), |ui| {
                        widgets::select(ui, ("device-colour", &device.serial), colour.clone(), 200.0, |ui| {
                            for (name, _) in &device.images {
                                if widgets::option(ui, *name == colour, name).clicked() {
                                    chosen = Some(name.clone());
                                }
                            }
                        });
                    });
                    if let Some(name) = chosen {
                        colour = name;
                        app.settings.colours.insert(device.serial.clone(), colour);
                        app.save_settings();
                    }
                    widgets::divider(ui);
                }
                widgets::fact(ui, t("Name"), &device.name);
                widgets::fact(ui, t("Type"), kind_label(&device.kind));
                widgets::fact(ui, t("USB id"), &format!("1532:{:04X}", device.pid));
                widgets::fact(ui, t("Serial"), &device.serial);
                widgets::fact(ui, t("Firmware"), device.firmware.as_deref().unwrap_or("—"));
                if let Some(dims) = caps["matrix_dimensions"].as_array().filter(|d| d.len() == 2) {
                    widgets::fact(ui, t("LED matrix"), &format!("{} × {}", dims[0], dims[1]));
                }
                if caps["transport"]["path"].as_str().is_some() {
                    widgets::fact(ui, t("Connected through"), if caps["protocol"] == "razer-ble" { "Bluetooth LE" } else { "USB" });
                }
                if let Some(address) = caps["bluetooth_address"].as_str() {
                    // As Windows shows it in Bluetooth settings: AA:BB:CC:DD:EE:FF.
                    let pairs: Vec<&str> = (0..address.len()).step_by(2).filter_map(|i| address.get(i..i + 2)).collect();
                    widgets::fact(ui, t("Bluetooth address"), &pairs.join(":"));
                }
            });
            return;
        }
        widgets::panel(ui, t("What it can do"), |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
                let flags = [
                    ("dpi", t("DPI")),
                    ("dpi_stages", t("DPI stages")),
                    ("poll_rate", t("Polling rate")),
                    ("battery", t("Battery")),
                    ("idle_time", t("Sleep timer")),
                    ("game_mode", t("Game mode")),
                    ("dedicated_macro_keys", t("Macro keys")),
                    ("custom_frame", t("Per-key lighting")),
                    ("software_effects", t("Live effects")),
                    ("scroll_mode", t("Scroll modes")),
                ];
                for (key, name) in flags {
                    if caps[key] == true {
                        widgets::chip(ui, name);
                    }
                }
            });
            if let Some(zones) = caps["zones"].as_object() {
                ui.add_space(6.0);
                widgets::section_title(ui, t("Lighting zones"));
                for (zone, info) in zones {
                    let count = info["effects"].as_array().map_or(0, Vec::len);
                    widgets::fact(ui, super::effects::zone_label(zone, &device.kind), &tf("{} effects", &[&count]));
                }
            }
        });
    });
}
