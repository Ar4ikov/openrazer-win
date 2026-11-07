//! Snap Tap: of two opposing keys, the one pressed last wins.
//!
//! The daemon does it on the PC, for every keyboard at once, so this panel
//! shows the same thing on every keyboard's page. Its settings are the
//! daemon's: nothing is kept here, and every change goes straight to it.
//! The panel is two rows like any other: the keys, and the programs left alone.

use super::app::App;
use crate::worker::{Command, Device, Snapshot};
use egui::{vec2, Align, Align2, Color32, CornerRadius, Frame, Layout, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, CONTROL_H, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::{t, tf};
use openrazer_studio_core::widgets::{self, Button, IconButton};
use serde_json::{json, Value};

const KEY_W: f32 = 130.0;
const SWITCH_W: f32 = 16.0;
/// A pair across, so that every row's button falls in the same column.
const LIST_W: f32 = KEY_W * 2.0 + SWITCH_W + 16.0;
/// Where text starts inside a box, for text in a row of boxes.
const TEXT_INSET: f32 = 10.0;

/// The hook sees keys, not keyboards, so any of them will do.
pub fn shown_on(device: &Device) -> bool {
    device.kind == "keyboard" || device.kind == "keypad"
}

/// `KEY_LEFTBRACE` as a person would say it, near enough.
fn key_label(name: &str) -> String {
    name.strip_prefix("KEY_").unwrap_or(name).to_string()
}

fn pairs_of(status: &Value) -> Vec<[String; 2]> {
    let key = |pair: &Value, index: usize| pair[index].as_str().unwrap_or_default().to_string();
    status["pairs"].as_array().map(|rows| rows.iter().map(|pair| [key(pair, 0), key(pair, 1)]).collect()).unwrap_or_default()
}

fn denied_of(status: &Value) -> Vec<String> {
    status["deny"].as_array().map(|rows| rows.iter().filter_map(|name| name.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

/// A typed name the way the daemon will keep it.
fn program_name(typed: &str) -> String {
    typed.trim().to_lowercase()
}

fn with_added(denied: &[String], name: &str) -> Vec<String> {
    denied.iter().cloned().chain([name.to_string()]).collect()
}

fn without(denied: &[String], index: usize) -> Vec<String> {
    denied.iter().enumerate().filter(|(at, _)| *at != index).map(|(_, name)| name.clone()).collect()
}

/// Two keys not yet in a pair, for a new row to start from.
fn free_keys(keys: &[String], pairs: &[[String; 2]]) -> Option<[String; 2]> {
    let mut free = keys.iter().filter(|key| !pairs.iter().flatten().any(|used| used == *key));
    Some([free.next()?.clone(), free.next()?.clone()])
}

pub fn panel(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    if !shown_on(device) {
        return;
    }
    let status = &snap.snaptap;
    let confirming = egui::Id::new("snaptap-confirm");
    let mut switched = None;
    widgets::panel_with(
        ui,
        "Snap Tap",
        |ui| {
            if status.is_object() {
                // The title row keeps 10 at its end: this lines the switch up with the rows below.
                ui.add_space(theme::PAD as f32 - 10.0);
                let mut on = status["enabled"] == true;
                if widgets::toggle(ui, &mut on).changed() {
                    switched = Some(on);
                }
            }
        },
        |ui| {
            if !status.is_object() {
                ui.label(widgets::faint(t("Needs a newer daemon, running")));
                return;
            }
            if let Some((colour, text)) = attention(status) {
                widgets::status_dot(ui, colour, &text);
            }
            pair_rows(app, ui, status);
            widgets::divider(ui);
            deny_rows(app, ui, status, snap.snaptap_deny_error.as_deref());
        },
    );
    match switched {
        Some(true) if status["acknowledged"] != true => ui.ctx().data_mut(|data| data.insert_temp(confirming, true)),
        Some(on) => app.send(Command::SnapTap(json!({ "enabled": on }))),
        None => {}
    }
    if ui.ctx().data(|data| data.get_temp::<bool>(confirming)).unwrap_or(false) {
        warning_dialog(app, ui, confirming);
    }
}

/// What is wrong, if anything: the switch says the rest.
fn attention(status: &Value) -> Option<(Color32, String)> {
    if status["enabled"] != true {
        return None;
    }
    if status["running"] != true {
        return Some((theme::WARN, t("Not working: the key filter is not available").to_string()));
    }
    let program = status["foreground"].as_str().unwrap_or_default();
    let text = match status["paused"].as_str()? {
        "denied" => tf("Paused while {} is in front", &[&program]),
        "not allowed" => tf("Paused: {} is not on the allow list", &[&program]),
        _ => t("Paused: can't tell which program is in front").to_string(),
    };
    Some((theme::INFO, text))
}

/// One key box of a pair: row, then side.
type Slot = (usize, usize);

/// Whether a key box is waiting for a key press.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
enum Listen {
    #[default]
    Off,
    /// `fresh` is a pair just added with stand-in keys: it is not kept unless both its keys get chosen.
    On { slot: Slot, fresh: bool },
}

/// What can happen to a box that is listening.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Heard {
    Key(egui::Key),
    Esc,
    /// A click on a key box.
    Click(Slot),
    /// A click on anything else, or leaving the window.
    Away,
}

/// What the panel must do about it.
#[derive(Clone, Debug, PartialEq)]
enum Effect {
    Nothing,
    Set(Slot, String),
    /// A key the daemon has no name for: said so, and still listening.
    Unknown,
    /// A key already in a pair (the daemon's rule): said so, and still listening.
    Taken,
    /// Take back a pair that was only a stand-in.
    Drop(usize),
}

/// The daemon's name for a physical key, by the key's position and not what it types:
/// egui gives the key the board sends (winit's physical key) and the names follow the
/// scan codes of `openrazer_win/keys/codes.py`. The number pad cannot be told from the
/// main block, and modifiers, Pause, Print Screen and the lock keys are not egui keys at all.
fn key_name(key: egui::Key) -> Option<String> {
    use egui::Key as K;
    let fixed = match key {
        K::Minus => "MINUS",
        K::Equals => "EQUAL",
        K::OpenBracket => "LEFTBRACE",
        K::CloseBracket => "RIGHTBRACE",
        K::Semicolon => "SEMICOLON",
        K::Quote => "APOSTROPHE",
        K::Backtick => "GRAVE",
        K::Backslash => "BACKSLASH",
        K::Comma => "COMMA",
        K::Period => "DOT",
        K::Slash => "SLASH",
        K::Space => "SPACE",
        K::Tab => "TAB",
        K::Backspace => "BACKSPACE",
        K::Enter => "ENTER",
        K::Insert => "INSERT",
        K::Delete => "DELETE",
        K::Home => "HOME",
        K::End => "END",
        K::PageUp => "PAGEUP",
        K::PageDown => "PAGEDOWN",
        K::ArrowUp => "UP",
        K::ArrowDown => "DOWN",
        K::ArrowLeft => "LEFT",
        K::ArrowRight => "RIGHT",
        _ => "",
    };
    if !fixed.is_empty() {
        return Some(format!("KEY_{fixed}"));
    }
    // `A`, `7`, `F5`: the letters and numbers are the key's own name.
    let name = key.name();
    let digit = Some(name).filter(|name| name.len() == 1 && name.as_bytes()[0].is_ascii_digit());
    let function = name.strip_prefix('F').and_then(|n| n.parse::<u8>().ok()).filter(|n| (1..=24).contains(n));
    match (digit, function) {
        (Some(digit), _) => Some(format!("KEY_{digit}")),
        (_, Some(n)) => Some(format!("KEY_F{n}")),
        _ if name.len() == 1 && name.as_bytes()[0].is_ascii_uppercase() => Some(format!("KEY_{name}")),
        _ => None,
    }
}

/// The key pressed, by position when the platform says where it is.
fn pressed_key(key: egui::Key, physical: Option<egui::Key>) -> egui::Key {
    physical.unwrap_or(key)
}

/// Takes the key presses out of `events` (so nothing else reacts to them) and says which keys went down.
fn take_presses(events: &mut Vec<egui::Event>) -> Vec<egui::Key> {
    let mut down = Vec::new();
    events.retain(|event| match event {
        egui::Event::Key { key, physical_key, pressed, repeat, .. } => {
            if *pressed && !*repeat {
                down.push(pressed_key(*key, *physical_key));
            }
            false
        }
        _ => true,
    });
    down
}

/// The listening state machine: what `heard` does to `listen`, given the daemon's `keys` and the current `pairs`.
fn step(listen: Listen, heard: Heard, keys: &[String], pairs: &[[String; 2]]) -> (Listen, Effect) {
    let drop_fresh = |slot: Slot, fresh: bool| if fresh && slot.0 < pairs.len() { Effect::Drop(slot.0) } else { Effect::Nothing };
    match (listen, heard) {
        (Listen::Off, Heard::Click(slot)) => (Listen::On { slot, fresh: false }, Effect::Nothing),
        (Listen::Off, _) => (Listen::Off, Effect::Nothing),
        (Listen::On { slot, .. }, Heard::Click(again)) if slot == again => (listen, Effect::Nothing),
        (Listen::On { slot, fresh }, Heard::Click(other)) => {
            // Rows after a dropped one move up.
            let other = if fresh && slot.0 < other.0 && slot.0 < pairs.len() { (other.0 - 1, other.1) } else { other };
            (Listen::On { slot: other, fresh: false }, drop_fresh(slot, fresh))
        }
        (Listen::On { slot, fresh }, Heard::Esc | Heard::Away) => (Listen::Off, drop_fresh(slot, fresh)),
        (Listen::On { slot, fresh }, Heard::Key(key)) => {
            // A pair added a moment ago is not there to change until the daemon has answered.
            let Some(pair) = pairs.get(slot.0) else { return (listen, Effect::Nothing) };
            let Some(name) = key_name(key).filter(|name| keys.contains(name)) else { return (listen, Effect::Unknown) };
            let next = if fresh && slot.1 == 0 { Listen::On { slot: (slot.0, 1), fresh } } else { Listen::Off };
            if pair[slot.1] == name {
                return (next, Effect::Nothing);
            }
            if pairs.iter().flatten().any(|used| *used == name) {
                return (listen, Effect::Taken);
            }
            (next, Effect::Set(slot, name))
        }
    }
}

/// A short word about a key that was not taken.
#[derive(Clone)]
struct Hint {
    text: String,
    until: f64,
}

const HINT_SECONDS: f64 = 2.5;

fn pair_rows(app: &mut App, ui: &mut egui::Ui, status: &Value) {
    let keys: Vec<String> = status["keys"].as_array().map(|rows| rows.iter().filter_map(|key| key.as_str().map(str::to_string)).collect()).unwrap_or_default();
    let mut pairs = pairs_of(status);
    let spare = free_keys(&keys, &pairs);
    let ctx = ui.ctx().clone();
    let listen_id = egui::Id::new("snaptap-listen");
    let hint_id = egui::Id::new("snaptap-hint");
    let now = ctx.input(|i| i.time);
    let mut listen: Listen = ctx.data(|data| data.get_temp(listen_id)).unwrap_or_default();
    let mut hint: Option<Hint> = ctx.data(|data| data.get_temp::<Option<Hint>>(hint_id)).flatten().filter(|hint| hint.until > now);
    let mut changed = false;
    let mut remove = None;
    let mut clicked_slot = None;
    let mut add = false;

    let apply = |listen: &mut Listen, pairs: &mut Vec<[String; 2]>, heard: Heard, changed: &mut bool, hint: &mut Option<Hint>| {
        let (next, effect) = step(*listen, heard, &keys, pairs);
        *listen = next;
        let say = |text: &str| Some(Hint { text: text.to_string(), until: now + HINT_SECONDS });
        match effect {
            Effect::Nothing => {}
            Effect::Set((row, side), name) => {
                pairs[row][side] = name;
                *changed = true;
                *hint = None;
            }
            Effect::Unknown => *hint = say(t("That key can't be used")),
            Effect::Taken => *hint = say(t("That key is already in a pair")),
            Effect::Drop(row) => {
                pairs.remove(row);
                *changed = true;
            }
        }
    };
    // Keys go to the listening box first, before anything else can use them.
    if listen != Listen::Off {
        let presses = ctx.input_mut(|i| take_presses(&mut i.events));
        for key in presses {
            let heard = if key == egui::Key::Escape { Heard::Esc } else { Heard::Key(key) };
            apply(&mut listen, &mut pairs, heard, &mut changed, &mut hint);
        }
        if !ctx.input(|i| i.focused) {
            apply(&mut listen, &mut pairs, Heard::Away, &mut changed, &mut hint);
        }
    }

    widgets::row(ui, t("Opposing keys"), t("When two opposing keys are held, only the last one pressed counts. Let go of it and the other takes over. Tip: helps you strafe faster in shooters."), |ui| {
        ui.vertical(|ui| {
            for (index, pair) in pairs.iter().enumerate() {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    let waiting = |side: usize| matches!(listen, Listen::On { slot, .. } if slot == (index, side));
                    if key_slot(ui, &pair[0], waiting(0)).clicked() {
                        clicked_slot = Some((index, 0));
                    }
                    let (rect, _) = ui.allocate_exact_size(vec2(SWITCH_W, CONTROL_H), Sense::hover());
                    ui.painter().text(rect.center(), Align2::CENTER_CENTER, icon::SWITCH, theme::icon_font(14.0), TEXT_FAINT);
                    if key_slot(ui, &pair[1], waiting(1)).clicked() {
                        clicked_slot = Some((index, 1));
                    }
                    if ui.add(IconButton::new(icon::CLOSE, t("Remove")).danger()).clicked() {
                        remove = Some(index);
                    }
                });
            }
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                let (rect, _) = ui.allocate_exact_size(vec2(LIST_W, CONTROL_H), Sense::hover());
                if pairs.is_empty() {
                    ui.painter().text(rect.left_center() + vec2(TEXT_INSET, 0.0), Align2::LEFT_CENTER, t("No pairs: nothing to do"), theme::body(12.5), TEXT_FAINT);
                }
                add = ui.add(IconButton::new(icon::ADD, t("Add a pair")).enabled(spare.is_some())).clicked();
            });
            if let Some(hint) = &hint {
                ui.set_max_width(LIST_W);
                ui.add(egui::Label::new(RichText::new(&hint.text).size(12.5).color(theme::DANGER)).wrap());
            }
        });
    });

    // A click on a key box listens there; a click anywhere else stops listening.
    if let Some(slot) = clicked_slot {
        apply(&mut listen, &mut pairs, Heard::Click(slot), &mut changed, &mut hint);
        hint = None;
    } else if listen != Listen::Off && ctx.input(|i| i.pointer.any_click()) {
        apply(&mut listen, &mut pairs, Heard::Away, &mut changed, &mut hint);
    }
    // Rows can have gone (a stand-in taken back) since the buttons were drawn.
    if let Some(index) = remove.filter(|index| *index < pairs.len()) {
        pairs.remove(index);
        changed = true;
        listen = Listen::Off;
    }
    if add {
        // A new pair starts from two spare keys, so that it is valid at once, and the first box listens;
        // after that key the second one does. Esc or a click away takes the new pair back.
        if let Some(spare) = spare.filter(|_| remove.is_none()) {
            pairs.push(spare);
            listen = Listen::On { slot: (pairs.len() - 1, 0), fresh: true };
            hint = None;
            changed = true;
        }
    }
    if let Some(shown) = &hint {
        ctx.request_repaint_after(std::time::Duration::from_secs_f64((shown.until - now).max(0.0) + 0.05));
    }
    ctx.data_mut(|data| {
        data.insert_temp(listen_id, listen);
        data.insert_temp(hint_id, hint);
    });
    if changed {
        // The daemon says no to a key used twice, and the toast says why.
        app.send(Command::SnapTap(json!({ "pairs": pairs })));
    }
}

/// A box showing one key of a pair; click it, then press the key to use.
fn key_slot(ui: &mut egui::Ui, chosen: &str, listening: bool) -> egui::Response {
    let (rect, response) = ui.allocate_exact_size(vec2(KEY_W, CONTROL_H), Sense::click());
    let response = response.on_hover_cursor(egui::CursorIcon::PointingHand);
    let (edge, text, colour) = if listening {
        (Stroke::new(1.5_f32, theme::ACCENT), t("Press a key…").to_string(), theme::ACCENT)
    } else {
        (Stroke::new(1.0_f32, if response.hovered() { theme::BORDER_HOVER } else { theme::BORDER_HI }), key_label(chosen), TEXT)
    };
    ui.painter().rect(rect, CornerRadius::same(theme::RADIUS_SM), theme::WELL, edge, egui::StrokeKind::Inside);
    let galley = widgets::galley_fit(ui, &text, theme::body(14.0), colour, KEY_W - TEXT_INSET * 2.0);
    ui.painter().galley(rect.left_center() + vec2(TEXT_INSET, -galley.size().y / 2.0), galley, colour);
    response
}

/// The box for a program to add, between frames.
#[derive(Clone, Default)]
struct Draft {
    text: String,
    /// Sent to the daemon: it goes once the list has it, and stays to be put right if not.
    adding: bool,
    /// Typed in since the daemon said no, so what it said is about something else.
    edited: bool,
}

fn deny_rows(app: &mut App, ui: &mut egui::Ui, status: &Value, error: Option<&str>) {
    let denied = denied_of(status);
    let id = egui::Id::new("snaptap-draft");
    let mut draft: Draft = ui.ctx().data(|data| data.get_temp(id)).unwrap_or_default();
    if draft.adding && denied.contains(&program_name(&draft.text)) {
        draft = Draft::default();
    }
    let mut list = None;
    widgets::row(ui, t("Stays out of"), t("Some games forbid Snap Tap, so it pauses while these are in front."), |ui| {
        ui.vertical(|ui| {
            for (index, name) in denied.iter().enumerate() {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    let (rect, _) = ui.allocate_exact_size(vec2(LIST_W, CONTROL_H), Sense::hover());
                    let text = widgets::galley_fit(ui, name, theme::body(14.0), TEXT, LIST_W - TEXT_INSET);
                    ui.painter().galley(rect.left_center() + vec2(TEXT_INSET, -text.size().y / 2.0), text, TEXT);
                    if ui.add(IconButton::new(icon::CLOSE, t("Remove")).danger()).clicked() {
                        list = Some(without(&denied, index));
                        draft.edited = false;
                    }
                });
            }
            if denied.is_empty() {
                ui.allocate_ui_with_layout(vec2(LIST_W, CONTROL_H), Layout::left_to_right(Align::Center), |ui| {
                    ui.set_min_height(CONTROL_H);
                    ui.add_space(TEXT_INSET);
                    ui.label(widgets::faint(t("No programs")));
                });
            }
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                let field = widgets::text_field(ui, &mut draft.text, "game.exe", LIST_W);
                if field.changed() {
                    draft.adding = false;
                    draft.edited = true;
                }
                let enter = field.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                let name = draft.text.trim().to_string();
                let add = ui.add(IconButton::new(icon::ADD, t("Add a program")).enabled(!name.is_empty())).clicked();
                if (add || enter) && !name.is_empty() {
                    list = Some(with_added(&denied, &name));
                    draft.adding = true;
                    draft.edited = false;
                    field.request_focus();
                }
            });
            if let Some(error) = error.filter(|_| !draft.edited) {
                ui.set_max_width(LIST_W);
                ui.add(egui::Label::new(RichText::new(error).size(12.5).color(theme::DANGER)).wrap());
            }
        });
    });
    ui.ctx().data_mut(|data| data.insert_temp(id, draft));
    if let Some(list) = list {
        // Whether a name will do is the daemon's to say, under the box.
        app.send(Command::SnapTap(json!({ "deny": list })));
    }
}

/// Shown once: turning it on means having read this.
fn warning_dialog(app: &mut App, ui: &mut egui::Ui, confirming: egui::Id) {
    let mut close = false;
    let frame = Frame::new().fill(theme::SURFACE).stroke(Stroke::new(1.0_f32, theme::BORDER_HI)).corner_radius(CornerRadius::same(theme::RADIUS)).shadow(theme::shadow()).inner_margin(Margin::same(20));
    let modal = egui::Modal::new(egui::Id::new("snaptap-warning")).frame(frame).show(ui.ctx(), |ui| {
        ui.set_width(460.0);
        ui.spacing_mut().item_spacing = vec2(theme::SPACING, 10.0);
        ui.label(RichText::new(t("Before you turn on Snap Tap")).font(theme::bold(17.0)).color(TEXT));
        ui.add(egui::Label::new(RichText::new(t("Snap Tap changes your key presses in software. Some games forbid this: Counter-Strike 2 may kick you from official matches, and tournaments or other platforms may go further. Turn it off in those games. It does nothing in games running as administrator, and it may not work in every game.")).color(TEXT_DIM)).wrap());
        widgets::divider(ui);
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            if ui.add(Button::primary(t("I understand, turn it on"))).clicked() {
                app.send(Command::SnapTap(json!({ "enabled": true, "acknowledged": true })));
                close = true;
            }
            if ui.add(Button::secondary(t("Cancel")).min_width(96.0)).clicked() {
                close = true;
            }
        });
    });
    if close || modal.should_close() {
        ui.ctx().data_mut(|data| data.remove::<bool>(confirming));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|name| name.to_string()).collect()
    }

    #[test]
    fn a_key_reads_without_its_prefix() {
        assert_eq!(key_label("KEY_A"), "A");
        assert_eq!(key_label("KEY_LEFTSHIFT"), "LEFTSHIFT");
    }

    #[test]
    fn a_new_pair_starts_from_keys_no_pair_uses() {
        let keys = names(&["KEY_A", "KEY_B", "KEY_C", "KEY_D"]);
        let pairs = [["KEY_A".to_string(), "KEY_D".to_string()]];
        assert_eq!(free_keys(&keys, &pairs), Some(["KEY_B".to_string(), "KEY_C".to_string()]));
    }

    #[test]
    fn with_too_few_keys_left_there_is_no_new_pair() {
        let keys = names(&["KEY_A", "KEY_B", "KEY_C"]);
        let pairs = [["KEY_A".to_string(), "KEY_B".to_string()]];
        assert_eq!(free_keys(&keys, &pairs), None);
    }

    #[test]
    fn pairs_are_read_from_the_status_of_the_daemon() {
        let status = json!({ "pairs": [["KEY_A", "KEY_D"], ["KEY_W", "KEY_S"]] });
        assert_eq!(pairs_of(&status)[1], ["KEY_W".to_string(), "KEY_S".to_string()]);
        assert!(pairs_of(&Value::Null).is_empty());
    }

    #[test]
    fn a_typed_program_is_kept_the_way_the_daemon_keeps_it() {
        assert_eq!(program_name("  Game.EXE "), "game.exe");
    }

    #[test]
    fn the_list_sent_keeps_what_is_there_and_changes_one_name() {
        let denied = names(&["cs2.exe", "valorant.exe"]);
        assert_eq!(with_added(&denied, "game.exe"), names(&["cs2.exe", "valorant.exe", "game.exe"]));
        assert_eq!(without(&denied, 0), names(&["valorant.exe"]));
    }

    #[test]
    fn removing_the_last_program_sends_an_empty_list() {
        assert_eq!(without(&names(&["cs2.exe"]), 0), Vec::<String>::new());
    }

    #[test]
    fn a_working_snap_tap_says_nothing() {
        assert_eq!(attention(&json!({ "enabled": true, "running": true, "paused": null })), None);
        assert_eq!(attention(&json!({ "enabled": false, "running": false })), None);
    }

    #[test]
    fn a_pause_names_the_program_in_front() {
        let status = json!({ "enabled": true, "running": true, "paused": "denied", "foreground": "cs2.exe" });
        assert_eq!(attention(&status).map(|(_, text)| text), Some("Paused while cs2.exe is in front".to_string()));
    }

    #[test]
    fn a_pause_for_a_program_with_no_name_says_so() {
        let status = json!({ "enabled": true, "running": true, "paused": "unknown program", "foreground": null });
        assert_eq!(attention(&status).map(|(_, text)| text), Some("Paused: can't tell which program is in front".to_string()));
    }

    #[test]
    fn not_running_while_on_is_shown() {
        assert!(attention(&json!({ "enabled": true, "running": false })).is_some());
    }

    #[test]
    fn only_keyboards_show_it() {
        let device = |kind: &str| Device { kind: kind.to_string(), ..Device::default() };
        assert!(shown_on(&device("keyboard")));
        assert!(!shown_on(&device("mouse")));
    }

    fn key_list() -> Vec<String> {
        names(&["KEY_A", "KEY_B", "KEY_C", "KEY_D", "KEY_E", "KEY_F", "KEY_LEFTBRACE", "KEY_F5", "KEY_7"])
    }

    fn one_pair() -> Vec<[String; 2]> {
        vec![["KEY_A".to_string(), "KEY_D".to_string()]]
    }

    fn two_pairs() -> Vec<[String; 2]> {
        vec![["KEY_A".to_string(), "KEY_D".to_string()], ["KEY_B".to_string(), "KEY_C".to_string()]]
    }

    fn on(row: usize, side: usize, fresh: bool) -> Listen {
        Listen::On { slot: (row, side), fresh }
    }

    #[test]
    fn keys_are_named_by_position_as_in_codes_py() {
        use egui::Key as K;
        assert_eq!(key_name(K::A).as_deref(), Some("KEY_A"));
        assert_eq!(key_name(K::Num7).as_deref(), Some("KEY_7"));
        assert_eq!(key_name(K::F5).as_deref(), Some("KEY_F5"));
        assert_eq!(key_name(K::F24).as_deref(), Some("KEY_F24"));
        assert_eq!(key_name(K::F25), None);
        assert_eq!(key_name(K::OpenBracket).as_deref(), Some("KEY_LEFTBRACE"));
        assert_eq!(key_name(K::Quote).as_deref(), Some("KEY_APOSTROPHE"));
        assert_eq!(key_name(K::Backtick).as_deref(), Some("KEY_GRAVE"));
        assert_eq!(key_name(K::Period).as_deref(), Some("KEY_DOT"));
        assert_eq!(key_name(K::ArrowUp).as_deref(), Some("KEY_UP"));
        assert_eq!(key_name(K::PageDown).as_deref(), Some("KEY_PAGEDOWN"));
        assert_eq!(key_name(K::Plus), None);
        assert_eq!(key_name(K::Colon), None);
        assert_eq!(key_name(K::Escape), None);
    }

    #[test]
    fn the_physical_key_wins_over_the_typed_one() {
        use egui::Key as K;
        // The key that types Ñ on a Spanish board sits where ; is on a US one.
        assert_eq!(pressed_key(K::Colon, Some(K::Semicolon)), K::Semicolon);
        assert_eq!(pressed_key(K::Q, None), K::Q);
    }

    #[test]
    fn presses_are_taken_out_of_the_events_and_repeats_and_releases_are_not_presses() {
        use egui::{Event, Key, Modifiers};
        let key = |key, physical_key, pressed, repeat| Event::Key { key, physical_key, pressed, repeat, modifiers: Modifiers::NONE };
        let mut events = vec![
            Event::PointerGone,
            key(Key::A, Some(Key::A), true, false),
            key(Key::A, Some(Key::A), true, true),
            key(Key::A, Some(Key::A), false, false),
            key(Key::Colon, Some(Key::Semicolon), true, false),
        ];
        assert_eq!(take_presses(&mut events), vec![Key::A, Key::Semicolon]);
        assert_eq!(events.len(), 1);
    }

    #[test]
    fn a_click_starts_listening_and_a_key_is_assigned() {
        let (keys, pairs) = (key_list(), one_pair());
        let (listen, effect) = step(Listen::Off, Heard::Click((0, 1)), &keys, &pairs);
        assert_eq!((listen, effect), (on(0, 1, false), Effect::Nothing));
        let (listen, effect) = step(listen, Heard::Key(egui::Key::E), &keys, &pairs);
        assert_eq!((listen, effect), (Listen::Off, Effect::Set((0, 1), "KEY_E".to_string())));
    }

    #[test]
    fn keys_do_nothing_when_not_listening() {
        assert_eq!(step(Listen::Off, Heard::Key(egui::Key::E), &key_list(), &one_pair()), (Listen::Off, Effect::Nothing));
        assert_eq!(step(Listen::Off, Heard::Esc, &key_list(), &one_pair()), (Listen::Off, Effect::Nothing));
    }

    #[test]
    fn esc_or_a_click_away_cancels_and_keeps_the_key() {
        let (keys, pairs) = (key_list(), one_pair());
        assert_eq!(step(on(0, 0, false), Heard::Esc, &keys, &pairs), (Listen::Off, Effect::Nothing));
        assert_eq!(step(on(0, 0, false), Heard::Away, &keys, &pairs), (Listen::Off, Effect::Nothing));
    }

    #[test]
    fn only_one_box_listens_at_a_time() {
        let (keys, pairs) = (key_list(), one_pair());
        assert_eq!(step(on(0, 0, false), Heard::Click((0, 1)), &keys, &pairs).0, on(0, 1, false));
        assert_eq!(step(on(0, 0, false), Heard::Click((0, 0)), &keys, &pairs).0, on(0, 0, false));
    }

    #[test]
    fn a_key_the_daemon_does_not_know_is_not_taken_and_listening_goes_on() {
        let (keys, pairs) = (key_list(), one_pair());
        // The daemon's list here has no Z, and no name exists for Plus.
        assert_eq!(step(on(0, 0, false), Heard::Key(egui::Key::Z), &keys, &pairs), (on(0, 0, false), Effect::Unknown));
        assert_eq!(step(on(0, 0, false), Heard::Key(egui::Key::Plus), &keys, &pairs), (on(0, 0, false), Effect::Unknown));
    }

    #[test]
    fn a_key_already_in_a_pair_is_refused_like_the_daemon_does() {
        let (keys, pairs) = (key_list(), two_pairs());
        // The other side of the same pair, and a key of another pair.
        assert_eq!(step(on(0, 0, false), Heard::Key(egui::Key::D), &keys, &pairs), (on(0, 0, false), Effect::Taken));
        assert_eq!(step(on(0, 1, false), Heard::Key(egui::Key::B), &keys, &pairs), (on(0, 1, false), Effect::Taken));
    }

    #[test]
    fn pressing_the_key_a_box_already_has_changes_nothing() {
        let (keys, pairs) = (key_list(), one_pair());
        assert_eq!(step(on(0, 0, false), Heard::Key(egui::Key::A), &keys, &pairs), (Listen::Off, Effect::Nothing));
    }

    #[test]
    fn a_new_pair_listens_to_the_first_box_then_the_second() {
        let (keys, pairs) = (key_list(), two_pairs());
        let (listen, effect) = step(on(1, 0, true), Heard::Key(egui::Key::E), &keys, &pairs);
        assert_eq!((listen, effect), (on(1, 1, true), Effect::Set((1, 0), "KEY_E".to_string())));
        let (listen, effect) = step(listen, Heard::Key(egui::Key::F), &keys, &pairs);
        assert_eq!((listen, effect), (Listen::Off, Effect::Set((1, 1), "KEY_F".to_string())));
    }

    #[test]
    fn cancelling_a_new_pair_takes_it_back() {
        let (keys, pairs) = (key_list(), two_pairs());
        assert_eq!(step(on(1, 1, true), Heard::Esc, &keys, &pairs), (Listen::Off, Effect::Drop(1)));
        assert_eq!(step(on(1, 0, true), Heard::Away, &keys, &pairs), (Listen::Off, Effect::Drop(1)));
        // Clicking a box of a later row does too, and that row moves up.
        assert_eq!(step(on(0, 0, true), Heard::Click((1, 1)), &keys, &pairs), (on(0, 1, false), Effect::Drop(0)));
    }

    #[test]
    fn a_new_pair_the_daemon_has_not_shown_yet_waits() {
        let (keys, pairs) = (key_list(), one_pair());
        assert_eq!(step(on(1, 0, true), Heard::Key(egui::Key::E), &keys, &pairs), (on(1, 0, true), Effect::Nothing));
        assert_eq!(step(on(1, 0, true), Heard::Esc, &keys, &pairs), (Listen::Off, Effect::Nothing));
    }
}
