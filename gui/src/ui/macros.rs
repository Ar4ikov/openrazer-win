//! Macro keys: what each one does, and changing it.

use super::app::App;
use super::theme::{self, ACCENT, TEXT, TEXT_DIM, TEXT_FAINT};
use super::widgets;
use crate::worker::{Command, Device, Snapshot};
use egui::{vec2, Color32, CornerRadius, Frame, Margin, RichText, Stroke};
use serde_json::{json, Value};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Keys,
    Text,
    Launch,
    Disabled,
}

pub struct MacroEdit {
    pub serial: String,
    pub key: String,
    pub kind: Kind,
    pub sequence: String,
    pub text: String,
    pub target: String,
}

/// A readable name for a recorded key event.
fn key_name(event: &Value) -> String {
    let vk = event["vk"].as_u64().unwrap_or(0) as u32;
    let name = match vk {
        0x08 => "Backspace",
        0x09 => "Tab",
        0x0D => "Enter",
        0x13 => "Pause",
        0x14 => "Caps Lock",
        0x1B => "Esc",
        0x20 => "Space",
        0x21 => "Page Up",
        0x22 => "Page Down",
        0x23 => "End",
        0x24 => "Home",
        0x25 => "←",
        0x26 => "↑",
        0x27 => "→",
        0x28 => "↓",
        0x2C => "Print Screen",
        0x2D => "Insert",
        0x2E => "Delete",
        0x5B | 0x5C => "Win",
        0xA0 | 0xA1 | 0x10 => "Shift",
        0xA2 | 0xA3 | 0x11 => "Ctrl",
        0xA4 | 0xA5 | 0x12 => "Alt",
        0xAD => "Mute",
        0xAE => "Volume down",
        0xAF => "Volume up",
        0xB0 => "Next track",
        0xB1 => "Previous track",
        0xB3 => "Play/Pause",
        0x30..=0x39 | 0x41..=0x5A => return char::from_u32(vk).map(String::from).unwrap_or_default(),
        0x70..=0x87 => return format!("F{}", vk - 0x6F),
        0x60..=0x69 => return format!("Num {}", vk - 0x60),
        _ => return format!("key {vk:#04x}"),
    };
    name.to_string()
}

/// One line saying what a binding does.
pub fn summary(action: Option<&Value>, key: &str) -> (String, bool) {
    let Some(action) = action else {
        let default = match key {
            "M1" => "F13",
            "M2" => "F14",
            "M3" => "F15",
            "M4" => "F16",
            "M5" => "F17",
            "M6" | "SIDE1" => "F18",
            "SIDE2" => "F19",
            "SIDE3" => "F20",
            "DIAL" => "F24",
            _ => "its default key",
        };
        return (format!("Sends {default}"), false);
    };
    let text = match action["type"].as_str().unwrap_or_default() {
        "keys" => {
            let events = action["events"].as_array().cloned().unwrap_or_default();
            let mut chords: Vec<String> = Vec::new();
            let mut held: Vec<String> = Vec::new();
            for event in &events {
                let name = key_name(event);
                if event["down"] == true {
                    held.push(name);
                } else if !held.is_empty() {
                    chords.push(held.join("+"));
                    held.clear();
                }
            }
            let mut shown = chords.iter().take(6).cloned().collect::<Vec<_>>().join(", ");
            if chords.len() > 6 {
                shown.push_str(" …");
            }
            format!("Presses {shown}")
        }
        "text" => format!("Types “{}”", truncate(action["text"].as_str().unwrap_or_default(), 48)),
        "launch" => format!("Opens {}", truncate(action["target"].as_str().unwrap_or_default(), 56)),
        "disabled" => "Does nothing".into(),
        other => other.to_string(),
    };
    (text, true)
}

fn truncate(text: &str, limit: usize) -> String {
    let flat = text.replace('\n', " ⏎ ");
    if flat.chars().count() <= limit {
        flat
    } else {
        format!("{}…", flat.chars().take(limit).collect::<String>())
    }
}

pub fn page(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(info) = snap.details.get(&device.serial).and_then(|d| d.macros.clone()) else { return };
    let serial = device.serial.clone();
    let handled = info["handled"] == true;
    let keys: Vec<String> = info["keys"].as_array().map(|k| k.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default();
    let bindings = info["bindings"].clone();
    let recording = &snap.keys["recording"];
    let recording_here = recording["serial"] == serial.as_str();

    banner(ui, handled, snap.keys["running"] == true);
    ui.add_space(12.0);

    widgets::card(ui, |ui| {
        ui.label(RichText::new("Keys").font(theme::bold(16.0)));
        ui.add_space(2.0);
        for key in &keys {
            let action = bindings.get(key);
            let (text, bound) = summary(action, key);
            let editing = app.macro_edit.as_ref().is_some_and(|e| e.serial == serial && &e.key == key);
            Frame::new()
                .fill(if editing { Color32::from_rgb(0x1C, 0x2A, 0x1E) } else { Color32::from_rgb(0x15, 0x18, 0x1F) })
                .corner_radius(CornerRadius::same(10))
                .inner_margin(Margin::symmetric(12, 8))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        Frame::new()
                            .fill(if bound { ACCENT } else { Color32::from_rgb(0x2A, 0x30, 0x3B) })
                            .corner_radius(CornerRadius::same(6))
                            .inner_margin(Margin::symmetric(9, 4))
                            .show(ui, |ui| {
                                ui.label(RichText::new(key).font(theme::bold(13.5)).color(if bound { Color32::from_rgb(0x08, 0x14, 0x06) } else { TEXT }));
                            });
                        ui.add_space(6.0);
                        let live = recording_here && recording["key"] == key.as_str();
                        if live {
                            ui.label(RichText::new(format!("Recording… {} keys so far", recording["events"].as_u64().unwrap_or(0))).color(theme::DANGER));
                        } else {
                            ui.label(RichText::new(text).color(if bound { TEXT } else { TEXT_DIM }));
                        }
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if live {
                                if widgets::danger_button(ui, "Stop recording").clicked() {
                                    app.send(Command::MacroRecordStop { serial: serial.clone() });
                                }
                                return;
                            }
                            if bound && ui.add(egui::Button::new(RichText::new(theme::icon::DELETE).font(theme::icon_font(13.0))).frame(false)).on_hover_text("Back to the default").clicked() {
                                app.send(Command::MacroDelete { serial: serial.clone(), key: key.clone() });
                            }
                            if bound && ui.add(egui::Button::new(RichText::new(theme::icon::PLAY).font(theme::icon_font(13.0))).frame(false)).on_hover_text("Run it now").clicked() {
                                app.send(Command::MacroPlay { serial: serial.clone(), key: key.clone() });
                            }
                            if handled && !recording_here && ui.add(egui::Button::new(RichText::new(theme::icon::RECORD).font(theme::icon_font(13.0)).color(theme::DANGER)).frame(false)).on_hover_text("Record: type the macro anywhere, then stop").clicked() {
                                app.send(Command::MacroRecordStart { serial: serial.clone(), key: key.clone() });
                            }
                            if widgets::icon_button(ui, theme::icon::EDIT, "Edit").clicked() {
                                app.macro_edit = Some(start_edit(&serial, key, action));
                            }
                        });
                    });
                });
            ui.add_space(2.0);
        }
    });

    let mut outcome: Option<Option<(String, Value)>> = None;
    if let Some(edit) = app.macro_edit.as_mut().filter(|e| e.serial == serial) {
        ui.add_space(12.0);
        let mut close = false;
        let mut save: Option<Value> = None;
        widgets::card(ui, |ui| {
            ui.label(RichText::new(format!("{} does…", edit.key)).font(theme::bold(16.0)));
            let options = vec![
                (Kind::Keys, "Keys".to_string()),
                (Kind::Text, "Text".to_string()),
                (Kind::Launch, "Open something".to_string()),
                (Kind::Disabled, "Nothing".to_string()),
            ];
            widgets::segmented(ui, &options, &mut edit.kind);
            ui.add_space(6.0);
            match edit.kind {
                Kind::Keys => {
                    ui.label(widgets::dim("Keys to press, chords joined with + and separated by commas."));
                    ui.add(egui::TextEdit::singleline(&mut edit.sequence).hint_text("ctrl+shift+esc, win+e").desired_width(f32::INFINITY).font(egui::TextStyle::Monospace));
                    ui.label(RichText::new("Names: ctrl shift alt win, a–z, 0–9, f1–f24, enter esc tab space, up down left right, home end pgup pgdn, mute volup voldown play next prev.").size(12.0).color(TEXT_FAINT));
                }
                Kind::Text => {
                    ui.add(egui::TextEdit::multiline(&mut edit.text).hint_text("Text to type").desired_rows(3).desired_width(f32::INFINITY));
                }
                Kind::Launch => {
                    ui.horizontal(|ui| {
                        ui.add(egui::TextEdit::singleline(&mut edit.target).hint_text("A program, document, folder or https:// link").desired_width(ui.available_width() - 110.0));
                        if widgets::icon_button(ui, theme::icon::FOLDER, "Browse").clicked() {
                            if let Some(path) = rfd::FileDialog::new().set_title("What should this key open?").pick_file() {
                                edit.target = path.display().to_string();
                            }
                        }
                    });
                }
                Kind::Disabled => {
                    ui.label(widgets::dim("The key will do nothing at all."));
                }
            }
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                if widgets::primary_button(ui, "Save").clicked() {
                    save = Some(match edit.kind {
                        Kind::Keys => json!({ "type": "keys", "sequence": edit.sequence }),
                        Kind::Text => json!({ "type": "text", "text": edit.text }),
                        Kind::Launch => json!({ "type": "launch", "target": edit.target }),
                        Kind::Disabled => json!({ "type": "disabled" }),
                    });
                }
                if ui.button("Cancel").clicked() {
                    close = true;
                }
            });
        });
        if let Some(action) = save {
            outcome = Some(Some((edit.key.clone(), action)));
        } else if close {
            outcome = Some(None);
        }
    }
    if let Some(saved) = outcome {
        if let Some((key, action)) = saved {
            app.send(Command::MacroSet { serial: serial.clone(), key, action });
        }
        app.macro_edit = None;
    }

    ui.add_space(12.0);
    widgets::card(ui, |ui| {
        ui.label(RichText::new("On the keyboard").font(theme::bold(16.0)));
        let tips = [
            ("Fn + F9", "record a macro: press it, press a macro key, type, then Fn + F9 again"),
            ("Fn + F10", "game mode — the Windows key is held back"),
            ("Fn + F11 / F12", "dim or brighten the backlight"),
            ("Unbound keys", "send F13–F17, so any program that takes hotkeys can use them"),
        ];
        egui::Grid::new("tips").spacing(vec2(20.0, 6.0)).show(ui, |ui| {
            for (keys, what) in tips {
                ui.label(RichText::new(keys).font(theme::bold(13.5)));
                ui.label(widgets::dim(what));
                ui.end_row();
            }
        });
    });
}

fn start_edit(serial: &str, key: &str, action: Option<&Value>) -> MacroEdit {
    let mut edit = MacroEdit { serial: serial.into(), key: key.into(), kind: Kind::Keys, sequence: String::new(), text: String::new(), target: String::new() };
    if let Some(action) = action {
        match action["type"].as_str() {
            Some("text") => {
                edit.kind = Kind::Text;
                edit.text = action["text"].as_str().unwrap_or_default().into();
            }
            Some("launch") => {
                edit.kind = Kind::Launch;
                edit.target = action["target"].as_str().unwrap_or_default().into();
            }
            Some("disabled") => edit.kind = Kind::Disabled,
            _ => {}
        }
    }
    edit
}

fn banner(ui: &mut egui::Ui, handled: bool, service_running: bool) {
    let (colour, glyph, title, text) = if handled {
        (ACCENT, theme::icon::CHECK, "Macro keys are live", "The daemon handles this keyboard's macro keys and its Fn layer.")
    } else if service_running {
        (theme::WARN, theme::icon::WARNING, "Macro keys are idle", "The daemon could not read this keyboard's key report, so the keyboard keeps its own Fn layer and its macro keys do nothing. Bindings can still be set up.")
    } else {
        (theme::WARN, theme::icon::WARNING, "Macro keys are switched off", "The daemon runs without key handling (--no-keys). Restart it normally to use macro keys.")
    };
    Frame::new()
        .fill(colour.gamma_multiply(0.10))
        .stroke(Stroke::new(1.0, colour.gamma_multiply(0.5)))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(Margin::symmetric(16, 12))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                widgets::icon(ui, glyph, 16.0, colour);
                ui.vertical(|ui| {
                    ui.label(RichText::new(title).font(theme::bold(14.5)));
                    ui.label(widgets::dim(text));
                });
            });
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unbound_key_says_what_it_sends() {
        assert_eq!(summary(None, "M1"), ("Sends F13".to_string(), false));
        assert_eq!(summary(None, "M5"), ("Sends F17".to_string(), false));
    }

    #[test]
    fn key_macros_read_as_chords() {
        let action = json!({"type": "keys", "events": [
            {"vk": 0xA2, "down": true}, {"vk": 0x43, "down": true}, {"vk": 0x43, "down": false}, {"vk": 0xA2, "down": false},
            {"vk": 0x70, "down": true}, {"vk": 0x70, "down": false},
        ]});
        assert_eq!(summary(Some(&action), "M1").0, "Presses Ctrl+C, F1");
    }

    #[test]
    fn text_is_quoted_and_cut_short() {
        let action = json!({"type": "text", "text": "x".repeat(100)});
        assert!(summary(Some(&action), "M2").0.ends_with("…”"));
    }
}
