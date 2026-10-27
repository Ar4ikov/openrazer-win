//! Macros: a library to build them in, and the keys to put them on.
//!
//! The daemon stores what each macro key does, per device. The library is
//! Studio's: macros are made and named once, then put on any key of any
//! device -- the binding is a copy, so changing the library later does not
//! change a key until the macro is put on it again.

use super::app::{short_name, App};
use crate::settings::LibraryMacro;
use crate::worker::{Command, Device, Snapshot};
use egui::{vec2, CornerRadius, Frame, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, BORDER, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::{t, tf, widgets};
use serde_json::{json, Value};
use std::time::Instant;

/// Key names and Windows virtual-key codes, as offered in the editor.
#[rustfmt::skip]
pub const KEYS: &[(&str, u8)] = &[
    ("A", 0x41), ("B", 0x42), ("C", 0x43), ("D", 0x44), ("E", 0x45), ("F", 0x46), ("G", 0x47), ("H", 0x48), ("I", 0x49),
    ("J", 0x4A), ("K", 0x4B), ("L", 0x4C), ("M", 0x4D), ("N", 0x4E), ("O", 0x4F), ("P", 0x50), ("Q", 0x51), ("R", 0x52),
    ("S", 0x53), ("T", 0x54), ("U", 0x55), ("V", 0x56), ("W", 0x57), ("X", 0x58), ("Y", 0x59), ("Z", 0x5A),
    ("0", 0x30), ("1", 0x31), ("2", 0x32), ("3", 0x33), ("4", 0x34), ("5", 0x35), ("6", 0x36), ("7", 0x37), ("8", 0x38), ("9", 0x39),
    ("F1", 0x70), ("F2", 0x71), ("F3", 0x72), ("F4", 0x73), ("F5", 0x74), ("F6", 0x75), ("F7", 0x76), ("F8", 0x77),
    ("F9", 0x78), ("F10", 0x79), ("F11", 0x7A), ("F12", 0x7B), ("F13", 0x7C), ("F14", 0x7D), ("F15", 0x7E), ("F16", 0x7F),
    ("F17", 0x80), ("F18", 0x81), ("F19", 0x82), ("F20", 0x83), ("F21", 0x84), ("F22", 0x85), ("F23", 0x86), ("F24", 0x87),
    ("Ctrl", 0xA2), ("Shift", 0xA0), ("Alt", 0xA4), ("Win", 0x5B), ("Right Ctrl", 0xA3), ("Right Shift", 0xA1), ("Right Alt", 0xA5),
    ("Enter", 0x0D), ("Esc", 0x1B), ("Tab", 0x09), ("Space", 0x20), ("Backspace", 0x08), ("Caps Lock", 0x14),
    ("Insert", 0x2D), ("Delete", 0x2E), ("Home", 0x24), ("End", 0x23), ("Page Up", 0x21), ("Page Down", 0x22),
    ("←", 0x25), ("↑", 0x26), ("→", 0x27), ("↓", 0x28), ("Print Screen", 0x2C), ("Pause", 0x13),
    ("-", 0xBD), ("=", 0xBB), ("[", 0xDB), ("]", 0xDD), ("\\", 0xDC), (";", 0xBA), ("'", 0xDE), (",", 0xBC), (".", 0xBE), ("/", 0xBF), ("`", 0xC0),
    ("Mute", 0xAD), ("Volume down", 0xAE), ("Volume up", 0xAF), ("Next track", 0xB0), ("Previous track", 0xB1), ("Play/Pause", 0xB3), ("Stop", 0xB2),
];

/// Keys Windows wants flagged as extended when they are typed for you.
fn extended(vk: u8) -> bool {
    matches!(vk, 0x21..=0x28 | 0x2D | 0x2E | 0x5B | 0xA3 | 0xA5 | 0xAD..=0xB3)
}

pub fn key_name(vk: u8) -> String {
    match KEYS.iter().find(|(_, code)| *code == vk) {
        Some((name, _)) => t_key(name).to_string(),
        None => format!("key {vk:#04x}"),
    }
}

/// Key names that are words are translated; letters and symbols are not.
fn t_key(name: &'static str) -> &'static str {
    match name {
        // Keys keep the names printed on them; only the ones that are words get translated.
        "Mute" | "Volume down" | "Volume up" | "Next track" | "Previous track" | "Play/Pause" | "Right Ctrl" | "Right Shift" | "Right Alt" => t(name),
        other => other,
    }
}

/// One step of a key macro, as the editor shows it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Step {
    /// Press and let go.
    Tap(u8),
    Down(u8),
    Up(u8),
    /// Wait this many milliseconds.
    Wait(u32),
}

pub fn to_events(steps: &[Step]) -> Vec<Value> {
    let mut events = Vec::new();
    let mut delay = 0u32;
    let mut event = |vk: u8, down: bool, delay: &mut u32| {
        events.push(json!({ "vk": vk, "scan": 0, "ext": extended(vk), "down": down, "delay": (*delay).min(30_000) }));
        *delay = 0;
    };
    for step in steps {
        match *step {
            Step::Tap(vk) => {
                event(vk, true, &mut delay);
                event(vk, false, &mut delay);
            }
            Step::Down(vk) => event(vk, true, &mut delay),
            Step::Up(vk) => event(vk, false, &mut delay),
            Step::Wait(ms) => delay += ms,
        }
    }
    events
}

pub fn from_events(events: &[Value]) -> Vec<Step> {
    let mut steps = Vec::new();
    let mut i = 0;
    while i < events.len() {
        let e = &events[i];
        let vk = e["vk"].as_u64().unwrap_or(0) as u8;
        let delay = e["delay"].as_u64().unwrap_or(0) as u32;
        if delay > 0 {
            steps.push(Step::Wait(delay));
        }
        let down = e["down"] != false;
        let next = events.get(i + 1);
        // A press let go at once is one tap.
        if down && next.is_some_and(|n| n["vk"].as_u64() == Some(vk as u64) && n["down"] == false && n["delay"].as_u64().unwrap_or(0) == 0) {
            steps.push(Step::Tap(vk));
            i += 2;
            continue;
        }
        steps.push(if down { Step::Down(vk) } else { Step::Up(vk) });
        i += 1;
    }
    steps
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Keys,
    Text,
    Launch,
    Disabled,
}

struct Draft {
    /// Index in the library; None for one not saved yet.
    index: Option<usize>,
    name: String,
    kind: Kind,
    steps: Vec<Step>,
    text: String,
    target: String,
    /// When recording started, and when the last key came.
    recording: Option<(Instant, Instant, egui::Modifiers)>,
    pick: u8,
}

impl Draft {
    fn new() -> Draft {
        Draft { index: None, name: String::new(), kind: Kind::Keys, steps: Vec::new(), text: String::new(), target: String::new(), recording: None, pick: 0x41 }
    }

    fn from(index: usize, item: &LibraryMacro) -> Draft {
        let mut draft = Draft::new();
        draft.index = Some(index);
        draft.name = item.name.clone();
        match item.action["type"].as_str() {
            Some("text") => {
                draft.kind = Kind::Text;
                draft.text = item.action["text"].as_str().unwrap_or_default().into();
            }
            Some("launch") => {
                draft.kind = Kind::Launch;
                draft.target = item.action["target"].as_str().unwrap_or_default().into();
            }
            Some("disabled") => draft.kind = Kind::Disabled,
            _ => draft.steps = from_events(item.action["events"].as_array().map(Vec::as_slice).unwrap_or(&[])),
        }
        draft
    }

    fn action(&self) -> Result<Value, &'static str> {
        Ok(match self.kind {
            Kind::Keys if self.steps.iter().all(|s| matches!(s, Step::Wait(_))) => return Err(t("Add at least one key.")),
            Kind::Keys => json!({ "type": "keys", "events": to_events(&self.steps) }),
            Kind::Text if self.text.is_empty() => return Err(t("Type the text it should type.")),
            Kind::Text => json!({ "type": "text", "text": self.text }),
            Kind::Launch if self.target.trim().is_empty() => return Err(t("Choose what it should open.")),
            Kind::Launch => json!({ "type": "launch", "target": self.target.trim() }),
            Kind::Disabled => json!({ "type": "disabled" }),
        })
    }
}

#[derive(Default)]
pub struct MacrosUi {
    draft: Option<Draft>,
    selected: Option<usize>,
}

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        ui.horizontal_top(|ui| {
            ui.vertical(|ui| {
                ui.set_width(270.0);
                library(app, ui);
            });
            ui.add_space(14.0);
            ui.vertical(|ui| {
                ui.set_width(ui.available_width());
                if app.macros.draft.is_some() {
                    editor(app, ui);
                    ui.add_space(12.0);
                }
                assign(app, ui, snap);
            });
        });
        ui.add_space(20.0);
    });
}

fn library(app: &mut App, ui: &mut egui::Ui) {
    widgets::card(ui, |ui| {
        ui.horizontal(|ui| {
            widgets::heading(ui, t("Library"));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button(RichText::new(format!("+ {}", t("New"))).strong()).clicked() {
                    let mut draft = Draft::new();
                    draft.name = tf("Macro {}", &[&(app.settings.macros.len() + 1)]);
                    app.macros.draft = Some(draft);
                    app.macros.selected = None;
                }
            });
        });
        if app.settings.macros.is_empty() {
            ui.label(widgets::dim(t("Nothing here yet. Make a macro once, then put it on any macro key.")));
        }
        let mut open: Option<usize> = None;
        for (index, item) in app.settings.macros.iter().enumerate() {
            let selected = app.macros.selected == Some(index);
            let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), 44.0), Sense::click());
            let painter = ui.painter();
            painter.rect_filled(rect, CornerRadius::same(theme::RADIUS), if selected { theme::SURFACE_ACTIVE } else if response.hovered() { theme::SURFACE_HOVER } else { theme::SURFACE_HI });
            if selected {
                painter.rect_stroke(rect, CornerRadius::same(theme::RADIUS), Stroke::new(1.0_f32, ACCENT), egui::StrokeKind::Inside);
            }
            let glyph = match item.action["type"].as_str() {
                Some("text") => icon::TEXT,
                Some("launch") => icon::OPEN,
                Some("disabled") => icon::CLOSE,
                _ => icon::KEYBOARD,
            };
            painter.text(rect.left_center() + vec2(14.0, 0.0), egui::Align2::LEFT_CENTER, glyph, theme::icon_font(14.0), if selected { ACCENT } else { TEXT_DIM });
            painter.text(rect.left_top() + vec2(36.0, 7.0), egui::Align2::LEFT_TOP, &item.name, theme::bold(13.0), TEXT);
            let (summary, _) = summary(Some(&item.action), "");
            painter.text(rect.left_bottom() + vec2(36.0, -7.0), egui::Align2::LEFT_BOTTOM, truncate(&summary, 34), egui::FontId::proportional(11.5), TEXT_FAINT);
            if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                open = Some(index);
            }
        }
        if let Some(index) = open {
            app.macros.selected = Some(index);
            app.macros.draft = Some(Draft::from(index, &app.settings.macros[index]));
        }
    });
    ui.add_space(12.0);
    widgets::panel(ui, t("Tips"), |ui| {
        ui.label(widgets::faint(t("An unbound macro key sends F13–F17, so any game or app that takes hotkeys can use it as is.")));
        ui.label(widgets::faint(t("On the keyboard: Fn + F9 records a macro straight onto a key.")));
    });
}

/// Turn what was typed in the window into steps, while recording.
fn record(ui: &egui::Ui, draft: &mut Draft) {
    let Some((_, last, previous)) = draft.recording else { return };
    let mut last = last;
    let mut previous = previous;
    let events: Vec<egui::Event> = ui.input(|i| i.events.clone());
    let modifiers = ui.input(|i| i.modifiers);
    let push = |draft: &mut Draft, step: Step, last: &mut Instant| {
        let gap = last.elapsed().as_millis() as u32;
        if gap > 30 && !draft.steps.is_empty() {
            draft.steps.push(Step::Wait(gap.min(30_000)));
        }
        draft.steps.push(step);
        *last = Instant::now();
    };
    for (on, was, vk) in [(modifiers.ctrl, previous.ctrl, 0xA2u8), (modifiers.shift, previous.shift, 0xA0), (modifiers.alt, previous.alt, 0xA4)] {
        if on != was {
            push(draft, if on { Step::Down(vk) } else { Step::Up(vk) }, &mut last);
        }
    }
    previous = modifiers;
    for event in events {
        if let egui::Event::Key { key, pressed, repeat: false, .. } = event {
            if let Some(vk) = vk_of(key) {
                push(draft, if pressed { Step::Down(vk) } else { Step::Up(vk) }, &mut last);
            }
        }
    }
    // Tidy a press immediately let go into one tap.
    let mut tidy: Vec<Step> = Vec::new();
    for step in draft.steps.drain(..) {
        if let (Step::Up(up), Some(Step::Down(down))) = (step, tidy.last().copied()) {
            if up == down {
                tidy.pop();
                tidy.push(Step::Tap(up));
                continue;
            }
        }
        tidy.push(step);
    }
    draft.steps = tidy;
    if let Some(recording) = draft.recording.as_mut() {
        recording.1 = last;
        recording.2 = previous;
    }
}

fn vk_of(key: egui::Key) -> Option<u8> {
    use egui::Key::*;
    Some(match key {
        A => 0x41, B => 0x42, C => 0x43, D => 0x44, E => 0x45, F => 0x46, G => 0x47, H => 0x48, I => 0x49, J => 0x4A, K => 0x4B, L => 0x4C, M => 0x4D,
        N => 0x4E, O => 0x4F, P => 0x50, Q => 0x51, R => 0x52, S => 0x53, T => 0x54, U => 0x55, V => 0x56, W => 0x57, X => 0x58, Y => 0x59, Z => 0x5A,
        Num0 => 0x30, Num1 => 0x31, Num2 => 0x32, Num3 => 0x33, Num4 => 0x34, Num5 => 0x35, Num6 => 0x36, Num7 => 0x37, Num8 => 0x38, Num9 => 0x39,
        F1 => 0x70, F2 => 0x71, F3 => 0x72, F4 => 0x73, F5 => 0x74, F6 => 0x75, F7 => 0x76, F8 => 0x77, F9 => 0x78, F10 => 0x79, F11 => 0x7A, F12 => 0x7B,
        Enter => 0x0D, Escape => 0x1B, Tab => 0x09, Space => 0x20, Backspace => 0x08, Insert => 0x2D, Delete => 0x2E, Home => 0x24, End => 0x23,
        PageUp => 0x21, PageDown => 0x22, ArrowLeft => 0x25, ArrowUp => 0x26, ArrowRight => 0x27, ArrowDown => 0x28,
        Minus => 0xBD, Equals => 0xBB, OpenBracket => 0xDB, CloseBracket => 0xDD, Backslash => 0xDC, Semicolon => 0xBA, Quote => 0xDE,
        Comma => 0xBC, Period => 0xBE, Slash => 0xBF, Backtick => 0xC0,
        _ => return None,
    })
}

fn editor(app: &mut App, ui: &mut egui::Ui) {
    let mut close = false;
    let mut save: Option<(Option<usize>, LibraryMacro)> = None;
    let mut delete: Option<usize> = None;
    let mut problem: Option<&str> = None;
    let Some(draft) = app.macros.draft.as_mut() else { return };
    record(ui, draft);
    if draft.recording.is_some() {
        ui.ctx().request_repaint();
    }
    widgets::card(ui, |ui| {
        ui.horizontal(|ui| {
            widgets::heading(ui, t("Macro"));
            ui.add(egui::TextEdit::singleline(&mut draft.name).desired_width(260.0).font(theme::bold(14.0)));
        });
        let options = vec![(Kind::Keys, t("Key presses").to_string()), (Kind::Text, t("Type text").to_string()), (Kind::Launch, t("Open something").to_string()), (Kind::Disabled, t("Do nothing").to_string())];
        widgets::segmented(ui, &options, &mut draft.kind);
        ui.add_space(6.0);
        match draft.kind {
            Kind::Keys => {
                let recording = draft.recording.is_some();
                ui.horizontal(|ui| {
                    if recording {
                        if widgets::danger_button(ui, &format!("■  {}", t("Stop recording"))).clicked() {
                            draft.recording = None;
                        }
                        ui.label(RichText::new(t("Recording: type the macro in this window. Pauses are kept.")).color(theme::DANGER));
                    } else {
                        if ui.add(egui::Button::new(RichText::new(format!("●  {}", t("Record"))).color(theme::DANGER))).clicked() {
                            draft.steps.clear();
                            draft.recording = Some((Instant::now(), Instant::now(), ui.input(|i| i.modifiers)));
                        }
                        ui.label(widgets::faint(t("or build it step by step:")));
                    }
                });
                // The steps, in order.
                let mut remove: Option<usize> = None;
                let mut swap: Option<(usize, usize)> = None;
                Frame::new().fill(theme::WELL).corner_radius(CornerRadius::same(theme::RADIUS)).inner_margin(Margin::same(10)).show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    if draft.steps.is_empty() {
                        ui.label(widgets::faint(t("No steps yet.")));
                    }
                    ui.horizontal_wrapped(|ui| {
                        ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
                        let count = draft.steps.len();
                        for (i, step) in draft.steps.iter_mut().enumerate() {
                            let (text, colour) = match *step {
                                Step::Tap(vk) => (key_name(vk), TEXT),
                                Step::Down(vk) => (format!("{} ↓", key_name(vk)), ACCENT),
                                Step::Up(vk) => (format!("{} ↑", key_name(vk)), TEXT_DIM),
                                Step::Wait(ms) => (format!("⏱ {ms} ms"), TEXT_FAINT),
                            };
                            let response = Frame::new()
                                .fill(theme::SURFACE_HI)
                                .stroke(Stroke::new(1.0_f32, BORDER))
                                .corner_radius(CornerRadius::same(4))
                                .inner_margin(Margin::symmetric(8, 4))
                                .show(ui, |ui| {
                                    ui.horizontal(|ui| {
                                        ui.spacing_mut().item_spacing.x = 4.0;
                                        if let Step::Wait(ms) = step {
                                            ui.label(RichText::new("⏱").color(TEXT_FAINT));
                                            ui.add(egui::DragValue::new(ms).range(10..=30_000).speed(10.0).suffix(" ms"));
                                        } else {
                                            ui.label(RichText::new(&text).font(theme::bold(12.5)).color(colour));
                                        }
                                        if i > 0 && ui.add(egui::Button::new(RichText::new("‹").color(TEXT_FAINT)).frame(false)).clicked() {
                                            swap = Some((i, i - 1));
                                        }
                                        if i + 1 < count && ui.add(egui::Button::new(RichText::new("›").color(TEXT_FAINT)).frame(false)).clicked() {
                                            swap = Some((i, i + 1));
                                        }
                                        if ui.add(egui::Button::new(RichText::new(icon::CLOSE).font(theme::icon_font(9.0)).color(TEXT_FAINT)).frame(false)).clicked() {
                                            remove = Some(i);
                                        }
                                    });
                                })
                                .response;
                            response.on_hover_text(match step {
                                Step::Tap(_) => t("Press and let go"),
                                Step::Down(_) => t("Hold down"),
                                Step::Up(_) => t("Let go"),
                                Step::Wait(_) => t("Wait"),
                            });
                        }
                    });
                });
                if let Some(i) = remove {
                    draft.steps.remove(i);
                }
                if let Some((a, b)) = swap {
                    draft.steps.swap(a, b);
                }
                ui.horizontal_wrapped(|ui| {
                    egui::ComboBox::from_id_salt("pick-key").selected_text(key_name(draft.pick)).width(150.0).height(320.0).show_ui(ui, |ui| {
                        for (name, vk) in KEYS {
                            ui.selectable_value(&mut draft.pick, *vk, t_key(name));
                        }
                    });
                    if ui.button(t("Press")).on_hover_text(t("Press and let go")).clicked() {
                        draft.steps.push(Step::Tap(draft.pick));
                    }
                    if ui.button(t("Hold")).on_hover_text(t("Hold down, for combinations such as Ctrl + C")).clicked() {
                        draft.steps.push(Step::Down(draft.pick));
                    }
                    if ui.button(t("Let go")).clicked() {
                        draft.steps.push(Step::Up(draft.pick));
                    }
                    if widgets::icon_button(ui, icon::TIMER, t("Pause")).clicked() {
                        draft.steps.push(Step::Wait(100));
                    }
                    if !draft.steps.is_empty() && ui.button(t("Clear")).clicked() {
                        draft.steps.clear();
                    }
                });
                let held: Vec<u8> = {
                    let mut down: Vec<u8> = Vec::new();
                    for step in &draft.steps {
                        match step {
                            Step::Down(vk) => down.push(*vk),
                            Step::Up(vk) => down.retain(|d| d != vk),
                            _ => {}
                        }
                    }
                    down
                };
                if !held.is_empty() {
                    ui.label(RichText::new(tf("Still held down at the end: {}", &[&held.iter().map(|vk| key_name(*vk)).collect::<Vec<_>>().join(", ")])).size(12.5).color(theme::WARN));
                }
            }
            Kind::Text => {
                ui.add(egui::TextEdit::multiline(&mut draft.text).hint_text(t("Text to type")).desired_rows(3).desired_width(f32::INFINITY));
                ui.label(widgets::faint(t("Typed exactly as written, whatever the keyboard layout.")));
            }
            Kind::Launch => {
                ui.horizontal(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut draft.target).hint_text(t("A program, document, folder or https:// link")).desired_width(ui.available_width() - 110.0));
                    if widgets::icon_button(ui, icon::FOLDER, t("Browse")).clicked() {
                        if let Some(path) = rfd::FileDialog::new().set_title(t("What should this macro open?")).pick_file() {
                            draft.target = path.display().to_string();
                        }
                    }
                });
            }
            Kind::Disabled => {
                ui.label(widgets::dim(t("The key will do nothing at all: handy for a key that gets in the way.")));
            }
        }
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            if widgets::primary_button(ui, t("Save to library")).clicked() {
                match draft.action() {
                    Ok(action) => {
                        let name = if draft.name.trim().is_empty() { t("Macro").to_string() } else { draft.name.trim().to_string() };
                        save = Some((draft.index, LibraryMacro { name, action }));
                    }
                    Err(why) => problem = Some(why),
                }
            }
            if ui.button(t("Close")).clicked() {
                close = true;
            }
            if let Some(index) = draft.index {
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if widgets::danger_button(ui, t("Delete")).clicked() {
                        delete = Some(index);
                    }
                });
            }
        });
    });
    if let Some(why) = problem {
        app.toast(why, true);
    }
    if let Some((index, item)) = save {
        let name = item.name.clone();
        match index {
            Some(i) if i < app.settings.macros.len() => app.settings.macros[i] = item,
            _ => {
                app.settings.macros.push(item);
                let index = app.settings.macros.len() - 1;
                app.macros.selected = Some(index);
                if let Some(draft) = app.macros.draft.as_mut() {
                    draft.index = Some(index);
                }
            }
        }
        app.save_settings();
        app.toast(tf("“{}” saved to the library", &[&name]), false);
    }
    if let Some(index) = delete {
        app.settings.macros.remove(index);
        app.save_settings();
        app.macros.draft = None;
        app.macros.selected = None;
    }
    if close {
        app.macros.draft = None;
        app.macros.selected = None;
    }
}

/// Every device with macro keys, and what each key does.
fn assign(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    let with_keys: Vec<Device> = snap
        .devices
        .iter()
        .filter(|d| snap.details.get(&d.serial).and_then(|x| x.macros.as_ref()).is_some_and(|m| m["keys"].as_array().is_some_and(|k| !k.is_empty())))
        .cloned()
        .collect();
    if with_keys.is_empty() {
        widgets::panel(ui, t("Macro keys"), |ui| {
            ui.label(widgets::dim(t("None of the devices here has macro keys the port can handle. Macros made in the library can be put on a keyboard's macro keys once one is plugged in.")));
            ui.label(widgets::faint(t("Mouse buttons cannot be reassigned yet.")));
        });
        return;
    }
    for device in with_keys {
        device_keys(app, ui, &device, snap);
        ui.add_space(12.0);
    }
}

/// The macro keys of one device: what each does, and changing it.
pub fn device_keys(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(info) = snap.details.get(&device.serial).and_then(|d| d.macros.clone()) else { return };
    let serial = device.serial.clone();
    let handled = info["handled"] == true;
    let keys: Vec<String> = info["keys"].as_array().map(|k| k.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default();
    let bindings = info["bindings"].clone();
    let recording = &snap.keys["recording"];
    let recording_here = recording["serial"] == serial.as_str();

    widgets::card(ui, |ui| {
        ui.horizontal(|ui| {
            widgets::heading(ui, &tf("{} · macro keys", &[&short_name(&device.name)]));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if handled {
                    widgets::badge(ui, t("Live"), ACCENT);
                } else if snap.keys["running"] == true {
                    widgets::badge(ui, t("Idle"), theme::WARN).on_hover_text(t("The daemon could not read this keyboard's key report, so its macro keys do nothing. Bindings can still be set up."));
                } else {
                    widgets::badge(ui, t("Off"), theme::WARN).on_hover_text(t("The daemon runs without key handling (--no-keys). Restart it normally to use macro keys."));
                }
            });
        });
        for key in &keys {
            let action = bindings.get(key);
            let (text, bound) = summary(action, key);
            Frame::new().fill(theme::SURFACE_HI).corner_radius(CornerRadius::same(theme::RADIUS)).inner_margin(Margin::symmetric(12, 7)).show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    Frame::new().fill(if bound { ACCENT } else { theme::BORDER_HI }).corner_radius(CornerRadius::same(4)).inner_margin(Margin::symmetric(9, 3)).show(ui, |ui| {
                        ui.label(RichText::new(key).font(theme::bold(13.0)).color(if bound { theme::ON_ACCENT } else { TEXT }));
                    });
                    let live = recording_here && recording["key"] == key.as_str();
                    if live {
                        ui.label(RichText::new(tf("Recording… {} keys so far", &[&recording["events"].as_u64().unwrap_or(0)])).color(theme::DANGER));
                    } else {
                        ui.add(egui::Label::new(RichText::new(text).color(if bound { TEXT } else { TEXT_DIM })).truncate());
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if live {
                            if widgets::danger_button(ui, t("Stop recording")).clicked() {
                                app.send(Command::MacroRecordStop { serial: serial.clone() });
                            }
                            return;
                        }
                        if bound && widgets::ghost(ui, icon::DELETE, t("Back to the default")).clicked() {
                            app.send(Command::MacroDelete { serial: serial.clone(), key: key.clone() });
                        }
                        if bound && widgets::ghost(ui, icon::PLAY, t("Run it now")).clicked() {
                            app.send(Command::MacroPlay { serial: serial.clone(), key: key.clone() });
                        }
                        if handled && !recording_here && ui.add(egui::Button::new(RichText::new(icon::RECORD).font(theme::icon_font(13.0)).color(theme::DANGER)).frame(false)).on_hover_text(t("Record on the keyboard: type the macro anywhere, then stop")).clicked() {
                            app.send(Command::MacroRecordStart { serial: serial.clone(), key: key.clone() });
                        }
                        let mut chosen: Option<usize> = None;
                        egui::ComboBox::from_id_salt(("assign", &serial, key)).selected_text(t("Put a macro on it")).width(170.0).show_ui(ui, |ui| {
                            if app.settings.macros.is_empty() {
                                ui.label(widgets::faint(t("The library is empty")));
                            }
                            for (i, item) in app.settings.macros.iter().enumerate() {
                                if ui.selectable_label(false, &item.name).clicked() {
                                    chosen = Some(i);
                                }
                            }
                        });
                        if let Some(i) = chosen {
                            let action = app.settings.macros[i].action.clone();
                            app.send(Command::MacroSet { serial: serial.clone(), key: key.clone(), action });
                        }
                        if bound && widgets::ghost(ui, icon::SAVE, t("Copy into the library")).clicked() {
                            if let Some(action) = action {
                                app.settings.macros.push(LibraryMacro { name: tf("{} of {}", &[key, &short_name(&device.name)]), action: action.clone() });
                                app.save_settings();
                                app.toast(t("Copied into the library"), false);
                            }
                        }
                    });
                });
            });
            ui.add_space(2.0);
        }
    });
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
            _ => "—",
        };
        return (tf("Sends {}", &[&default]), false);
    };
    let text = match action["type"].as_str().unwrap_or_default() {
        "keys" => {
            let steps = from_events(action["events"].as_array().map(Vec::as_slice).unwrap_or(&[]));
            let mut chords: Vec<String> = Vec::new();
            let mut held: Vec<String> = Vec::new();
            for step in steps {
                match step {
                    Step::Tap(vk) => {
                        let mut chord = held.clone();
                        chord.push(key_name(vk));
                        chords.push(chord.join("+"));
                    }
                    Step::Down(vk) => held.push(key_name(vk)),
                    Step::Up(vk) => {
                        let name = key_name(vk);
                        if held.last() == Some(&name) && chords.last().is_none_or(|c| !c.starts_with(&held.join("+"))) {
                            chords.push(held.join("+"));
                        }
                        held.retain(|h| *h != name);
                    }
                    Step::Wait(_) => {}
                }
            }
            let mut shown = chords.iter().take(6).cloned().collect::<Vec<_>>().join(", ");
            if chords.len() > 6 {
                shown.push_str(" …");
            }
            tf("Presses {}", &[&shown])
        }
        "text" => tf("Types “{}”", &[&truncate(action["text"].as_str().unwrap_or_default(), 48)]),
        "launch" => tf("Opens {}", &[&truncate(action["target"].as_str().unwrap_or_default(), 56)]),
        "disabled" => t("Does nothing").to_string(),
        _ => t("Other").to_string(),
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn steps_become_events_and_back() {
        let steps = vec![Step::Down(0xA2), Step::Tap(0x43), Step::Up(0xA2), Step::Wait(250), Step::Tap(0x25)];
        let events = to_events(&steps);
        assert_eq!(events.len(), 6);
        assert_eq!(events[4]["delay"], 250);
        assert_eq!(events[4]["ext"], true, "arrows are extended keys");
        assert_eq!(from_events(&events), steps);
    }

    #[test]
    fn a_chord_reads_as_one() {
        set_english();
        let action = json!({"type": "keys", "events": to_events(&[Step::Down(0xA2), Step::Tap(0x43), Step::Up(0xA2), Step::Tap(0x70)])});
        assert_eq!(summary(Some(&action), "M1").0, "Presses Ctrl+C, F1");
    }

    #[test]
    fn an_unbound_key_says_what_it_sends() {
        set_english();
        assert_eq!(summary(None, "M1"), ("Sends F13".to_string(), false));
    }

    fn set_english() {
        openrazer_studio_core::i18n::set(openrazer_studio_core::i18n::Lang::En);
    }
}
