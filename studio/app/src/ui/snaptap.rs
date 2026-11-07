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

fn pair_rows(app: &mut App, ui: &mut egui::Ui, status: &Value) {
    let keys: Vec<String> = status["keys"].as_array().map(|rows| rows.iter().filter_map(|key| key.as_str().map(str::to_string)).collect()).unwrap_or_default();
    let mut pairs = pairs_of(status);
    let spare = free_keys(&keys, &pairs);
    let mut changed = false;
    let mut remove = None;
    widgets::row(ui, t("Opposing keys"), t("When two opposing keys are held, only the last one pressed counts. Let go of it and the other takes over. Tip: helps you strafe faster in shooters."), |ui| {
        ui.vertical(|ui| {
            for (index, pair) in pairs.iter_mut().enumerate() {
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    let [first, second] = pair;
                    changed |= key_select(ui, (index, 0), first, &keys);
                    let (rect, _) = ui.allocate_exact_size(vec2(SWITCH_W, CONTROL_H), Sense::hover());
                    ui.painter().text(rect.center(), Align2::CENTER_CENTER, icon::SWITCH, theme::icon_font(14.0), TEXT_FAINT);
                    changed |= key_select(ui, (index, 1), second, &keys);
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
                if ui.add(IconButton::new(icon::ADD, t("Add a pair")).enabled(spare.is_some())).clicked() {
                    if let Some(spare) = spare {
                        pairs.push(spare);
                        changed = true;
                    }
                }
            });
        });
    });
    if let Some(index) = remove {
        pairs.remove(index);
        changed = true;
    }
    if changed {
        // The daemon says no to a key used twice, and the toast says why.
        app.send(Command::SnapTap(json!({ "pairs": pairs })));
    }
}

fn key_select(ui: &mut egui::Ui, slot: (usize, usize), chosen: &mut String, keys: &[String]) -> bool {
    let mut changed = false;
    widgets::select(ui, ("snaptap-key", slot), key_label(chosen), KEY_W, |ui| {
        for key in keys {
            if widgets::option(ui, key == chosen, &key_label(key)).clicked() {
                *chosen = key.clone();
                changed = true;
            }
        }
    });
    changed
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
}
