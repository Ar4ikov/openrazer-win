//! The pieces every page is built from: a small design system.
//!
//! Layout: [`page_header`], [`panel`] (a bordered box with a title row),
//! [`row`] (label and help on the left, the control on the right),
//! [`divider`], [`columns`], [`equalise`].
//! Actions: [`Button`] (primary, secondary, ghost, danger) and [`IconButton`].
//! Input: [`text_field`], [`number_field`], [`slider`], [`slider_field`],
//! [`select`], [`toggle`], [`segmented`], [`tabs`], [`colour_picker`],
//! [`hex_field`]. Lists: [`list_row`]. Status: [`badge`], [`chip`],
//! [`banner`], [`empty_state`].
//!
//! Every control has one size in every state -- hover, pressed, selected,
//! disabled -- so nothing beside it moves; state shows in colour only.

use crate::theme::{self, ACCENT, ACCENT_DIM, ACCENT_HI, BORDER, BORDER_HI, BORDER_HOVER, CONTROL_H, CONTROL_H_SM, DANGER, ON_ACCENT, RADIUS, RADIUS_SM, SURFACE, SURFACE_ACTIVE, SURFACE_HI, SURFACE_HOVER, TEXT, TEXT_DIM, TEXT_FAINT, WELL};
use egui::text::{LayoutJob, TextFormat, TextWrapping};
use egui::{pos2, vec2, Align, Align2, Color32, CornerRadius, FontId, Frame, Galley, Id, Layout, Margin, Rect, Response, RichText, Sense, Stroke, StrokeKind, TextEdit, Ui, UiBuilder, WidgetText};
use std::hash::Hash;
use std::ops::RangeInclusive;
use std::sync::Arc;

// -- text --------------------------------------------------------------------------------

pub fn dim(text: impl Into<String>) -> RichText {
    RichText::new(text).color(TEXT_DIM)
}

pub fn faint(text: impl Into<String>) -> RichText {
    RichText::new(text).size(12.5).color(TEXT_FAINT)
}

/// Help text: muted, wrapping.
pub fn help(ui: &mut Ui, text: &str) {
    ui.add(egui::Label::new(RichText::new(text).size(12.5).color(TEXT_FAINT)).wrap());
}

pub fn icon(ui: &mut Ui, glyph: &str, size: f32, colour: Color32) -> Response {
    ui.label(RichText::new(glyph).font(theme::icon_font(size)).color(colour))
}

/// `text` laid out on one line.
pub fn galley(ui: &Ui, text: &str, font: FontId, colour: Color32, spacing: f32) -> Arc<Galley> {
    let mut job = LayoutJob::default();
    job.append(text, 0.0, TextFormat { font_id: font, color: colour, extra_letter_spacing: spacing, valign: Align::Center, ..Default::default() });
    ui.fonts(|f| f.layout_job(job))
}

/// `text` on one line, cut short with an ellipsis past `width`.
pub fn galley_fit(ui: &Ui, text: &str, font: FontId, colour: Color32, width: f32) -> Arc<Galley> {
    let mut job = LayoutJob::default();
    job.append(text, 0.0, TextFormat { font_id: font, color: colour, valign: Align::Center, ..Default::default() });
    job.wrap = TextWrapping::truncate_at_width(width.max(8.0));
    ui.fonts(|f| f.layout_job(job))
}

/// SMALL CAPITALS, spaced out: the title of a panel or a group.
pub fn section_title(ui: &mut Ui, title: &str) -> Response {
    ui.label(RichText::new(title.to_uppercase()).font(theme::bold(11.5)).color(TEXT_DIM).extra_letter_spacing(1.2))
}

/// A heading over a few controls inside a panel.
pub fn heading(ui: &mut Ui, title: &str) {
    ui.label(RichText::new(title).font(theme::bold(14.0)).color(TEXT));
}

// -- layout --------------------------------------------------------------------------------

/// The top of a page: its title, a line under it, and actions on the right.
pub fn page_header(ui: &mut Ui, title: &str, subtitle: &str, actions: impl FnOnce(&mut Ui)) {
    let width = ui.available_width();
    ui.allocate_ui_with_layout(vec2(width, 56.0), Layout::left_to_right(Align::Center), |ui| {
        ui.set_min_size(vec2(width, 56.0));
        let text_width = (width - 260.0).max(width * 0.5);
        ui.allocate_ui_with_layout(vec2(text_width, 56.0), Layout::top_down(Align::Min), |ui| {
            ui.spacing_mut().item_spacing.y = 2.0;
            ui.add_space(if subtitle.is_empty() { 12.0 } else { 4.0 });
            ui.label(RichText::new(title.to_uppercase()).font(theme::heavy(22.0)).color(TEXT).extra_letter_spacing(1.0));
            if !subtitle.is_empty() {
                ui.add(egui::Label::new(RichText::new(subtitle).size(13.0).color(TEXT_DIM)).truncate());
            }
        });
        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            ui.spacing_mut().item_spacing.x = 8.0;
            actions(ui);
        });
    });
    ui.add_space(8.0);
}

/// A hairline across the available width.
pub fn divider(ui: &mut Ui) {
    let (rect, _) = ui.allocate_exact_size(vec2(ui.available_width(), 1.0), Sense::hover());
    ui.painter().hline(rect.x_range(), rect.center().y, Stroke::new(1.0_f32, BORDER));
}

fn panel_frame() -> Frame {
    Frame::new().fill(SURFACE).stroke(Stroke::new(1.0_f32, BORDER)).corner_radius(CornerRadius::same(RADIUS))
}

/// A bordered box holding one group of settings, under a title row.
pub fn panel<R>(ui: &mut Ui, title: &str, add: impl FnOnce(&mut Ui) -> R) -> R {
    panel_with(ui, title, |_| {}, add)
}

/// A panel with actions on the right of its title row.
pub fn panel_with<R>(ui: &mut Ui, title: &str, actions: impl FnOnce(&mut Ui), add: impl FnOnce(&mut Ui) -> R) -> R {
    panel_frame()
        .show(ui, |ui| {
            let width = ui.available_width();
            ui.set_width(width);
            ui.spacing_mut().item_spacing.y = 0.0;
            if !title.is_empty() {
                ui.allocate_ui_with_layout(vec2(width, 44.0), Layout::left_to_right(Align::Center), |ui| {
                    ui.set_min_size(vec2(width, 44.0));
                    ui.add_space(theme::PAD as f32);
                    section_title(ui, title);
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.spacing_mut().item_spacing.x = 6.0;
                        ui.add_space(10.0);
                        actions(ui);
                    });
                });
                divider(ui);
            }
            Frame::new()
                .inner_margin(Margin::same(theme::PAD))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.spacing_mut().item_spacing.y = 8.0;
                    add(ui)
                })
                .inner
        })
        .inner
}

/// A panel without a title: tiles, empty states.
pub fn plain_panel<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> R {
    panel_frame()
        .inner_margin(Margin::same(theme::PAD))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        })
        .inner
}

/// One setting: its name (and a line of help under it) on the left, its
/// control on the right. The control column starts at the same x on every row.
pub fn row(ui: &mut Ui, label: &str, help_text: &str, control: impl FnOnce(&mut Ui)) {
    let width = ui.available_width();
    let label_width = (width * 0.38).clamp(110.0, theme::LABEL_W);
    ui.horizontal_top(|ui| {
        ui.spacing_mut().item_spacing.x = theme::GAP;
        label_column(ui, label_width, label, help_text);
        let rest = ui.available_width();
        ui.allocate_ui_with_layout(vec2(rest, CONTROL_H), Layout::left_to_right(Align::Center), |ui| {
            ui.set_min_size(vec2(rest, CONTROL_H));
            ui.spacing_mut().item_spacing.x = 8.0;
            control(ui);
        });
    });
}

/// A setting's name, and a line of help under it, `width` wide.
fn label_column(ui: &mut Ui, width: f32, label: &str, help_text: &str) {
    ui.allocate_ui_with_layout(vec2(width, CONTROL_H), Layout::top_down(Align::Min), |ui| {
        ui.set_width(width);
        ui.spacing_mut().item_spacing.y = 2.0;
        ui.add_space(6.0);
        ui.add(egui::Label::new(RichText::new(label).size(14.0).color(TEXT)).wrap());
        if !help_text.is_empty() {
            help(ui, help_text);
        }
    });
}

/// A setting whose control is one button: the name and help across the
/// row, the button (`control_width` wide at most) at its right end.
pub fn action_row(ui: &mut Ui, label: &str, help_text: &str, control_width: f32, control: impl FnOnce(&mut Ui)) {
    let width = ui.available_width();
    ui.horizontal_top(|ui| {
        ui.spacing_mut().item_spacing.x = theme::GAP;
        label_column(ui, (width - control_width - theme::GAP).max(80.0), label, help_text);
        ui.allocate_ui_with_layout(vec2(control_width, CONTROL_H), Layout::right_to_left(Align::Center), |ui| {
            ui.set_min_size(vec2(control_width, CONTROL_H));
            control(ui);
        });
    });
}

/// A label and a value, read-only.
pub fn fact(ui: &mut Ui, label: &str, value: &str) {
    row(ui, label, "", |ui| {
        ui.add(egui::Label::new(RichText::new(value).color(TEXT_DIM)).truncate());
    });
}

/// Two columns side by side, `split` of the width to the first; one above
/// the other when there is less than `min_width` to share. `column` draws
/// column 0, then column 1.
pub fn columns(ui: &mut Ui, split: f32, min_width: f32, mut column: impl FnMut(&mut Ui, usize)) {
    let width = ui.available_width();
    let gap = theme::GAP;
    if width < min_width {
        ui.vertical(|ui| {
            ui.spacing_mut().item_spacing.y = gap;
            column(ui, 0);
            column(ui, 1);
        });
        return;
    }
    let first = ((width - gap) * split).floor();
    let second = width - gap - first;
    ui.horizontal_top(|ui| {
        ui.spacing_mut().item_spacing.x = gap;
        for (i, w) in [first, second].into_iter().enumerate() {
            ui.allocate_ui_with_layout(vec2(w, 0.0), Layout::top_down(Align::Min), |ui| {
                ui.set_width(w);
                // The gap is between the columns, not between what is in them.
                ui.spacing_mut().item_spacing = vec2(theme::SPACING, gap);
                column(ui, i);
            });
        }
    });
}

/// Pad the cell being drawn so that what follows starts as low as in the
/// tallest cell sharing `row`: tiles in a row then end level, with their
/// buttons in line, whatever each holds above them.
pub fn equalise(ui: &mut Ui, row: Id) {
    let natural = ui.min_rect().height();
    let pass = ui.ctx().cumulative_pass_nr();
    let (mut last, mut now, mut seen) = ui.data(|d| d.get_temp::<(f32, f32, u64)>(row)).unwrap_or((0.0, 0.0, pass));
    if seen != pass {
        if (now - last).abs() > 0.5 {
            ui.ctx().request_repaint();
        }
        last = now;
        now = 0.0;
        seen = pass;
    }
    now = now.max(natural);
    ui.data_mut(|d| d.insert_temp(row, (last, now, seen)));
    if last > natural {
        ui.add_space(last - natural);
    }
}

// -- buttons ---------------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    /// The one thing a page or panel is for: green, dark text.
    Primary,
    /// Everything else you click: filled, with a border.
    Secondary,
    /// Quieter, for toolbars: no fill until hovered, still a border.
    Ghost,
    /// Removes or stops something.
    Danger,
}

/// A button: always a box with a border or a fill, never bare text.
#[must_use]
pub struct Button<'a> {
    text: &'a str,
    glyph: Option<&'a str>,
    kind: Kind,
    height: f32,
    min_width: f32,
    width: Option<f32>,
    enabled: bool,
}

impl<'a> Button<'a> {
    fn new(kind: Kind, text: &'a str) -> Self {
        Button { text, glyph: None, kind, height: CONTROL_H, min_width: 0.0, width: None, enabled: true }
    }
    pub fn primary(text: &'a str) -> Self {
        Self::new(Kind::Primary, text)
    }
    pub fn secondary(text: &'a str) -> Self {
        Self::new(Kind::Secondary, text)
    }
    pub fn ghost(text: &'a str) -> Self {
        Self::new(Kind::Ghost, text)
    }
    pub fn danger(text: &'a str) -> Self {
        Self::new(Kind::Danger, text)
    }
    pub fn icon(mut self, glyph: &'a str) -> Self {
        self.glyph = Some(glyph);
        self
    }
    pub fn small(mut self) -> Self {
        self.height = CONTROL_H_SM;
        self
    }
    pub fn min_width(mut self, width: f32) -> Self {
        self.min_width = width;
        self
    }
    /// Exactly this wide; the label is cut short if it must be.
    pub fn width(mut self, width: f32) -> Self {
        self.width = Some(width);
        self
    }
    pub fn enabled(mut self, enabled: bool) -> Self {
        self.enabled = enabled;
        self
    }
}

impl egui::Widget for Button<'_> {
    fn ui(self, ui: &mut Ui) -> Response {
        let enabled = self.enabled && ui.is_enabled();
        let font = if self.kind == Kind::Primary { theme::bold(13.5) } else { theme::body(13.5) };
        let pad = if self.height < CONTROL_H { 10.0 } else { 14.0 };
        let icon_galley = self.glyph.map(|g| galley(ui, g, theme::icon_font(13.0), TEXT, 0.0));
        let icon_width = icon_galley.as_ref().map_or(0.0, |g| g.size().x + if self.text.is_empty() { 0.0 } else { 8.0 });
        let natural = galley(ui, self.text, font.clone(), TEXT, 0.0);
        let width = self.width.unwrap_or((natural.size().x + icon_width + pad * 2.0).max(self.min_width).max(self.height));
        let text = galley_fit(ui, self.text, font, TEXT, width - pad * 2.0 - icon_width);
        let (rect, response) = ui.allocate_exact_size(vec2(width, self.height), if enabled { Sense::click() } else { Sense::hover() });
        if ui.is_rect_visible(rect) {
            let hovered = enabled && response.hovered();
            let pressed = enabled && response.is_pointer_button_down_on();
            let (fill, stroke, fg) = match self.kind {
                Kind::Primary => (if pressed { ACCENT_DIM } else if hovered { ACCENT_HI } else { ACCENT }, Stroke::NONE, ON_ACCENT),
                Kind::Secondary => (if pressed { SURFACE_HI } else if hovered { SURFACE_HOVER } else { SURFACE_HI }, Stroke::new(1.0_f32, if hovered { BORDER_HOVER } else { BORDER_HI }), TEXT),
                Kind::Ghost => (if hovered { SURFACE_HI } else { Color32::TRANSPARENT }, Stroke::new(1.0_f32, if hovered { BORDER_HOVER } else { BORDER_HI }), TEXT),
                Kind::Danger => (DANGER.gamma_multiply(if hovered { 0.2 } else { 0.1 }), Stroke::new(1.0_f32, DANGER.gamma_multiply(if hovered { 0.95 } else { 0.6 })), DANGER),
            };
            let (fill, stroke, fg) = if enabled {
                (fill, stroke, fg)
            } else if self.kind == Kind::Primary {
                (ACCENT.gamma_multiply(0.3), stroke, ON_ACCENT)
            } else {
                (fill, Stroke::new(1.0_f32, BORDER), TEXT_FAINT)
            };
            let painter = ui.painter();
            painter.rect(rect, CornerRadius::same(RADIUS_SM), fill, stroke, StrokeKind::Inside);
            let content = icon_width + text.size().x;
            let mut x = rect.center().x - content / 2.0;
            if let Some(g) = icon_galley {
                painter.galley_with_override_text_color(pos2(x, rect.center().y - g.size().y / 2.0), g.clone(), fg);
                x += icon_width;
            }
            painter.galley_with_override_text_color(pos2(x, rect.center().y - text.size().y / 2.0), text, fg);
        }
        if enabled {
            response.on_hover_cursor(egui::CursorIcon::PointingHand)
        } else {
            response
        }
    }
}

/// A square button with an icon, for actions on a row. Bordered, so it reads
/// as a button; the hint says what it does.
#[must_use]
pub struct IconButton<'a> {
    glyph: &'a str,
    hint: &'a str,
    size: f32,
    danger: bool,
    on: bool,
    enabled: bool,
}

impl<'a> IconButton<'a> {
    pub fn new(glyph: &'a str, hint: &'a str) -> Self {
        IconButton { glyph, hint, size: CONTROL_H_SM, danger: false, on: false, enabled: true }
    }
    pub fn danger(mut self) -> Self {
        self.danger = true;
        self
    }
    /// Drawn pressed in: a switch that is on.
    pub fn on(mut self, on: bool) -> Self {
        self.on = on;
        self
    }
    pub fn size(mut self, size: f32) -> Self {
        self.size = size;
        self
    }
    pub fn enabled(mut self, enabled: bool) -> Self {
        self.enabled = enabled;
        self
    }
}

impl egui::Widget for IconButton<'_> {
    fn ui(self, ui: &mut Ui) -> Response {
        let enabled = self.enabled && ui.is_enabled();
        let (rect, response) = ui.allocate_exact_size(vec2(self.size, self.size), if enabled { Sense::click() } else { Sense::hover() });
        if ui.is_rect_visible(rect) {
            let hovered = enabled && response.hovered();
            let fill = if hovered { SURFACE_HOVER } else if self.on { SURFACE_HI } else { Color32::TRANSPARENT };
            let stroke = Stroke::new(1.0_f32, if hovered { BORDER_HOVER } else if enabled { BORDER_HI } else { BORDER });
            let colour = match (enabled, self.danger, hovered) {
                (false, _, _) => TEXT_FAINT.gamma_multiply(0.6),
                (true, true, true) => DANGER,
                (true, _, true) => TEXT,
                (true, _, false) => TEXT_DIM,
            };
            ui.painter().rect(rect, CornerRadius::same(RADIUS_SM), fill, stroke, StrokeKind::Inside);
            ui.painter().text(rect.center(), Align2::CENTER_CENTER, self.glyph, theme::icon_font(self.size * 0.45), colour);
        }
        let response = if enabled { response.on_hover_cursor(egui::CursorIcon::PointingHand) } else { response };
        if self.hint.is_empty() {
            response
        } else {
            response.on_hover_text(self.hint)
        }
    }
}

/// A button that opens a menu under it, drawn like a secondary button with a
/// chevron. `add` fills the menu (with [`option`]); its result comes back
/// while the menu is open.
pub fn menu_button<R>(ui: &mut Ui, glyph: &str, text: &str, add: impl FnOnce(&mut Ui) -> R) -> Option<R> {
    let mut job = LayoutJob::default();
    let format = |font: FontId, color: Color32| TextFormat { font_id: font, color, valign: Align::Center, ..Default::default() };
    if !glyph.is_empty() {
        job.append(glyph, 0.0, format(theme::icon_font(12.0), TEXT));
    }
    job.append(text, if glyph.is_empty() { 0.0 } else { 8.0 }, format(theme::body(13.5), TEXT));
    job.append(theme::icon::CHEVRON_DOWN, 8.0, format(theme::icon_font(9.0), TEXT_DIM));
    ui.scope(|ui| {
        ui.spacing_mut().button_padding = vec2(10.0, 4.0);
        let button = egui::Button::new(job).min_size(vec2(0.0, CONTROL_H_SM));
        egui::menu::menu_custom_button(ui, button, |ui| {
            ui.set_min_width(220.0);
            ui.spacing_mut().item_spacing.y = 2.0;
            add(ui)
        })
        .inner
    })
    .inner
}

/// A small label over a control, for narrow panels where a two-column row
/// does not fit.
pub fn stacked<R>(ui: &mut Ui, label: &str, add: impl FnOnce(&mut Ui) -> R) -> R {
    ui.vertical(|ui| {
        ui.spacing_mut().item_spacing.y = 4.0;
        ui.label(RichText::new(label).size(12.5).color(TEXT_DIM));
        add(ui)
    })
    .inner
}

/// A link that is also a button: opens `url` in the browser.
pub fn link_button(ui: &mut Ui, text: &str, url: &str) -> Response {
    let response = ui.add(Button::ghost(text).icon(theme::icon::OPEN).small());
    if response.clicked() {
        ui.ctx().open_url(egui::OpenUrl::new_tab(url));
    }
    response.on_hover_text(url)
}

/// An underlined link, for inside running text.
pub fn link(ui: &mut Ui, text: &str, url: &str) {
    ui.hyperlink_to(RichText::new(text).color(ACCENT), url);
}

// -- input ------------------------------------------------------------------------------------

/// Lay `add` out in exactly `size`: a text box left to itself leaves more
/// room after it than it takes, pushing whatever follows out of line.
fn exact<R>(ui: &mut Ui, size: egui::Vec2, add: impl FnOnce(&mut Ui) -> R) -> R {
    ui.allocate_ui_with_layout(size, Layout::left_to_right(Align::Center), |ui| {
        ui.set_min_size(size);
        add(ui)
    })
    .inner
}

/// A box to type one line in: sunk, bordered, a green ring when focused.
pub fn text_field(ui: &mut Ui, text: &mut String, hint: &str, width: f32) -> Response {
    let width = if width.is_finite() { width } else { ui.available_width() };
    exact(ui, vec2(width, CONTROL_H), |ui| ui.add(
        TextEdit::singleline(text)
            .hint_text(RichText::new(hint).color(TEXT_FAINT))
            .font(theme::body(14.0))
            .margin(Margin::symmetric(10, 0))
            .min_size(vec2(width, CONTROL_H))
            .desired_width(width - 20.0)
            .vertical_align(Align::Center)
            .background_color(WELL),
    ))
}

/// A box to type several lines in.
pub fn text_area(ui: &mut Ui, text: &mut String, hint: &str, rows: usize) -> Response {
    let width = ui.available_width();
    ui.add(
        TextEdit::multiline(text)
            .hint_text(RichText::new(hint).color(TEXT_FAINT))
            .font(theme::body(14.0))
            .margin(Margin::symmetric(10, 8))
            .desired_rows(rows)
            .desired_width(width - 20.0)
            .background_color(WELL),
    )
}

fn parse_number(text: &str, suffix: &str) -> Option<f64> {
    let text = text.trim();
    let text = text.strip_suffix(suffix.trim()).unwrap_or(text);
    let cleaned: String = text.chars().filter(|c| c.is_ascii_digit() || matches!(c, '.' | ',' | '-')).map(|c| if c == ',' { '.' } else { c }).collect();
    cleaned.parse::<f64>().ok().filter(|v| v.is_finite())
}

/// A box to type a number in, with its unit inside on the right. The value
/// is taken on Enter or when the box is left, and kept within `range`.
/// True when the value changed.
#[allow(clippy::too_many_arguments)]
pub fn number_field(ui: &mut Ui, id_salt: impl Hash, value: &mut f64, range: RangeInclusive<f64>, decimals: usize, suffix: &str, width: f32) -> bool {
    let id = Id::new(("number-field", id_salt));
    let shown = |v: f64| format!("{v:.decimals$}");
    let focused = ui.memory(|m| m.has_focus(id));
    let mut text = if focused { ui.data(|d| d.get_temp::<String>(id)).unwrap_or_else(|| shown(*value)) } else { shown(*value) };
    let unit = (!suffix.is_empty()).then(|| galley(ui, suffix, theme::body(12.0), TEXT_FAINT, 0.0));
    let right = 8.0 + unit.as_ref().map_or(0.0, |g| g.size().x + 4.0);
    let output = exact(ui, vec2(width, CONTROL_H), |ui| {
        TextEdit::singleline(&mut text)
            .id(id)
            .font(theme::bold(14.0))
            .margin(Margin { left: 8, right: right.min(120.0) as i8, top: 0, bottom: 0 })
            .min_size(vec2(width, CONTROL_H))
            .desired_width((width - 8.0 - right).max(16.0))
            .vertical_align(Align::Center)
            .horizontal_align(Align::Center)
            .background_color(WELL)
            .show(ui)
    });
    let response = output.response;
    if let Some(unit) = unit {
        let outer_right = response.rect.right() + right;
        ui.painter().galley(pos2(outer_right - 8.0 - unit.size().x, response.rect.center().y - unit.size().y / 2.0), unit, TEXT_FAINT);
    }
    let mut changed = false;
    if response.has_focus() {
        ui.data_mut(|d| d.insert_temp(id, text.clone()));
    }
    if response.lost_focus() {
        if let Some(parsed) = parse_number(&text, suffix) {
            let parsed = parsed.clamp(*range.start(), *range.end());
            if (parsed - *value).abs() > f64::EPSILON {
                *value = parsed;
                changed = true;
            }
        }
        ui.data_mut(|d| d.remove::<String>(id));
    }
    changed
}

/// A slider: a track, the filled part green, a round handle. Click or drag.
pub fn slider(ui: &mut Ui, value: &mut f64, range: RangeInclusive<f64>, step: f64, width: f32) -> Response {
    let (rect, mut response) = ui.allocate_exact_size(vec2(width, CONTROL_H), Sense::click_and_drag());
    let (lo, hi) = (*range.start(), *range.end());
    let track = Rect::from_center_size(rect.center(), vec2((rect.width() - 16.0).max(8.0), 4.0));
    if response.is_pointer_button_down_on() || response.dragged() {
        if let Some(pointer) = response.interact_pointer_pos() {
            let t = ((pointer.x - track.left()) / track.width()).clamp(0.0, 1.0) as f64;
            let mut v = lo + t * (hi - lo);
            if step > 0.0 {
                v = lo + ((v - lo) / step).round() * step;
            }
            let v = v.clamp(lo, hi);
            if (v - *value).abs() > f64::EPSILON {
                *value = v;
                response.mark_changed();
            }
        }
    }
    if ui.is_rect_visible(rect) {
        let t = if hi > lo { ((*value - lo) / (hi - lo)).clamp(0.0, 1.0) as f32 } else { 0.0 };
        let x = track.left() + track.width() * t;
        let enabled = ui.is_enabled();
        let hovered = enabled && (response.hovered() || response.dragged());
        let painter = ui.painter();
        painter.rect_filled(track, CornerRadius::same(2), BORDER_HI);
        painter.rect_filled(Rect::from_min_max(track.min, pos2(x, track.max.y)), CornerRadius::same(2), if enabled { ACCENT } else { TEXT_FAINT });
        let centre = pos2(x, track.center().y);
        painter.circle(centre, 8.0, if enabled { TEXT } else { TEXT_DIM }, Stroke::new(2.0_f32, if hovered { ACCENT } else { theme::SURFACE }));
    }
    if ui.is_enabled() {
        response.on_hover_cursor(egui::CursorIcon::PointingHand)
    } else {
        response
    }
}

/// A slider with a box beside it to type the value: the way every value
/// with a range is set. True when the value changed.
#[allow(clippy::too_many_arguments)]
pub fn slider_field(ui: &mut Ui, id_salt: impl Hash, value: &mut f64, range: RangeInclusive<f64>, step: f64, decimals: usize, suffix: &str, box_width: f32) -> bool {
    let mut changed = false;
    let width = ui.available_width();
    ui.allocate_ui_with_layout(vec2(width, CONTROL_H), Layout::left_to_right(Align::Center), |ui| {
        ui.spacing_mut().item_spacing.x = 12.0;
        let slider_width = (width - box_width - 12.0).clamp(60.0, 420.0);
        changed |= slider(ui, value, range.clone(), step, slider_width).changed();
        changed |= number_field(ui, id_salt, value, range, decimals, suffix, box_width);
    });
    changed
}

/// A drop-down to choose one of several. `add` fills the list, usually with
/// `ui.selectable_label`; its result comes back if the list was open.
pub fn select<R>(ui: &mut Ui, id_salt: impl Hash, selected: impl Into<WidgetText>, width: f32, add: impl FnOnce(&mut Ui) -> R) -> Option<R> {
    ui.scope(|ui| {
        let widgets = &mut ui.visuals_mut().widgets;
        for w in [&mut widgets.inactive, &mut widgets.hovered, &mut widgets.active, &mut widgets.open] {
            w.weak_bg_fill = WELL;
        }
        ui.spacing_mut().button_padding = vec2(10.0, 6.0);
        egui::ComboBox::from_id_salt(id_salt)
            .selected_text(selected)
            .width(width)
            .height(340.0)
            .truncate()
            .icon(|ui, rect, visuals, _open, _| {
                ui.painter().text(rect.center(), Align2::CENTER_CENTER, theme::icon::CHEVRON_DOWN, theme::icon_font(10.0), visuals.fg_stroke.color);
            })
            .show_ui(ui, |ui| {
                ui.set_min_width(width);
                ui.spacing_mut().item_spacing.y = 2.0;
                add(ui)
            })
            .inner
    })
    .inner
}

/// One choice in a [`select`] list.
pub fn option(ui: &mut Ui, selected: bool, text: &str) -> Response {
    let width = ui.available_width().max(ui.min_rect().width());
    let (rect, response) = ui.allocate_exact_size(vec2(width, CONTROL_H_SM + 2.0), Sense::click());
    let hovered = response.hovered();
    let painter = ui.painter();
    if selected {
        painter.rect_filled(rect, CornerRadius::same(RADIUS_SM - 1), SURFACE_ACTIVE);
    } else if hovered {
        painter.rect_filled(rect, CornerRadius::same(RADIUS_SM - 1), SURFACE_HOVER);
    }
    let colour = if selected || hovered { TEXT } else { TEXT_DIM };
    let label = galley_fit(ui, text, theme::body(14.0), colour, rect.width() - 40.0);
    painter.galley(pos2(rect.left() + 10.0, rect.center().y - label.size().y / 2.0), label, colour);
    if selected {
        painter.text(pos2(rect.right() - 12.0, rect.center().y), Align2::RIGHT_CENTER, theme::icon::CHECK, theme::icon_font(11.0), ACCENT);
    }
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A switch: green when on.
pub fn toggle(ui: &mut Ui, on: &mut bool) -> Response {
    let size = vec2(40.0, 22.0);
    let (rect, mut response) = ui.allocate_exact_size(size, Sense::click());
    if response.clicked() {
        *on = !*on;
        response.mark_changed();
    }
    let t = ui.ctx().animate_bool_with_time(response.id, *on, 0.12);
    let hovered = ui.is_enabled() && response.hovered();
    let track = SURFACE_HI.lerp_to_gamma(ACCENT, t);
    let edge = (if hovered { BORDER_HOVER } else { BORDER_HI }).lerp_to_gamma(ACCENT, t);
    let painter = ui.painter();
    painter.rect(rect, CornerRadius::same(11), track, Stroke::new(1.0_f32, edge), StrokeKind::Inside);
    let x = egui::lerp(rect.left() + 11.0..=rect.right() - 11.0, t);
    painter.circle_filled(pos2(x, rect.center().y), 7.0, TEXT_DIM.lerp_to_gamma(Color32::WHITE, t));
    if ui.is_enabled() {
        response.on_hover_cursor(egui::CursorIcon::PointingHand)
    } else {
        response
    }
}

/// A switch with its name on one settings row: the name and help across
/// the row, the switch at its right end.
pub fn toggle_row(ui: &mut Ui, label: &str, help_text: &str, on: &mut bool) -> bool {
    let mut changed = false;
    let width = ui.available_width();
    ui.horizontal_top(|ui| {
        ui.spacing_mut().item_spacing.x = theme::GAP;
        label_column(ui, (width - 40.0 - theme::GAP).max(80.0), label, help_text);
        ui.allocate_ui_with_layout(vec2(40.0, CONTROL_H), Layout::left_to_right(Align::Center), |ui| {
            ui.set_min_height(CONTROL_H);
            changed = toggle(ui, on).changed();
        });
    });
    changed
}

/// A row of equal-width segments, one chosen. The chosen one is filled; the
/// type is the same either way, so nothing moves. True when it changed.
pub fn segmented<T: PartialEq + Clone>(ui: &mut Ui, options: &[(T, String)], current: &mut T) -> bool {
    let font = theme::body(13.5);
    let widest = options.iter().map(|(_, label)| galley(ui, label, font.clone(), TEXT, 0.0).size().x).fold(0.0_f32, f32::max);
    // Squeezed, labels cut short, rather than run past the edge.
    let fits = ((ui.available_width() - 4.0) / options.len().max(1) as f32).floor();
    let segment = (widest + 28.0).max(52.0).min(fits.max(44.0));
    let size = vec2(segment * options.len() as f32 + 4.0, CONTROL_H);
    let (rect, response) = ui.allocate_exact_size(size, Sense::hover());
    let painter = ui.painter().clone();
    painter.rect(rect, CornerRadius::same(RADIUS_SM), WELL, Stroke::new(1.0_f32, BORDER_HI), StrokeKind::Inside);
    let mut changed = false;
    for (i, (value, label)) in options.iter().enumerate() {
        let cell = Rect::from_min_size(rect.min + vec2(2.0 + segment * i as f32, 2.0), vec2(segment, CONTROL_H - 4.0));
        let hit = ui.interact(cell, response.id.with(i), Sense::click());
        let selected = value == current;
        let hovered = ui.is_enabled() && hit.hovered();
        if selected {
            painter.rect(cell, CornerRadius::same(RADIUS_SM - 1), SURFACE_ACTIVE, Stroke::new(1.0_f32, ACCENT.gamma_multiply(0.8)), StrokeKind::Inside);
        } else if hovered {
            painter.rect_filled(cell, CornerRadius::same(RADIUS_SM - 1), SURFACE_HI);
        }
        let colour = if selected || hovered { TEXT } else { TEXT_DIM };
        let text = galley_fit(ui, label, font.clone(), colour, segment - 12.0);
        painter.galley(cell.center() - text.size() / 2.0, text, colour);
        if hit.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() && !selected {
            *current = value.clone();
            changed = true;
        }
    }
    changed
}

/// Tabs along the top of a page: one line of capitals, the chosen one white
/// and underlined in green. True when the tab changed.
pub fn tabs<T: PartialEq + Copy>(ui: &mut Ui, options: &[(T, &str)], current: &mut T) -> bool {
    let height = 42.0;
    let (bar, response) = ui.allocate_exact_size(vec2(ui.available_width(), height), Sense::hover());
    let painter = ui.painter().clone();
    painter.hline(bar.x_range(), bar.bottom() - 0.5, Stroke::new(1.0_f32, BORDER));
    let mut x = bar.left();
    let mut changed = false;
    for (i, (value, label)) in options.iter().enumerate() {
        let text = galley(ui, &label.to_uppercase(), theme::bold(12.5), TEXT, 1.0);
        let width = text.size().x + 32.0;
        let cell = Rect::from_min_size(pos2(x, bar.top()), vec2(width, height));
        let hit = ui.interact(cell, response.id.with(i), Sense::click());
        let selected = value == current;
        let colour = if selected || hit.hovered() { TEXT } else { TEXT_DIM };
        painter.galley_with_override_text_color(pos2(cell.left() + 16.0, cell.center().y - text.size().y / 2.0), text, colour);
        if selected {
            painter.rect_filled(Rect::from_min_max(pos2(cell.left() + 8.0, bar.bottom() - 2.0), pos2(cell.right() - 8.0, bar.bottom())), CornerRadius::same(1), ACCENT);
        }
        if hit.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() && !selected {
            *current = *value;
            changed = true;
        }
        x += width;
    }
    changed
}

/// A row in a list that is clicked to choose it: filled, lighter under the
/// pointer, green-edged when chosen. `add` draws what is on it, left to right.
pub fn list_row<R>(ui: &mut Ui, selected: bool, height: f32, add: impl FnOnce(&mut Ui) -> R) -> (Response, R) {
    let (rect, response) = ui.allocate_exact_size(vec2(ui.available_width(), height), Sense::click());
    let hovered = ui.rect_contains_pointer(rect) && ui.is_enabled();
    let fill = if selected { SURFACE_ACTIVE } else if hovered { SURFACE_HOVER } else { SURFACE_HI };
    let stroke = Stroke::new(1.0_f32, if selected { ACCENT.gamma_multiply(0.7) } else if hovered { BORDER_HI } else { Color32::TRANSPARENT });
    ui.painter().rect(rect, CornerRadius::same(RADIUS_SM), fill, stroke, StrokeKind::Inside);
    if selected {
        ui.painter().rect_filled(Rect::from_min_size(rect.min + vec2(0.0, 6.0), vec2(3.0, rect.height() - 12.0)), CornerRadius::same(2), ACCENT);
    }
    let mut child = ui.new_child(UiBuilder::new().max_rect(rect.shrink2(vec2(12.0, 0.0))).layout(Layout::left_to_right(Align::Center)));
    child.spacing_mut().item_spacing.x = 8.0;
    let inner = add(&mut child);
    (response.on_hover_cursor(egui::CursorIcon::PointingHand), inner)
}

// -- status ---------------------------------------------------------------------------------------

/// A small rounded label: a status, a count, "Planned". Drawn at its own
/// size, so a row it sits in does not stretch it.
pub fn badge(ui: &mut Ui, text: &str, colour: Color32) -> Response {
    let text = galley(ui, &text.to_uppercase(), theme::bold(10.5), colour, 0.6);
    let (rect, response) = ui.allocate_exact_size(text.size() + vec2(14.0, 6.0), Sense::hover());
    ui.painter().rect(rect, CornerRadius::same(RADIUS_SM - 1), colour.gamma_multiply(0.14), Stroke::new(1.0_f32, colour.gamma_multiply(0.5)), StrokeKind::Inside);
    ui.painter().galley(rect.center() - text.size() / 2.0, text, colour);
    response
}

/// A neutral fact about something, in a box.
pub fn chip(ui: &mut Ui, text: &str) -> Response {
    let text = galley(ui, text, theme::body(12.5), TEXT_DIM, 0.0);
    let (rect, response) = ui.allocate_exact_size(text.size() + vec2(18.0, 6.0), Sense::hover());
    ui.painter().rect(rect, CornerRadius::same(RADIUS_SM - 1), SURFACE_HI, Stroke::new(1.0_f32, BORDER), StrokeKind::Inside);
    ui.painter().galley(rect.center() - text.size() / 2.0, text, TEXT_DIM);
    response
}

/// A note across the page with an icon and a coloured edge: a warning, a tip.
pub fn banner(ui: &mut Ui, colour: Color32, glyph: &str, title: &str, text: &str, action: impl FnOnce(&mut Ui)) {
    Frame::new()
        .fill(colour.gamma_multiply(0.07))
        .stroke(Stroke::new(1.0_f32, colour.gamma_multiply(0.45)))
        .corner_radius(CornerRadius::same(RADIUS))
        .inner_margin(Margin::symmetric(16, 12))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal_top(|ui| {
                ui.spacing_mut().item_spacing.x = 12.0;
                ui.add_space(2.0);
                icon(ui, glyph, 16.0, colour);
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 4.0;
                    ui.label(RichText::new(title).font(theme::bold(14.0)).color(TEXT));
                    if !text.is_empty() {
                        ui.add(egui::Label::new(dim(text)).wrap());
                    }
                    ui.horizontal_wrapped(|ui| {
                        ui.spacing_mut().item_spacing.x = 8.0;
                        action(ui);
                    });
                });
            });
        });
}

/// Nothing to show yet: a ring, a title, why, and what to do.
pub fn empty_state(ui: &mut Ui, title: &str, text: &str, action: impl FnOnce(&mut Ui)) {
    ui.vertical_centered(|ui| {
        ui.add_space(24.0);
        let (rect, _) = ui.allocate_exact_size(vec2(56.0, 56.0), Sense::hover());
        ui.painter().circle_stroke(rect.center(), 24.0, Stroke::new(2.0_f32, BORDER_HI));
        ui.painter().circle_filled(rect.center(), 6.0, BORDER_HI);
        ui.add_space(10.0);
        ui.label(RichText::new(title).font(theme::bold(16.0)).color(TEXT));
        if !text.is_empty() {
            ui.set_max_width(520.0);
            ui.add(egui::Label::new(dim(text)).wrap());
        }
        ui.add_space(6.0);
        action(ui);
        ui.add_space(24.0);
    });
}

/// A status light and a word: running, waiting, stopped.
pub fn status_dot(ui: &mut Ui, colour: Color32, text: &str) {
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 8.0;
        let (rect, _) = ui.allocate_exact_size(vec2(8.0, 16.0), Sense::hover());
        ui.painter().circle_filled(rect.center(), 4.0, colour);
        ui.label(RichText::new(text).size(13.0).color(TEXT_DIM));
    });
}

/// A key cap with a name on it, the way keys are shown.
pub fn keycap(ui: &mut Ui, text: &str, width: f32, lit: bool) -> Response {
    let (rect, response) = ui.allocate_exact_size(vec2(width, 26.0), Sense::hover());
    let (fill, stroke, colour) = if lit { (ACCENT, Stroke::NONE, ON_ACCENT) } else { (SURFACE_HOVER, Stroke::new(1.0_f32, BORDER_HOVER), TEXT) };
    ui.painter().rect(rect, CornerRadius::same(4), fill, stroke, StrokeKind::Inside);
    let text = galley_fit(ui, text, theme::bold(12.5), colour, width - 8.0);
    ui.painter().galley(rect.center() - text.size() / 2.0, text, colour);
    response
}

// -- colour -----------------------------------------------------------------------------------------

pub fn hsv(h: f32, s: f32, v: f32) -> Color32 {
    let h = h.rem_euclid(1.0) * 6.0;
    let i = h.floor() as i32;
    let f = h - i as f32;
    let (p, q, t) = (v * (1.0 - s), v * (1.0 - s * f), v * (1.0 - s * (1.0 - f)));
    let (r, g, b) = match i {
        0 => (v, t, p),
        1 => (q, v, p),
        2 => (p, v, t),
        3 => (p, q, v),
        4 => (t, p, v),
        _ => (v, p, q),
    };
    Color32::from_rgb((r * 255.0).round() as u8, (g * 255.0).round() as u8, (b * 255.0).round() as u8)
}

/// Hue, saturation and value of `c`, each 0..1.
pub fn to_hsv(c: Color32) -> (f32, f32, f32) {
    let (r, g, b) = (c.r() as f32 / 255.0, c.g() as f32 / 255.0, c.b() as f32 / 255.0);
    let max = r.max(g).max(b);
    let min = r.min(g).min(b);
    let d = max - min;
    let h = if d <= f32::EPSILON {
        0.0
    } else if max == r {
        ((g - b) / d).rem_euclid(6.0) / 6.0
    } else if max == g {
        ((b - r) / d + 2.0) / 6.0
    } else {
        ((r - g) / d + 4.0) / 6.0
    };
    let s = if max <= f32::EPSILON { 0.0 } else { d / max };
    (h, s, max)
}

/// A colour swatch; `selected` draws a ring around it, outside its box.
pub fn swatch(ui: &mut Ui, colour: Color32, size: f32, selected: bool) -> Response {
    let (rect, response) = ui.allocate_exact_size(vec2(size, size), Sense::click());
    let painter = ui.painter();
    let hovered = ui.is_enabled() && response.hovered();
    painter.rect_filled(rect, CornerRadius::same(RADIUS_SM - 1), colour);
    painter.rect_stroke(rect, CornerRadius::same(RADIUS_SM - 1), Stroke::new(1.0_f32, Color32::from_white_alpha(30)), StrokeKind::Inside);
    if selected {
        painter.rect_stroke(rect.expand(3.0), CornerRadius::same(RADIUS_SM + 2), Stroke::new(2.0_f32, TEXT), StrokeKind::Inside);
    } else if hovered {
        painter.rect_stroke(rect.expand(3.0), CornerRadius::same(RADIUS_SM + 2), Stroke::new(1.0_f32, BORDER_HOVER), StrokeKind::Inside);
    }
    if ui.is_enabled() {
        response.on_hover_cursor(egui::CursorIcon::PointingHand)
    } else {
        response
    }
}

/// A box to type a colour in as #RRGGBB. True when a valid colour was typed.
pub fn hex_field(ui: &mut Ui, id_salt: impl Hash, colour: &mut Color32, width: f32) -> bool {
    let id = Id::new(("hex-field", id_salt));
    let shown = format!("#{:02X}{:02X}{:02X}", colour.r(), colour.g(), colour.b());
    let focused = ui.memory(|m| m.has_focus(id));
    let mut text = if focused { ui.data(|d| d.get_temp::<String>(id)).unwrap_or(shown) } else { shown };
    let response = exact(ui, vec2(width, CONTROL_H), |ui| {
        ui.add(
            TextEdit::singleline(&mut text)
                .id(id)
                .font(theme::body(14.0))
                .char_limit(7)
                .margin(Margin::symmetric(10, 0))
                .min_size(vec2(width, CONTROL_H))
                .desired_width(width - 20.0)
                .vertical_align(Align::Center)
                .background_color(WELL),
        )
    });
    let mut changed = false;
    if response.has_focus() {
        ui.data_mut(|d| d.insert_temp(id, text.clone()));
        let hex = text.trim().trim_start_matches('#');
        if hex.len() == 6 {
            if let Ok(value) = u32::from_str_radix(hex, 16) {
                let parsed = Color32::from_rgb((value >> 16) as u8, (value >> 8) as u8, value as u8);
                if parsed != *colour {
                    *colour = parsed;
                    changed = true;
                }
            }
        }
    }
    if response.lost_focus() {
        ui.data_mut(|d| d.remove::<String>(id));
    }
    changed
}

/// Choosing any colour: a saturation-and-brightness square over a hue strip,
/// both `width` wide. True when the colour changed.
pub fn colour_picker(ui: &mut Ui, id_salt: impl Hash, colour: &mut Color32, width: f32, height: f32) -> bool {
    let id = Id::new(("colour-picker", id_salt));
    // Keep the hue while the colour is grey or black, where it cannot be read back.
    let (mut h, mut s, mut v) = match ui.data(|d| d.get_temp::<(f32, f32, f32, Color32)>(id)) {
        Some((h, s, v, c)) if c == *colour => (h, s, v),
        Some((h, ..)) => {
            let (nh, ns, nv) = to_hsv(*colour);
            (if ns < 0.01 || nv < 0.01 { h } else { nh }, ns, nv)
        }
        None => to_hsv(*colour),
    };
    let mut changed = false;
    let enabled = ui.is_enabled();

    let (area, area_response) = ui.allocate_exact_size(vec2(width, height), Sense::click_and_drag());
    if area_response.is_pointer_button_down_on() || area_response.dragged() {
        if let Some(p) = area_response.interact_pointer_pos() {
            s = ((p.x - area.left()) / area.width()).clamp(0.0, 1.0);
            v = 1.0 - ((p.y - area.top()) / area.height()).clamp(0.0, 1.0);
            changed = true;
        }
    }
    ui.add_space(4.0);
    let (strip, strip_response) = ui.allocate_exact_size(vec2(width, 14.0), Sense::click_and_drag());
    if strip_response.is_pointer_button_down_on() || strip_response.dragged() {
        if let Some(p) = strip_response.interact_pointer_pos() {
            h = ((p.x - strip.left()) / strip.width()).clamp(0.0, 0.9999);
            changed = true;
        }
    }

    if ui.is_rect_visible(area) {
        let painter = ui.painter();
        // The square: saturation across, brightness down.
        let mut mesh = egui::epaint::Mesh::default();
        let n = 12u32;
        for row in 0..=n {
            for col in 0..=n {
                let (fs, fv) = (col as f32 / n as f32, 1.0 - row as f32 / n as f32);
                mesh.colored_vertex(pos2(area.left() + area.width() * col as f32 / n as f32, area.top() + area.height() * row as f32 / n as f32), hsv(h, fs, fv));
            }
        }
        for row in 0..n {
            for col in 0..n {
                let a = row * (n + 1) + col;
                mesh.add_triangle(a, a + 1, a + n + 1);
                mesh.add_triangle(a + 1, a + n + 2, a + n + 1);
            }
        }
        painter.add(egui::Shape::mesh(mesh));
        painter.rect_stroke(area, CornerRadius::same(2), Stroke::new(1.0_f32, BORDER_HI), StrokeKind::Outside);
        let at = pos2(area.left() + s * area.width(), area.top() + (1.0 - v) * area.height());
        painter.circle(at, 7.0, hsv(h, s, v), Stroke::new(2.0_f32, Color32::WHITE));
        painter.circle_stroke(at, 8.5, Stroke::new(1.0_f32, Color32::from_black_alpha(140)));

        // The hue strip.
        let mut mesh = egui::epaint::Mesh::default();
        let steps = 24u32;
        for i in 0..=steps {
            let x = strip.left() + strip.width() * i as f32 / steps as f32;
            let c = hsv(i as f32 / steps as f32, 1.0, 1.0);
            mesh.colored_vertex(pos2(x, strip.top()), c);
            mesh.colored_vertex(pos2(x, strip.bottom()), c);
        }
        for i in 0..steps {
            let k = i * 2;
            mesh.add_triangle(k, k + 1, k + 2);
            mesh.add_triangle(k + 1, k + 2, k + 3);
        }
        painter.add(egui::Shape::mesh(mesh));
        let hx = strip.left() + h * strip.width();
        let handle = Rect::from_center_size(pos2(hx, strip.center().y), vec2(6.0, strip.height() + 4.0));
        painter.rect(handle, CornerRadius::same(2), hsv(h, 1.0, 1.0), Stroke::new(2.0_f32, Color32::WHITE), StrokeKind::Outside);
    }
    if enabled {
        area_response.on_hover_cursor(egui::CursorIcon::Crosshair);
        strip_response.on_hover_cursor(egui::CursorIcon::PointingHand);
    }
    if changed {
        let picked = hsv(h, s, v);
        changed = picked != *colour;
        *colour = picked;
    }
    ui.data_mut(|d| d.insert_temp(id, (h, s, v, *colour)));
    changed
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn colours_go_to_hsv_and_back() {
        for c in [Color32::from_rgb(0x44, 0xD6, 0x2C), Color32::from_rgb(255, 0, 64), Color32::from_rgb(10, 20, 250), Color32::WHITE, Color32::BLACK] {
            let (h, s, v) = to_hsv(c);
            assert_eq!(hsv(h, s, v), c, "{c:?}");
        }
    }

    #[test]
    fn numbers_are_read_whatever_the_decimal_mark_and_unit() {
        assert_eq!(parse_number("1 600 DPI", "DPI"), Some(1600.0));
        assert_eq!(parse_number("2,5", "min"), Some(2.5));
        assert_eq!(parse_number("abc", ""), None);
    }
}
