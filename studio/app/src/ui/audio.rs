//! A headset's sound: the settings it keeps itself, and where its mute button stands.
//!
//! These live in the headset rather than on this PC, so they follow it to a
//! phone or a console, and there is nothing to save or put back here.

use super::app::App;
use super::device::TWO_COLUMNS;
use crate::worker::{Command, Detail, Device, Snapshot, Then};
use openrazer_studio_core::theme::{self, icon, ACCENT};
use openrazer_studio_core::widgets::{self, Button};
use openrazer_studio_core::t;
use serde_json::{json, Value};
use std::time::{Duration, Instant};

/// How often to look at the mute button while its state is on screen.
const MIC_POLL: Duration = Duration::from_secs(3);

fn preset_label(preset: &str) -> &'static str {
    match preset {
        "flat" => t("Flat"),
        "game" => t("Game"),
        "music" => t("Music"),
        "movie" => t("Movie"),
        "custom" => t("Custom"),
        _ => "—",
    }
}

fn led_label(mode: &str) -> &'static str {
    match mode {
        "off" => t("Off"),
        "link" => t("Connection"),
        "battery" => t("Battery"),
        "warning" => t("Warnings"),
        _ => "—",
    }
}

fn band_label(frequency: i64) -> String {
    if frequency >= 1000 {
        format!("{} kHz", frequency / 1000)
    } else {
        format!("{frequency} Hz")
    }
}

fn names(list: &Value) -> Vec<String> {
    list.as_array().map(|items| items.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

fn call(serial: &str, method: &str, argument: Value) -> Command {
    Command::Call { serial: serial.into(), method: method.into(), args: vec![argument], zone: None, then: Then::Audio }
}

pub fn page(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(detail) = snap.details.get(&device.serial) else { return };
    let Some(caps) = detail.caps.clone() else { return };
    let Some(audio) = detail.audio.clone() else {
        ui.label(widgets::dim(t("Not reported yet.")));
        return;
    };
    if app.device_ui.audio_read.is_none_or(|at| at.elapsed() >= MIC_POLL) {
        app.device_ui.audio_read = Some(Instant::now());
        app.send(Command::ReadAudio(device.serial.clone()));
    }
    let tx = app.tx.clone();
    if !snap.synapse.is_empty() {
        widgets::banner(
            ui,
            theme::WARN,
            icon::WARNING,
            t("Razer Synapse is running"),
            t("With Synapse open, this headset's mute button can leave the microphone level in Windows at zero. Close it and the button simply mutes, and everything on this page still works."),
            |ui| {
                if ui.add(Button::primary(t("Close Synapse")).small()).clicked() {
                    let _ = tx.send(Command::CloseSynapse);
                }
            },
        );
    }
    let leds = names(&caps["audio"]["dongle_led_modes"]);
    let connected = audio["connected"] == true;
    if !connected {
        widgets::plain_panel(ui, |ui| widgets::empty_state(ui, t("The headset is switched off"), t("Switch it on to see and change its settings."), |_| {}));
        receiver(app, ui, device, &audio, &leds);
        return;
    }
    widgets::columns(ui, 0.46, TWO_COLUMNS, |ui, column| {
        if column == 0 {
            microphone(app, ui, device, &audio, &caps);
            receiver(app, ui, device, &audio, &leds);
        } else {
            equalizer(app, ui, device, &caps);
        }
    });
}

fn microphone(app: &mut App, ui: &mut egui::Ui, device: &Device, audio: &Value, caps: &Value) {
    let serial = device.serial.clone();
    let mut send: Vec<Command> = Vec::new();
    let mut later: Vec<(String, Command)> = Vec::new();
    let edit = app.edits.entry(serial.clone()).or_default();
    widgets::panel(ui, t("Microphone"), |ui| {
        widgets::row(ui, t("Right now"), t("The mute button on the headset decides this."), |ui| {
            if audio["microphone_muted"] == true {
                widgets::badge(ui, t("Muted"), theme::WARN);
            } else {
                widgets::badge(ui, t("On"), ACCENT);
            }
        });
        widgets::divider(ui);
        if widgets::toggle_row(ui, t("Hear your own voice"), t("The microphone played back quietly in the headset, so you do not shout."), &mut edit.audio.sidetone) {
            send.push(call(&serial, "set_sidetone", json!(edit.audio.sidetone)));
        }
        widgets::divider(ui);
        let top = caps["audio"]["sidetone_max"].as_f64().unwrap_or(15.0);
        widgets::row(ui, t("Level"), "", |ui| {
            ui.add_enabled_ui(edit.audio.sidetone, |ui| {
                if widgets::slider_field(ui, ("sidetone", &serial), &mut edit.audio.sidetone_level, 0.0..=top, 1.0, 0, "", 72.0) {
                    later.push((format!("sidetone:{serial}"), call(&serial, "set_sidetone_level", json!(edit.audio.sidetone_level.round() as i64))));
                }
            });
        });
        widgets::divider(ui);
        if widgets::toggle_row(ui, t("Do not disturb"), t("Silences Bluetooth calls while you are on the 2.4 GHz receiver."), &mut edit.audio.do_not_disturb) {
            send.push(call(&serial, "set_do_not_disturb", json!(edit.audio.do_not_disturb)));
        }
    });
    if !send.is_empty() || !later.is_empty() {
        edit.touch();
    }
    for command in send {
        app.send(command);
    }
    for (key, command) in later {
        app.debounce(key, command, 250);
    }
}

fn equalizer(app: &mut App, ui: &mut egui::Ui, device: &Device, caps: &Value) {
    let serial = device.serial.clone();
    let presets = names(&caps["audio"]["equalizer_presets"]);
    let frequencies: Vec<i64> = caps["audio"]["equalizer_frequencies"].as_array().map(|f| f.iter().filter_map(Value::as_i64).collect()).unwrap_or_default();
    let low = caps["audio"]["equalizer_range"][0].as_f64().unwrap_or(-9.0);
    let high = caps["audio"]["equalizer_range"][1].as_f64().unwrap_or(6.0);
    let mut send: Vec<Command> = Vec::new();
    let mut later: Vec<(String, Command)> = Vec::new();
    let edit = app.edits.entry(serial.clone()).or_default();
    widgets::panel(ui, t("Equalizer"), |ui| {
        widgets::help(ui, t("Kept in the headset, so it also works on a phone or a console."));
        ui.add_space(4.0);
        if widgets::toggle_row(ui, t("Use the equalizer"), "", &mut edit.audio.equalizer) {
            send.push(call(&serial, "set_equalizer", json!(edit.audio.equalizer)));
        }
        widgets::divider(ui);
        ui.add_enabled_ui(edit.audio.equalizer, |ui| {
            let options: Vec<(String, String)> = presets.iter().map(|p| (p.clone(), preset_label(p).to_string())).collect();
            widgets::stacked(ui, t("Preset"), |ui| {
                if widgets::segmented(ui, &options, &mut edit.audio.preset) {
                    send.push(call(&serial, "set_equalizer_preset", json!(edit.audio.preset)));
                }
            });
            if edit.audio.preset == "custom" {
                widgets::divider(ui);
                let mut changed = false;
                for (band, frequency) in frequencies.iter().enumerate() {
                    let Some(gain) = edit.audio.bands.get_mut(band) else { continue };
                    widgets::row(ui, &band_label(*frequency), "", |ui| {
                        changed |= widgets::slider_field(ui, ("band", &serial, band), gain, low..=high, 1.0, 0, t("dB"), 72.0);
                    });
                }
                if changed {
                    let gains: Vec<i64> = edit.audio.bands.iter().map(|g| g.round() as i64).collect();
                    later.push((format!("bands:{serial}"), call(&serial, "set_equalizer_bands", json!(gains))));
                }
            }
        });
    });
    if !send.is_empty() || !later.is_empty() {
        edit.touch();
    }
    for command in send {
        app.send(command);
    }
    for (key, command) in later {
        app.debounce(key, command, 300);
    }
}

/// The light on the USB receiver, which answers with the headset off too.
fn receiver(app: &mut App, ui: &mut egui::Ui, device: &Device, audio: &Value, leds: &[String]) {
    if leds.is_empty() {
        return;
    }
    let serial = device.serial.clone();
    let current = audio["dongle_led"].as_str().unwrap_or_default().to_string();
    let mut chosen = None;
    widgets::panel(ui, t("Receiver"), |ui| {
        widgets::row(ui, t("Its light shows"), "", |ui| {
            widgets::select(ui, ("dongle-led", &serial), led_label(&current), ui.available_width().min(200.0), |ui| {
                for mode in leds {
                    if widgets::option(ui, *mode == current, led_label(mode)).clicked() {
                        chosen = Some(mode.clone());
                    }
                }
            });
        });
    });
    if let Some(mode) = chosen {
        app.send(call(&serial, "set_dongle_led", json!(mode)));
    }
}

/// The one sound setting worth having on Home.
pub fn quick(app: &mut App, ui: &mut egui::Ui, device: &Device, detail: &Detail, caps: &Value) {
    let Some(audio) = detail.audio.as_ref().filter(|audio| audio["connected"] == true) else {
        ui.label(widgets::dim(t("The headset is switched off")));
        return;
    };
    let serial = device.serial.clone();
    let presets = names(&caps["audio"]["equalizer_presets"]);
    let current = if audio["equalizer"] == true { audio["equalizer_preset"].as_str().unwrap_or_default() } else { "" };
    let mut chosen = None;
    widgets::row(ui, t("Equalizer"), "", |ui| {
        widgets::select(ui, ("home-eq", &serial), if current.is_empty() { t("Off") } else { preset_label(current) }, ui.available_width(), |ui| {
            for preset in &presets {
                if widgets::option(ui, preset == current, preset_label(preset)).clicked() {
                    chosen = Some(preset.clone());
                }
            }
        });
    });
    if let Some(preset) = chosen {
        app.send(call(&serial, "set_equalizer_preset", json!(preset)));
    }
    widgets::row(ui, t("Microphone"), "", |ui| {
        if audio["microphone_muted"] == true {
            widgets::badge(ui, t("Muted"), theme::WARN);
        } else {
            widgets::badge(ui, t("On"), ACCENT);
        }
    });
}
