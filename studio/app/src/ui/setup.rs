//! What the window shows while there is no daemon: why, and how to get one.

use super::app::{scroll, App};
use crate::worker::{Command, Snapshot};
use egui::{CornerRadius, Frame, Margin, RichText, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, TEXT, TEXT_DIM};
use openrazer_studio_core::widgets::{self, Button};
use openrazer_studio_core::{t, tf};

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot, reason: &str) {
    widgets::page_header(ui, t("The daemon is not running"), t("It is the background service that owns your devices; Studio only talks to it."), |_| {});
    scroll(ui, "setup", |ui| {
        ui.set_max_width(ui.available_width().min(820.0));
        if let Some((stage, progress)) = &snap.setup {
            widgets::panel(ui, t("Setting up"), |ui| {
                ui.horizontal(|ui| {
                    ui.add(egui::Spinner::new().size(18.0).color(ACCENT));
                    ui.label(RichText::new(stage).font(theme::bold(15.0)).color(TEXT));
                });
                if let Some(fraction) = progress {
                    ui.add(egui::ProgressBar::new(*fraction).desired_height(8.0).fill(ACCENT).show_percentage());
                }
            });
            return;
        }
        if snap.busy {
            widgets::panel(ui, t("Daemon"), |ui| {
                ui.horizontal(|ui| {
                    ui.add(egui::Spinner::new().size(18.0).color(ACCENT));
                    ui.label(RichText::new(t("Starting the daemon…")).font(theme::bold(15.0)).color(TEXT));
                });
            });
            return;
        }

        let Some(error) = snap.start_error.clone() else {
            widgets::panel(ui, t("Daemon"), |ui| {
                widgets::help(ui, reason);
                if ui.add(Button::primary(t("Start the daemon")).icon(icon::PLAY)).clicked() {
                    app.send(Command::StartDaemon);
                }
            });
            return;
        };

        if let Some(installed) = app.installed.clone() {
            widgets::banner(ui, theme::WARN, icon::WRENCH, t("Studio may be damaged"), t("Repair puts back the daemon and every other file Studio was installed with. Profiles and macros are kept."), |ui| {
                if ui.add(Button::primary(t("Repair Studio")).icon(icon::WRENCH)).clicked() {
                    let _ = std::process::Command::new(installed.setup()).arg("--repair").spawn();
                }
            });
        } else if error.missing {
            widgets::panel(ui, t("Set it up"), |ui| {
                match &snap.python {
                    Some(python) => {
                        ui.add(egui::Label::new(RichText::new(tf("Python is installed ({}), but openrazer-win is not in it yet.", &[&python.display()])).color(TEXT)).wrap());
                        widgets::help(ui, t("This installs it with pip, then starts the daemon."));
                        if ui.add(Button::primary(t("Install openrazer-win")).icon(icon::DOWNLOAD)).clicked() {
                            app.send(Command::InstallPackage);
                        }
                    }
                    None => {
                        let version = crate::pysetup::PYTHON_VERSION;
                        ui.add(egui::Label::new(RichText::new(tf("The daemon runs on Python, which this computer does not have. This downloads Python {} from python.org and opens its installer (click Install Now there), then installs openrazer-win into it and starts the daemon.", &[&version])).color(TEXT)).wrap());
                        if ui.add(Button::primary(&tf("Install Python {}", &[&version])).icon(icon::DOWNLOAD)).clicked() {
                            app.send(Command::InstallPython);
                        }
                    }
                }
                widgets::divider(ui);
                ui.horizontal_wrapped(|ui| {
                    ui.label(widgets::faint(t("Easier: install Studio with its installer, which brings the daemon along.")));
                    widgets::link_button(ui, t("Get the installer"), openrazer_studio_core::install::RELEASES);
                });
            });
        }

        widgets::panel_with(
            ui,
            t("What happened"),
            |ui| {
                widgets::icon(ui, icon::WARNING, 14.0, theme::WARN);
            },
            |ui| {
                Frame::new().fill(theme::WELL).stroke(Stroke::new(1.0_f32, theme::BORDER)).corner_radius(CornerRadius::same(theme::RADIUS_SM)).inner_margin(Margin::same(12)).show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.add(egui::Label::new(RichText::new(&error.message).monospace().size(12.0).color(TEXT_DIM)).wrap());
                });
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    if ui.add(Button::secondary(t("Try again")).icon(icon::REFRESH)).clicked() {
                        app.send(Command::StartDaemon);
                    }
                    if ui.add(Button::ghost(t("Open the log folder")).icon(icon::FOLDER)).clicked() {
                        let dir = openrazer_studio_core::paths::data_dir();
                        let _ = std::fs::create_dir_all(&dir);
                        let _ = std::process::Command::new("explorer").arg(&dir).spawn();
                    }
                });
            },
        );
    });
}
