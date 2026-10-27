//! Widgets only the app needs: device drawings, batteries, effect tiles and
//! the colour chooser.

use super::effects::{self, PALETTE};
use crate::preview;
use egui::{epaint::Mesh, pos2, vec2, Color32, CornerRadius, FontId, Pos2, Rect, Response, RichText, Sense, Shape, Stroke, StrokeKind, Ui};
use openrazer_studio_core::theme::{self, ACCENT, BORDER, SURFACE, SURFACE_ACTIVE, SURFACE_HOVER, TEXT, TEXT_DIM, TEXT_FAINT, WELL};
use openrazer_studio_core::{t, widgets};

fn gradient(painter: &egui::Painter, rect: Rect, colours: &[Color32]) {
    let mut mesh = Mesh::default();
    let n = colours.len().max(2);
    for (i, colour) in colours.iter().enumerate() {
        let x = egui::lerp(rect.left()..=rect.right(), i as f32 / (n - 1) as f32);
        mesh.colored_vertex(pos2(x, rect.top()), *colour);
        mesh.colored_vertex(pos2(x, rect.bottom()), *colour);
    }
    for i in 0..(colours.len() as u32).saturating_sub(1) {
        let k = i * 2;
        mesh.add_triangle(k, k + 1, k + 2);
        mesh.add_triangle(k + 1, k + 2, k + 3);
    }
    painter.add(Shape::mesh(mesh));
}

/// A strip of LEDs showing an effect as it runs, `t` seconds in.
pub fn effect_strip(painter: &egui::Painter, rect: Rect, effect: &str, colours: &[Color32; 3], speed: i64, direction: i64, t: f32) {
    painter.rect_filled(rect, CornerRadius::same(4), WELL);
    if effect == "none" {
        painter.line_segment([rect.left_bottom() + vec2(12.0, -10.0), rect.right_top() + vec2(-12.0, 10.0)], Stroke::new(2.0_f32, TEXT_FAINT));
        return;
    }
    let cells = 12;
    let inner = rect.shrink(5.0);
    let w = inner.width() / cells as f32;
    let mut stops = Vec::new();
    for i in 0..cells {
        let x = i as f32 / (cells - 1) as f32;
        let seed = preview::twinkle_phase(0, i as u8);
        let mut colour = preview::hardware(effect, colours, speed, direction, t, x, seed);
        if matches!(effect, "reactive" | "ripple" | "ripple_random") {
            // A key pressed every couple of seconds, somewhere along the strip.
            let hit = ((t / 1.6) as u32 % cells as u32) as f32;
            let age = (t % 1.6) / 1.6;
            let reach = if effect == "reactive" { 0.6 } else { age * 6.0 };
            let distance = (i as f32 - hit).abs();
            if (effect == "reactive" && distance < reach) || (effect != "reactive" && (distance - reach).abs() < 1.0) {
                let base = if effect == "ripple_random" { preview::hue(hit / cells as f32) } else { colours[0] };
                colour = base.gamma_multiply(1.0 - age);
            }
        }
        stops.push(colour);
        let cell = Rect::from_min_size(pos2(inner.left() + i as f32 * w, inner.top()), vec2(w, inner.height())).shrink(1.0);
        painter.rect_filled(cell, CornerRadius::same(2), colour);
    }
    // A soft glow under the strip, like light on a desk.
    let glow = Rect::from_min_max(pos2(inner.left(), inner.bottom() - 2.0), pos2(inner.right(), inner.bottom() + 3.0));
    gradient(painter, glow, &stops.iter().map(|c| c.gamma_multiply(0.5)).collect::<Vec<_>>());
}

/// A selectable tile showing an effect running, and its name.
pub fn effect_tile(ui: &mut Ui, effect: &str, colours: &[Color32; 3], selected: bool, t: f32) -> Response {
    let size = vec2(132.0, 86.0);
    let (rect, response) = ui.allocate_exact_size(size, Sense::click());
    let painter = ui.painter_at(rect.expand(2.0));
    let fill = if selected { SURFACE_ACTIVE } else if response.hovered() { SURFACE_HOVER } else { SURFACE };
    painter.rect_filled(rect, CornerRadius::same(theme::RADIUS), fill);
    let stroke = if selected { Stroke::new(1.5_f32, ACCENT) } else { Stroke::new(1.0_f32, BORDER) };
    painter.rect_stroke(rect, CornerRadius::same(theme::RADIUS), stroke, StrokeKind::Inside);
    let strip = Rect::from_min_size(rect.min + vec2(10.0, 12.0), vec2(rect.width() - 20.0, 34.0));
    effect_strip(&painter.with_clip_rect(strip.expand(4.0)), strip, effect, colours, 2, 1, t);
    painter.text(
        pos2(rect.center().x, rect.bottom() - 18.0),
        egui::Align2::CENTER_CENTER,
        effects::label(effect),
        if selected { theme::bold(12.5) } else { FontId::proportional(12.5) },
        if selected { TEXT } else { TEXT_DIM },
    );
    if effects::SOFTWARE.contains(&effect) {
        painter.text(rect.right_top() + vec2(-8.0, 8.0), egui::Align2::RIGHT_TOP, theme::icon::CODE, theme::icon_font(10.0), TEXT_FAINT);
    }
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A round colour to pick, glowing when it is the one chosen.
pub fn dot(ui: &mut Ui, colour: Color32, size: f32, selected: bool) -> Response {
    let (rect, response) = ui.allocate_exact_size(vec2(size, size), Sense::click());
    let painter = ui.painter();
    let radius = size / 2.0 - if selected { 3.0 } else { 1.0 };
    if selected {
        theme::glow(painter, rect.center(), vec2(size, size), colour.gamma_multiply(0.45));
        painter.circle_stroke(rect.center(), size / 2.0, Stroke::new(1.5_f32, TEXT));
    } else if response.hovered() {
        painter.circle_stroke(rect.center(), size / 2.0, Stroke::new(1.0_f32, TEXT_DIM));
    }
    painter.circle_filled(rect.center(), radius, colour);
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A simple line drawing of a device of `kind`, inside `rect`.
pub fn device_glyph(painter: &egui::Painter, rect: Rect, kind: &str, colour: Color32) {
    let stroke = Stroke::new((rect.width() / 18.0).max(1.4), colour);
    let c = rect.center();
    let s = rect.width().min(rect.height());
    match kind {
        "keyboard" | "keypad" => {
            let body = Rect::from_center_size(c, vec2(s * 0.9, s * 0.42));
            painter.rect_stroke(body, CornerRadius::same(3), stroke, StrokeKind::Middle);
            for row in 0..3 {
                for col in 0..7 {
                    let p = pos2(body.left() + body.width() * (0.11 + col as f32 * 0.13), body.top() + body.height() * (0.25 + row as f32 * 0.25));
                    painter.circle_filled(p, stroke.width * 0.55, colour);
                }
            }
        }
        "mouse" => {
            let body = Rect::from_center_size(c, vec2(s * 0.46, s * 0.74));
            painter.rect_stroke(body, CornerRadius::same((s * 0.22) as u8), stroke, StrokeKind::Middle);
            painter.line_segment([pos2(c.x, body.top()), pos2(c.x, body.top() + body.height() * 0.36)], stroke);
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
        }
        _ => {
            painter.circle_stroke(c, s * 0.3, stroke);
            painter.circle_filled(c, s * 0.08, colour);
        }
    }
}

/// A battery: outline, fill to `level`, a bolt when charging, and the number.
pub fn battery(ui: &mut Ui, level: f64, charging: bool) {
    let (rect, _) = ui.allocate_exact_size(vec2(28.0, 13.0), Sense::hover());
    let painter = ui.painter();
    let body = Rect::from_min_size(rect.min, vec2(24.0, 13.0));
    painter.rect_stroke(body, CornerRadius::same(3), Stroke::new(1.2_f32, TEXT_DIM), StrokeKind::Inside);
    painter.rect_filled(Rect::from_min_size(pos2(body.right() + 1.0, body.center().y - 3.0), vec2(2.5, 6.0)), CornerRadius::same(1), TEXT_DIM);
    let colour = if charging { ACCENT } else if level < 20.0 { theme::DANGER } else if level < 40.0 { theme::WARN } else { ACCENT };
    let fill = Rect::from_min_size(body.min + vec2(2.5, 2.5), vec2((body.width() - 5.0) * (level.clamp(0.0, 100.0) / 100.0) as f32, body.height() - 5.0));
    painter.rect_filled(fill, CornerRadius::same(1), colour);
    ui.label(RichText::new(format!("{level:.0}%{}", if charging { " ⚡" } else { "" })).size(12.5).color(TEXT_DIM));
}

/// Choosing one colour: the picker, presets, recent colours and hex.
/// Returns true when the colour changed.
pub fn colour_chooser(ui: &mut Ui, colour: &mut Color32, hex: &mut String, recent: &[[u8; 3]]) -> bool {
    let mut changed = false;
    // Side by side where there is room; one above the other in a narrow panel.
    let narrow = ui.available_width() < 680.0;
    let layout = if narrow { egui::Layout::top_down(egui::Align::Min) } else { egui::Layout::left_to_right(egui::Align::Min) };
    ui.with_layout(layout, |ui| {
        ui.vertical(|ui| {
            if narrow {
                ui.spacing_mut().slider_width = (ui.available_width() - 8.0).min(260.0);
            }
            if egui::widgets::color_picker::color_picker_color32(ui, colour, egui::color_picker::Alpha::Opaque) {
                changed = true;
            }
        });
        if !narrow {
            ui.add_space(16.0);
        }
        ui.vertical(|ui| {
            widgets::section(ui, t("Presets"));
            egui::Grid::new(ui.next_auto_id()).spacing(vec2(7.0, 7.0)).show(ui, |ui| {
                for (i, preset) in PALETTE.iter().enumerate() {
                    if widgets::swatch(ui, *preset, 26.0, *colour == *preset).clicked() {
                        *colour = *preset;
                        changed = true;
                    }
                    if i % 6 == 5 {
                        ui.end_row();
                    }
                }
            });
            if !recent.is_empty() {
                ui.add_space(4.0);
                widgets::section(ui, t("Recent"));
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 7.0;
                    for rgb in recent.iter().take(6) {
                        let c = Color32::from_rgb(rgb[0], rgb[1], rgb[2]);
                        if widgets::swatch(ui, c, 22.0, *colour == c).clicked() {
                            *colour = c;
                            changed = true;
                        }
                    }
                });
            }
            ui.add_space(4.0);
            widgets::section(ui, t("Hex"));
            let response = ui.add(egui::TextEdit::singleline(hex).desired_width(96.0).font(egui::TextStyle::Monospace));
            if response.changed() {
                if let Some(parsed) = effects::parse_hex(hex) {
                    if parsed != *colour {
                        *colour = parsed;
                        changed = true;
                    }
                }
            }
            if !response.has_focus() {
                *hex = effects::hex(*colour);
            }
        });
    });
    changed
}
