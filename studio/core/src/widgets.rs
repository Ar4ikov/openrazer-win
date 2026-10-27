//! The small pieces every page is built from.

use crate::theme::{self, ACCENT, BORDER, BORDER_HI, ON_ACCENT, SURFACE, SURFACE_HI, TEXT, TEXT_DIM, TEXT_FAINT};
use egui::{pos2, vec2, Color32, CornerRadius, FontId, Frame, Margin, Response, RichText, Sense, Stroke, StrokeKind, Ui};

/// A raised panel holding one group of settings.
pub fn card<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> R {
    Frame::new()
        .fill(SURFACE)
        .stroke(Stroke::new(1.0_f32, BORDER))
        .corner_radius(CornerRadius::same(theme::RADIUS + 2))
        .shadow(theme::shadow())
        .inner_margin(Margin::same(20))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        })
        .inner
}

/// A card with a heading in small capitals, the way every panel starts.
pub fn panel<R>(ui: &mut Ui, title: &str, add: impl FnOnce(&mut Ui) -> R) -> R {
    card(ui, |ui| {
        heading(ui, title);
        ui.add_space(2.0);
        add(ui)
    })
}

/// SMALL CAPITALS, spaced out: a heading over a group.
pub fn heading(ui: &mut Ui, title: &str) {
    ui.label(RichText::new(title.to_uppercase()).font(theme::bold(12.5)).color(TEXT).extra_letter_spacing(1.2));
}

/// A quieter label over a few controls.
pub fn section(ui: &mut Ui, title: &str) {
    ui.label(RichText::new(title.to_uppercase()).font(theme::bold(11.0)).color(TEXT_FAINT).extra_letter_spacing(1.0));
}

/// The big title at the top of a page.
pub fn page_title(ui: &mut Ui, title: &str, subtitle: &str) {
    ui.label(RichText::new(title.to_uppercase()).font(theme::heavy(26.0)).color(TEXT).extra_letter_spacing(1.5));
    if !subtitle.is_empty() {
        ui.add(egui::Label::new(RichText::new(subtitle).color(TEXT_DIM)).wrap());
    }
}

pub fn dim(text: impl Into<String>) -> RichText {
    RichText::new(text).color(TEXT_DIM)
}

pub fn faint(text: impl Into<String>) -> RichText {
    RichText::new(text).size(12.5).color(TEXT_FAINT)
}

pub fn icon(ui: &mut Ui, glyph: &str, size: f32, color: Color32) -> Response {
    ui.label(RichText::new(glyph).font(theme::icon_font(size)).color(color))
}

fn glyph_job(glyph: &str, text: &str, colour: Color32, font: FontId) -> egui::text::LayoutJob {
    let mut job = egui::text::LayoutJob::default();
    job.append(glyph, 0.0, egui::TextFormat { font_id: theme::icon_font(13.0), color: colour, valign: egui::Align::Center, ..Default::default() });
    if !text.is_empty() {
        job.append(text, 8.0, egui::TextFormat { font_id: font, color: colour, valign: egui::Align::Center, ..Default::default() });
    }
    job
}

/// A plain button with an icon glyph before its label.
pub fn icon_button(ui: &mut Ui, glyph: &str, text: &str) -> Response {
    ui.add(egui::Button::new(glyph_job(glyph, text, TEXT, FontId::proportional(13.5))).corner_radius(CornerRadius::same(255)).min_size(vec2(0.0, 32.0)))
}

/// The one thing a panel is for: green, with dark text on it.
/// A soft accent glow under a button just laid out.
fn glow_under(ui: &Ui, response: &Response) {
    let rect = response.rect;
    let strength = if response.hovered() { 0.32 } else { 0.18 };
    let painter = ui.painter().clone().with_layer_id(egui::LayerId::new(egui::Order::Background, egui::Id::new("button-glow")));
    theme::glow(&painter, rect.center() + vec2(0.0, 4.0), vec2(rect.width() * 0.75, rect.height() * 1.1), ACCENT.gamma_multiply(strength));
}

pub fn primary_button(ui: &mut Ui, text: &str) -> Response {
    let response = ui.add(
        egui::Button::new(RichText::new(text.to_uppercase()).font(theme::bold(12.5)).color(ON_ACCENT).extra_letter_spacing(0.8))
            .fill(ACCENT)
            .stroke(Stroke::NONE)
            .corner_radius(CornerRadius::same(255))
            .min_size(vec2(0.0, 32.0)),
    );
    glow_under(ui, &response);
    response
}

pub fn primary_icon_button(ui: &mut Ui, glyph: &str, text: &str) -> Response {
    let response = ui.add(
        egui::Button::new(glyph_job(glyph, &text.to_uppercase(), ON_ACCENT, theme::bold(12.5)))
            .fill(ACCENT)
            .stroke(Stroke::NONE)
            .corner_radius(CornerRadius::same(255))
            .min_size(vec2(0.0, 32.0)),
    );
    glow_under(ui, &response);
    response
}

pub fn danger_button(ui: &mut Ui, text: &str) -> Response {
    ui.add(
        egui::Button::new(RichText::new(text).color(theme::DANGER))
            .fill(Color32::from_rgb(0x24, 0x10, 0x13))
            .stroke(Stroke::new(1.0_f32, Color32::from_rgb(0x55, 0x22, 0x2A)))
            .corner_radius(CornerRadius::same(255))
            .min_size(vec2(0.0, 32.0)),
    )
}

/// A frameless icon, for actions on a row.
pub fn ghost(ui: &mut Ui, glyph: &str, hint: &str) -> Response {
    ui.add(egui::Button::new(RichText::new(glyph).font(theme::icon_font(13.5)).color(TEXT_DIM)).frame(false)).on_hover_text(hint)
}

/// A row of pills, one selected.  Returns true when the selection changed.
pub fn segmented<T: PartialEq + Clone>(ui: &mut Ui, options: &[(T, String)], current: &mut T) -> bool {
    let mut changed = false;
    Frame::new()
        .fill(theme::WELL)
        .stroke(Stroke::new(1.0_f32, BORDER))
        .corner_radius(CornerRadius::same(255))
        .inner_margin(Margin::same(3))
        .show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing.x = 2.0;
                for (value, label) in options {
                    let selected = value == current;
                    let text = RichText::new(label).size(13.0).color(if selected { ON_ACCENT } else { TEXT_DIM });
                    let button = egui::Button::new(if selected { text.font(theme::bold(13.0)) } else { text })
                        .fill(if selected { ACCENT } else { Color32::TRANSPARENT })
                        .stroke(Stroke::NONE)
                        .corner_radius(CornerRadius::same(255))
                        .min_size(vec2(44.0, 26.0));
                    if ui.add(button).clicked() && !selected {
                        *current = value.clone();
                        changed = true;
                    }
                }
            });
        });
    changed
}

/// A switch.
pub fn toggle(ui: &mut Ui, on: &mut bool) -> Response {
    let size = vec2(38.0, 20.0);
    let (rect, mut response) = ui.allocate_exact_size(size, Sense::click());
    if response.clicked() {
        *on = !*on;
        response.mark_changed();
    }
    let t = ui.ctx().animate_bool_responsive(response.id, *on);
    let track = SURFACE_HI.lerp_to_gamma(ACCENT, t);
    ui.painter().rect_filled(rect, CornerRadius::same(10), track);
    ui.painter().rect_stroke(rect, CornerRadius::same(10), Stroke::new(1.0_f32, BORDER_HI.lerp_to_gamma(ACCENT, t)), StrokeKind::Inside);
    let x = egui::lerp(rect.left() + 10.0..=rect.right() - 10.0, t);
    ui.painter().circle_filled(pos2(x, rect.center().y), 7.0, if *on { ON_ACCENT } else { TEXT_DIM });
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A labelled switch on one row: label and hint on the left, switch on the right.
pub fn toggle_row(ui: &mut Ui, label: &str, hint: &str, on: &mut bool) -> bool {
    let mut changed = false;
    ui.horizontal(|ui| {
        ui.vertical(|ui| {
            ui.spacing_mut().item_spacing.y = 2.0;
            ui.label(RichText::new(label).size(14.0));
            if !hint.is_empty() {
                ui.add(egui::Label::new(RichText::new(hint).size(12.5).color(TEXT_DIM)).wrap());
            }
        });
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            changed = toggle(ui, on).changed();
        });
    });
    changed
}

/// A small rounded label: a status, a count, "Planned".
pub fn badge(ui: &mut Ui, text: &str, colour: Color32) -> Response {
    Frame::new()
        .fill(colour.gamma_multiply(0.14))
        .stroke(Stroke::new(1.0_f32, colour.gamma_multiply(0.45)))
        .corner_radius(CornerRadius::same(255))
        .inner_margin(Margin::symmetric(7, 2))
        .show(ui, |ui| {
            ui.label(RichText::new(text.to_uppercase()).font(theme::bold(10.5)).color(colour).extra_letter_spacing(0.6));
        })
        .response
}

/// A neutral chip: a fact about a device.
pub fn chip(ui: &mut Ui, text: &str) {
    Frame::new().fill(SURFACE_HI).corner_radius(CornerRadius::same(255)).inner_margin(Margin::symmetric(10, 3)).show(ui, |ui| {
        ui.label(RichText::new(text).size(12.0).color(TEXT_DIM));
    });
}

/// A note with an icon and a coloured edge: a warning, a tip.
pub fn banner(ui: &mut Ui, colour: Color32, glyph: &str, title: &str, text: &str, action: impl FnOnce(&mut Ui)) {
    Frame::new()
        .fill(colour.gamma_multiply(0.08))
        .stroke(Stroke::new(1.0_f32, colour.gamma_multiply(0.45)))
        .corner_radius(CornerRadius::same(theme::RADIUS + 2))
        .inner_margin(Margin::symmetric(18, 14))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.horizontal(|ui| {
                icon(ui, glyph, 16.0, colour);
                ui.vertical(|ui| {
                    ui.spacing_mut().item_spacing.y = 3.0;
                    ui.label(RichText::new(title).font(theme::bold(14.0)));
                    if !text.is_empty() {
                        ui.add(egui::Label::new(dim(text)).wrap());
                    }
                    action(ui);
                });
            });
        });
}

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
    Color32::from_rgb((r * 255.0) as u8, (g * 255.0) as u8, (b * 255.0) as u8)
}

/// A colour swatch; `selected` draws a ring around it.
pub fn swatch(ui: &mut Ui, colour: Color32, size: f32, selected: bool) -> Response {
    let (rect, response) = ui.allocate_exact_size(vec2(size, size), Sense::click());
    let painter = ui.painter();
    if selected {
        painter.rect_stroke(rect.expand(3.0), CornerRadius::same(theme::RADIUS + 2), Stroke::new(2.0_f32, TEXT), StrokeKind::Outside);
    } else if response.hovered() {
        painter.rect_stroke(rect.expand(2.0), CornerRadius::same(theme::RADIUS + 1), Stroke::new(1.0_f32, TEXT_DIM), StrokeKind::Outside);
    }
    painter.rect_filled(rect, CornerRadius::same(theme::RADIUS), colour);
    painter.rect_stroke(rect, CornerRadius::same(theme::RADIUS), Stroke::new(1.0_f32, Color32::from_white_alpha(28)), StrokeKind::Inside);
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A key cap with a name on it, the way shortcuts are shown.
pub fn keycap(ui: &mut Ui, text: &str) {
    Frame::new()
        .fill(SURFACE_HI)
        .stroke(Stroke::new(1.0_f32, BORDER_HI))
        .corner_radius(CornerRadius::same(4))
        .inner_margin(Margin::symmetric(7, 2))
        .show(ui, |ui| {
            ui.label(RichText::new(text).font(theme::bold(12.0)).color(TEXT));
        });
}

/// A label that is also a link, opened in the browser.
pub fn link(ui: &mut Ui, text: &str, url: &str) {
    ui.hyperlink_to(RichText::new(text).color(ACCENT), url);
}
