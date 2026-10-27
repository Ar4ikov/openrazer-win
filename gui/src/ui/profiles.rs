//! Named lighting profiles: save what is showing, put it back later.

use super::app::App;
use super::theme::{self, TEXT_DIM, TEXT_FAINT};
use super::widgets;
use crate::worker::{Command, Device, Snapshot};
use egui::{Color32, CornerRadius, Frame, Margin, RichText};

pub fn page(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let serial = device.serial.clone();
    let profiles = snap.details.get(&serial).map(|d| d.profiles.clone()).unwrap_or_default();

    widgets::card(ui, |ui| {
        ui.label(RichText::new("Save what is showing now").font(theme::bold(16.0)));
        ui.label(widgets::dim("Effect, colours and brightness of every zone. Up to ten per device."));
        ui.horizontal(|ui| {
            let input = ui.add(egui::TextEdit::singleline(&mut app.profile_name).hint_text("Profile name, e.g. Night").desired_width(260.0));
            let enter = input.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
            let name = app.profile_name.trim().to_string();
            let save = ui.add_enabled(!name.is_empty(), egui::Button::new(RichText::new("Save").font(theme::bold(14.0))));
            if (save.clicked() || enter) && !name.is_empty() {
                app.send(Command::Profile { action: "save", serial: serial.clone(), name });
                app.profile_name.clear();
            }
        });
    });
    ui.add_space(12.0);
    widgets::card(ui, |ui| {
        ui.label(RichText::new("Saved profiles").font(theme::bold(16.0)));
        if profiles.is_empty() {
            ui.label(widgets::dim("None yet."));
        }
        for profile in &profiles {
            let name = profile["name"].as_str().unwrap_or_default().to_string();
            let saved = profile["saved"].as_str().unwrap_or_default();
            Frame::new()
                .fill(Color32::from_rgb(0x15, 0x18, 0x1F))
                .corner_radius(CornerRadius::same(10))
                .inner_margin(Margin::symmetric(14, 9))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        ui.vertical(|ui| {
                            ui.spacing_mut().item_spacing.y = 2.0;
                            ui.label(RichText::new(&name).font(theme::bold(14.5)));
                            let when = saved.split('.').next().unwrap_or(saved).replace('T', " ");
                            ui.label(RichText::new(format!("saved {when}")).size(12.0).color(TEXT_FAINT));
                        });
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui.add(egui::Button::new(RichText::new(theme::icon::DELETE).font(theme::icon_font(13.0)).color(TEXT_DIM)).frame(false)).on_hover_text("Delete").clicked() {
                                app.send(Command::Profile { action: "delete", serial: serial.clone(), name: name.clone() });
                            }
                            if widgets::primary_button(ui, "Apply").clicked() {
                                app.send(Command::Profile { action: "load", serial: serial.clone(), name: name.clone() });
                            }
                        });
                    });
                });
            ui.add_space(2.0);
        }
    });
}
