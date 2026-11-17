//! The Buttons page: what each button does, stored in the mouse's own memory.

use super::app::App;
use super::theme::{self, ACCENT, TEXT, TEXT_DIM, TEXT_FAINT};
use super::widgets;
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{Color32, CornerRadius, Frame, Margin, RichText};
use serde_json::{json, Value};

pub fn page(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let serial = device.serial.clone();
    let Some(onboard) = snap.details.get(&serial).and_then(|d| d.onboard.clone()) else {
        ui.label(widgets::dim("Reading the buttons from the mouse…"));
        return;
    };
    let profile = onboard["profile"].as_i64().unwrap_or(1);
    let active = onboard["profiles"]["active"].as_i64();
    // The page moved to another profile under an open editor (a reload, or a
    // switch that finished late): what it was showing no longer applies.
    if app.button_edit.as_ref().is_some_and(|e| e.serial == serial && e.profile != profile) {
        app.button_edit = None;
    }
    let layer = app.buttons_layer.clone();
    let buttons: Vec<Value> = onboard["buttons"].as_array().cloned().unwrap_or_default();

    ui.horizontal_wrapped(|ui| {
        ui.vertical(|ui| {
            ui.label(widgets::dim("Profile on the mouse"));
            let options: Vec<(i64, String)> = onboard["profiles"]["profiles"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|p| {
                    let id = p["id"].as_i64()?;
                    let name = p["name"].as_str().map_or_else(|| id.to_string(), |n| format!("\"{n}\""));
                    Some((id, if Some(id) == active { format!("{name} · active") } else { name }))
                })
                .collect();
            let mut picked = profile;
            if profile_picker(ui, &options, &mut picked) {
                app.button_edit = None;
                // Picking a profile here switches the mouse to it, as its own button
                // does; then the page reads it. The worker runs the two in order.
                app.send(Command::Call {
                    serial: serial.clone(),
                    method: "set_active_onboard_profile".into(),
                    args: vec![json!(picked)],
                    zone: None,
                    then: Then::Nothing,
                });
                app.send(Command::ButtonsLoad { serial: serial.clone(), profile: picked });
            }
        });
        ui.add_space(16.0);
        ui.vertical(|ui| {
            ui.label(widgets::dim("Layer"));
            let options = [("normal".to_string(), "Normal".to_string()), ("hypershift".to_string(), "Hypershift (held)".to_string())];
            if widgets::segmented(ui, &options, &mut app.buttons_layer) {
                app.button_edit = None;
            }
        });
    });
    ui.add_space(12.0);

    // Side by side when there is room, so the editor is on screen the moment
    // Edit is clicked; stacked (editor first) in a narrow window.
    let mut outcome = None;
    if ui.available_width() >= 700.0 {
        ui.columns(2, |columns| {
            button_list(app, &mut columns[0], &serial, profile, &layer, &onboard);
            outcome = editor(app, &mut columns[1], &serial, &buttons, true);
        });
    } else {
        outcome = editor(app, ui, &serial, &buttons, false);
        ui.add_space(8.0);
        button_list(app, ui, &serial, profile, &layer, &onboard);
    }
    if let Some(saved) = outcome {
        if let Some((edit_profile, button, action, edit_layer)) = saved {
            app.send(Command::Call {
                serial: serial.clone(),
                method: "set_button_binding".into(),
                args: vec![json!(edit_profile), json!(button), json!(action), json!(edit_layer)],
                zone: None,
                then: Then::Buttons,
            });
        }
        app.button_edit = None;
    }

    ui.add_space(12.0);
    ui.horizontal(|ui| {
        if widgets::icon_button(ui, theme::icon::FOLDER, "Back up profile…").clicked() {
            if let Some(path) = rfd::FileDialog::new()
                .set_title("Back up this profile's buttons")
                .set_file_name(format!("buttons-profile-{profile}.json"))
                .add_filter("Button backup", &["json"])
                .save_file()
            {
                app.send(Command::ButtonsBackup { serial: serial.clone(), profile, path });
            }
        }
        if widgets::icon_button(ui, theme::icon::REFRESH, "Restore from file…").clicked() {
            if let Some(path) = rfd::FileDialog::new().set_title("Restore buttons from a backup").add_filter("Button backup", &["json"]).pick_file() {
                app.button_edit = None;
                app.send(Command::ButtonsRestore { serial: serial.clone(), path });
            }
        }
    });
    ui.label(RichText::new("A backup holds one profile, both layers; razerbind backups restore here too.").size(12.0).color(TEXT_DIM));
}

/// What Save or Cancel in the editor asked for: `Some(None)` closes it,
/// `Some(Some((profile, button, action, layer)))` writes and closes.
type Outcome = Option<Option<(i64, i64, String, String)>>;

/// The buttons, one compact row each, scrolling inside their own card.
fn button_list(app: &mut App, ui: &mut egui::Ui, serial: &str, profile: i64, layer: &str, onboard: &Value) {
    let buttons = onboard["buttons"].as_array().cloned().unwrap_or_default();
    let bindings = onboard["bindings"][layer].as_array().cloned().unwrap_or_default();
    widgets::card(ui, |ui| {
        ui.label(RichText::new("Buttons").font(theme::bold(16.0)));
        ui.label(widgets::dim("Saved in the mouse itself, so they work on any computer, with nothing running."));
        ui.add_space(4.0);
        egui::ScrollArea::vertical().id_salt("onboard-buttons").max_height(330.0).auto_shrink([false, true]).show(ui, |ui| {
            for (index, button) in buttons.iter().enumerate() {
                let id = button["id"].as_i64().unwrap_or(0);
                let label = button_label(id, button["name"].as_str().unwrap_or(""));
                let action = bindings.get(index).and_then(|b| b["action"].as_str()).unwrap_or("").to_string();
                let known = !action.starts_with("fn=");
                let editing = app.button_edit.as_ref().is_some_and(|e| e.serial == serial && e.button == id);
                let row = Frame::new()
                    .fill(if editing { Color32::from_rgb(0x1C, 0x2A, 0x1E) } else { Color32::from_rgb(0x15, 0x18, 0x1F) })
                    .corner_radius(CornerRadius::same(8))
                    .inner_margin(Margin::symmetric(8, 3))
                    .show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        ui.horizontal(|ui| {
                            Frame::new()
                                .fill(if editing { ACCENT } else { Color32::from_rgb(0x2A, 0x30, 0x3B) })
                                .corner_radius(CornerRadius::same(5))
                                .inner_margin(Margin::symmetric(7, 2))
                                .show(ui, |ui| {
                                    ui.set_min_width(124.0);
                                    ui.label(RichText::new(&label).font(theme::bold(12.5)).color(if editing { Color32::from_rgb(0x08, 0x14, 0x06) } else { TEXT }));
                                });
                            // Leave room for "Edit" so a long binding is cut short, not drawn under it.
                            let width = (ui.available_width() - 64.0).max(40.0);
                            ui.allocate_ui(egui::vec2(width, 18.0), |ui| {
                                let text = macro_label(&action, &onboard["macros"]).unwrap_or_else(|| friendly_action(&action));
                                ui.add(egui::Label::new(RichText::new(&text).size(13.0).color(if known { TEXT } else { TEXT_FAINT })).truncate()).on_hover_text(&text);
                            });
                            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                                ui.label(RichText::new("Edit").size(12.5).color(TEXT_DIM));
                                ui.label(RichText::new(theme::icon::EDIT).font(theme::icon_font(12.0)).color(TEXT_DIM));
                            });
                        });
                    });
                // The whole row opens the editor, not just the word Edit.
                if row.response.interact(egui::Sense::click()).on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                    app.button_edit = Some(start_edit(serial, id, profile, layer, &action));
                }
                ui.add_space(1.0);
            }
        });
    });
}

/// The editor for the button being changed, or a hint while none is.
fn editor(app: &mut App, ui: &mut egui::Ui, serial: &str, buttons: &[Value], beside_list: bool) -> Outcome {
    let mut outcome = None;
    let Some(edit) = app.button_edit.as_mut().filter(|e| e.serial == serial) else {
        if beside_list {
            widgets::card(ui, |ui| {
                ui.label(RichText::new("Change a button").font(theme::bold(16.0)));
                ui.label(widgets::dim("Pick a button on the left to change what it does."));
            });
        }
        return outcome;
    };
    let label = buttons
        .iter()
        .find(|b| b["id"].as_i64() == Some(edit.button))
        .map(|b| button_label(edit.button, b["name"].as_str().unwrap_or("")))
        .unwrap_or_default();
    widgets::card(ui, |ui| {
        ui.label(RichText::new(format!("{label} does…")).font(theme::bold(16.0)));
        let kinds = [
            (Kind::Mouse, "Mouse button".to_string()),
            (Kind::Keys, "Keys".to_string()),
            (Kind::Media, "Media".to_string()),
            (Kind::Dpi, "DPI".to_string()),
            (Kind::Nothing, "Nothing".to_string()),
        ];
        if widgets::segmented(ui, &kinds, &mut edit.kind) {
            edit.choice = match edit.kind {
                Kind::Mouse => "left",
                Kind::Media => "volup",
                Kind::Dpi => "cycle",
                _ => "",
            }
            .into();
        }
        ui.add_space(6.0);
        let table = match edit.kind {
            Kind::Mouse => MOUSE_ACTIONS,
            Kind::Media => MEDIA_ACTIONS,
            Kind::Dpi => DPI_ACTIONS,
            _ => &[],
        };
        if !table.is_empty() {
            let options: Vec<(String, String)> = table.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
            widgets::segmented(ui, &options, &mut edit.choice);
        }
        match edit.kind {
            Kind::Keys => {
                if edit.recording {
                    // Take the first key pressed, with whatever modifiers are held, and keep
                    // it (and its typed text) away from the rest of the window.
                    let pressed = ui.input_mut(|input| {
                        let mut found = None;
                        input.events.retain(|event| match event {
                            egui::Event::Key { key, pressed: true, modifiers, .. } => {
                                found = found.or(Some((*key, *modifiers)));
                                false
                            }
                            egui::Event::Key { .. } | egui::Event::Text(_) => false,
                            _ => true,
                        });
                        found
                    });
                    if let Some((key, modifiers)) = pressed {
                        match combo_from(key, modifiers) {
                            Some(combo) => {
                                edit.keys = combo;
                                edit.recording = false;
                                edit.note = None;
                            }
                            None => edit.note = Some(format!("{} can't be stored on the mouse. Try another key.", key.name())),
                        }
                    }
                    ui.ctx().request_repaint();
                }
                ui.horizontal(|ui| {
                    if edit.recording {
                        ui.label(RichText::new("Press the keys now…").font(theme::bold(14.0)).color(ACCENT));
                        if ui.button("Stop").clicked() {
                            edit.recording = false;
                        }
                    } else {
                        if widgets::icon_button(ui, theme::icon::RECORD, "Record keys").clicked() {
                            edit.recording = true;
                            edit.note = None;
                        }
                        ui.add(egui::TextEdit::singleline(&mut edit.keys).hint_text("or type: ctrl+shift+esc").desired_width(f32::INFINITY).font(egui::TextStyle::Monospace));
                    }
                });
                if let Some(note) = &edit.note {
                    ui.label(RichText::new(note).size(12.0).color(theme::WARN));
                }
                ui.label(RichText::new("Record: click it, then press the combo (one key plus any of Ctrl, Shift, Alt). Windows keeps the Win key to itself, so type win+… for those, or rctrl for a right-hand Ctrl.").size(12.0).color(TEXT_FAINT));
            }
            Kind::Dpi if edit.choice == "clutch" => {
                ui.horizontal(|ui| {
                    ui.label("While held:");
                    ui.add(egui::DragValue::new(&mut edit.clutch).range(100..=26000).speed(50).suffix(" DPI"));
                });
            }
            Kind::Nothing => {
                ui.label(widgets::dim("The button will do nothing at all."));
            }
            _ => {}
        }
        let action = action_for(edit);
        let risky = edit.button == 1 && action.as_deref() != Some("left");
        if risky {
            ui.add_space(4.0);
            ui.checkbox(&mut edit.confirm_left, RichText::new("Left click will stop clicking. I have another way to click, or a backup to restore with the keyboard.").color(theme::WARN));
        }
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            let ready = action.is_some() && (!risky || edit.confirm_left);
            if ui.add_enabled(ready, egui::Button::new(RichText::new("Save to mouse").font(theme::bold(14.0)))).clicked() {
                outcome = Some(action.map(|a| (edit.profile, edit.button, a, edit.layer.clone())));
            }
            if ui.button("Cancel").clicked() {
                outcome = Some(None);
            }
        });
    });
    outcome
}

/// An editor opened on what the button does now.
fn start_edit(serial: &str, button: i64, profile: i64, layer: &str, action: &str) -> ButtonEdit {
    let mut edit = ButtonEdit {
        serial: serial.to_string(),
        profile,
        button,
        layer: layer.to_string(),
        kind: Kind::Mouse,
        choice: "left".into(),
        keys: String::new(),
        clutch: 400,
        confirm_left: false,
        recording: false,
        note: None,
    };
    if MOUSE_ACTIONS.iter().any(|(k, _)| *k == action) {
        edit.choice = action.into();
    } else if action == "off" {
        edit.kind = Kind::Nothing;
    } else if let Some(keys) = action.strip_prefix("key:") {
        edit.kind = Kind::Keys;
        edit.keys = keys.into();
    } else if let Some(media) = action.strip_prefix("media:") {
        edit.kind = Kind::Media;
        edit.choice = media.into();
    } else if let Some(dpi) = action.strip_prefix("dpi:clutch:") {
        edit.kind = Kind::Dpi;
        edit.choice = "clutch".into();
        edit.clutch = dpi.parse().unwrap_or(400);
    } else if let Some(dpi) = action.strip_prefix("dpi:") {
        edit.kind = Kind::Dpi;
        edit.choice = dpi.into();
    }
    edit
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Mouse,
    Keys,
    Media,
    Dpi,
    Nothing,
}

pub struct ButtonEdit {
    pub serial: String,
    /// The onboard profile the editor was opened on; Save writes there and nowhere else.
    pub profile: i64,
    pub button: i64,
    pub layer: String,
    pub kind: Kind,
    /// The picked option for Mouse, Media and DPI.
    pub choice: String,
    pub keys: String,
    pub clutch: i64,
    pub confirm_left: bool,
    /// Waiting for a key press to fill `keys`.
    pub recording: bool,
    /// Why the last pressed key was not taken.
    pub note: Option<String>,
}

/// What a mouse button can be set to, as the daemon spells it and as people read it.
pub const MOUSE_ACTIONS: &[(&str, &str)] = &[
    ("left", "Left click"),
    ("right", "Right click"),
    ("middle", "Middle click"),
    ("back", "Back"),
    ("forward", "Forward"),
    ("double", "Double click"),
    ("wheelup", "Scroll up"),
    ("wheeldown", "Scroll down"),
    ("tiltleft", "Scroll left"),
    ("tiltright", "Scroll right"),
    ("hypershift", "Hypershift (hold)"),
    ("profile:next", "Next profile"),
    ("wheelmode", "Switch scroll mode"),
    // The Basilisk's own wheel-tilt bindings: horizontal scroll that repeats.
    ("turbo:tiltleft:20", "Scroll left, repeating"),
    ("turbo:tiltright:20", "Scroll right, repeating"),
];
pub const MEDIA_ACTIONS: &[(&str, &str)] = &[
    ("volup", "Volume up"),
    ("voldown", "Volume down"),
    ("mute", "Mute"),
    ("playpause", "Play / pause"),
    ("next", "Next track"),
    ("prev", "Previous track"),
];
pub const DPI_ACTIONS: &[(&str, &str)] = &[
    ("up", "DPI up"),
    ("down", "DPI down"),
    ("cycle", "Next DPI stage"),
    ("clutch", "Hold for a DPI"),
];

fn lookup(table: &[(&str, &str)], key: &str) -> Option<String> {
    table.iter().find(|(k, _)| *k == key).map(|(_, v)| v.to_string())
}

/// One key of a combo: `lctrl` is "Left Ctrl", `c` is "C", `f13` is "F13", `esc` is "Esc".
fn key_word(token: &str) -> String {
    for (prefix, side) in [("l", "Left"), ("r", "Right")] {
        if let Some(modifier) = token.strip_prefix(prefix).filter(|m| ["ctrl", "shift", "alt", "win"].contains(m)) {
            return format!("{side} {}", key_word(modifier));
        }
    }
    if token.len() == 1 || (token.starts_with('f') && token[1..].parse::<u8>().is_ok()) {
        return token.to_uppercase();
    }
    let mut chars = token.chars();
    chars.next().map(|c| c.to_uppercase().collect::<String>() + chars.as_str()).unwrap_or_default()
}

/// A binding as people would say it: `key:ctrl+c` is "Ctrl + C".
pub fn friendly_action(action: &str) -> String {
    if let Some(found) = lookup(MOUSE_ACTIONS, action) {
        return found;
    }
    if action == "off" {
        return "Nothing".into();
    }
    if let Some(keys) = action.strip_prefix("key:") {
        return keys.split('+').map(key_word).collect::<Vec<_>>().join(" + ");
    }
    if let Some(media) = action.strip_prefix("media:") {
        return lookup(MEDIA_ACTIONS, media).unwrap_or_else(|| action.to_string());
    }
    if let Some(dpi) = action.strip_prefix("dpi:clutch:") {
        return format!("Hold for {dpi} DPI");
    }
    if let Some(dpi) = action.strip_prefix("dpi:") {
        return lookup(DPI_ACTIONS, dpi).unwrap_or_else(|| action.to_string());
    }
    if let Some(macro_) = action.strip_prefix("macro:") {
        return format!("Macro on the mouse, {}", playback_words(macro_.rsplit(':').next().unwrap_or("")));
    }
    if let Some(turbo) = action.strip_prefix("turbo:") {
        // turbo:<button>:<rate> or turbo:key:<keys>:<rate>
        let what = turbo.rsplit_once(':').map_or(turbo, |(what, _rate)| what);
        let what = match what.strip_prefix("key:") {
            Some(keys) => friendly_action(&format!("key:{keys}")),
            None => friendly_action(what),
        };
        return format!("{what}, repeating");
    }
    if let Some(raw) = action.strip_prefix("fn=0x") {
        return format!("Set by Synapse, not decoded (fn {})", raw.get(..2).unwrap_or(raw));
    }
    action.to_string()
}

/// A physical button's name; ones the port has no name for go by number.
pub fn button_label(id: i64, name: &str) -> String {
    match name {
        "left" => "Left click".into(),
        "right" => "Right click".into(),
        "middle" => "Wheel click".into(),
        "back" => "Back (side)".into(),
        "forward" => "Forward (side)".into(),
        "wheelup" => "Scroll up".into(),
        "wheeldown" => "Scroll down".into(),
        "dpi" => "DPI button".into(),
        "clutch" => "Sensitivity clutch".into(),
        "tiltleft" => "Wheel tilt left".into(),
        "tiltright" => "Wheel tilt right".into(),
        "profile" => "Profile button".into(),
        "wheelmode" => "Wheel mode button".into(),
        _ => format!("Button 0x{id:02x}"),
    }
}

/// The colour the light under a Basilisk shows for an onboard profile slot.
/// Slot 1 is white (Synapse's default), then red, green and blue, as seen on a
/// Basilisk V3; slot 5 is cyan by the same pattern, not yet seen on hardware.
pub fn profile_colour(id: i64) -> Option<Color32> {
    match id {
        1 => Some(Color32::WHITE),
        2 => Some(Color32::from_rgb(0xFF, 0x3B, 0x3B)),
        3 => Some(Color32::from_rgb(0x44, 0xD6, 0x2C)),
        4 => Some(Color32::from_rgb(0x3B, 0x7B, 0xFF)),
        5 => Some(Color32::from_rgb(0x2C, 0xD6, 0xD6)),
        _ => None,
    }
}

/// Like `widgets::segmented`, with each profile's light colour as a dot before its name.
fn profile_picker(ui: &mut egui::Ui, options: &[(i64, String)], current: &mut i64) -> bool {
    let mut changed = false;
    Frame::new()
        .fill(Color32::from_rgb(0x12, 0x15, 0x1B))
        .stroke(egui::Stroke::new(1.0_f32, theme::BORDER))
        .corner_radius(CornerRadius::same(9))
        .inner_margin(Margin::same(3))
        .show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing.x = 3.0;
                for (value, label) in options {
                    let selected = value == current;
                    let colour = profile_colour(*value);
                    // Room on the left for the dot, drawn over the button below.
                    let text = if colour.is_some() { format!("      {label}") } else { label.clone() };
                    let text = RichText::new(text).size(13.5).color(if selected { Color32::from_rgb(0x08, 0x14, 0x06) } else { TEXT_DIM });
                    let button = egui::Button::new(if selected { text.font(theme::bold(13.5)) } else { text })
                        .fill(if selected { ACCENT } else { Color32::TRANSPARENT })
                        .stroke(egui::Stroke::NONE)
                        .corner_radius(CornerRadius::same(7))
                        .min_size(egui::vec2(44.0, 28.0));
                    let response = ui.add(button);
                    if let Some(colour) = colour {
                        let centre = egui::pos2(response.rect.left() + 15.0, response.rect.center().y);
                        ui.painter().circle(centre, 5.5, colour, egui::Stroke::new(1.5_f32, Color32::from_rgb(0x08, 0x14, 0x06)));
                    }
                    if response.on_hover_text("Switch the mouse to this profile; the light under it turns this colour").clicked() && !selected {
                        *current = *value;
                        changed = true;
                    }
                }
            });
        });
    changed
}

/// A macro binding as "<name>: <what it does>, <playback>", when the mouse told us about it.
pub fn macro_label(action: &str, macros: &Value) -> Option<String> {
    let (id, playback) = action.strip_prefix("macro:")?.split_once(':')?;
    let known = &macros[id];
    Some(format!("{}: {}, {}", known["name"].as_str()?, known["summary"].as_str()?, playback_words(playback)))
}

fn playback_words(playback: &str) -> String {
    match playback {
        "once" => "runs once".into(),
        "held" => "repeats while held".into(),
        "toggle" => "press to start or stop".into(),
        times => format!("runs {} times", times.trim_end_matches('x')),
    }
}

/// A key press as the daemon spells a combo (`ctrl+shift+esc`); `None` for a key the mouse can't store.
pub fn combo_from(key: egui::Key, modifiers: egui::Modifiers) -> Option<String> {
    use egui::Key;
    let name = match key {
        Key::Escape => "esc".to_string(),
        Key::Enter => "enter".into(),
        Key::Tab => "tab".into(),
        Key::Space => "space".into(),
        Key::Backspace => "backspace".into(),
        Key::Delete => "delete".into(),
        Key::Home => "home".into(),
        Key::End => "end".into(),
        Key::PageUp => "pageup".into(),
        Key::PageDown => "pagedown".into(),
        Key::ArrowUp => "up".into(),
        Key::ArrowDown => "down".into(),
        Key::ArrowLeft => "left".into(),
        Key::ArrowRight => "right".into(),
        other => {
            let name = other.name();
            let letter_or_digit = name.len() == 1 && name.chars().all(|c| c.is_ascii_alphanumeric());
            let f_key = name.strip_prefix('F').and_then(|n| n.parse::<u8>().ok()).is_some_and(|n| (1..=24).contains(&n));
            if !(letter_or_digit || f_key) {
                return None;
            }
            name.to_lowercase()
        }
    };
    let held = [(modifiers.ctrl, "ctrl"), (modifiers.shift, "shift"), (modifiers.alt, "alt")];
    let mut parts: Vec<String> = held.iter().filter(|(on, _)| *on).map(|(_, m)| m.to_string()).collect();
    parts.push(name);
    Some(parts.join("+"))
}

/// The daemon's action string for what the editor holds; `None` while it is incomplete.
pub fn action_for(edit: &ButtonEdit) -> Option<String> {
    match edit.kind {
        Kind::Mouse => Some(edit.choice.clone()),
        Kind::Keys => {
            let keys: String = edit.keys.chars().filter(|c| !c.is_whitespace()).collect::<String>().to_lowercase();
            (!keys.is_empty()).then(|| format!("key:{keys}"))
        }
        Kind::Media => Some(format!("media:{}", edit.choice)),
        Kind::Dpi if edit.choice == "clutch" => Some(format!("dpi:clutch:{}", edit.clutch)),
        Kind::Dpi => Some(format!("dpi:{}", edit.choice)),
        Kind::Nothing => Some("off".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn edit(kind: Kind, choice: &str, keys: &str, clutch: i64) -> ButtonEdit {
        ButtonEdit {
            serial: "s".into(),
            profile: 1,
            button: 5,
            layer: "normal".into(),
            kind,
            choice: choice.into(),
            keys: keys.into(),
            clutch,
            confirm_left: false,
            recording: false,
            note: None,
        }
    }

    #[test]
    fn actions_read_as_plain_words() {
        assert_eq!(friendly_action("middle"), "Middle click");
        assert_eq!(friendly_action("forward"), "Forward");
        assert_eq!(friendly_action("dpi:clutch:400"), "Hold for 400 DPI");
        assert_eq!(friendly_action("dpi:cycle"), "Next DPI stage");
        assert_eq!(friendly_action("key:lctrl"), "Left Ctrl");
        assert_eq!(friendly_action("key:ctrl+c"), "Ctrl + C");
        assert_eq!(friendly_action("media:volup"), "Volume up");
        assert_eq!(friendly_action("off"), "Nothing");
        assert_eq!(friendly_action("fn=0x22 data=04"), "Set by Synapse, not decoded (fn 22)");
    }

    #[test]
    fn what_synapse_stores_reads_as_plain_words() {
        assert_eq!(friendly_action("macro:1615:once"), "Macro on the mouse, runs once");
        assert_eq!(friendly_action("macro:1615:4x"), "Macro on the mouse, runs 4 times");
        assert_eq!(friendly_action("macro:0f7b:held"), "Macro on the mouse, repeats while held");
        assert_eq!(friendly_action("macro:0f7b:toggle"), "Macro on the mouse, press to start or stop");
        assert_eq!(friendly_action("profile:next"), "Next profile");
        assert_eq!(friendly_action("wheelmode"), "Switch scroll mode");
        assert_eq!(friendly_action("turbo:tiltleft:20"), "Scroll left, repeating");
        assert_eq!(friendly_action("turbo:left:142"), "Left click, repeating");
        assert_eq!(friendly_action("turbo:key:ctrl+c:142"), "Ctrl + C, repeating");
    }

    #[test]
    fn profiles_have_the_colour_of_the_light_under_the_mouse() {
        assert_eq!(profile_colour(1), Some(Color32::WHITE));
        assert_eq!(profile_colour(2), Some(Color32::from_rgb(0xFF, 0x3B, 0x3B)));
        assert_eq!(profile_colour(3), Some(Color32::from_rgb(0x44, 0xD6, 0x2C)));
        assert_eq!(profile_colour(4), Some(Color32::from_rgb(0x3B, 0x7B, 0xFF)));
        assert_eq!(profile_colour(5), Some(Color32::from_rgb(0x2C, 0xD6, 0xD6)));
        assert_eq!(profile_colour(6), None);
    }

    #[test]
    fn macros_show_what_they_do() {
        let macros = serde_json::json!({ "1615": { "name": "Macro 6", "summary": "Win + Tab" } });
        assert_eq!(macro_label("macro:1615:once", &macros).as_deref(), Some("Macro 6: Win + Tab, runs once"));
        assert_eq!(macro_label("macro:0f7b:held", &macros), None);
        assert_eq!(macro_label("middle", &macros), None);
    }

    #[test]
    fn pressed_keys_become_a_combo() {
        use egui::{Key, Modifiers};
        let ctrl_shift = Modifiers { ctrl: true, shift: true, ..Default::default() };
        assert_eq!(combo_from(Key::Escape, ctrl_shift).as_deref(), Some("ctrl+shift+esc"));
        assert_eq!(combo_from(Key::C, Modifiers { ctrl: true, ..Default::default() }).as_deref(), Some("ctrl+c"));
        assert_eq!(combo_from(Key::F13, Modifiers::default()).as_deref(), Some("f13"));
        assert_eq!(combo_from(Key::ArrowLeft, Modifiers { alt: true, ..Default::default() }).as_deref(), Some("alt+left"));
        assert_eq!(combo_from(Key::Num7, Modifiers::default()).as_deref(), Some("7"));
        assert_eq!(combo_from(Key::Semicolon, Modifiers::default()), None);
    }

    #[test]
    fn an_edit_remembers_its_profile_and_layer() {
        let edit = start_edit("s", 5, 3, "hypershift", "dpi:clutch:800");
        assert_eq!((edit.profile, edit.layer.as_str(), edit.kind, edit.clutch), (3, "hypershift", Kind::Dpi, 800));
    }

    #[test]
    fn buttons_have_names() {
        assert_eq!(button_label(4, "back"), "Back (side)");
        assert_eq!(button_label(1, "left"), "Left click");
        assert_eq!(button_label(0x77, "btn0x77"), "Button 0x77");
        assert_eq!(button_label(0x0F, "clutch"), "Sensitivity clutch");
        assert_eq!(button_label(0x34, "tiltleft"), "Wheel tilt left");
        assert_eq!(button_label(0x0E, "profile"), "Profile button");
        assert_eq!(button_label(0x6A, "wheelmode"), "Wheel mode button");
    }

    #[test]
    fn edits_become_daemon_actions() {
        assert_eq!(action_for(&edit(Kind::Mouse, "middle", "", 0)).as_deref(), Some("middle"));
        assert_eq!(action_for(&edit(Kind::Keys, "", " Ctrl + Shift + Esc ", 0)).as_deref(), Some("key:ctrl+shift+esc"));
        assert_eq!(action_for(&edit(Kind::Keys, "", "  ", 0)), None);
        assert_eq!(action_for(&edit(Kind::Media, "volup", "", 0)).as_deref(), Some("media:volup"));
        assert_eq!(action_for(&edit(Kind::Dpi, "clutch", "", 800)).as_deref(), Some("dpi:clutch:800"));
        assert_eq!(action_for(&edit(Kind::Dpi, "up", "", 0)).as_deref(), Some("dpi:up"));
        assert_eq!(action_for(&edit(Kind::Nothing, "", "", 0)).as_deref(), Some("off"));
    }
}
