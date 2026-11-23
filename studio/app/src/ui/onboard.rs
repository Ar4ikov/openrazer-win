//! Buttons stored in the mouse itself: its onboard profiles, what each button
//! does on each layer, and changing one.
//!
//! Unlike the side-button reassignment in `macros`, which a hook does on the PC,
//! these live in the mouse and work on any computer with nothing running. Only
//! mice whose capabilities say `onboard_buttons` get the panel.

use super::app::{title, App};
use crate::worker::{Command, Device, Snapshot, Then};
use egui::{vec2, Align, Color32, CornerRadius, Frame, Layout, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, ON_ACCENT, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::widgets::{self, Button, IconButton};
use openrazer_studio_core::{t, tf};
use serde_json::{json, Value};

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

/// What Save or Cancel in the editor asked for: `Some(None)` closes it,
/// `Some(Some((profile, button, action, layer)))` writes and closes.
type Outcome = Option<Option<(i64, i64, String, String)>>;

pub fn panel(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let serial = device.serial.clone();
    let heading = tf("{} · on the mouse", &[&title(device, snap)]);
    let Some(onboard) = snap.details.get(&serial).and_then(|d| d.onboard.clone()) else {
        widgets::panel(ui, &heading, |ui| ui.label(widgets::dim(t("Reading the buttons from the mouse…"))));
        return;
    };
    let profile = onboard["profile"].as_i64().unwrap_or(1);
    let active = onboard["profiles"]["active"].as_i64();
    // The page moved to another profile under an open editor (a reload, or the
    // mouse's own profile button): what it was showing no longer applies.
    if app.device_ui.onboard_edit.as_ref().is_some_and(|e| e.serial == serial && e.profile != profile) {
        app.device_ui.onboard_edit = None;
    }
    let layer = if app.device_ui.onboard_hypershift { "hypershift" } else { "normal" };
    let buttons: Vec<Value> = onboard["buttons"].as_array().cloned().unwrap_or_default();

    let mut outcome: Outcome = None;
    widgets::panel_with(
        ui,
        &heading,
        |ui| {
            widgets::badge(ui, t("Saved in the mouse"), ACCENT).on_hover_text(t("These work on any computer, with nothing running."));
        },
        |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing.x = theme::GAP;
                widgets::stacked(ui, t("Profile"), |ui| {
                    let options: Vec<(i64, String)> = onboard["profiles"]["profiles"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|p| {
                            let id = p["id"].as_i64()?;
                            let name = p["name"].as_str().map_or_else(|| id.to_string(), |n| format!("“{n}”"));
                            Some((id, if Some(id) == active { tf("{} · active", &[&name]) } else { name }))
                        })
                        .collect();
                    let mut picked = profile;
                    if profile_picker(ui, &options, &mut picked) {
                        app.device_ui.onboard_edit = None;
                        // Picking a profile switches the mouse to it, as its own button
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
                widgets::stacked(ui, t("Layer"), |ui| {
                    let options = [(false, t("Normal").to_string()), (true, t("Hypershift (held)").to_string())];
                    if widgets::segmented(ui, &options, &mut app.device_ui.onboard_hypershift) {
                        app.device_ui.onboard_edit = None;
                    }
                });
            });
            ui.add_space(theme::SPACING);
            // Side by side when there is room, so the editor is on screen the
            // moment a button is picked; one above the other when not.
            widgets::columns(ui, 0.55, 760.0, |ui, column| {
                if column == 0 {
                    button_list(app, ui, &serial, profile, layer, &onboard);
                } else {
                    outcome = editor(app, ui, &serial, &buttons);
                }
            });
            ui.add_space(theme::SPACING);
            ui.horizontal(|ui| {
                if ui.add(Button::secondary(t("Back up profile…")).icon(icon::SAVE).small()).clicked() {
                    if let Some(path) = rfd::FileDialog::new()
                        .set_title(t("Back up this profile's buttons"))
                        .set_file_name(format!("buttons-profile-{profile}.json"))
                        .add_filter(t("Button backup"), &["json"])
                        .save_file()
                    {
                        app.send(Command::ButtonsBackup { serial: serial.clone(), profile, path });
                    }
                }
                if ui.add(Button::secondary(t("Restore from file…")).icon(icon::FOLDER).small()).clicked() {
                    if let Some(path) = rfd::FileDialog::new().set_title(t("Restore buttons from a backup")).add_filter(t("Button backup"), &["json"]).pick_file() {
                        app.device_ui.onboard_edit = None;
                        app.send(Command::ButtonsRestore { serial: serial.clone(), path });
                    }
                }
                ui.label(widgets::faint(t("A backup holds one profile, both layers.")));
            });
        },
    );
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
        app.device_ui.onboard_edit = None;
    }
}

/// The buttons, one compact row each, scrolling on their own.
fn button_list(app: &mut App, ui: &mut egui::Ui, serial: &str, profile: i64, layer: &str, onboard: &Value) {
    let buttons = onboard["buttons"].as_array().cloned().unwrap_or_default();
    let bindings = onboard["bindings"][layer].as_array().cloned().unwrap_or_default();
    // A column has no height of its own to grow into, so the list is given one.
    let height = (buttons.len() as f32 * 38.0).min(340.0);
    egui::ScrollArea::vertical().id_salt("onboard-buttons").min_scrolled_height(height).max_height(height).auto_shrink([false, true]).show(ui, |ui| {
        ui.spacing_mut().item_spacing.y = 4.0;
        for (index, button) in buttons.iter().enumerate() {
            let id = button["id"].as_i64().unwrap_or(0);
            let label = button_label(id, button["name"].as_str().unwrap_or(""));
            let action = bindings.get(index).and_then(|b| b["action"].as_str()).unwrap_or("").to_string();
            let known = !action.starts_with("fn=");
            let editing = app.device_ui.onboard_edit.as_ref().is_some_and(|e| e.serial == serial && e.button == id);
            let row = Frame::new()
                .fill(if editing { theme::SURFACE_ACTIVE } else { theme::SURFACE_HI })
                .corner_radius(CornerRadius::same(theme::RADIUS_SM))
                .inner_margin(Margin::symmetric(8, 4))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 10.0;
                        widgets::keycap(ui, &label, 140.0, editing);
                        let text = macro_label(&action, &onboard["macros"]).unwrap_or_else(|| friendly_action(&action));
                        let room = (ui.available_width() - 40.0).max(40.0);
                        let (rect, response) = ui.allocate_exact_size(vec2(room, 26.0), Sense::hover());
                        let colour = if known { TEXT } else { TEXT_FAINT };
                        let galley = widgets::galley_fit(ui, &text, theme::body(13.0), colour, room);
                        ui.painter().galley(egui::pos2(rect.left(), rect.center().y - galley.size().y / 2.0), galley, colour);
                        response.on_hover_text(&text);
                        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                            widgets::icon(ui, icon::EDIT, 13.0, TEXT_DIM);
                        });
                    });
                });
            // The whole row opens the editor.
            if row.response.interact(Sense::click()).on_hover_cursor(egui::CursorIcon::PointingHand).on_hover_text(t("Change what this button does")).clicked() {
                app.device_ui.onboard_edit = Some(start_edit(serial, id, profile, layer, &action));
            }
        }
    });
}

/// The editor for the button being changed, or a hint while none is.
fn editor(app: &mut App, ui: &mut egui::Ui, serial: &str, buttons: &[Value]) -> Outcome {
    let mut outcome = None;
    let Some(edit) = app.device_ui.onboard_edit.as_mut().filter(|e| e.serial == serial) else {
        Frame::new().fill(theme::WELL).corner_radius(CornerRadius::same(theme::RADIUS)).inner_margin(Margin::same(theme::PAD)).show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.label(widgets::dim(t("Pick a button to change what it does.")));
        });
        return outcome;
    };
    let label = buttons
        .iter()
        .find(|b| b["id"].as_i64() == Some(edit.button))
        .map(|b| button_label(edit.button, b["name"].as_str().unwrap_or("")))
        .unwrap_or_default();
    Frame::new().fill(theme::WELL).corner_radius(CornerRadius::same(theme::RADIUS)).inner_margin(Margin::same(theme::PAD)).show(ui, |ui| {
        ui.set_width(ui.available_width());
        ui.spacing_mut().item_spacing.y = theme::SPACING;
        ui.label(RichText::new(tf("{} does…", &[&label])).font(theme::bold(15.0)).color(TEXT));
        let kinds = [
            (Kind::Mouse, t("Mouse button").to_string()),
            (Kind::Keys, t("Keys").to_string()),
            (Kind::Media, t("Media").to_string()),
            (Kind::Dpi, t("DPI").to_string()),
            (Kind::Nothing, t("Nothing").to_string()),
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
        let table = match edit.kind {
            Kind::Mouse => mouse_actions().to_vec(),
            Kind::Media => media_actions().to_vec(),
            Kind::Dpi => dpi_actions().to_vec(),
            _ => vec![],
        };
        if !table.is_empty() {
            let options: Vec<(String, String)> = table.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
            choice_grid(ui, &options, &mut edit.choice);
        }
        match edit.kind {
            Kind::Keys => keys_editor(ui, edit),
            Kind::Dpi if edit.choice == "clutch" => {
                ui.horizontal(|ui| {
                    ui.label(widgets::dim(t("While held:")));
                    ui.add(egui::DragValue::new(&mut edit.clutch).range(100..=26000).speed(50).suffix(" DPI"));
                });
            }
            Kind::Nothing => {
                ui.label(widgets::dim(t("The button will do nothing at all.")));
            }
            _ => {}
        }
        let action = action_for(edit);
        let risky = edit.button == 1 && action.as_deref() != Some("left");
        if risky {
            ui.checkbox(&mut edit.confirm_left, RichText::new(t("Left click will stop clicking. I have another way to click, or a backup to restore with the keyboard.")).color(theme::WARN));
        }
        ui.horizontal(|ui| {
            let ready = action.is_some() && (!risky || edit.confirm_left);
            if ui.add(Button::primary(t("Save to mouse")).enabled(ready)).clicked() {
                outcome = Some(action.map(|a| (edit.profile, edit.button, a, edit.layer.clone())));
            }
            if ui.add(Button::ghost(t("Cancel"))).clicked() {
                outcome = Some(None);
            }
        });
    });
    outcome
}

/// The choices of one kind, as a wrapping set of buttons: there are too many
/// mouse actions for a single segmented row.
fn choice_grid(ui: &mut egui::Ui, options: &[(String, String)], current: &mut String) {
    ui.horizontal_wrapped(|ui| {
        ui.spacing_mut().item_spacing = vec2(4.0, 4.0);
        for (value, label) in options {
            let selected = value == current;
            let text = RichText::new(label).size(13.0).color(if selected { ON_ACCENT } else { TEXT_DIM });
            let button = egui::Button::new(text)
                .fill(if selected { ACCENT } else { theme::SURFACE_HI })
                .stroke(Stroke::NONE)
                .corner_radius(CornerRadius::same(theme::RADIUS_SM))
                .min_size(vec2(0.0, theme::CONTROL_H_SM));
            if ui.add(button).clicked() {
                *current = value.clone();
            }
        }
    });
}

/// A key combo, recorded by pressing it or typed.
fn keys_editor(ui: &mut egui::Ui, edit: &mut ButtonEdit) {
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
                None => edit.note = Some(tf("{} can't be stored on the mouse. Try another key.", &[&key.name()])),
            }
        }
        ui.ctx().request_repaint();
    }
    ui.horizontal(|ui| {
        if edit.recording {
            ui.label(RichText::new(t("Press the keys now…")).font(theme::bold(14.0)).color(ACCENT));
            if ui.add(Button::secondary(t("Stop")).small()).clicked() {
                edit.recording = false;
            }
        } else {
            if ui.add(IconButton::new(icon::RECORD, t("Record keys: click, then press the combo"))).clicked() {
                edit.recording = true;
                edit.note = None;
            }
            let width = ui.available_width();
            widgets::text_field(ui, &mut edit.keys, t("or type: ctrl+shift+esc"), width);
        }
    });
    if let Some(note) = &edit.note {
        ui.label(RichText::new(note).size(12.0).color(theme::WARN));
    }
    ui.label(widgets::faint(t("One key plus any of Ctrl, Shift and Alt. Windows keeps the Win key to itself, so type win+… for those, or rctrl for a right-hand Ctrl.")));
}

/// Like `widgets::segmented`, with a dot in each profile's light colour.
fn profile_picker(ui: &mut egui::Ui, options: &[(i64, String)], current: &mut i64) -> bool {
    let mut changed = false;
    Frame::new()
        .fill(theme::WELL)
        .stroke(Stroke::new(1.0_f32, theme::BORDER))
        .corner_radius(CornerRadius::same(theme::RADIUS))
        .inner_margin(Margin::same(3))
        .show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing.x = 3.0;
                for (value, label) in options {
                    let selected = value == current;
                    let colour = profile_colour(*value);
                    // Room on the left for the dot, drawn over the button below.
                    let text = if colour.is_some() { format!("      {label}") } else { label.clone() };
                    let text = RichText::new(text).size(13.5).color(if selected { ON_ACCENT } else { TEXT_DIM });
                    let button = egui::Button::new(if selected { text.font(theme::bold(13.5)) } else { text })
                        .fill(if selected { ACCENT } else { Color32::TRANSPARENT })
                        .stroke(Stroke::NONE)
                        .corner_radius(CornerRadius::same(theme::RADIUS_SM))
                        .min_size(vec2(44.0, theme::CONTROL_H_SM));
                    let response = ui.add(button);
                    if let Some(colour) = colour {
                        let centre = egui::pos2(response.rect.left() + 15.0, response.rect.center().y);
                        ui.painter().circle(centre, 5.5, colour, Stroke::new(1.5_f32, ON_ACCENT));
                    }
                    if response.on_hover_text(t("Switch the mouse to this profile; the light under it turns this colour")).clicked() && !selected {
                        *current = *value;
                        changed = true;
                    }
                }
            });
        });
    changed
}

/// What a mouse button can be set to, as the daemon spells it and as people read it.
fn mouse_actions() -> [(&'static str, &'static str); 15] {
    [
        ("left", t("Left click")),
        ("right", t("Right click")),
        ("middle", t("Middle click")),
        ("back", t("Back")),
        ("forward", t("Forward")),
        ("double", t("Double click")),
        ("wheelup", t("Scroll up")),
        ("wheeldown", t("Scroll down")),
        ("tiltleft", t("Scroll left")),
        ("tiltright", t("Scroll right")),
        ("hypershift", t("Hypershift (hold)")),
        ("profile:next", t("Next profile")),
        ("wheelmode", t("Switch scroll mode")),
        // The Basilisk's own wheel-tilt bindings: horizontal scroll that repeats.
        ("turbo:tiltleft:20", t("Scroll left, repeating")),
        ("turbo:tiltright:20", t("Scroll right, repeating")),
    ]
}

fn media_actions() -> [(&'static str, &'static str); 6] {
    [
        ("volup", t("Volume up")),
        ("voldown", t("Volume down")),
        ("mute", t("Mute")),
        ("playpause", t("Play/Pause")),
        ("next", t("Next track")),
        ("prev", t("Previous track")),
    ]
}

fn dpi_actions() -> [(&'static str, &'static str); 4] {
    [("up", t("DPI up")), ("down", t("DPI down")), ("cycle", t("Next DPI stage")), ("clutch", t("Hold for a DPI"))]
}

fn lookup(table: &[(&str, &'static str)], key: &str) -> Option<String> {
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

fn playback_words(playback: &str) -> String {
    match playback {
        "once" => t("runs once").into(),
        "held" => t("repeats while held").into(),
        "toggle" => t("press to start or stop").into(),
        times => tf("runs {} times", &[&times.trim_end_matches('x')]),
    }
}

/// A binding as people would say it: `key:ctrl+c` is "Ctrl + C".
pub fn friendly_action(action: &str) -> String {
    if let Some(found) = lookup(&mouse_actions(), action) {
        return found;
    }
    if action == "off" {
        return t("Nothing").into();
    }
    if let Some(keys) = action.strip_prefix("key:") {
        return keys.split('+').map(key_word).collect::<Vec<_>>().join(" + ");
    }
    if let Some(media) = action.strip_prefix("media:") {
        return lookup(&media_actions(), media).unwrap_or_else(|| action.to_string());
    }
    if let Some(dpi) = action.strip_prefix("dpi:clutch:") {
        return tf("Hold for {} DPI", &[&dpi]);
    }
    if let Some(dpi) = action.strip_prefix("dpi:") {
        return lookup(&dpi_actions(), dpi).unwrap_or_else(|| action.to_string());
    }
    if let Some(macro_) = action.strip_prefix("macro:") {
        return tf("Macro on the mouse, {}", &[&playback_words(macro_.rsplit(':').next().unwrap_or(""))]);
    }
    if let Some(turbo) = action.strip_prefix("turbo:") {
        // turbo:<button>:<rate> or turbo:key:<keys>:<rate>
        let what = turbo.rsplit_once(':').map_or(turbo, |(what, _rate)| what);
        let what = match what.strip_prefix("key:") {
            Some(keys) => friendly_action(&format!("key:{keys}")),
            None => friendly_action(what),
        };
        return tf("{}, repeating", &[&what]);
    }
    if let Some(raw) = action.strip_prefix("fn=0x") {
        return tf("Set by Synapse, not decoded (fn {})", &[&raw.get(..2).unwrap_or(raw)]);
    }
    action.to_string()
}

/// A physical button's name; ones the port has no name for go by number.
pub fn button_label(id: i64, name: &str) -> String {
    match name {
        "left" => t("Left click").into(),
        "right" => t("Right click").into(),
        "middle" => t("Wheel click").into(),
        "back" => t("Back (side)").into(),
        "forward" => t("Forward (side)").into(),
        "wheelup" => t("Scroll up").into(),
        "wheeldown" => t("Scroll down").into(),
        "dpi" => t("DPI button").into(),
        "clutch" => t("Sensitivity clutch").into(),
        "tiltleft" => t("Wheel tilt left").into(),
        "tiltright" => t("Wheel tilt right").into(),
        "profile" => t("Profile button").into(),
        "wheelmode" => t("Wheel mode button").into(),
        "rightback" => t("Right-hand back").into(),
        "rightforward" => t("Right-hand forward").into(),
        _ => tf("Button 0x{}", &[&format!("{id:02x}")]),
    }
}

/// The colour the light under a Basilisk shows for an onboard profile slot:
/// white (Synapse's default), red, green and blue, as seen on a Basilisk V3;
/// slot 5 is cyan by the same pattern, not yet seen on hardware.
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

/// A macro binding as "<name>: <what it does>, <playback>", when the mouse told us about it.
pub fn macro_label(action: &str, macros: &Value) -> Option<String> {
    let (id, playback) = action.strip_prefix("macro:")?.split_once(':')?;
    let known = &macros[id];
    Some(format!("{}: {}, {}", known["name"].as_str()?, known["summary"].as_str()?, playback_words(playback)))
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
    if mouse_actions().iter().any(|(k, _)| *k == action) {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn edit(kind: Kind, choice: &str, keys: &str, clutch: i64) -> ButtonEdit {
        let mut edit = start_edit("s", 5, 1, "normal", "forward");
        edit.kind = kind;
        edit.choice = choice.into();
        edit.keys = keys.into();
        edit.clutch = clutch;
        edit
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
    fn buttons_have_names() {
        assert_eq!(button_label(4, "back"), "Back (side)");
        assert_eq!(button_label(1, "left"), "Left click");
        assert_eq!(button_label(0x77, "btn0x77"), "Button 0x77");
        assert_eq!(button_label(0x0F, "clutch"), "Sensitivity clutch");
        assert_eq!(button_label(0x0E, "profile"), "Profile button");
        assert_eq!(button_label(0x06, "rightback"), "Right-hand back");
    }

    #[test]
    fn profiles_have_the_colour_of_the_light_under_the_mouse() {
        assert_eq!(profile_colour(1), Some(Color32::WHITE));
        assert_eq!(profile_colour(2), Some(Color32::from_rgb(0xFF, 0x3B, 0x3B)));
        assert_eq!(profile_colour(4), Some(Color32::from_rgb(0x3B, 0x7B, 0xFF)));
        assert_eq!(profile_colour(6), None);
    }

    #[test]
    fn macros_show_what_they_do() {
        let macros = json!({ "1615": { "name": "Macro 6", "summary": "Win + Tab" } });
        assert_eq!(macro_label("macro:1615:once", &macros).as_deref(), Some("Macro 6: Win + Tab, runs once"));
        assert_eq!(macro_label("macro:0f7b:held", &macros), None);
        assert_eq!(macro_label("middle", &macros), None);
    }

    #[test]
    fn pressed_keys_become_a_combo() {
        use egui::{Key, Modifiers};
        let ctrl_shift = Modifiers { ctrl: true, shift: true, ..Default::default() };
        assert_eq!(combo_from(Key::Escape, ctrl_shift).as_deref(), Some("ctrl+shift+esc"));
        assert_eq!(combo_from(Key::F13, Modifiers::default()).as_deref(), Some("f13"));
        assert_eq!(combo_from(Key::ArrowLeft, Modifiers { alt: true, ..Default::default() }).as_deref(), Some("alt+left"));
        assert_eq!(combo_from(Key::Semicolon, Modifiers::default()), None);
    }

    #[test]
    fn edits_become_daemon_actions() {
        assert_eq!(action_for(&edit(Kind::Mouse, "middle", "", 0)).as_deref(), Some("middle"));
        assert_eq!(action_for(&edit(Kind::Keys, "", " Ctrl + Shift + Esc ", 0)).as_deref(), Some("key:ctrl+shift+esc"));
        assert_eq!(action_for(&edit(Kind::Keys, "", "  ", 0)), None);
        assert_eq!(action_for(&edit(Kind::Media, "volup", "", 0)).as_deref(), Some("media:volup"));
        assert_eq!(action_for(&edit(Kind::Dpi, "clutch", "", 800)).as_deref(), Some("dpi:clutch:800"));
        assert_eq!(action_for(&edit(Kind::Nothing, "", "", 0)).as_deref(), Some("off"));
    }

    #[test]
    fn an_edit_remembers_its_profile_and_layer() {
        let edit = start_edit("s", 5, 3, "hypershift", "dpi:clutch:800");
        assert_eq!((edit.profile, edit.layer.as_str(), edit.kind, edit.clutch), (3, "hypershift", Kind::Dpi, 800));
    }
}
