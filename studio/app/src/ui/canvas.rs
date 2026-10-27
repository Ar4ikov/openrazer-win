//! The device drawn in its own lights, to click on.
//!
//! Drawings are built off the UI thread -- tracing a photo and fitting a key
//! layout to it takes a few hundred milliseconds -- and kept per device.

use super::app::App;
use crate::art::{Art, Geom, Shape};
use crate::layout::Variant;
use crate::worker::Device;
use egui::{pos2, vec2, Color32, CornerRadius, Pos2, Rect, Sense, Stroke, StrokeKind, TextureHandle, TextureOptions, Vec2};
use openrazer_studio_core::theme::{self, ACCENT, TEXT, TEXT_FAINT};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// How long to wait for a product photo before drawing the device without one.
const PHOTO_PATIENCE: f32 = 8.0;

struct Slot {
    key: String,
    art: Option<Arc<Art>>,
    asked: Instant,
    building: bool,
    texture: Option<TextureHandle>,
    painted: Vec<Color32>,
}

#[derive(Default)]
pub struct Arts {
    slots: HashMap<String, Slot>,
    built: Arc<Mutex<Vec<(String, String, Art)>>>,
}

impl Arts {
    fn collect(&mut self) {
        for (serial, key, art) in std::mem::take(&mut *self.built.lock().unwrap()) {
            if let Some(slot) = self.slots.get_mut(&serial) {
                if slot.key == key {
                    slot.art = Some(Arc::new(art));
                    slot.building = false;
                    slot.painted.clear();
                }
            }
        }
    }

    /// Forget every drawing, so they are built again (a layout was changed).
    pub fn forget(&mut self, serial: &str) {
        self.slots.remove(serial);
    }
}

/// The drawing of `device`, once it is ready.
pub fn art_for(app: &mut App, ctx: &egui::Context, device: &Device, caps: &Value) -> Option<Arc<Art>> {
    app.arts.collect();
    let url = app.picture(device);
    let forced = app.settings.key_layouts.get(&device.serial).copied();
    let key = format!("{}|{:?}", url.clone().unwrap_or_default(), forced);
    let slot = app.arts.slots.entry(device.serial.clone()).or_insert_with(|| Slot { key: String::new(), art: None, asked: Instant::now(), building: false, texture: None, painted: Vec::new() });
    if slot.key != key {
        *slot = Slot { key: key.clone(), art: None, asked: Instant::now(), building: false, texture: None, painted: Vec::new() };
    }
    if slot.art.is_some() || slot.building {
        return slot.art.clone();
    }
    let photo = match &url {
        Some(url) => {
            let photo = app.images.photo(ctx, url);
            let waited = app.arts.slots[&device.serial].asked.elapsed().as_secs_f32();
            if photo.is_none() && !app.images.failed(url) && waited < PHOTO_PATIENCE {
                ctx.request_repaint_after(std::time::Duration::from_millis(250));
                return None;
            }
            photo
        }
        None => None,
    };
    let slot = app.arts.slots.get_mut(&device.serial).expect("just made");
    slot.building = true;
    let built = app.arts.built.clone();
    let (serial, kind, caps, ctx) = (device.serial.clone(), device.kind.clone(), caps.clone(), ctx.clone());
    std::thread::Builder::new()
        .name("outline".into())
        .spawn(move || {
            let art = Art::build_with(&kind, &caps, photo.as_deref(), forced);
            built.lock().unwrap().push((serial, key, art));
            ctx.request_repaint();
        })
        .ok();
    None
}

/// What happened on the canvas this frame.
#[derive(Default)]
pub struct Picked {
    pub clicked: Option<usize>,
    pub hovered: Option<usize>,
    /// Shapes inside a rectangle dragged out with the mouse.
    pub swept: Option<Vec<usize>>,
    pub modifiers: egui::Modifiers,
    pub secondary: bool,
    /// Clicked anywhere on the canvas, on a shape or not.
    pub any_click: bool,
}

pub struct View<'a> {
    pub visible: &'a dyn Fn(&Shape) -> bool,
    pub selected: &'a dyn Fn(&Shape) -> bool,
    /// Draw key names: for drawn keyboards, which have none of their own.
    pub labels: bool,
}

/// The device's light, averaged: for a glow under it, as on a desk.
pub fn ambient(art: &Art, colours: &[Color32], visible: &dyn Fn(&Shape) -> bool) -> Option<Color32> {
    let (mut r, mut g, mut b, mut n) = (0u32, 0u32, 0u32, 0u32);
    for (shape, colour) in art.shapes.iter().zip(colours) {
        if visible(shape) && (colour.r() as u32 + colour.g() as u32 + colour.b() as u32) > 30 {
            r += colour.r() as u32;
            g += colour.g() as u32;
            b += colour.b() as u32;
            n += 1;
        }
    }
    (n > 0).then(|| Color32::from_rgb((r / n) as u8, (g / n) as u8, (b / n) as u8))
}

fn screen_geom(shape: &Shape, to_screen: &dyn Fn(Pos2) -> Pos2, scale: f32) -> Option<(Rect, bool)> {
    Some(match &shape.geom {
        Geom::Rect(rect) => (Rect::from_min_max(to_screen(rect.min), to_screen(rect.max)), false),
        Geom::Dot(centre, radius) | Geom::Pixels(_, centre, radius) => (Rect::from_center_size(to_screen(*centre), Vec2::splat(radius * 2.0 * scale)), true),
        Geom::Strip(points, width) => {
            let mut rect = Rect::NOTHING;
            for p in points {
                rect.extend_with(to_screen(*p));
            }
            (rect.expand(width * scale), false)
        }
        Geom::Body => return None,
    })
}

/// Draw `art` lit in `colours` (one per shape) into `size`, and report clicks.
pub fn show(app: &mut App, ui: &mut egui::Ui, serial: &str, art: &Art, colours: &[Color32], size: Vec2, view: View) -> Picked {
    let (rect, response) = ui.allocate_exact_size(size, Sense::click_and_drag());
    let slot = app.arts.slots.get_mut(serial);
    let Some(slot) = slot else { return Picked::default() };
    // Only what is shown lights the drawing: a zone drawn LED by LED must not
    // also glow as the zone.
    let colours: Vec<Color32> = art.shapes.iter().zip(colours).map(|(shape, c)| if (view.visible)(shape) { *c } else { Color32::BLACK }).collect();
    let colours = colours.as_slice();
    if slot.painted != colours || slot.texture.is_none() {
        let image = art.paint(colours);
        match &mut slot.texture {
            Some(texture) => texture.set(image, TextureOptions::LINEAR),
            None => slot.texture = Some(ui.ctx().load_texture(format!("art-{serial}"), image, TextureOptions::LINEAR)),
        }
        slot.painted = colours.to_vec();
    }
    let scale = ((rect.width() - 24.0) / art.w as f32).min((rect.height() - 24.0) / art.h as f32);
    let drawn = Rect::from_center_size(rect.center(), vec2(art.w as f32 * scale, art.h as f32 * scale));
    let to_screen = |p: Pos2| pos2(drawn.left() + p.x * scale, drawn.top() + p.y * scale);
    let to_art = |p: Pos2| pos2((p.x - drawn.left()) / scale, (p.y - drawn.top()) / scale);
    let painter = ui.painter_at(rect);
    if let Some(colour) = ambient(art, colours, view.visible) {
        theme::glow(&painter, drawn.center() + vec2(0.0, drawn.height() * 0.08), vec2(drawn.width() * 0.62, drawn.height() * 0.62), colour.gamma_multiply(0.16));
    }
    if let Some(texture) = &slot.texture {
        painter.image(texture.id(), drawn, Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)), Color32::WHITE);
    }

    let mut picked = Picked { modifiers: ui.input(|i| i.modifiers), ..Default::default() };
    let pointer = response.hover_pos();
    picked.hovered = pointer.and_then(|p| art.hit(to_art(p), view.visible));

    for (index, shape) in art.shapes.iter().enumerate() {
        if !(view.visible)(shape) {
            continue;
        }
        let Some((bounds, round)) = screen_geom(shape, &to_screen, scale) else { continue };
        let selected = (view.selected)(shape);
        let hovered = picked.hovered == Some(index);
        let radius = if round { CornerRadius::same(255) } else { CornerRadius::same((3.0 * scale.min(1.5)) as u8) };
        if selected {
            painter.rect_filled(bounds, radius, ACCENT.gamma_multiply(0.08));
            painter.rect_stroke(bounds, radius, Stroke::new(1.3_f32, ACCENT.gamma_multiply(0.85)), StrokeKind::Inside);
        }
        if hovered {
            painter.rect_stroke(bounds.expand(1.0), radius, Stroke::new(1.2_f32, TEXT), StrokeKind::Outside);
        }
        if view.labels && !shape.label.is_empty() && bounds.height() > 16.0 {
            let colour = colours.get(index).copied().unwrap_or(Color32::BLACK);
            let lit = colour.r().max(colour.g()).max(colour.b()) > 150;
            let size = (bounds.height() * 0.3).clamp(8.0, 12.0);
            painter.text(bounds.center(), egui::Align2::CENTER_CENTER, &shape.label, egui::FontId::proportional(size), if lit { Color32::from_black_alpha(200) } else { TEXT_FAINT });
        }
    }

    if picked.hovered.is_some() {
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
    }
    if let (Some(index), Some(_)) = (picked.hovered, pointer) {
        let label = &art.shapes[index].label;
        if !label.is_empty() && !response.dragged() {
            response.clone().on_hover_text_at_pointer(shape_label(label));
        }
    }

    // Dragging sweeps out a rectangle; a click picks one shape.
    if response.dragged() {
        if let (Some(origin), Some(now)) = (ui.input(|i| i.pointer.press_origin()), pointer) {
            let sweep = Rect::from_two_pos(origin, now);
            painter.rect_filled(sweep, CornerRadius::same(2), ACCENT.gamma_multiply(0.08));
            painter.rect_stroke(sweep, CornerRadius::same(2), Stroke::new(1.0_f32, ACCENT), StrokeKind::Inside);
        }
    }
    if response.drag_stopped() {
        if let (Some(origin), Some(end)) = (ui.input(|i| i.pointer.press_origin()), ui.input(|i| i.pointer.interact_pos())) {
            let sweep = Rect::from_two_pos(origin, end);
            if sweep.width() > 4.0 || sweep.height() > 4.0 {
                let hits: Vec<usize> = art
                    .shapes
                    .iter()
                    .enumerate()
                    .filter(|(_, s)| (view.visible)(s))
                    .filter(|(_, s)| screen_geom(s, &to_screen, scale).is_some_and(|(b, _)| sweep.intersects(b)))
                    .map(|(i, _)| i)
                    .collect();
                picked.swept = Some(hits);
            }
        }
    }
    if response.clicked() {
        picked.clicked = picked.hovered;
        picked.any_click = true;
    }
    if response.secondary_clicked() {
        picked.clicked = picked.hovered;
        picked.secondary = true;
    }
    if !art.photo {
        painter.text(rect.right_bottom() - vec2(6.0, 4.0), egui::Align2::RIGHT_BOTTOM, openrazer_studio_core::t("Drawn: no product photo"), egui::FontId::proportional(10.5), TEXT_FAINT);
    }
    picked
}

/// A shape's name for its tooltip: words translated, key names as they are.
pub fn shape_label(label: &str) -> String {
    use openrazer_studio_core::t;
    match label {
        "Logo" => t("Logo").into(),
        "Scroll wheel" => t("Scroll wheel").into(),
        "Left ear" => t("Left ear").into(),
        "Right ear" => t("Right ear").into(),
        "Both ears" => t("Both ears").into(),
        "Backlight" => t("Backlight").into(),
        "Dial" => t("Dial").into(),
        "left" => t("Left side").into(),
        "right" => t("Right side").into(),
        "" => t("Space").into(),
        other => other.replace("LED", t("LED")),
    }
}

/// A placeholder while the drawing is made.
pub fn waiting(ui: &mut egui::Ui, size: Vec2) {
    let (rect, _) = ui.allocate_exact_size(size, Sense::hover());
    let painter = ui.painter_at(rect);
    painter.rect_stroke(rect.shrink(8.0), CornerRadius::same(theme::RADIUS), Stroke::new(1.0_f32, theme::BORDER), StrokeKind::Inside);
    let t = ui.input(|i| i.time) as f32;
    for i in 0..3 {
        let phase = (t * 3.0 - i as f32 * 0.6).sin() * 0.5 + 0.5;
        painter.circle_filled(rect.center() + vec2((i as f32 - 1.0) * 16.0, 0.0), 4.0, ACCENT.gamma_multiply(0.3 + 0.7 * phase));
    }
    painter.text(rect.center() + vec2(0.0, 22.0), egui::Align2::CENTER_TOP, openrazer_studio_core::t("Tracing the device…"), egui::FontId::proportional(12.0), TEXT_FAINT);
    ui.ctx().request_repaint();
}

pub fn variant_label(variant: Variant) -> &'static str {
    match variant {
        Variant::Ansi => "ANSI",
        Variant::Iso => "ISO",
    }
}
