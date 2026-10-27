//! Integrations: what Studio will connect to, and the one thing it already does.
//!
//! Nothing here pretends to work. Each tile says plainly whether it is
//! planned or only an idea, and asks for a vote, so what gets built next is
//! what people actually want.

use super::app::{scroll, App};
use crate::worker::Snapshot;
use egui::{vec2, CornerRadius, Frame, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::install::REPOSITORY;
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, SURFACE, SURFACE_HI, TEXT, TEXT_DIM};
use openrazer_studio_core::{t, widgets};

#[derive(Clone, Copy, PartialEq)]
enum Status {
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
        Item { glyph: icon::GAME, name: t("Games with Razer Chroma"), text: t("Games that light Razer devices through Razer's Chroma SDK, lighting them here instead."), status: Status::Planned },
        Item { glyph: icon::MUSIC, name: t("Music and sound"), text: t("Keys that pulse with whatever is playing."), status: Status::Planned },
        Item { glyph: icon::PALETTE, name: t("Windows accent colour"), text: t("Every device in the colour Windows is set to, following it when it changes."), status: Status::Planned },
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
    let _ = (app, snap);
    widgets::page_header(ui, t("Integrations"), t("Nothing here works yet except the local API: vote for what should come first."), |ui| {
        widgets::link_button(ui, t("Suggest an integration"), &format!("{REPOSITORY}/issues/new?title=Integration:%20&labels=integration"));
    });
    scroll(ui, "integrations", |ui| {
        developers(ui);
        grid(ui, "apps", t("Apps and games"), apps());
        grid(ui, "devices", t("Other devices"), devices());
    });
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
        widgets::link_button(ui, t("I want this"), &format!("{REPOSITORY}/issues/new?title=Integration:%20{title}&labels=integration"));
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
