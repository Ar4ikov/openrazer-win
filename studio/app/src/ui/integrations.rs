//! Integrations: what works today, with its switches, and what is still to come.
//!
//! Nothing here pretends to work. Each tile says plainly whether it is
//! planned or only an idea, and asks for a vote, so what gets built next is
//! what people actually want.

use super::app::{scroll, App};
use crate::chroma;
use crate::worker::{Command, Snapshot};
use egui::{vec2, CornerRadius, Frame, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::install::REPOSITORY;
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, SURFACE, SURFACE_HI, TEXT, TEXT_DIM};
use openrazer_studio_core::widgets::Button;
use openrazer_studio_core::{t, tf, widgets};
use serde_json::Value;
use std::path::Path;

#[derive(Clone, Copy, PartialEq)]
enum Status {
    Ready,
    Planned,
    Idea,
}

struct Item {
    glyph: &'static str,
    name: &'static str,
    text: &'static str,
    status: Status,
}

fn apps() -> Vec<Item> {
    vec![
        Item { glyph: icon::MUSIC, name: t("Music and sound"), text: t("Keys that pulse with whatever is playing."), status: Status::Ready },
        Item { glyph: icon::CHAT, name: "Discord", text: t("Calls, mentions and a muted microphone shown on the keyboard."), status: Status::Idea },
        Item { glyph: icon::SCREEN, name: t("Screen ambience"), text: t("Devices glowing in the colours at the edges of the screen."), status: Status::Idea },
        Item { glyph: icon::RECORD, name: "OBS Studio", text: t("A key that lights up while recording or streaming."), status: Status::Idea },
        Item { glyph: icon::BELL, name: t("Notifications"), text: t("A flash for a low battery, a message, or a finished download."), status: Status::Idea },
    ]
}

fn devices() -> Vec<Item> {
    vec![
        Item { glyph: icon::SYNC, name: "OpenRGB", text: t("Razer devices in an OpenRGB setup, and other brands' lights following Studio."), status: Status::Planned },
        Item { glyph: icon::BULB, name: "Philips Hue", text: t("Room lights matching the desk."), status: Status::Idea },
        Item { glyph: icon::BULB, name: "Nanoleaf", text: t("Panels on the wall in step with the keyboard."), status: Status::Idea },
        Item { glyph: icon::BULB, name: "WLED", text: t("LED strips behind the monitor or desk."), status: Status::Idea },
        Item { glyph: icon::HOMEAUTO, name: "Home Assistant", text: t("Devices and effects as switches and lights in a smart home."), status: Status::Idea },
    ]
}

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    widgets::page_header(ui, t("Integrations"), t("Most of this is still to come: vote for what should come first."), |ui| {
        widgets::link_button(ui, t("Suggest an integration"), &format!("{REPOSITORY}/issues/new?title=Integration:%20&labels=integration"));
    });
    scroll(ui, "integrations", |ui| {
        widgets::columns(ui, 0.5, 860.0, |ui, column| {
            if column == 0 {
                games(app, ui, &snap.integrations);
            } else {
                accent(app, ui, &snap.integrations);
            }
        });
        developers(ui);
        grid(ui, "apps", t("Apps and games"), apps());
        grid(ui, "devices", t("Other devices"), devices());
    });
}

/// A switch for one of the daemon's integrations, or why there is none.
fn switch(app: &mut App, ui: &mut egui::Ui, status: &Value, name: &str, label: &str, help: &str) -> bool {
    let mut on = status[name]["enabled"] == true;
    let known = status[name].is_object();
    widgets::row(ui, label, help, |ui| {
        if !known {
            ui.label(widgets::faint(t("Needs the daemon from Studio 1.6.0 or later, running")));
        } else if widgets::toggle(ui, &mut on).changed() {
            app.send(Command::Integration { name: name.into(), enabled: on });
        }
    });
    on
}

fn accent(app: &mut App, ui: &mut egui::Ui, status: &Value) {
    widgets::panel_with(
        ui,
        t("Windows accent colour"),
        |ui| {
            widgets::badge(ui, t("Works now"), ACCENT);
        },
        |ui| {
            widgets::help(ui, t("Every device in the colour Windows is set to, following it when it changes."));
            let on = switch(app, ui, status, "accent", t("Follow the accent colour"), t("While on, a new colour chosen in Windows' personalisation settings is put on every device."));
            widgets::divider(ui);
            widgets::row(ui, t("The colour now"), "", |ui| {
                let colour = crate::accent::colour();
                widgets::swatch(ui, colour, 26.0, false);
                ui.label(widgets::dim(super::effects::hex(colour)));
            });
            if !on {
                widgets::help(ui, t("One device or one ear can follow it on its own too: choose Windows accent colour as its effect, under Lighting."));
            }
        },
    );
}

fn games(app: &mut App, ui: &mut egui::Ui, status: &Value) {
    widgets::panel_with(
        ui,
        t("Games with Razer Chroma"),
        |ui| {
            widgets::badge(ui, t("Works now"), ACCENT);
        },
        |ui| {
            widgets::help(ui, t("Games made for Razer Chroma light your devices as they would with Synapse: health, ammunition, the keys a game uses."));
            let on = switch(app, ui, status, "chroma", t("Let games light devices"), t("While a game shows something, it has the devices; when it closes, your own lighting comes back."));
            let chroma = &status["chroma"];
            if on {
                if chroma["listening"] == true {
                    let text = match chroma["showing"].as_str() {
                        Some(game) => tf("Showing now: {}", &[&game]),
                        None => t("Ready: no game is showing anything").to_string(),
                    };
                    widgets::status_dot(ui, ACCENT, &text);
                } else if chroma["error"].is_string() {
                    widgets::status_dot(ui, theme::WARN, t("Razer Synapse holds the port games look for. Close it, then switch this off and on again."));
                }
            }
            widgets::divider(ui);
            game_list(app, ui);
            widgets::help(ui, t("Leave out games with anti-cheat, such as online shooters: it may take a file it does not know for a cheat. Games built on Razer's newer SDK check that the file is Razer's own, and light nothing here."));
        },
    );
}

fn game_list(app: &mut App, ui: &mut egui::Ui) {
    let available = chroma::available();
    widgets::row(ui, t("Games"), t("Most games look for Razer's SDK beside their program, and find it only once it is put there."), |ui| {
        if ui.add(Button::secondary(t("Add a game…")).icon(icon::ADD).enabled(available)).clicked() {
            if let Some(path) = rfd::FileDialog::new().add_filter(t("Programs"), &["exe"]).set_title(t("Which game should light your devices?")).pick_file() {
                match chroma::give(&path) {
                    Ok(()) => {
                        let path = path.display().to_string();
                        if !app.settings.chroma_games.contains(&path) {
                            app.settings.chroma_games.push(path);
                            app.save_settings();
                        }
                        app.toast(t("The game can light your devices from its next start"), false);
                    }
                    Err(error) => app.toast(tf("Could not add it: {}", &[&error]), true),
                }
            }
        }
    });
    if !available {
        widgets::help(ui, t("This build of Studio has no Chroma DLL to give games; the one installed from a release has."));
    }
    let mut removed = None;
    for (index, game) in app.settings.chroma_games.iter().enumerate() {
        let path = Path::new(game);
        let name = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| game.clone());
        widgets::row(ui, &name, game, |ui| {
            if ui.add(Button::ghost(t("Remove")).icon(icon::DELETE).small()).clicked() {
                removed = Some(index);
            }
            if !chroma::given(path) {
                ui.label(widgets::faint(t("Updated or moved: add it again")));
            }
        });
    }
    if let Some(index) = removed {
        let game = app.settings.chroma_games.remove(index);
        app.save_settings();
        match chroma::take_back(Path::new(&game)) {
            Ok(()) => app.toast(t("Removed from beside the game"), false),
            Err(error) => app.toast(tf("Could not remove it: {}", &[&error]), true),
        }
    }
}

fn grid(ui: &mut egui::Ui, id: &str, title: &str, items: Vec<Item>) {
    ui.vertical(|ui| {
        ui.spacing_mut().item_spacing.y = 10.0;
        widgets::section_title(ui, title);
        let gap = theme::GAP;
        let width = ui.available_width();
        let columns = ((width + gap) / (300.0 + gap)).floor().clamp(1.0, 4.0) as usize;
        let tile = ((width - gap * (columns as f32 - 1.0)) / columns as f32).floor();
        for (r, row) in items.chunks(columns).enumerate() {
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = gap;
                for item in row {
                    ui.allocate_ui_with_layout(vec2(tile, 0.0), egui::Layout::top_down(egui::Align::Min), |ui| {
                        ui.set_width(tile);
                        ui.spacing_mut().item_spacing.x = theme::SPACING;
                        card(ui, item, egui::Id::new(("integration-row", id, r, columns)));
                    });
                }
            });
        }
    });
}

fn card(ui: &mut egui::Ui, item: &Item, row: egui::Id) {
    Frame::new().fill(SURFACE).stroke(Stroke::new(1.0_f32, BORDER)).corner_radius(CornerRadius::same(theme::RADIUS)).inner_margin(Margin::same(theme::PAD)).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.spacing_mut().item_spacing.y = 8.0;
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 10.0;
            let (rect, _) = ui.allocate_exact_size(vec2(36.0, 36.0), Sense::hover());
            ui.painter().rect_filled(rect, CornerRadius::same(theme::RADIUS_SM), SURFACE_HI);
            ui.painter().text(rect.center(), egui::Align2::CENTER_CENTER, item.glyph, theme::icon_font(17.0), TEXT_DIM);
            ui.label(RichText::new(item.name).font(theme::bold(14.5)).color(TEXT));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| match item.status {
                Status::Ready => {
                    widgets::badge(ui, t("Works now"), ACCENT);
                }
                Status::Planned => {
                    widgets::badge(ui, t("Planned"), theme::INFO);
                }
                Status::Idea => {
                    widgets::badge(ui, t("Idea"), TEXT_DIM);
                }
            });
        });
        ui.add(egui::Label::new(RichText::new(item.text).size(13.0).color(TEXT_DIM)).wrap());
        let title = item.name.replace(' ', "%20");
        // The vote sits at the bottom whatever the text above it says.
        widgets::equalise(ui, row);
        if item.status == Status::Ready {
            ui.label(widgets::faint(t("Choose it as the effect, under Lighting.")));
        } else {
            widgets::link_button(ui, t("I want this"), &format!("{REPOSITORY}/issues/new?title=Integration:%20{title}&labels=integration"));
        }
    });
}

/// The daemon's API, which is real today.
fn developers(ui: &mut egui::Ui) {
    widgets::panel_with(
        ui,
        t("For your own scripts"),
        |ui| {
            widgets::badge(ui, t("Works now"), ACCENT);
        },
        |ui| {
            widgets::help(ui, t("Everything Studio does goes through the daemon's local API, which any program on this PC can use: JSON-RPC over a loopback port, with a token only your account can read."));
            let code = |ui: &mut egui::Ui, text: &str| {
                Frame::new().fill(theme::WELL).stroke(Stroke::new(1.0_f32, BORDER)).corner_radius(CornerRadius::same(4)).inner_margin(Margin::symmetric(8, 4)).show(ui, |ui| {
                    ui.add(egui::Label::new(RichText::new(text).monospace().size(12.5).color(TEXT)).truncate());
                });
            };
            widgets::row(ui, t("Python"), "", |ui| code(ui, "pip install openrazer-win   ·   from openrazer_win.client import DeviceManager"));
            widgets::row(ui, t("Command line"), "", |ui| code(ui, "openrazer-win list   ·   openrazer-win effect static ff0000"));
            let endpoint = openrazer_studio_core::rpc::Endpoint::read().map(|e| format!("{}:{}", e.host, e.port)).unwrap_or_else(|| t("the daemon is not running").to_string());
            widgets::row(ui, t("Endpoint"), &openrazer_studio_core::rpc::Endpoint::path().display().to_string(), |ui| code(ui, &endpoint));
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                widgets::link_button(ui, t("Documentation"), "https://ar4ikov.github.io/openrazer-win/");
                widgets::link_button(ui, t("Examples"), &format!("{REPOSITORY}/tree/main/examples"));
            });
        },
    );
}
