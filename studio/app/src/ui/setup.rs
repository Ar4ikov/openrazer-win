//! What the window shows while there is no daemon: why, and how to get one.

use super::app::App;
use crate::worker::{Command, Snapshot};
use egui::RichText;
use openrazer_studio_core::theme::{self, icon, ACCENT, TEXT_DIM};
use openrazer_studio_core::{t, tf, widgets};

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot, reason: &str) {
    ui.set_max_width(ui.available_width().min(780.0));
    ui.add_space(16.0);
    widgets::page_title(ui, t("The daemon is not running"), t("It is the background service that owns your devices; Studio only talks to it."));
    ui.add_space(14.0);

    if let Some((stage, progress)) = &snap.setup {
        widgets::card(ui, |ui| {
            ui.horizontal(|ui| {
                ui.add(egui::Spinner::new().size(18.0).color(ACCENT));
                ui.label(RichText::new(stage).font(theme::bold(15.0)));
            });
            if let Some(fraction) = progress {
                ui.add(egui::ProgressBar::new(*fraction).desired_height(8.0).fill(ACCENT).show_percentage());
            }
        });
        return;
    }
    if snap.busy {
        widgets::card(ui, |ui| {
            ui.horizontal(|ui| {
                ui.add(egui::Spinner::new().size(18.0).color(ACCENT));
                ui.label(RichText::new(t("Starting the daemon…")).font(theme::bold(15.0)));
            });
        });
        return;
    }

    let Some(error) = snap.start_error.clone() else {
        widgets::card(ui, |ui| {
            ui.label(widgets::dim(reason));
            if widgets::primary_button(ui, t("Start the daemon")).clicked() {
                app.send(Command::StartDaemon);
            }
        });
        return;
    };

    if let Some(installed) = app.installed.clone() {
        widgets::banner(ui, theme::WARN, icon::WRENCH, t("Studio may be damaged"), t("Repair puts back the daemon and every other file Studio was installed with. Profiles and macros are kept."), |ui| {
            if widgets::primary_icon_button(ui, icon::WRENCH, t("Repair Studio")).clicked() {
                let _ = std::process::Command::new(installed.setup()).arg("--repair").spawn();
            }
        });
        ui.add_space(12.0);
    } else if error.missing {
        widgets::panel(ui, t("Set it up"), |ui| {
            match &snap.python {
                Some(python) => {
                    ui.label(tf("Python is installed ({}), but openrazer-win is not in it yet.", &[&python.display()]));
                    ui.label(widgets::dim(t("This installs it with pip, then starts the daemon.")));
                    if widgets::primary_button(ui, t("Install openrazer-win")).clicked() {
                        app.send(Command::InstallPackage);
                    }
                }
                None => {
                    let version = crate::pysetup::PYTHON_VERSION;
                    ui.label(tf("The daemon runs on Python, which this computer does not have. This downloads Python {} from python.org and opens its installer (click Install Now there), then installs openrazer-win into it and starts the daemon.", &[&version]));
                    ui.horizontal(|ui| {
                        if widgets::primary_button(ui, &tf("Install Python {}", &[&version])).clicked() {
                            app.send(Command::InstallPython);
                        }
                    });
                }
            }
            ui.horizontal_wrapped(|ui| {
                ui.label(widgets::faint(t("Easier: install Studio with its installer, which brings the daemon along.")));
                widgets::link(ui, t("Get the installer"), openrazer_studio_core::install::RELEASES);
            });
        });
        ui.add_space(12.0);
    }

    widgets::card(ui, |ui| {
        ui.horizontal(|ui| {
            widgets::icon(ui, icon::WARNING, 15.0, theme::WARN);
            widgets::heading(ui, t("What happened"));
        });
        egui::Frame::new().fill(theme::WELL).corner_radius(egui::CornerRadius::same(theme::RADIUS)).inner_margin(egui::Margin::same(12)).show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.add(egui::Label::new(RichText::new(&error.message).monospace().size(12.0).color(TEXT_DIM)).wrap());
        });
        ui.horizontal(|ui| {
            if widgets::icon_button(ui, icon::REFRESH, t("Try again")).clicked() {
                app.send(Command::StartDaemon);
            }
            if widgets::icon_button(ui, icon::FOLDER, t("Open the log folder")).clicked() {
                let dir = openrazer_studio_core::paths::data_dir();
                let _ = std::fs::create_dir_all(&dir);
                let _ = std::process::Command::new("explorer").arg(&dir).spawn();
            }
        });
    });
}
