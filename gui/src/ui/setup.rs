//! What the window shows while there is no daemon: why, and how to get one.

use super::app::App;
use super::theme::{self, ACCENT, TEXT_DIM, TEXT_FAINT};
use super::widgets;
use crate::worker::{Command, Snapshot};
use egui::RichText;

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot, reason: &str) {
    ui.set_max_width(ui.available_width().min(780.0));
    ui.add_space(24.0);
    ui.label(RichText::new("The daemon is not running").font(theme::bold(26.0)));
    ui.label(widgets::dim("It is the background service that owns your devices; this window only talks to it."));
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
                ui.label(RichText::new("Starting the daemon…").font(theme::bold(15.0)));
            });
        });
        return;
    }

    let Some(error) = &snap.start_error else {
        widgets::card(ui, |ui| {
            ui.label(widgets::dim(reason));
            if widgets::primary_button(ui, "Start the daemon").clicked() {
                app.send(Command::StartDaemon);
            }
        });
        return;
    };

    if error.missing {
        widgets::card(ui, |ui| {
            ui.label(RichText::new("Set it up").font(theme::bold(16.0)));
            match &snap.python {
                Some(python) => {
                    ui.label(format!("Python is installed ({}), but openrazer-win is not in it yet.", python.display()));
                    ui.label(widgets::dim("This installs it with pip, then starts the daemon."));
                    ui.add_space(4.0);
                    if widgets::primary_button(ui, "Install openrazer-win").clicked() {
                        app.send(Command::InstallPackage);
                    }
                }
                None => {
                    let version = crate::setup::PYTHON_VERSION;
                    ui.label(format!(
                        "The daemon runs on Python, which this computer does not have. This downloads Python {version} \
                         from python.org and opens its installer — click Install Now there — then installs \
                         openrazer-win into it and starts the daemon."
                    ));
                    ui.add_space(4.0);
                    ui.horizontal(|ui| {
                        if widgets::primary_button(ui, &format!("Install Python {version}")).clicked() {
                            app.send(Command::InstallPython);
                        }
                        ui.hyperlink_to(RichText::new("the installer, to run yourself").size(13.0), crate::setup::installer_url());
                    });
                }
            }
            ui.add_space(4.0);
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new("Or put openrazer-win.exe from").size(12.5).color(TEXT_FAINT));
                ui.hyperlink_to(RichText::new("the latest release").size(12.5), crate::daemon::RELEASES);
                ui.label(RichText::new("next to this program and try again.").size(12.5).color(TEXT_FAINT));
            });
        });
        ui.add_space(12.0);
    }

    widgets::card(ui, |ui| {
        ui.horizontal(|ui| {
            widgets::icon(ui, theme::icon::WARNING, 15.0, theme::WARN);
            ui.label(RichText::new("What happened").font(theme::bold(16.0)));
        });
        egui::Frame::new()
            .fill(egui::Color32::from_rgb(0x0B, 0x0D, 0x11))
            .corner_radius(egui::CornerRadius::same(8))
            .inner_margin(egui::Margin::same(12))
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.add(egui::Label::new(RichText::new(&error.message).monospace().size(12.5).color(TEXT_DIM)).wrap());
            });
        ui.horizontal(|ui| {
            if widgets::icon_button(ui, theme::icon::REFRESH, "Try again").clicked() {
                app.send(Command::StartDaemon);
            }
            if widgets::icon_button(ui, theme::icon::FOLDER, "Open the log folder").clicked() {
                let dir = crate::rpc::config_dir();
                let _ = std::fs::create_dir_all(&dir);
                let _ = std::process::Command::new("explorer").arg(&dir).spawn();
            }
        });
    });
}
