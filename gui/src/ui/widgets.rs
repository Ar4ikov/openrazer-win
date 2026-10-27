//! Small widgets the pages are built from.

use super::theme::{self, ACCENT, BORDER, CARD, CARD_ACTIVE, CARD_HOVER, TEXT, TEXT_DIM, TEXT_FAINT};
use egui::{
    epaint::Mesh, pos2, vec2, Color32, CornerRadius, FontId, Frame, Margin, Pos2, Rect, Response, RichText,
    Sense, Shape, Stroke, StrokeKind, Ui,
};

pub fn card<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> R {
    Frame::new()
        .fill(CARD)
        .stroke(Stroke::new(1.0, BORDER))
        .corner_radius(CornerRadius::same(theme::RADIUS))
        .inner_margin(Margin::same(18))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        })
        .inner
}

pub fn section(ui: &mut Ui, title: &str) {
    ui.label(RichText::new(title.to_uppercase()).font(theme::bold(11.5)).color(TEXT_FAINT).extra_letter_spacing(0.8));
}

pub fn icon(ui: &mut Ui, glyph: &str, size: f32, color: Color32) -> Response {
    ui.label(RichText::new(glyph).font(theme::icon_font(size)).color(color))
}

/// A button with an icon glyph and a label.
pub fn icon_button(ui: &mut Ui, glyph: &str, text: &str) -> Response {
    let mut job = egui::text::LayoutJob::default();
    job.append(glyph, 0.0, egui::TextFormat { font_id: theme::icon_font(13.0), color: TEXT, valign: egui::Align::Center, ..Default::default() });
    if !text.is_empty() {
        job.append(text, 8.0, egui::TextFormat { font_id: FontId::proportional(14.0), color: TEXT, valign: egui::Align::Center, ..Default::default() });
    }
    ui.add(egui::Button::new(job))
}

pub fn primary_button(ui: &mut Ui, text: &str) -> Response {
    ui.add(
        egui::Button::new(RichText::new(text).font(theme::bold(14.0)).color(Color32::from_rgb(0x08, 0x14, 0x06)))
            .fill(ACCENT)
            .corner_radius(CornerRadius::same(8)),
    )
}

pub fn danger_button(ui: &mut Ui, text: &str) -> Response {
    ui.add(egui::Button::new(RichText::new(text).color(theme::DANGER)).fill(Color32::from_rgb(0x2A, 0x17, 0x19)))
}

/// A row of pills, one selected.  Returns true when the selection changed.
pub fn segmented<T: PartialEq + Clone>(ui: &mut Ui, options: &[(T, String)], current: &mut T) -> bool {
    let mut changed = false;
    Frame::new()
        .fill(Color32::from_rgb(0x12, 0x15, 0x1B))
        .stroke(Stroke::new(1.0, BORDER))
        .corner_radius(CornerRadius::same(9))
        .inner_margin(Margin::same(3))
        .show(ui, |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.spacing_mut().item_spacing.x = 3.0;
                for (value, label) in options {
                    let selected = value == current;
                    let text = RichText::new(label).size(13.5).color(if selected { Color32::from_rgb(0x08, 0x14, 0x06) } else { TEXT_DIM });
                    let button = egui::Button::new(if selected { text.font(theme::bold(13.5)) } else { text })
                        .fill(if selected { ACCENT } else { Color32::TRANSPARENT })
                        .stroke(Stroke::NONE)
                        .corner_radius(CornerRadius::same(7))
                        .min_size(vec2(44.0, 28.0));
                    if ui.add(button).clicked() && !selected {
                        *current = value.clone();
                        changed = true;
                    }
                }
            });
        });
    changed
}

/// An iOS-style switch.
pub fn toggle(ui: &mut Ui, on: &mut bool) -> Response {
    let size = vec2(40.0, 22.0);
    let (rect, mut response) = ui.allocate_exact_size(size, Sense::click());
    if response.clicked() {
        *on = !*on;
        response.mark_changed();
    }
    let t = ui.ctx().animate_bool_responsive(response.id, *on);
    let track = Color32::from_rgb(0x2D, 0x33, 0x40).lerp_to_gamma(ACCENT, t);
    ui.painter().rect_filled(rect, CornerRadius::same(11), track);
    let x = egui::lerp(rect.left() + 11.0..=rect.right() - 11.0, t);
    ui.painter().circle_filled(pos2(x, rect.center().y), 8.0, Color32::WHITE);
    response
}

/// A labelled switch on one row, label on the left.
pub fn toggle_row(ui: &mut Ui, label: &str, hint: &str, on: &mut bool) -> bool {
    let mut changed = false;
    ui.horizontal(|ui| {
        ui.vertical(|ui| {
            ui.label(RichText::new(label).size(14.5));
            if !hint.is_empty() {
                ui.label(RichText::new(hint).size(12.5).color(TEXT_DIM));
            }
        });
        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
            changed = toggle(ui, on).changed();
        });
    });
    changed
}

pub fn hsv(h: f32, s: f32, v: f32) -> Color32 {
    let h = (h.rem_euclid(1.0)) * 6.0;
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

fn gradient(painter: &egui::Painter, rect: Rect, colours: &[Color32], horizontal: bool) {
    let mut mesh = Mesh::default();
    let n = colours.len().max(2);
    for (i, colour) in colours.iter().enumerate() {
        let t = i as f32 / (n - 1) as f32;
        let (a, b) = if horizontal {
            let x = egui::lerp(rect.left()..=rect.right(), t);
            (pos2(x, rect.top()), pos2(x, rect.bottom()))
        } else {
            let y = egui::lerp(rect.top()..=rect.bottom(), t);
            (pos2(rect.left(), y), pos2(rect.right(), y))
        };
        mesh.colored_vertex(a, *colour);
        mesh.colored_vertex(b, *colour);
    }
    for i in 0..(colours.len() as u32 - 1) {
        let k = i * 2;
        mesh.add_triangle(k, k + 1, k + 2);
        mesh.add_triangle(k + 1, k + 2, k + 3);
    }
    painter.add(Shape::mesh(mesh));
}

/// A small picture of what an effect looks like.
pub fn effect_preview(painter: &egui::Painter, rect: Rect, effect: &str, colours: &[Color32; 3]) {
    let dark = Color32::from_rgb(0x0B, 0x0D, 0x11);
    painter.rect_filled(rect, CornerRadius::same(6), dark);
    let inner = rect.shrink(1.0);
    let c1 = colours[0];
    match effect {
        "static" | "on" => {
            painter.rect_filled(inner, CornerRadius::same(5), if effect == "on" { Color32::from_rgb(0xDD, 0xF5, 0xD8) } else { c1 });
        }
        "none" => {
            painter.line_segment([inner.left_bottom() + vec2(10.0, -8.0), inner.right_top() + vec2(-10.0, 8.0)], Stroke::new(2.0, TEXT_FAINT));
        }
        "spectrum" | "spectrum_soft" => {
            let hues: Vec<Color32> = (0..=12).map(|i| hsv(i as f32 / 12.0, 0.85, 1.0)).collect();
            gradient(painter, inner, &hues, true);
        }
        "wave" | "wave_soft" | "wheel" => {
            let bars = 9;
            for i in 0..bars {
                let w = inner.width() / bars as f32;
                let r = Rect::from_min_size(pos2(inner.left() + i as f32 * w, inner.top()), vec2(w + 0.5, inner.height()));
                let colour = if effect == "wave_soft" {
                    c1.gamma_multiply(0.25 + 0.75 * ((i as f32 / bars as f32) * std::f32::consts::PI).sin().abs())
                } else {
                    hsv(i as f32 / bars as f32, 0.85, 1.0)
                };
                painter.rect_filled(r, CornerRadius::ZERO, colour);
            }
        }
        e if e.starts_with("breath") => {
            let mut stops = vec![dark, c1, dark];
            if e == "breath_dual" {
                stops = vec![c1, dark, colours[1]];
            } else if e == "breath_triple" {
                stops = vec![c1, colours[1], colours[2]];
            } else if e == "breath_random" {
                stops = vec![hsv(0.95, 0.8, 1.0), dark, hsv(0.55, 0.8, 1.0)];
            }
            gradient(painter, inner, &stops, true);
        }
        "reactive" | "ripple" | "ripple_random" => {
            let centre = inner.center();
            for (i, radius) in [6.0, 13.0, 20.0, 27.0].iter().enumerate() {
                let colour = if effect == "ripple_random" { hsv(i as f32 * 0.22, 0.8, 1.0) } else { c1 };
                painter.circle_stroke(centre, *radius, Stroke::new(2.0, colour.gamma_multiply(1.0 - i as f32 * 0.22)));
            }
            painter.circle_filled(centre, 3.5, c1);
        }
        e if e.starts_with("starlight") => {
            let points = [(0.15, 0.3), (0.35, 0.7), (0.55, 0.25), (0.75, 0.6), (0.88, 0.2), (0.25, 0.85), (0.65, 0.88), (0.45, 0.5)];
            for (i, (x, y)) in points.iter().enumerate() {
                let colour = match e {
                    "starlight_random" => hsv(i as f32 * 0.13, 0.75, 1.0),
                    "starlight_dual" if i % 2 == 1 => colours[1],
                    _ => c1,
                };
                painter.circle_filled(pos2(inner.left() + inner.width() * x, inner.top() + inner.height() * y), 2.0 + (i % 3) as f32, colour);
            }
        }
        "blinking" => {
            let half = Rect::from_min_max(inner.min, pos2(inner.center().x, inner.max.y));
            painter.rect_filled(half, CornerRadius::same(5), c1);
        }
        _ => {
            let cells = 6;
            let w = inner.width() / cells as f32;
            let h = inner.height() / 3.0;
            for row in 0..3 {
                for col in 0..cells {
                    let r = Rect::from_min_size(pos2(inner.left() + col as f32 * w, inner.top() + row as f32 * h), vec2(w, h)).shrink(1.5);
                    painter.rect_filled(r, CornerRadius::same(2), hsv((row * cells + col) as f32 * 0.07, 0.7, 0.9));
                }
            }
        }
    }
}

/// A selectable tile with an effect preview and its name.
pub fn effect_tile(ui: &mut Ui, effect: &str, label: &str, colours: &[Color32; 3], selected: bool) -> Response {
    let size = vec2(128.0, 92.0);
    let (rect, response) = ui.allocate_exact_size(size, Sense::click());
    let painter = ui.painter_at(rect.expand(2.0));
    let fill = if selected { CARD_ACTIVE } else if response.hovered() { CARD_HOVER } else { CARD };
    painter.rect_filled(rect, CornerRadius::same(theme::RADIUS), fill);
    let stroke = if selected { Stroke::new(1.5, ACCENT) } else { Stroke::new(1.0, BORDER) };
    painter.rect_stroke(rect, CornerRadius::same(theme::RADIUS), stroke, StrokeKind::Inside);
    let preview = Rect::from_min_size(rect.min + vec2(10.0, 10.0), vec2(rect.width() - 20.0, 46.0));
    effect_preview(&painter.with_clip_rect(preview), preview, effect, colours);
    painter.text(
        pos2(rect.center().x, rect.bottom() - 17.0),
        egui::Align2::CENTER_CENTER,
        label,
        if selected { theme::bold(13.0) } else { FontId::proportional(13.0) },
        if selected { TEXT } else { TEXT_DIM },
    );
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A colour swatch; `selected` draws a ring around it.
pub fn swatch(ui: &mut Ui, colour: Color32, size: f32, selected: bool) -> Response {
    let (rect, response) = ui.allocate_exact_size(vec2(size, size), Sense::click());
    let painter = ui.painter();
    if selected {
        painter.rect_stroke(rect.expand(3.0), CornerRadius::same(10), Stroke::new(2.0, TEXT), StrokeKind::Outside);
    } else if response.hovered() {
        painter.rect_stroke(rect.expand(2.0), CornerRadius::same(10), Stroke::new(1.0, TEXT_DIM), StrokeKind::Outside);
    }
    painter.rect_filled(rect, CornerRadius::same(8), colour);
    painter.rect_stroke(rect, CornerRadius::same(8), Stroke::new(1.0, Color32::from_white_alpha(28)), StrokeKind::Inside);
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A simple line drawing of a device of `kind`, inside `rect`.
pub fn device_glyph(painter: &egui::Painter, rect: Rect, kind: &str, colour: Color32) {
    let stroke = Stroke::new((rect.width() / 18.0).max(1.4), colour);
    let c = rect.center();
    let s = rect.width().min(rect.height());
    match kind {
        "keyboard" | "keypad" => {
            let body = Rect::from_center_size(c, vec2(s * 0.86, s * 0.5));
            painter.rect_stroke(body, CornerRadius::same(4), stroke, StrokeKind::Middle);
            for row in 0..3 {
                for col in 0..6 {
                    let p = pos2(body.left() + body.width() * (0.14 + col as f32 * 0.144), body.top() + body.height() * (0.25 + row as f32 * 0.25));
                    let w = if row == 2 && (col == 2 || col == 3) { 0.0 } else { 1.0 };
                    if w > 0.0 {
                        painter.circle_filled(p, stroke.width * 0.6, colour);
                    }
                }
            }
            painter.line_segment([pos2(body.center().x - s * 0.13, body.bottom() - body.height() * 0.25), pos2(body.center().x + s * 0.13, body.bottom() - body.height() * 0.25)], stroke);
        }
        "mouse" => {
            let body = Rect::from_center_size(c, vec2(s * 0.46, s * 0.74));
            painter.rect_stroke(body, CornerRadius::same((s * 0.22) as u8), stroke, StrokeKind::Middle);
            painter.line_segment([pos2(c.x, body.top()), pos2(c.x, body.top() + body.height() * 0.36)], stroke);
            painter.line_segment([pos2(body.left(), body.top() + body.height() * 0.36), pos2(body.right(), body.top() + body.height() * 0.36)], stroke);
        }
        "headset" => {
            let radius = s * 0.32;
            let points: Vec<Pos2> = (0..=20)
                .map(|i| {
                    let a = std::f32::consts::PI * (1.0 + i as f32 / 20.0);
                    pos2(c.x + radius * a.cos(), c.y + s * 0.06 + radius * a.sin())
                })
                .collect();
            painter.add(Shape::line(points, stroke));
            for side in [-1.0f32, 1.0] {
                let cup = Rect::from_center_size(pos2(c.x + side * radius, c.y + s * 0.16), vec2(s * 0.16, s * 0.26));
                painter.rect_filled(cup, CornerRadius::same(4), colour);
            }
        }
        "mousemat" => {
            let body = Rect::from_center_size(c, vec2(s * 0.84, s * 0.56));
            painter.rect_stroke(body, CornerRadius::same(5), stroke, StrokeKind::Middle);
            painter.line_segment([body.left_bottom() + vec2(4.0, -4.0), body.right_bottom() + vec2(-4.0, -4.0)], Stroke::new(stroke.width, ACCENT));
        }
        _ => {
            painter.circle_stroke(c, s * 0.3, stroke);
            painter.circle_filled(c, s * 0.08, colour);
        }
    }
}

/// A battery: outline, fill to `level`, and a bolt when charging.
pub fn battery(ui: &mut Ui, level: f64, charging: bool) {
    let (rect, _) = ui.allocate_exact_size(vec2(30.0, 14.0), Sense::hover());
    let painter = ui.painter();
    let body = Rect::from_min_size(rect.min, vec2(26.0, 14.0));
    painter.rect_stroke(body, CornerRadius::same(3), Stroke::new(1.3, TEXT_DIM), StrokeKind::Inside);
    painter.rect_filled(Rect::from_min_size(pos2(body.right() + 1.0, body.center().y - 3.0), vec2(2.5, 6.0)), CornerRadius::same(1), TEXT_DIM);
    let colour = if charging { ACCENT } else if level < 20.0 { theme::DANGER } else if level < 40.0 { theme::WARN } else { ACCENT };
    let fill = Rect::from_min_size(body.min + vec2(2.5, 2.5), vec2((body.width() - 5.0) * (level.clamp(0.0, 100.0) / 100.0) as f32, body.height() - 5.0));
    painter.rect_filled(fill, CornerRadius::same(1), colour);
    ui.label(RichText::new(format!("{level:.0}%{}", if charging { " ⚡" } else { "" })).size(13.0).color(TEXT_DIM));
}

pub fn dim(text: impl Into<String>) -> RichText {
    RichText::new(text).color(TEXT_DIM)
}
