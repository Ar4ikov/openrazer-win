//! Integrations: what Studio will connect to, and the one thing it already does.
//!
//! Nothing here pretends to work. Each card says plainly whether it is
//! planned or only an idea, and asks for a vote, so what gets built next is
//! what people actually want.

use super::app::App;
use crate::worker::Snapshot;
use egui::{vec2, CornerRadius, Frame, Margin, RichText, Stroke};
use openrazer_studio_core::install::REPOSITORY;
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, SURFACE, TEXT_DIM};
use openrazer_studio_core::{t, widgets};

#[derive(Clone, Copy, PartialEq)]
enum Status {
    Available,
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
    let _ = snap;
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        ui.set_max_width(ui.available_width().min(1100.0));
        ui.label(widgets::dim(t("Studio will connect your devices to the apps you use and to lights from other makers. Nothing here works yet except the local API: vote for what should come first.")));
        ui.add_space(12.0);
        developers(app, ui);
        ui.add_space(16.0);
        grid(ui, t("Apps and games"), apps());
        ui.add_space(16.0);
        grid(ui, t("Other devices"), devices());
        ui.add_space(16.0);
        ui.horizontal(|ui| {
            ui.label(widgets::dim(t("Something else in mind?")));
            widgets::link(ui, t("Suggest an integration"), &format!("{REPOSITORY}/issues/new?title=Integration:%20&labels=integration"));
        });
        ui.add_space(24.0);
    });
}

fn grid(ui: &mut egui::Ui, title: &str, items: Vec<Item>) {
    widgets::heading(ui, title);
    ui.add_space(4.0);
    let width = 320.0;
    let columns = ((ui.available_width() + 12.0) / (width + 12.0)).floor().max(1.0) as usize;
    let width = (ui.available_width() - 12.0 * (columns as f32 - 1.0)) / columns as f32;
    for row in items.chunks(columns) {
        ui.horizontal_top(|ui| {
            ui.spacing_mut().item_spacing.x = 12.0;
            for item in row {
                ui.allocate_ui_with_layout(vec2(width, 0.0), egui::Layout::top_down(egui::Align::Min), |ui| card(ui, item, width));
            }
        });
        ui.add_space(4.0);
    }
}

fn card(ui: &mut egui::Ui, item: &Item, width: f32) {
    Frame::new().fill(SURFACE).stroke(Stroke::new(1.0_f32, BORDER)).corner_radius(CornerRadius::same(theme::RADIUS)).inner_margin(Margin::same(14)).show(ui, |ui| {
        ui.set_width(width - 30.0);
        ui.set_min_height(104.0);
        ui.horizontal(|ui| {
            widgets::icon(ui, item.glyph, 18.0, if item.status == Status::Available { ACCENT } else { TEXT_DIM });
            ui.label(RichText::new(item.name).font(theme::bold(14.5)));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| match item.status {
                Status::Available => {
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
        ui.add(egui::Label::new(RichText::new(item.text).size(12.5).color(TEXT_DIM)).wrap());
        let title = item.name.replace(' ', "%20");
        widgets::link(ui, &format!("{}  ›", t("I want this")), &format!("{REPOSITORY}/issues/new?title=Integration:%20{title}&labels=integration"));
    });
}

/// The daemon's API, which is real today.
fn developers(app: &mut App, ui: &mut egui::Ui) {
    widgets::card(ui, |ui| {
        ui.horizontal(|ui| {
            widgets::icon(ui, icon::CODE, 18.0, ACCENT);
            widgets::heading(ui, t("For your own scripts"));
            widgets::badge(ui, t("Works now"), ACCENT);
        });
        ui.label(widgets::dim(t("Everything Studio does goes through the daemon's local API, which any program on this PC can use: JSON-RPC over a loopback port, with a token only your account can read.")));
        ui.add_space(4.0);
        egui::Grid::new("api").num_columns(2).spacing(vec2(18.0, 6.0)).show(ui, |ui| {
            ui.label(widgets::faint(t("Python")));
            ui.label(RichText::new("pip install openrazer-win   ·   from openrazer_win.client import DeviceManager").monospace().size(12.5));
            ui.end_row();
            ui.label(widgets::faint(t("Command line")));
            ui.label(RichText::new("openrazer-win list   ·   openrazer-win effect static ff0000").monospace().size(12.5));
            ui.end_row();
            ui.label(widgets::faint(t("Endpoint")));
            let endpoint = openrazer_studio_core::rpc::Endpoint::read().map(|e| format!("{}:{}", e.host, e.port)).unwrap_or_else(|| t("the daemon is not running").to_string());
            ui.horizontal(|ui| {
                ui.label(RichText::new(&endpoint).monospace().size(12.5));
                ui.label(widgets::faint(format!("({})", openrazer_studio_core::rpc::Endpoint::path().display())));
            });
            ui.end_row();
        });
        ui.horizontal(|ui| {
            widgets::link(ui, t("Documentation"), "https://ar4ikov.github.io/openrazer-win/");
            ui.label(widgets::faint("·"));
            widgets::link(ui, t("Examples"), &format!("{REPOSITORY}/tree/main/examples"));
        });
        let _ = app;
    });
}
