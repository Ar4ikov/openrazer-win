//! The daemon, starting with Windows, and about.

use super::app::App;
use super::theme::{self, ACCENT, TEXT_DIM, TEXT_FAINT};
use super::widgets;
use crate::worker::{Command, DaemonState, Snapshot};
use egui::{vec2, RichText};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

fn uptime(seconds: f64) -> String {
    let s = seconds as u64;
    match s {
        0..=59 => format!("{s} s"),
        60..=3599 => format!("{} min", s / 60),
        _ => format!("{} h {} min", s / 3600, s % 3600 / 60),
    }
}

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    ui.set_max_width(ui.available_width().min(860.0));
    ui.label(RichText::new("Settings").font(theme::bold(26.0)));
    ui.add_space(8.0);

    widgets::card(ui, |ui| {
        ui.label(RichText::new("Daemon").font(theme::bold(16.0)));
        ui.label(widgets::dim("The background service that owns the devices. This window is only a client of it."));
        ui.add_space(4.0);
        match &snap.daemon {
            DaemonState::Running(info) => {
                egui::Grid::new("daemon").num_columns(2).spacing(vec2(28.0, 8.0)).show(ui, |ui| {
                    let mut row = |name: &str, value: String| {
                        ui.label(widgets::dim(name));
                        ui.label(value);
                        ui.end_row();
                    };
                    let state = if info["state"] == "starting" { "starting — finding devices" } else { "running" };
                    row("State", state.to_string());
                    row("Version", format!("{}  (device data from OpenRazer {})", info["version"].as_str().unwrap_or("?"), info["upstream"].as_str().unwrap_or("?")));
                    row("Process", format!("{}", info["pid"]));
                    row("Up for", uptime(info["uptime"].as_f64().unwrap_or(0.0)));
                    row("Devices", format!("{}", info["devices"]));
                    let keys = &snap.keys;
                    let handled = keys["devices"].as_array().map_or(0, |d| d.iter().filter(|x| x["handled"] == true).count());
                    row("Macro keys", if keys["running"] == true { format!("on — {handled} keyboard(s) handled") } else { "off".into() });
                    if let Some(pending) = info["pending"].as_array().filter(|p| !p.is_empty()) {
                        let names: Vec<&str> = pending.iter().filter_map(|p| p.as_str()).collect();
                        row("Waiting for", format!("{} (found, not answering yet — retrying)", names.join(", ")));
                    }
                });
                let version = info["version"].as_str().unwrap_or_default();
                if !version.is_empty() && version != VERSION {
                    ui.add_space(4.0);
                    ui.horizontal(|ui| {
                        widgets::icon(ui, theme::icon::WARNING, 14.0, theme::WARN);
                        ui.label(RichText::new(format!("The daemon is version {version}; this app is {VERSION}. If you just upgraded, restart the daemon.")).color(theme::WARN));
                    });
                }
                ui.add_space(6.0);
                ui.horizontal(|ui| {
                    if widgets::icon_button(ui, theme::icon::REFRESH, "Restart").clicked() {
                        app.send(Command::RestartDaemon);
                    }
                    if widgets::icon_button(ui, theme::icon::POWER, "Stop").clicked() {
                        app.send(Command::StopDaemon);
                    }
                    if widgets::icon_button(ui, theme::icon::REFRESH, "Look for devices").clicked() {
                        app.send(Command::Rescan);
                    }
                    if snap.busy {
                        ui.add(egui::Spinner::new().color(ACCENT));
                    }
                });
            }
            DaemonState::NotRunning(reason) => {
                ui.label(RichText::new(format!("Not running: {reason}")).color(theme::DANGER));
                if widgets::primary_button(ui, "Start the daemon").clicked() {
                    app.send(Command::StartDaemon);
                }
            }
            DaemonState::Connecting => {
                ui.add(egui::Spinner::new());
            }
        }
    });

    ui.add_space(12.0);
    widgets::card(ui, |ui| {
        ui.label(RichText::new("This app").font(theme::bold(16.0)));
        let mut autostart = app.autostart;
        if widgets::toggle_row(ui, "Start with Windows", "Sits in the tray at sign-in and keeps the daemon running.", &mut autostart) {
            match crate::daemon::set_autostart(autostart) {
                Ok(()) => app.autostart = autostart,
                Err(error) => {
                    app.shared.lock().unwrap().toast = Some(crate::worker::Toast { text: error, error: true, at: std::time::Instant::now() });
                }
            }
        }
        ui.separator();
        let mut tray = app.settings.close_to_tray;
        if widgets::toggle_row(ui, "Keep running in the tray when closed", "Closing the window hides it; quit from the tray icon.", &mut tray) {
            app.settings.close_to_tray = tray;
            app.settings.save();
        }
        ui.separator();
        ui.horizontal(|ui| {
            let dir = crate::rpc::config_dir();
            if widgets::icon_button(ui, theme::icon::FOLDER, "Open the data folder").clicked() {
                let _ = std::process::Command::new("explorer").arg(&dir).spawn();
            }
            if widgets::icon_button(ui, theme::icon::INFO, "Open the daemon log").clicked() {
                let _ = std::process::Command::new("notepad").arg(dir.join("daemon.log")).spawn();
            }
        });
    });

    ui.add_space(12.0);
    widgets::card(ui, |ui| {
        ui.label(RichText::new(format!("OpenRazer for Windows {VERSION}")).font(theme::bold(16.0)));
        ui.label(widgets::dim("Razer device control without Synapse and without a kernel driver — a user-space port of OpenRazer."));
        ui.horizontal(|ui| {
            ui.hyperlink_to("github.com/Ar4ikov/openrazer-win", "https://github.com/Ar4ikov/openrazer-win");
            ui.label(RichText::new("·").color(TEXT_FAINT));
            ui.hyperlink_to("OpenRazer", "https://openrazer.github.io/");
        });
        ui.label(RichText::new("GPL-2.0-or-later. Not affiliated with Razer Inc.").size(12.0).color(TEXT_DIM));
    });
}
