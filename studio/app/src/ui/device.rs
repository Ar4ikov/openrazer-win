//! One device in full: performance, power, profiles, its keys, and about it.

use super::app::{draw_picture, kind_label, short_name, App, Page};
use crate::settings::AppRule;
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, CornerRadius, Frame, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::{t, tf, widgets};
use serde_json::json;

const DPI_PRESETS: [i64; 8] = [400, 800, 1200, 1600, 3200, 6400, 12800, 25600];

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Tab {
    #[default]
    Performance,
    Power,
    Profiles,
    Keys,
    About,
}

impl Tab {
    fn label(self) -> &'static str {
        match self {
            Tab::Performance => t("Performance"),
            Tab::Power => t("Power"),
            Tab::Profiles => t("Profiles"),
            Tab::Keys => t("Keys"),
            Tab::About => t("About"),
        }
    }

    pub fn from_name(name: &str) -> Tab {
        match name {
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
        return super::app::empty_state(ui, t("No device chosen"), "", None::<fn(&mut egui::Ui)>);
    };
    header(app, ui, &device, snap);
    ui.add_space(8.0);
    let tabs = tabs_for(&device, snap);
    if !tabs.contains(&app.device_ui.tab) {
        app.device_ui.tab = tabs[0];
    }
    tab_bar(ui, &tabs, &mut app.device_ui.tab);
    ui.add_space(14.0);
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        ui.set_max_width(ui.available_width().min(960.0));
        match app.device_ui.tab {
            Tab::Performance => performance(app, ui, &device, snap),
            Tab::Power => power(app, ui, &device, snap),
            Tab::Profiles => profiles(app, ui, &device, snap),
            Tab::Keys => keys(app, ui, &device, snap),
            Tab::About => about(app, ui, &device, snap),
        }
        ui.add_space(24.0);
    });
}

fn header(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let detail = snap.details.get(&device.serial);
    ui.horizontal(|ui| {
        let (rect, _) = ui.allocate_exact_size(vec2(150.0, 104.0), Sense::hover());
        draw_picture(app, ui, device, rect);
        ui.add_space(10.0);
        ui.vertical(|ui| {
            ui.add_space(10.0);
            ui.label(RichText::new(short_name(&device.name).to_uppercase()).font(theme::heavy(24.0)).extra_letter_spacing(1.2));
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing.x = 6.0;
                widgets::chip(ui, kind_label(&device.kind));
                widgets::chip(ui, &format!("1532:{:04X}", device.pid));
                if let Some(firmware) = &device.firmware {
                    widgets::chip(ui, &tf("Firmware {}", &[firmware]));
                }
                if let Some(level) = detail.and_then(|d| d.battery) {
                    super::widgets::battery(ui, level, detail.and_then(|d| d.charging).unwrap_or(false));
                }
            });
        });
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            if widgets::primary_icon_button(ui, icon::LIGHTING, t("Lighting")).clicked() {
                app.page = Page::Lighting;
            }
        });
    });
}

pub fn tab_bar<T: Copy + PartialEq + TabLabel>(ui: &mut egui::Ui, tabs: &[T], current: &mut T) {
    let (bar, _) = ui.allocate_exact_size(vec2(ui.available_width(), 0.0), Sense::hover());
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 26.0;
        for tab in tabs {
            let selected = tab == current;
            let text = RichText::new(tab.label().to_uppercase()).font(theme::bold(13.0)).extra_letter_spacing(1.0).color(if selected { TEXT } else { TEXT_FAINT });
            let response = ui.add(egui::Label::new(text).sense(Sense::click()));
            if selected {
                let r = response.rect;
                ui.painter().rect_filled(egui::Rect::from_min_size(egui::pos2(r.left(), r.bottom() + 7.0), vec2(r.width(), 2.5)), CornerRadius::same(1), ACCENT);
            }
            if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                *current = *tab;
            }
        }
    });
    let y = ui.cursor().top() + 6.0;
    ui.painter().line_segment([egui::pos2(bar.left(), y), egui::pos2(bar.left() + ui.available_width(), y)], Stroke::new(1.0_f32, BORDER));
    ui.add_space(6.0);
}

pub trait TabLabel {
    fn label(&self) -> &'static str;
}

impl TabLabel for Tab {
    fn label(&self) -> &'static str {
        Tab::label(*self)
    }
}

// -- performance ----------------------------------------------------------------------

fn performance(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(caps) = snap.details.get(&device.serial).and_then(|d| d.caps.clone()) else { return };
    let serial = device.serial.clone();
    let max_dpi = caps["max_dpi"].as_i64().unwrap_or(16000).max(800);
    let mut send: Vec<Command> = Vec::new();
    let mut later: Vec<(String, Command)> = Vec::new();
    let edit = app.edits.entry(serial.clone()).or_default();

    if caps["dpi"] == true {
        widgets::card(ui, |ui| {
            ui.horizontal(|ui| {
                widgets::heading(ui, t("Sensitivity"));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let mut separate = !edit.dpi_linked;
                    if widgets::toggle(ui, &mut separate).changed() {
                        edit.dpi_linked = !separate;
                    }
                    ui.label(widgets::dim(t("Separate X and Y")));
                });
            });
            let (mut x, mut y) = edit.dpi.unwrap_or((800, 800));
            ui.horizontal(|ui| {
                ui.label(RichText::new(format!("{x}")).font(theme::heavy(40.0)).color(ACCENT));
                if !edit.dpi_linked && y != x {
                    ui.label(RichText::new(format!("× {y}")).font(theme::bold(26.0)).color(TEXT_DIM));
                }
                ui.label(RichText::new("DPI").size(15.0).color(TEXT_DIM));
            });
            let mut changed = false;
            ui.spacing_mut().slider_width = (ui.available_width() - 20.0).max(200.0);
            if edit.dpi_linked {
                changed |= ui.add(egui::Slider::new(&mut x, 100..=max_dpi).step_by(50.0).show_value(false)).changed();
                y = x;
            } else {
                ui.label(widgets::dim(t("Horizontal")));
                changed |= ui.add(egui::Slider::new(&mut x, 100..=max_dpi).step_by(50.0).show_value(false)).changed();
                ui.label(widgets::dim(t("Vertical")));
                changed |= ui.add(egui::Slider::new(&mut y, 100..=max_dpi).step_by(50.0).show_value(false)).changed();
            }
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing.x = 5.0;
                for preset in DPI_PRESETS.iter().filter(|p| **p <= max_dpi) {
                    let selected = x == *preset && y == *preset;
                    let text = RichText::new(preset.to_string()).size(12.5);
                    if ui.add(egui::Button::new(if selected { text.color(theme::ON_ACCENT).strong() } else { text }).fill(if selected { ACCENT } else { theme::SURFACE_HI })).clicked() {
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
            widgets::panel(ui, t("Sensitivity stages"), |ui| {
                ui.label(widgets::dim(t("What the DPI button on the mouse steps through. The highlighted stage is the one in use.")));
                ui.add_space(4.0);
                let mut remove = None;
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing = vec2(10.0, 10.0);
                    for (i, stage) in stages.iter_mut().enumerate() {
                        let number = i as i64 + 1;
                        let selected = active == number;
                        Frame::new()
                            .fill(if selected { theme::SURFACE_ACTIVE } else { theme::SURFACE_HI })
                            .stroke(Stroke::new(1.0_f32, if selected { ACCENT } else { BORDER }))
                            .corner_radius(CornerRadius::same(theme::RADIUS))
                            .inner_margin(Margin::symmetric(12, 10))
                            .show(ui, |ui| {
                                ui.vertical(|ui| {
                                    ui.horizontal(|ui| {
                                        if ui.add(egui::Label::new(RichText::new(tf("STAGE {}", &[&number])).font(theme::bold(11.0)).color(if selected { ACCENT } else { TEXT_FAINT })).sense(Sense::click())).clicked() {
                                            active = number;
                                        }
                                        if widgets::ghost(ui, icon::CLOSE, t("Remove")).clicked() {
                                            remove = Some(i);
                                        }
                                    });
                                    ui.add(egui::DragValue::new(&mut stage.0).range(100..=max_dpi).speed(25.0).suffix(" DPI"));
                                    if !edit.dpi_linked {
                                        ui.add(egui::DragValue::new(&mut stage.1).range(100..=max_dpi).speed(25.0).suffix(" Y"));
                                    } else {
                                        stage.1 = stage.0;
                                    }
                                });
                            });
                    }
                });
                if let Some(i) = remove {
                    if stages.len() > 1 {
                        stages.remove(i);
                        active = active.min(stages.len() as i64);
                    }
                }
                ui.horizontal(|ui| {
                    if stages.len() < 5 && widgets::icon_button(ui, icon::ADD, t("Add a stage")).clicked() {
                        let last = stages.last().copied().unwrap_or((800, 800));
                        stages.push(((last.0 * 2).min(max_dpi), (last.1 * 2).min(max_dpi)));
                    }
                    if widgets::primary_button(ui, t("Apply stages")).clicked() {
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
        widgets::panel(ui, t("Polling rate"), |ui| {
            let rates: Vec<(i64, String)> = caps["supported_poll_rates"].as_array().map(|r| r.iter().filter_map(|v| v.as_i64()).map(|v| (v, format!("{v} Hz"))).collect()).unwrap_or_default();
            let mut rate = edit.poll_rate.unwrap_or(0);
            if widgets::segmented(ui, &rates, &mut rate) {
                edit.poll_rate = Some(rate);
                send.push(Command::Call { serial: serial.clone(), method: "set_poll_rate".into(), args: vec![json!(rate)], zone: None, then: Then::Performance });
            }
            ui.label(widgets::faint(t("How often the mouse reports to the PC. Higher is smoother; on a wireless mouse it costs battery.")));
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

// -- power ------------------------------------------------------------------------------

fn power(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(detail) = snap.details.get(&device.serial) else { return };
    let Some(caps) = detail.caps.clone() else { return };
    let serial = device.serial.clone();
    let mut later: Vec<(String, Command)> = Vec::new();
    let edit = app.edits.entry(serial.clone()).or_default();

    if caps["battery"] == true {
        widgets::panel(ui, t("Battery"), |ui| match detail.battery {
            Some(level) => {
                let charging = detail.charging.unwrap_or(false);
                ui.horizontal(|ui| {
                    ui.label(RichText::new(format!("{level:.0}%")).font(theme::heavy(40.0)).color(if level < 20.0 { theme::DANGER } else { ACCENT }));
                    ui.label(widgets::dim(if charging { t("charging") } else { t("on battery") }));
                });
                ui.add(egui::ProgressBar::new((level / 100.0) as f32).desired_height(6.0).fill(if level < 20.0 { theme::DANGER } else { ACCENT }));
            }
            None => {
                ui.label(widgets::dim(t("Not reported yet.")));
            }
        });
    }
    if caps["idle_time"] == true {
        ui.add_space(12.0);
        widgets::panel(ui, t("Sleep"), |ui| {
            let mut minutes = edit.idle_minutes.unwrap_or(5.0);
            ui.horizontal(|ui| {
                ui.label(widgets::dim(t("Turn off after")));
                let response = ui.add(egui::Slider::new(&mut minutes, 1.0..=15.0).step_by(0.5).show_value(false));
                ui.label(RichText::new(tf("{} min", &[&format!("{minutes:.1}")])).font(theme::bold(14.0)));
                if response.changed() {
                    edit.idle_minutes = Some(minutes);
                    later.push((format!("idle:{serial}"), Command::Call { serial: serial.clone(), method: "set_idle_time".into(), args: vec![json!((minutes * 60.0) as i64)], zone: None, then: Then::Power }));
                }
            });
        });
    }
    if caps["battery"] == true && detail.low_battery.is_some() {
        ui.add_space(12.0);
        widgets::panel(ui, t("Low battery warning"), |ui| {
            let mut threshold = edit.low_battery.unwrap_or(10.0);
            ui.horizontal(|ui| {
                ui.label(widgets::dim(t("Warn below")));
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

// -- profiles -------------------------------------------------------------------------------

/// The profile picker in the bar along the top.
pub fn profile_switcher(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let profiles = snap.details.get(&device.serial).map(|d| d.profiles.clone()).unwrap_or_default();
    if profiles.is_empty() {
        return;
    }
    let names: Vec<String> = profiles.iter().filter_map(|p| p["name"].as_str().map(str::to_string)).collect();
    let mut chosen: Option<String> = None;
    egui::ComboBox::from_id_salt(("profile", &device.serial)).selected_text(t("Apply a profile")).width(170.0).show_ui(ui, |ui| {
        for name in &names {
            if ui.selectable_label(false, name).clicked() {
                chosen = Some(name.clone());
            }
        }
    });
    widgets::icon(ui, icon::PROFILE, 14.0, TEXT_FAINT);
    if let Some(name) = chosen {
        app.send(Command::Profile { action: "load", serial: device.serial.clone(), name, quiet: false });
    }
}

fn profiles(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let serial = device.serial.clone();
    let profiles = snap.details.get(&serial).map(|d| d.profiles.clone()).unwrap_or_default();

    widgets::panel(ui, t("Save what is showing now"), |ui| {
        ui.label(widgets::dim(t("Every zone's effect, colours and brightness, and per-key Studio lighting. Up to ten per device.")));
        ui.horizontal(|ui| {
            let input = ui.add(egui::TextEdit::singleline(&mut app.device_ui.profile_name).hint_text(t("Profile name, e.g. Night")).desired_width(260.0));
            let enter = input.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
            let name = app.device_ui.profile_name.trim().to_string();
            let save = ui.add_enabled(!name.is_empty(), egui::Button::new(RichText::new(t("Save")).strong()));
            if (save.clicked() || enter) && !name.is_empty() {
                app.send(Command::Profile { action: "save", serial: serial.clone(), name, quiet: false });
                app.device_ui.profile_name.clear();
            }
        });
    });
    ui.add_space(12.0);

    let mut auto = app.settings.auto_switch;
    widgets::panel(ui, t("Saved profiles"), |ui| {
        if widgets::toggle_row(ui, t("Switch profiles by app"), t("Link a profile to a game or app, and it is applied whenever that app is in front."), &mut auto) {
            app.settings.auto_switch = auto;
            app.save_settings();
        }
        ui.add_space(6.0);
        if profiles.is_empty() {
            ui.label(widgets::dim(t("None yet.")));
        }
        for profile in &profiles {
            let name = profile["name"].as_str().unwrap_or_default().to_string();
            let saved = profile["saved"].as_str().unwrap_or_default();
            let is_default = app.settings.default_profiles.get(&serial) == Some(&name);
            Frame::new().fill(theme::SURFACE_HI).corner_radius(CornerRadius::same(theme::RADIUS)).inner_margin(Margin::symmetric(14, 10)).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    ui.vertical(|ui| {
                        ui.spacing_mut().item_spacing.y = 2.0;
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(&name).font(theme::bold(14.5)));
                            if is_default {
                                widgets::badge(ui, t("Default"), ACCENT);
                            }
                        });
                        let when = saved.split('.').next().unwrap_or(saved).replace('T', " ");
                        ui.label(widgets::faint(tf("saved {}", &[&when])));
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if widgets::ghost(ui, icon::DELETE, t("Delete")).clicked() {
                            app.send(Command::Profile { action: "delete", serial: serial.clone(), name: name.clone(), quiet: false });
                            app.settings.app_rules.retain(|r| !(r.serial == serial && r.profile == name));
                            if is_default {
                                app.settings.default_profiles.remove(&serial);
                            }
                            app.save_settings();
                        }
                        if widgets::primary_button(ui, t("Apply")).clicked() {
                            app.send(Command::Profile { action: "load", serial: serial.clone(), name: name.clone(), quiet: false });
                        }
                        let star = if is_default { t("Not the default") } else { t("Make default") };
                        if ui.button(star).on_hover_text(t("The profile to go back to when no linked app is in front")).clicked() {
                            if is_default {
                                app.settings.default_profiles.remove(&serial);
                            } else {
                                app.settings.default_profiles.insert(serial.clone(), name.clone());
                            }
                            app.save_settings();
                        }
                    });
                });
                // The apps that switch to it.
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing.x = 6.0;
                    let linked: Vec<String> = app.settings.app_rules.iter().filter(|r| r.serial == serial && r.profile == name).map(|r| r.exe.clone()).collect();
                    for exe in &linked {
                        Frame::new().fill(theme::WELL).corner_radius(CornerRadius::same(4)).inner_margin(Margin::symmetric(8, 3)).show(ui, |ui| {
                            ui.horizontal(|ui| {
                                widgets::icon(ui, icon::APP, 11.0, TEXT_DIM);
                                ui.label(RichText::new(exe).size(12.0));
                                if ui.add(egui::Button::new(RichText::new(icon::CLOSE).font(theme::icon_font(9.0)).color(TEXT_FAINT)).frame(false)).clicked() {
                                    app.settings.app_rules.retain(|r| !(r.serial == serial && r.profile == name && &r.exe == exe));
                                    app.save_settings();
                                }
                            });
                        });
                    }
                    let linking = app.device_ui.linking.as_deref() == Some(name.as_str());
                    if !linking && ui.add(egui::Button::new(RichText::new(format!("+ {}", t("Link an app"))).size(12.0).color(TEXT_DIM)).frame(false)).clicked() {
                        app.device_ui.linking = Some(name.clone());
                        app.device_ui.apps = openrazer_studio_core::process::windowed_apps();
                        app.device_ui.app_name.clear();
                    }
                });
                if app.device_ui.linking.as_deref() == Some(name.as_str()) {
                    link_app(app, ui, &serial, &name);
                }
            });
            ui.add_space(4.0);
        }
    });
}

/// Choosing the app a profile follows: one on screen now, a typed name, or a file.
fn link_app(app: &mut App, ui: &mut egui::Ui, serial: &str, profile: &str) {
    let mut chosen: Option<String> = None;
    ui.add_space(4.0);
    ui.label(widgets::faint(t("Apps open now:")));
    ui.horizontal_wrapped(|ui| {
        ui.spacing_mut().item_spacing = vec2(5.0, 5.0);
        for exe in app.device_ui.apps.clone() {
            if exe == openrazer_studio_core::install::APP_EXE || exe == "explorer.exe" {
                continue;
            }
            if ui.add(egui::Button::new(RichText::new(&exe).size(12.0))).clicked() {
                chosen = Some(exe);
            }
        }
    });
    ui.horizontal(|ui| {
        ui.add(egui::TextEdit::singleline(&mut app.device_ui.app_name).hint_text("game.exe").desired_width(200.0));
        if ui.button(t("Add")).clicked() && !app.device_ui.app_name.trim().is_empty() {
            chosen = Some(app.device_ui.app_name.trim().to_lowercase());
        }
        if widgets::icon_button(ui, icon::FOLDER, t("Browse")).clicked() {
            if let Some(path) = rfd::FileDialog::new().add_filter(t("Programs"), &["exe"]).set_title(t("Which program switches to this profile?")).pick_file() {
                chosen = path.file_name().map(|n| n.to_string_lossy().to_lowercase());
            }
        }
        if ui.button(t("Cancel")).clicked() {
            app.device_ui.linking = None;
        }
    });
    if let Some(exe) = chosen {
        // One profile per app and device: a new link replaces an old one.
        app.settings.app_rules.retain(|r| !(r.serial == serial && r.exe.eq_ignore_ascii_case(&exe)));
        app.settings.app_rules.push(AppRule { exe, serial: serial.to_string(), profile: profile.to_string() });
        app.device_ui.linking = None;
        app.save_settings();
    }
}

// -- keys -------------------------------------------------------------------------------------

fn keys(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    super::macros::device_keys(app, ui, device, snap);
}

// -- about --------------------------------------------------------------------------------------

fn about(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let caps = snap.details.get(&device.serial).and_then(|d| d.caps.clone()).unwrap_or_default();
    widgets::panel(ui, t("Details"), |ui| {
        egui::Grid::new("details").num_columns(2).spacing(vec2(28.0, 9.0)).show(ui, |ui| {
            if device.images.len() > 1 {
                // Nothing the device reports tells its colours apart.
                ui.label(widgets::dim(t("Colour")));
                let options: Vec<(String, String)> = device.images.iter().map(|(colour, _)| (colour.clone(), colour.clone())).collect();
                let mut colour = app.settings.colours.get(&device.serial).cloned().unwrap_or_else(|| {
                    let default = device.image.as_ref();
                    device.images.iter().find(|(_, url)| Some(url) == default).map_or_else(|| options[0].0.clone(), |(c, _)| c.clone())
                });
                if widgets::segmented(ui, &options, &mut colour) {
                    app.settings.colours.insert(device.serial.clone(), colour);
                    app.save_settings();
                }
                ui.end_row();
            }
            let mut row = |name: &str, value: String| {
                ui.label(widgets::dim(name));
                ui.label(value);
                ui.end_row();
            };
            row(t("Name"), device.name.clone());
            row(t("Type"), kind_label(&device.kind).to_string());
            row(t("USB id"), format!("1532:{:04X}", device.pid));
            row(t("Serial"), device.serial.clone());
            row(t("Firmware"), device.firmware.clone().unwrap_or_else(|| "—".into()));
            if let Some(dims) = caps["matrix_dimensions"].as_array().filter(|d| d.len() == 2) {
                row(t("LED matrix"), format!("{} × {}", dims[0], dims[1]));
            }
            if caps["transport"]["path"].as_str().is_some() {
                row(t("Connected through"), if caps["protocol"] == "razer-ble" { "Bluetooth LE".into() } else { "USB".into() });
            }
        });
    });
    ui.add_space(12.0);
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
            if let Some(zones) = caps["zones"].as_object() {
                for (zone, info) in zones {
                    let count = info["effects"].as_array().map_or(0, Vec::len);
                    widgets::chip(ui, &tf("{}: {} effects", &[&super::effects::zone_label(zone, &device.kind), &count]));
                }
            }
        });
    });
}
