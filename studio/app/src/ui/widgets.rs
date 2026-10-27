//! Widgets only the app needs: device drawings, batteries, effect tiles and
//! the colour chooser.

use super::effects::{self, PALETTE};
use crate::preview;
use egui::{epaint::Mesh, pos2, vec2, Align, Color32, CornerRadius, Layout, Pos2, Rect, Response, RichText, Sense, Shape, Stroke, StrokeKind, Ui};
use openrazer_studio_core::theme::{self, ACCENT, BORDER, BORDER_HI, BORDER_HOVER, SURFACE_ACTIVE, SURFACE_HI, SURFACE_HOVER, TEXT, TEXT_DIM, TEXT_FAINT, WELL};
use openrazer_studio_core::{t, widgets};

/// The height of an effect tile.
pub const TILE_HEIGHT: f32 = 84.0;

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
    painter.rect_filled(rect, CornerRadius::same(3), WELL);
    if effect == "none" {
        painter.line_segment([rect.left_bottom() + vec2(12.0, -8.0), rect.right_top() + vec2(-12.0, 8.0)], Stroke::new(1.5_f32, TEXT_FAINT));
        return;
    }
    let cells = 12;
    let inner = rect.shrink(4.0);
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
    let glow = Rect::from_min_max(pos2(inner.left(), inner.bottom() - 1.0), pos2(inner.right(), inner.bottom() + 3.0));
    gradient(painter, glow, &stops.iter().map(|c| c.gamma_multiply(0.4)).collect::<Vec<_>>());
}

/// A selectable tile showing an effect running, and its name. One size and
/// one typeface whether chosen or not; the chosen one is edged in green.
pub fn effect_tile(ui: &mut Ui, effect: &str, colours: &[Color32; 3], selected: bool, time: f32, width: f32) -> Response {
    let (rect, response) = ui.allocate_exact_size(vec2(width, TILE_HEIGHT), Sense::click());
    let hovered = ui.is_enabled() && response.hovered();
    let painter = ui.painter_at(rect);
    let fill = if selected { SURFACE_ACTIVE } else if hovered { SURFACE_HOVER } else { SURFACE_HI };
    let stroke = if selected { Stroke::new(1.5_f32, ACCENT) } else { Stroke::new(1.0_f32, if hovered { BORDER_HOVER } else { BORDER }) };
    painter.rect(rect, CornerRadius::same(theme::RADIUS_SM), fill, stroke, StrokeKind::Inside);
    let strip = Rect::from_min_size(rect.min + vec2(10.0, 10.0), vec2(rect.width() - 20.0, 32.0));
    effect_strip(&painter.with_clip_rect(strip.expand(4.0)), strip, effect, colours, 2, 1, time);
    let colour = if selected || hovered { TEXT } else { TEXT_DIM };
    let name = widgets::galley_fit(ui, effects::label(effect), theme::body(13.0), colour, rect.width() - 16.0);
    painter.galley(pos2(rect.center().x - name.size().x / 2.0, rect.bottom() - 13.0 - name.size().y / 2.0), name, colour);
    if effects::SOFTWARE.contains(&effect) {
        painter.text(rect.right_top() + vec2(-7.0, 6.0), egui::Align2::RIGHT_TOP, theme::icon::CODE, theme::icon_font(9.0), TEXT_FAINT);
    }
    let response = response.on_hover_cursor(egui::CursorIcon::PointingHand);
    if effects::SOFTWARE.contains(&effect) {
        response.on_hover_text(t("Drawn by the daemon, frame by frame"))
    } else {
        response
    }
}

/// Tiles for `effects` in a grid as wide as the panel; true for the one clicked.
pub fn effect_grid(ui: &mut Ui, list: &[String], colours: impl Fn(&str) -> [Color32; 3], selected: impl Fn(&str) -> bool, t: f32) -> Option<String> {
    let gap = 8.0;
    let width = ui.available_width();
    let columns = ((width + gap) / (124.0 + gap)).floor().max(1.0) as usize;
    let tile = ((width - gap * (columns as f32 - 1.0)) / columns as f32).floor();
    let mut clicked = None;
    for row in list.chunks(columns) {
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = gap;
            for effect in row {
                if effect_tile(ui, effect, &colours(effect), selected(effect), t, tile).clicked() {
                    clicked = Some(effect.clone());
                }
            }
        });
    }
    clicked
}

/// A round colour to pick; a ring outside it when chosen. One size either way.
pub fn dot(ui: &mut Ui, colour: Color32, size: f32, selected: bool) -> Response {
    let (rect, response) = ui.allocate_exact_size(vec2(size, size), Sense::click());
    let painter = ui.painter();
    let hovered = ui.is_enabled() && response.hovered();
    painter.circle_filled(rect.center(), size / 2.0 - 4.0, colour);
    if selected {
        painter.circle_stroke(rect.center(), size / 2.0 - 1.0, Stroke::new(2.0_f32, TEXT));
    } else if hovered {
        painter.circle_stroke(rect.center(), size / 2.0 - 1.0, Stroke::new(1.0_f32, BORDER_HOVER));
    }
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A simple line drawing of a device of `kind`, inside `rect`.
pub fn device_glyph(painter: &egui::Painter, rect: Rect, kind: &str, colour: Color32) {
    let stroke = Stroke::new((rect.width() / 18.0).clamp(1.2, 3.0), colour);
    let c = rect.center();
    let s = rect.width().min(rect.height());
    match kind {
        "keyboard" | "keypad" => {
            let body = Rect::from_center_size(c, vec2(rect.width().min(s * 1.6) * 0.9, s * 0.42));
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
            let body = Rect::from_center_size(c, vec2(rect.width().min(s * 1.5) * 0.84, s * 0.56));
            painter.rect_stroke(body, CornerRadius::same(5), stroke, StrokeKind::Middle);
        }
        _ => {
            painter.circle_stroke(c, s * 0.3, stroke);
            painter.circle_filled(c, s * 0.08, colour);
        }
    }
}

/// A battery: outline, fill to `level`, a bolt when charging, and the number.
/// Always the same width, so whatever follows it stays put.
pub fn battery(ui: &mut Ui, level: f64, charging: bool) -> Response {
    let (rect, response) = ui.allocate_exact_size(vec2(74.0, 20.0), Sense::hover());
    let painter = ui.painter();
    let body = Rect::from_min_size(pos2(rect.left(), rect.center().y - 6.5), vec2(24.0, 13.0));
    painter.rect_stroke(body, CornerRadius::same(3), Stroke::new(1.2_f32, TEXT_DIM), StrokeKind::Inside);
    painter.rect_filled(Rect::from_min_size(pos2(body.right() + 1.0, body.center().y - 3.0), vec2(2.5, 6.0)), CornerRadius::same(1), TEXT_DIM);
    let colour = if charging { ACCENT } else if level < 20.0 { theme::DANGER } else if level < 40.0 { theme::WARN } else { ACCENT };
    let fill = Rect::from_min_size(body.min + vec2(2.5, 2.5), vec2((body.width() - 5.0) * (level.clamp(0.0, 100.0) / 100.0) as f32, body.height() - 5.0));
    painter.rect_filled(fill, CornerRadius::same(1), colour);
    let text = format!("{level:.0}%{}", if charging { " ⚡" } else { "" });
    painter.text(pos2(body.right() + 9.0, rect.center().y), egui::Align2::LEFT_CENTER, text, theme::bold(12.5), TEXT_DIM);
    response.on_hover_text(if charging { t("charging") } else { t("on battery") })
}

/// Choosing one colour: a picker, a box for its hex code, preset swatches and
/// recently used ones. Side by side where there is room, else one above the
/// other; the same size whatever is chosen. True when the colour changed.
pub fn colour_chooser(ui: &mut Ui, id: &str, colour: &mut Color32, recent: &[[u8; 3]]) -> bool {
    let mut changed = false;
    let width = ui.available_width();
    // The presets beside the picker need 6 swatches' width.
    let details = 6.0 * 24.0 + 5.0 * 8.0;
    let wide = width >= details + 20.0 + 170.0;
    let picker_width = if wide { (width - details - 20.0).min(260.0) } else { width.min(320.0) };
    let picker_height = if wide { 132.0 } else { 110.0 };
    if wide {
        ui.horizontal_top(|ui| {
            ui.spacing_mut().item_spacing.x = 20.0;
            ui.vertical(|ui| {
                ui.set_width(picker_width);
                changed |= widgets::colour_picker(ui, id, colour, picker_width, picker_height);
            });
            changed |= chooser_details(ui, id, colour, recent);
        });
    } else {
        changed |= widgets::colour_picker(ui, id, colour, picker_width, picker_height);
        ui.add_space(4.0);
        changed |= chooser_details(ui, id, colour, recent);
    }
    changed
}

/// The hex box, presets and recent colours beside the picker.
fn chooser_details(ui: &mut Ui, id: &str, colour: &mut Color32, recent: &[[u8; 3]]) -> bool {
    let mut changed = false;
    ui.vertical(|ui| {
        ui.spacing_mut().item_spacing.y = 8.0;
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 8.0;
            let (rect, _) = ui.allocate_exact_size(vec2(32.0, 32.0), Sense::hover());
            ui.painter().rect(rect, CornerRadius::same(theme::RADIUS_SM), *colour, Stroke::new(1.0_f32, BORDER_HI), StrokeKind::Inside);
            changed |= widgets::hex_field(ui, (id, "hex"), colour, 104.0);
        });
        ui.label(RichText::new(t("Presets").to_uppercase()).font(theme::bold(10.5)).color(TEXT_FAINT).extra_letter_spacing(1.0));
        for line in PALETTE.chunks(6) {
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                for preset in line {
                    if widgets::swatch(ui, *preset, 24.0, *colour == *preset).on_hover_text(effects::hex(*preset)).clicked() {
                        *colour = *preset;
                        changed = true;
                    }
                }
            });
        }
        ui.label(RichText::new(t("Recent").to_uppercase()).font(theme::bold(10.5)).color(TEXT_FAINT).extra_letter_spacing(1.0));
        ui.allocate_ui_with_layout(vec2(6.0 * 24.0 + 5.0 * 8.0, 24.0), Layout::left_to_right(Align::Center), |ui| {
            ui.set_min_height(24.0);
            ui.spacing_mut().item_spacing.x = 8.0;
            if recent.is_empty() {
                ui.label(widgets::faint(t("None yet")));
            }
            for rgb in recent.iter().take(6) {
                let c = Color32::from_rgb(rgb[0], rgb[1], rgb[2]);
                if widgets::swatch(ui, c, 24.0, *colour == c).on_hover_text(effects::hex(c)).clicked() {
                    *colour = c;
                    changed = true;
                }
            }
        });
    });
    changed
}
