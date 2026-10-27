//! Settings: the app, the daemon, repairing and removing Studio, and about.

use super::app::App;
use crate::worker::{Command, DaemonState, Snapshot};
use egui::{vec2, RichText};
use openrazer_studio_core::i18n::Lang;
use openrazer_studio_core::install::{self, COMMUNITY, REPOSITORY};
use openrazer_studio_core::theme::{self, icon, ACCENT, TEXT_DIM};
use openrazer_studio_core::{t, tf, widgets};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

fn uptime(seconds: f64) -> String {
    let s = seconds as u64;
    match s {
        0..=59 => tf("{} s", &[&s]),
        60..=3599 => tf("{} min", &[&(s / 60)]),
        _ => tf("{} h {} min", &[&(s / 3600), &(s % 3600 / 60)]),
    }
}

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        ui.set_max_width(ui.available_width().min(880.0));
        general(app, ui);
        ui.add_space(12.0);
        daemon(app, ui, snap);
        ui.add_space(12.0);
        maintenance(app, ui, snap);
        ui.add_space(12.0);
        about(ui);
        ui.add_space(24.0);
    });
}

fn general(app: &mut App, ui: &mut egui::Ui) {
    widgets::panel(ui, t("General"), |ui| {
        ui.horizontal(|ui| {
            ui.label(t("Language"));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let mut language = app.settings.language;
                let options = vec![(Lang::Ru, "Русский".to_string()), (Lang::En, "English".to_string()), (Lang::System, t("As Windows").to_string())];
                if widgets::segmented(ui, &options, &mut language) {
                    app.settings.language = language;
                    openrazer_studio_core::i18n::set(language);
                    app.save_settings();
                }
            });
        });
        ui.separator();
        let mut autostart = app.autostart;
        let hint = match crate::daemon::autostart_taken() {
            Some(_) if !autostart => t("Something else starts at sign-in now (the classic control panel or the command line); turning this on takes over from it."),
            _ => t("Sits in the tray at sign-in and keeps your devices set up."),
        };
        if widgets::toggle_row(ui, t("Start with Windows"), hint, &mut autostart) {
            match crate::daemon::set_autostart(autostart) {
                Ok(()) => app.autostart = autostart,
                Err(error) => app.toast(error, true),
            }
        }
        ui.separator();
        let mut tray = app.settings.close_to_tray;
        if widgets::toggle_row(ui, t("Keep running in the tray when closed"), t("Closing the window hides it; quit from the tray icon. Needed for switching profiles by app."), &mut tray) {
            app.settings.close_to_tray = tray;
            app.save_settings();
        }
        ui.separator();
        let mut auto = app.settings.auto_switch;
        if widgets::toggle_row(ui, t("Switch profiles by app"), t("Profiles linked to an app are applied while it is in front. Links are made on each device's Profiles tab."), &mut auto) {
            app.settings.auto_switch = auto;
            app.save_settings();
        }
        ui.separator();
        let mut updates = app.settings.check_updates;
        if widgets::toggle_row(ui, t("Look for updates"), t("Asks GitHub once at start whether a newer release is out. Nothing is downloaded or sent."), &mut updates) {
            app.settings.check_updates = updates;
            app.save_settings();
        }
    });
}

fn daemon(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    widgets::panel(ui, t("Daemon"), |ui| {
        ui.label(widgets::dim(t("The background service that owns the devices. Studio is only a window onto it, so your lighting carries on with Studio closed.")));
        ui.add_space(4.0);
        match &snap.daemon {
            DaemonState::Running(info) => {
                egui::Grid::new("daemon").num_columns(2).spacing(vec2(28.0, 7.0)).show(ui, |ui| {
                    let mut row = |name: &str, value: String| {
                        ui.label(widgets::dim(name));
                        ui.label(value);
                        ui.end_row();
                    };
                    row(t("State"), if info["state"] == "starting" { t("starting: finding devices").into() } else { t("running").into() });
                    row(t("Version"), tf("{} (device data from OpenRazer {})", &[&info["version"].as_str().unwrap_or("?"), &info["upstream"].as_str().unwrap_or("?")]));
                    row(t("Process"), format!("{}", info["pid"]));
                    row(t("Up for"), uptime(info["uptime"].as_f64().unwrap_or(0.0)));
                    row(t("Devices"), format!("{}", info["devices"]));
                    let handled = snap.keys["devices"].as_array().map_or(0, |d| d.iter().filter(|x| x["handled"] == true).count());
                    row(t("Macro keys"), if snap.keys["running"] == true { tf("on: {} keyboard(s) handled", &[&handled]) } else { t("off").into() });
                    if let Some(pending) = info["pending"].as_array().filter(|p| !p.is_empty()) {
                        let names: Vec<&str> = pending.iter().filter_map(|p| p.as_str()).collect();
                        row(t("Waiting for"), tf("{} (found, not answering yet; retrying)", &[&names.join(", ")]));
                    }
                });
                let version = info["version"].as_str().unwrap_or_default();
                if !version.is_empty() && version != VERSION {
                    ui.add_space(4.0);
                    ui.horizontal(|ui| {
                        widgets::icon(ui, icon::WARNING, 14.0, theme::WARN);
                        ui.label(RichText::new(tf("The daemon is version {}; Studio is {}. If you just upgraded, restart the daemon.", &[&version, &VERSION])).color(theme::WARN));
                    });
                }
                ui.add_space(4.0);
                ui.horizontal(|ui| {
                    if widgets::icon_button(ui, icon::REFRESH, t("Restart")).clicked() {
                        app.send(Command::RestartDaemon);
                    }
                    if widgets::icon_button(ui, icon::POWER, t("Stop")).clicked() {
                        app.send(Command::StopDaemon);
                    }
                    if widgets::icon_button(ui, icon::DEVICES, t("Look for devices")).clicked() {
                        app.send(Command::Rescan);
                    }
                    if snap.busy {
                        ui.add(egui::Spinner::new().color(ACCENT));
                    }
                });
            }
            DaemonState::NotRunning(reason) => {
                ui.label(RichText::new(tf("Not running: {}", &[reason])).color(theme::DANGER));
                if widgets::primary_button(ui, t("Start the daemon")).clicked() {
                    app.send(Command::StartDaemon);
                }
            }
            DaemonState::Connecting => {
                ui.add(egui::Spinner::new());
            }
        }
        ui.add_space(2.0);
        ui.horizontal(|ui| {
            let dir = openrazer_studio_core::paths::data_dir();
            if widgets::icon_button(ui, icon::FOLDER, t("Open the data folder")).clicked() {
                let _ = std::fs::create_dir_all(&dir);
                let _ = std::process::Command::new("explorer").arg(&dir).spawn();
            }
            if widgets::icon_button(ui, icon::INFO, t("Open the daemon log")).clicked() {
                let _ = std::process::Command::new("notepad").arg(crate::daemon::log_path()).spawn();
            }
        });
    });
}

fn maintenance(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    widgets::panel(ui, t("Repair and remove"), |ui| {
        match app.installed.clone() {
            Some(installed) => {
                ui.label(widgets::dim(tf("Installed: version {} in {}", &[&installed.version, &installed.dir.display()])));
                ui.label(widgets::faint(t("Repair puts back any missing or damaged file, the Start menu entry and starting with Windows, and sets aside settings files that no longer read. Your profiles and macros are kept.")));
                ui.horizontal(|ui| {
                    if widgets::icon_button(ui, icon::WRENCH, t("Repair Studio")).clicked() {
                        run_setup(app, &installed, "--repair");
                    }
                    if widgets::danger_button(ui, t("Uninstall…")).clicked() {
                        run_setup(app, &installed, "--uninstall");
                    }
                });
                ui.label(widgets::faint(t("Both are also in Windows Settings › Apps › OpenRazer Studio, and work even when Studio itself will not start.")));
            }
            None => {
                ui.label(widgets::dim(t("This copy of Studio is not installed, so there is nothing to repair or remove: delete the folder it is in when you are done with it.")));
                widgets::link(ui, t("Get the installer"), install::RELEASES);
            }
        }
        ui.separator();
        ui.horizontal(|ui| {
            if widgets::icon_button(ui, icon::SAVE, t("Save a report for support…")).on_hover_text(t("Versions, devices and the daemon's log, in a text file you can read before sending")).clicked() {
                let report = crate::report::build(snap);
                if let Some(path) = rfd::FileDialog::new().set_file_name("openrazer-report.txt").add_filter("Text", &["txt"]).save_file() {
                    match std::fs::write(&path, report) {
                        Ok(()) => app.toast(tf("Saved {}", &[&path.display()]), false),
                        Err(error) => app.toast(error.to_string(), true),
                    }
                }
            }
            let confirm = ui.ctx().data(|d| d.get_temp::<bool>(egui::Id::new("reset-confirm"))).unwrap_or(false);
            let text = if confirm { t("Click again to reset") } else { t("Reset Studio's settings") };
            if ui.button(text).on_hover_text(t("The library of macros, app links and window settings. Devices, profiles and key bindings are kept by the daemon and stay.")).clicked() {
                if confirm {
                    app.settings = Default::default();
                    app.save_settings();
                    openrazer_studio_core::i18n::set(app.settings.language);
                    app.toast(t("Studio's settings were reset"), false);
                }
                ui.ctx().data_mut(|d| d.insert_temp(egui::Id::new("reset-confirm"), !confirm));
            }
        });
    });
}

/// Start the installed setup to repair or remove Studio, which closes this window.
fn run_setup(app: &mut App, installed: &install::Installed, action: &str) {
    match std::process::Command::new(installed.setup()).arg(action).spawn() {
        Ok(_) => {}
        Err(error) => app.toast(tf("Could not start {}: {}", &[&installed.setup().display(), &error]), true),
    }
}

fn about(ui: &mut egui::Ui) {
    widgets::panel(ui, &format!("OpenRazer Studio {VERSION}"), |ui| {
        ui.label(widgets::dim(t("Razer device control without Synapse: a user-space port of OpenRazer to Windows. No account, no cloud, nothing sent anywhere.")));
        ui.horizontal(|ui| {
            widgets::link(ui, "GitHub", REPOSITORY);
            ui.label(RichText::new("·").color(TEXT_DIM));
            widgets::link(ui, t("Community"), COMMUNITY);
            ui.label(RichText::new("·").color(TEXT_DIM));
            widgets::link(ui, "OpenRazer", "https://openrazer.github.io/");
            ui.label(RichText::new("·").color(TEXT_DIM));
            widgets::link(ui, t("Report a problem"), &format!("{REPOSITORY}/issues"));
        });
        ui.label(widgets::faint(t("GPL-2.0-or-later. Not affiliated with Razer Inc. Product pictures are Razer's, shown from Razer's own site.")));
    });
}
