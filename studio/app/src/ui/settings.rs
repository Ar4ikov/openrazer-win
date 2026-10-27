//! Settings: the app, the daemon, repairing and removing Studio, and about.

use super::app::{scroll, App};
use crate::worker::{Command, DaemonState, Snapshot};
use egui::RichText;
use openrazer_studio_core::i18n::Lang;
use openrazer_studio_core::install::{self, COMMUNITY, REPOSITORY};
use openrazer_studio_core::theme::{self, icon, ACCENT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::widgets::{self, Button};
use openrazer_studio_core::{t, tf};

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
    widgets::page_header(ui, t("Settings"), t("How Studio starts and behaves, and the daemon behind it."), |_| {});
    scroll(ui, "settings", |ui| {
        widgets::columns(ui, 0.5, super::device::TWO_COLUMNS, |ui, column| {
            if column == 0 {
                general(app, ui);
                support(app, ui, snap);
                about(ui);
            } else {
                daemon(app, ui, snap);
                maintenance(app, ui);
            }
        });
    });
}

fn language_name(lang: Lang) -> &'static str {
    match lang {
        Lang::Ru => "Русский",
        Lang::En => "English",
        Lang::System => t("As Windows"),
    }
}

fn general(app: &mut App, ui: &mut egui::Ui) {
    widgets::panel(ui, t("General"), |ui| {
        let mut chosen = None;
        widgets::row(ui, t("Language"), "", |ui| {
            widgets::select(ui, "language", language_name(app.settings.language), 200.0, |ui| {
                for lang in [Lang::Ru, Lang::En, Lang::System] {
                    if widgets::option(ui, lang == app.settings.language, language_name(lang)).clicked() {
                        chosen = Some(lang);
                    }
                }
            });
        });
        if let Some(language) = chosen {
            app.settings.language = language;
            openrazer_studio_core::i18n::set(language);
            app.save_settings();
        }
        widgets::divider(ui);
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
        widgets::divider(ui);
        let mut tray = app.settings.close_to_tray;
        if widgets::toggle_row(ui, t("Keep running in the tray when closed"), t("Closing the window hides it; quit from the tray icon. Needed for switching profiles by app."), &mut tray) {
            app.settings.close_to_tray = tray;
            app.save_settings();
        }
        widgets::divider(ui);
        let mut auto = app.settings.auto_switch;
        if widgets::toggle_row(ui, t("Switch profiles by app"), t("Profiles linked to an app are applied while it is in front. Links are made on each device's Profiles tab."), &mut auto) {
            app.settings.auto_switch = auto;
            app.save_settings();
        }
        widgets::divider(ui);
        let mut updates = app.settings.check_updates;
        if widgets::toggle_row(ui, t("Look for updates"), t("Asks GitHub once at start whether a newer release is out. Nothing is downloaded or sent."), &mut updates) {
            app.settings.check_updates = updates;
            app.save_settings();
        }
    });
}

fn daemon(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    widgets::panel(ui, t("Daemon"), |ui| {
        widgets::help(ui, t("The background service that owns the devices. Studio is only a window onto it, so your lighting carries on with Studio closed."));
        match &snap.daemon {
            DaemonState::Running(info) => {
                let starting = info["state"] == "starting";
                widgets::row(ui, t("State"), "", |ui| {
                    widgets::status_dot(ui, if starting { theme::WARN } else { ACCENT }, if starting { t("starting: finding devices") } else { t("running") });
                    if snap.busy {
                        ui.add(egui::Spinner::new().size(14.0).color(TEXT_FAINT));
                    }
                });
                widgets::fact(ui, t("Version"), &tf("{} (device data from OpenRazer {})", &[&info["version"].as_str().unwrap_or("?"), &info["upstream"].as_str().unwrap_or("?")]));
                widgets::fact(ui, t("Process"), &format!("{}", info["pid"]));
                widgets::fact(ui, t("Up for"), &uptime(info["uptime"].as_f64().unwrap_or(0.0)));
                widgets::fact(ui, t("Devices"), &format!("{}", info["devices"]));
                let handled = snap.keys["devices"].as_array().map_or(0, |d| d.iter().filter(|x| x["handled"] == true).count());
                widgets::fact(ui, t("Macro keys"), &if snap.keys["running"] == true { tf("on: {} keyboard(s) handled", &[&handled]) } else { t("off").into() });
                if let Some(pending) = info["pending"].as_array().filter(|p| !p.is_empty()) {
                    let names: Vec<&str> = pending.iter().filter_map(|p| p.as_str()).collect();
                    widgets::fact(ui, t("Waiting for"), &tf("{} (found, not answering yet; retrying)", &[&names.join(", ")]));
                }
                let version = info["version"].as_str().unwrap_or_default();
                if !version.is_empty() && version != VERSION {
                    ui.horizontal(|ui| {
                        widgets::icon(ui, icon::WARNING, 14.0, theme::WARN);
                        ui.add(egui::Label::new(RichText::new(tf("The daemon is version {}; Studio is {}. If you just upgraded, restart the daemon.", &[&version, &VERSION])).color(theme::WARN)).wrap());
                    });
                }
                ui.add_space(4.0);
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing = egui::vec2(8.0, 8.0);
                    if ui.add(Button::secondary(t("Restart")).icon(icon::REFRESH)).clicked() {
                        app.send(Command::RestartDaemon);
                    }
                    if ui.add(Button::secondary(t("Stop")).icon(icon::POWER)).clicked() {
                        app.send(Command::StopDaemon);
                    }
                    if ui.add(Button::secondary(t("Look for devices")).icon(icon::DEVICES)).clicked() {
                        app.send(Command::Rescan);
                    }
                });
            }
            DaemonState::NotRunning(reason) => {
                widgets::row(ui, t("State"), reason, |ui| widgets::status_dot(ui, theme::DANGER, t("Daemon not running")));
                if ui.add(Button::primary(t("Start the daemon")).icon(icon::PLAY)).clicked() {
                    app.send(Command::StartDaemon);
                }
            }
            DaemonState::Connecting => {
                widgets::row(ui, t("State"), "", |ui| {
                    ui.add(egui::Spinner::new().size(14.0).color(TEXT_FAINT));
                    ui.label(widgets::dim(t("Connecting…")));
                });
            }
        }
        widgets::divider(ui);
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = egui::vec2(8.0, 8.0);
            let dir = openrazer_studio_core::paths::data_dir();
            if ui.add(Button::ghost(t("Open the data folder")).icon(icon::FOLDER).small()).clicked() {
                let _ = std::fs::create_dir_all(&dir);
                let _ = std::process::Command::new("explorer").arg(&dir).spawn();
            }
            if ui.add(Button::ghost(t("Open the daemon log")).icon(icon::TEXT).small()).clicked() {
                let _ = std::process::Command::new("notepad").arg(crate::daemon::log_path()).spawn();
            }
        });
    });
}

fn maintenance(app: &mut App, ui: &mut egui::Ui) {
    widgets::panel(ui, t("Repair and remove"), |ui| match app.installed.clone() {
        Some(installed) => {
            widgets::fact(ui, t("Installed"), &tf("version {} in {}", &[&installed.version, &installed.dir.display()]));
            widgets::help(ui, t("Repair puts back any missing or damaged file, the Start menu entry and starting with Windows, and sets aside settings files that no longer read. Your profiles and macros are kept."));
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                if ui.add(Button::secondary(t("Repair Studio")).icon(icon::WRENCH)).clicked() {
                    run_setup(app, &installed, "--repair");
                }
                if ui.add(Button::danger(t("Uninstall…")).icon(icon::DELETE)).clicked() {
                    run_setup(app, &installed, "--uninstall");
                }
            });
            widgets::help(ui, t("Both are also in Windows Settings › Apps › OpenRazer Studio, and work even when Studio itself will not start."));
        }
        None => {
            widgets::help(ui, t("This copy of Studio is not installed, so there is nothing to repair or remove: delete the folder it is in when you are done with it."));
            widgets::link_button(ui, t("Get the installer"), install::RELEASES);
        }
    });
}

fn support(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    widgets::panel(ui, t("Support"), |ui| {
        widgets::action_row(ui, t("Report"), t("Versions, devices and the daemon's log, in a text file you can read before sending"), 190.0, |ui| {
            if ui.add(Button::secondary(t("Save a report…")).icon(icon::SAVE)).clicked() {
                let report = crate::report::build(snap);
                if let Some(path) = rfd::FileDialog::new().set_file_name("openrazer-report.txt").add_filter("Text", &["txt"]).save_file() {
                    match std::fs::write(&path, report) {
                        Ok(()) => app.toast(tf("Saved {}", &[&path.display()]), false),
                        Err(error) => app.toast(error.to_string(), true),
                    }
                }
            }
        });
        widgets::divider(ui);
        widgets::action_row(ui, t("Reset Studio's settings"), t("The library of macros, app links and window settings. Devices, profiles and key bindings are kept by the daemon and stay."), 190.0, |ui| {
            let confirm = ui.ctx().data(|d| d.get_temp::<bool>(egui::Id::new("reset-confirm"))).unwrap_or(false);
            let text = if confirm { t("Click again to reset") } else { t("Reset…") };
            if ui.add(Button::danger(text).min_width(160.0)).clicked() {
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
        widgets::help(ui, t("Razer device control without Synapse: a user-space port of OpenRazer to Windows. No account, no cloud, nothing sent anywhere."));
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = egui::vec2(8.0, 8.0);
            widgets::link_button(ui, "GitHub", REPOSITORY);
            widgets::link_button(ui, t("Community"), COMMUNITY);
            widgets::link_button(ui, "OpenRazer", "https://openrazer.github.io/");
            widgets::link_button(ui, t("Report a problem"), &format!("{REPOSITORY}/issues"));
        });
        ui.label(RichText::new(t("GPL-2.0-or-later. Not affiliated with Razer Inc. Product pictures are Razer's, shown from Razer's own site.")).size(12.0).color(TEXT_DIM));
    });
}
