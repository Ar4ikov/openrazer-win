//! A device drawn as a line drawing with its LEDs in place.
//!
//! [`Art::build`] takes the traced photo (or, without one, a drawing of a
//! device of that kind) and works out where each addressable part of the
//! lighting is: every key of a keyboard, fitted onto the keycaps in the
//! photo; the logo and the wheel of a mouse, found where the photo shows them
//! lit; LED strips, spaced along the outline. [`Art::paint`] then colours the
//! drawing: lit keys glow in their colour, the lines over them pick it up,
//! and underglow spills out past the outline.

use crate::layout::{self, Layout, Variant};
use crate::outline::{self, Trace};
use egui::{pos2, vec2, Color32, Pos2, Rect};
use serde_json::Value;

/// Every lighting zone the daemon knows, so a zone can be named by `&'static str`.
pub const ZONES: [&str; 8] = ["backlight", "logo", "scroll", "left", "right", "charging", "fast_charging", "fully_charged"];

pub fn zone_name(zone: &str) -> Option<&'static str> {
    ZONES.iter().copied().find(|z| *z == zone)
}

/// One addressable part of a device's lighting.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Region {
    /// An LED in the matrix: what per-key layers paint.
    Cell(u8, u8),
    /// A zone with effects of its own: what quick mode sets.
    Zone(&'static str),
}

#[derive(Clone, Debug)]
pub enum Geom {
    Rect(Rect),
    Dot(Pos2, f32),
    /// A line of light: points and half its width.
    Strip(Vec<Pos2>, f32),
    /// The whole body.
    Body,
    /// Exactly these pixels -- a lit logo's own shape -- with a centre and a
    /// radius to hit and outline it by.
    Pixels(Vec<u32>, Pos2, f32),
}

#[derive(Clone, Debug)]
pub struct Shape {
    pub region: Region,
    pub geom: Geom,
    pub label: String,
}

impl Shape {
    pub fn bounds(&self, art: &Art) -> Rect {
        match &self.geom {
            Geom::Rect(rect) => *rect,
            Geom::Dot(centre, radius) => Rect::from_center_size(*centre, vec2(radius * 2.0, radius * 2.0)),
            Geom::Strip(points, width) => {
                let mut rect = Rect::NOTHING;
                for p in points {
                    rect.extend_with(*p);
                }
                rect.expand(*width)
            }
            Geom::Body => Rect::from_min_size(pos2(art.bbox[0], art.bbox[1]), vec2(art.bbox[2] - art.bbox[0], art.bbox[3] - art.bbox[1])),
            Geom::Pixels(_, centre, radius) => Rect::from_center_size(*centre, vec2(radius * 2.0, radius * 2.0)),
        }
    }

    fn shift(&mut self, by: egui::Vec2, w: usize, new_w: usize, origin: (usize, usize)) {
        match &mut self.geom {
            Geom::Rect(rect) => *rect = rect.translate(by),
            Geom::Dot(centre, _) => *centre += by,
            Geom::Strip(points, _) => points.iter_mut().for_each(|p| *p += by),
            Geom::Body => {}
            Geom::Pixels(pixels, centre, _) => {
                *centre += by;
                for p in pixels.iter_mut() {
                    let (x, y) = (*p as usize % w, *p as usize / w);
                    *p = ((y - origin.1) * new_w + (x - origin.0)) as u32;
                }
            }
        }
    }
}

/// Distance from `p` to the segment `a`-`b`.
fn segment_distance(p: Pos2, a: Pos2, b: Pos2) -> f32 {
    let ab = b - a;
    let t = ((p - a).dot(ab) / ab.length_sq().max(1e-6)).clamp(0.0, 1.0);
    (p - (a + ab * t)).length()
}

pub struct Art {
    pub w: usize,
    pub h: usize,
    pub lines: Vec<u8>,
    pub body: Vec<bool>,
    pub bbox: [f32; 4],
    pub shapes: Vec<Shape>,
    /// For each pixel, the shape lighting it (index + 1), or 0.
    owner: Vec<u16>,
    /// ... and how strongly, 0-255.
    glow: Vec<u8>,
    /// Traced from the product photo, rather than drawn.
    pub photo: bool,
    /// The key layout a keyboard was drawn or fitted with.
    pub variant: Option<Variant>,
    /// One key unit, in pixels, for sizing labels.
    pub unit: f32,
    /// Shapes that are the light itself (a logo), drawn at full strength.
    vivid: Vec<bool>,
}

/// What the daemon says about the device's LEDs.
struct Leds {
    rows: usize,
    columns: usize,
    zones: Vec<&'static str>,
    macro_keys: bool,
}

impl Leds {
    fn from(caps: &Value) -> Leds {
        let dims = caps["matrix_dimensions"].as_array();
        let dim = |i: usize| dims.and_then(|d| d.get(i)).and_then(Value::as_u64).unwrap_or(0) as usize;
        let custom = caps["custom_frame"] == true;
        let (rows, columns) = if custom { (dim(0), dim(1)) } else { (0, 0) };
        let mut zones: Vec<&'static str> = caps["zones"].as_object().map(|z| z.keys().filter_map(|k| zone_name(k)).collect()).unwrap_or_default();
        zones.sort_by_key(|z| ZONES.iter().position(|o| o == z));
        Leds { rows, columns, zones, macro_keys: caps["dedicated_macro_keys"] == true }
    }
    fn cells(&self) -> usize {
        self.rows * self.columns
    }
    fn has(&self, zone: &str) -> bool {
        self.zones.contains(&zone)
    }
}

impl Art {
    /// Draw a device of `kind` with the capabilities `caps`, from `photo` if there is one.
    #[cfg(test)]
    pub fn build(kind: &str, caps: &Value, photo: Option<&image::RgbaImage>) -> Art {
        Art::build_with(kind, caps, photo, None)
    }

    /// ... with the keyboard layout `forced`, rather than the one that fits best.
    pub fn build_with(kind: &str, caps: &Value, photo: Option<&image::RgbaImage>, forced: Option<Variant>) -> Art {
        let leds = Leds::from(caps);
        let traced = photo.and_then(outline::trace);
        match kind {
            "keyboard" => keyboard(traced, &leds, false, forced),
            "keypad" => keyboard(traced, &leds, true, None),
            _ if leds.rows > 1 && kind == "accessory" => strips(&leds),
            "mouse" => {
                let found = traced.is_some();
                let trace = traced.unwrap_or_else(|| synthetic(Silhouette::Mouse));
                let shapes = mouse_shapes(&trace, &leds);
                Art::finish(trace, shapes, found, None, 0.0)
            }
            "headset" => {
                let found = traced.is_some();
                let trace = traced.unwrap_or_else(|| synthetic(Silhouette::Headset));
                let shapes = headset_shapes(&trace, &leds, caps["name"].as_str().unwrap_or_default());
                Art::finish(trace, shapes, found, None, 0.0)
            }
            _ => {
                let fallback = if kind == "mousemat" { Silhouette::Mat } else { Silhouette::Slab };
                let found = traced.is_some();
                let trace = traced.unwrap_or_else(|| synthetic(fallback));
                let shapes = strip_shapes(&trace, &leds);
                Art::finish(trace, shapes, found, None, 0.0)
            }
        }
    }

    fn finish(trace: Trace, mut shapes: Vec<Shape>, photo: bool, variant: Option<Variant>, unit: f32) -> Art {
        let [l, t, r, b] = trace.bbox;
        let unit = if unit > 0.0 { unit } else { ((r - l).max(b - t) + 1) as f32 / 12.0 };
        // Keep the device and a margin round it, wide enough for underglow.
        let mut keep = Rect::from_min_max(pos2(l as f32, t as f32), pos2(r as f32 + 1.0, b as f32 + 1.0));
        for shape in &shapes {
            let bounds = match &shape.geom {
                Geom::Rect(rect) => *rect,
                Geom::Dot(c, radius) | Geom::Pixels(_, c, radius) => Rect::from_center_size(*c, vec2(radius * 2.0, radius * 2.0)),
                Geom::Strip(points, _) => points.iter().fold(Rect::NOTHING, |r, p| r.union(Rect::from_min_max(*p, *p))),
                Geom::Body => keep,
            };
            keep = keep.union(bounds);
        }
        let margin = (unit * 0.6).max(keep.width().max(keep.height()) * 0.04).ceil();
        let x0 = (keep.left() - margin).floor().max(0.0) as usize;
        let y0 = (keep.top() - margin).floor().max(0.0) as usize;
        let x1 = ((keep.right() + margin).ceil() as usize).min(trace.w);
        let y1 = ((keep.bottom() + margin).ceil() as usize).min(trace.h);
        let (w, h) = (x1 - x0, y1 - y0);
        let mut lines = Vec::with_capacity(w * h);
        let mut body = Vec::with_capacity(w * h);
        for y in y0..y1 {
            for x in x0..x1 {
                lines.push(trace.lines[y * trace.w + x]);
                body.push(trace.body[y * trace.w + x]);
            }
        }
        let by = vec2(-(x0 as f32), -(y0 as f32));
        for shape in &mut shapes {
            shape.shift(by, trace.w, w, (x0, y0));
        }
        let bbox = [(l - x0) as f32, (t - y0) as f32, (r + 1 - x0) as f32, (b + 1 - y0) as f32];
        let vivid = shapes.iter().map(|s| matches!(s.geom, Geom::Pixels(..))).collect();
        let mut art = Art { w, h, owner: vec![0; w * h], glow: vec![0; w * h], lines, body, bbox, shapes, photo, variant, unit, vivid };
        art.light_up();
        art
    }

    /// Work out which shape lights each pixel, and how much.
    fn light_up(&mut self) {
        let unit = self.unit;
        for (index, shape) in self.shapes.iter().enumerate() {
            if let Geom::Pixels(pixels, ..) = &shape.geom {
                // The lit pixels at full strength, and a soft rim round them.
                let rim = (unit * 0.08).clamp(2.0, 6.0) as isize;
                for &p in pixels {
                    let (px, py) = ((p as usize % self.w) as isize, (p as usize / self.w) as isize);
                    for dy in -rim..=rim {
                        for dx in -rim..=rim {
                            let (x, y) = (px + dx, py + dy);
                            if x < 0 || y < 0 || x as usize >= self.w || y as usize >= self.h {
                                continue;
                            }
                            let d = ((dx * dx + dy * dy) as f32).sqrt();
                            let strength = if d < 0.5 {
                                255
                            } else if d < rim as f32 {
                                (170.0 * (1.0 - d / rim as f32)) as u8
                            } else {
                                continue;
                            };
                            let i = y as usize * self.w + x as usize;
                            if strength > self.glow[i] {
                                self.glow[i] = strength;
                                self.owner[i] = index as u16 + 1;
                            }
                        }
                    }
                }
                continue;
            }
            // How far past its edge a shape's light carries.
            let (bounds, reach) = match &shape.geom {
                Geom::Rect(rect) => (*rect, (unit * 0.1).max(1.5)),
                Geom::Dot(centre, radius) => (Rect::from_center_size(*centre, vec2(radius * 2.0, radius * 2.0)), radius * 1.1),
                Geom::Strip(points, width) => {
                    let mut rect = Rect::NOTHING;
                    for p in points {
                        rect.extend_with(*p);
                    }
                    (rect.expand(*width), width * 1.6)
                }
                Geom::Body => (Rect::from_min_max(pos2(self.bbox[0], self.bbox[1]), pos2(self.bbox[2], self.bbox[3])), 0.0),
                Geom::Pixels(..) => continue,
            };
            let area = bounds.expand(reach);
            let (x0, y0) = (area.left().floor().max(0.0) as usize, area.top().floor().max(0.0) as usize);
            let (x1, y1) = ((area.right().ceil() as usize).min(self.w), (area.bottom().ceil() as usize).min(self.h));
            for y in y0..y1 {
                for x in x0..x1 {
                    let i = y * self.w + x;
                    let p = pos2(x as f32 + 0.5, y as f32 + 0.5);
                    let distance = match &shape.geom {
                        Geom::Rect(rect) => {
                            let dx = (rect.left() - p.x).max(p.x - rect.right()).max(0.0);
                            let dy = (rect.top() - p.y).max(p.y - rect.bottom()).max(0.0);
                            (dx * dx + dy * dy).sqrt()
                        }
                        Geom::Dot(centre, radius) => ((p - *centre).length() - radius).max(0.0),
                        Geom::Strip(points, width) => {
                            let d = points.windows(2).map(|s| segment_distance(p, s[0], s[1])).fold(f32::INFINITY, f32::min);
                            (d - width).max(0.0)
                        }
                        Geom::Body => {
                            if self.body[i] {
                                0.0
                            } else {
                                continue;
                            }
                        }
                        Geom::Pixels(..) => continue,
                    };
                    let strength = if distance <= 0.0 {
                        // The whole body loses to anything more particular on it.
                        if matches!(shape.geom, Geom::Body) {
                            200
                        } else {
                            255
                        }
                    } else if distance < reach {
                        (200.0 * (1.0 - distance / reach)) as u8
                    } else {
                        continue;
                    };
                    if strength > self.glow[i] {
                        self.glow[i] = strength;
                        self.owner[i] = index as u16 + 1;
                    }
                }
            }
        }
    }

    /// The drawing, with each shape lit in `colours[shape]`.
    pub fn paint(&self, colours: &[Color32]) -> egui::ColorImage {
        let mut pixels = Vec::with_capacity(self.w * self.h);
        let body_fill = [17.0f32, 18.0, 21.0];
        let line_grey = [92.0f32, 97.0, 106.0];
        for i in 0..self.w * self.h {
            let line = self.lines[i] as f32 / 255.0;
            let inside = self.body[i];
            let mut fill = if inside { body_fill } else { [0.0; 3] };
            let mut alpha: f32 = if inside { 1.0 } else { 0.0 };
            let mut line_colour = line_grey;
            let owner = self.owner[i] as usize;
            if owner > 0 {
                let c = colours.get(owner - 1).copied().unwrap_or(Color32::BLACK);
                let c = [c.r() as f32, c.g() as f32, c.b() as f32];
                let k = self.glow[i] as f32 / 255.0;
                let brightness = c[0].max(c[1]).max(c[2]) / 255.0;
                if brightness > 0.01 {
                    if inside {
                        let strength = if self.vivid[owner - 1] { 0.95 } else { 0.42 };
                        for j in 0..3 {
                            fill[j] += c[j] * strength * k;
                        }
                    } else {
                        fill = c;
                        alpha = alpha.max(0.6 * k * brightness);
                    }
                    let tint = 0.85 * k.min(1.0);
                    for j in 0..3 {
                        line_colour[j] = line_grey[j] + (c[j].max(line_grey[j] * 0.6) - line_grey[j]) * tint;
                    }
                }
            }
            let mut out = [0u8; 4];
            for j in 0..3 {
                out[j] = (fill[j] + (line_colour[j] - fill[j]) * line).clamp(0.0, 255.0) as u8;
            }
            out[3] = (alpha.max(line) * 255.0) as u8;
            pixels.push(Color32::from_rgba_unmultiplied(out[0], out[1], out[2], out[3]));
        }
        egui::ColorImage { size: [self.w, self.h], pixels }
    }

    /// The shape under `p`, in the drawing's pixels; the smallest wins.
    pub fn hit(&self, p: Pos2, accept: impl Fn(&Shape) -> bool) -> Option<usize> {
        let mut best: Option<(usize, f32)> = None;
        for (index, shape) in self.shapes.iter().enumerate() {
            if !accept(shape) {
                continue;
            }
            let (inside, size) = match &shape.geom {
                Geom::Rect(rect) => (rect.contains(p), rect.area()),
                Geom::Dot(centre, radius) => ((p - *centre).length() <= radius.max(6.0), radius * radius * std::f32::consts::PI),
                Geom::Strip(points, width) => (points.windows(2).any(|s| segment_distance(p, s[0], s[1]) <= width.max(5.0)), 1e6),
                Geom::Body => {
                    let (x, y) = (p.x as isize, p.y as isize);
                    let inside = x >= 0 && y >= 0 && (x as usize) < self.w && (y as usize) < self.h && self.body[y as usize * self.w + x as usize];
                    (inside, 1e9)
                }
                Geom::Pixels(_, centre, radius) => ((p - *centre).length() <= radius.max(6.0), radius * radius * std::f32::consts::PI),
            };
            if inside && best.is_none_or(|(_, s)| size < s) {
                best = Some((index, size));
            }
        }
        best.map(|(index, _)| index)
    }
}

// -- keyboards -----------------------------------------------------------------------

/// Where a layout lands on a photo: key unit in pixels, and the top-left corner.
#[derive(Clone, Copy, Debug)]
pub struct Fit {
    pub unit: f32,
    pub x: f32,
    pub y: f32,
    /// How well it matches, against the photo's average edge strength.
    pub score: f32,
}

/// Points just inside each keycap's edge, in key units.
fn edge_samples(layout: &Layout) -> Vec<(f32, f32)> {
    let inset = 0.07;
    let mut samples = Vec::new();
    for key in &layout.keys {
        let (l, t, r, b) = (key.x + inset, key.y + inset, key.x + key.w - inset, key.y + key.h - inset);
        let along = |len: f32| ((len * 2.5).round() as usize).max(2);
        for j in 0..along(r - l) {
            let x = l + (r - l) * (j as f32 + 0.5) / along(r - l) as f32;
            samples.push((x, t));
            samples.push((x, b));
        }
        for j in 0..along(b - t) {
            let y = t + (b - t) * (j as f32 + 0.5) / along(b - t) as f32;
            samples.push((l, y));
            samples.push((r, y));
        }
    }
    samples
}

/// Slide and scale `layout` over the photo until its keycap edges sit on the
/// photo's edges.
pub fn fit_layout(trace: &Trace, layout: &Layout) -> Option<Fit> {
    if layout.keys.is_empty() {
        return None;
    }
    let samples = edge_samples(layout);
    let score = |unit: f32, ox: f32, oy: f32| -> f32 {
        let mut sum = 0.0;
        for &(u, v) in &samples {
            sum += trace.field_at(ox + u * unit, oy + v * unit);
        }
        sum / samples.len() as f32
    };
    let [left, top, right, bottom] = trace.bbox.map(|v| v as f32);
    let estimate = trace.bbox_w() / (layout.width + 0.7);
    let mut best = Fit { unit: estimate, x: left, y: top, score: -1.0 };
    for step in 0..=20 {
        let unit = estimate * (0.86 + 0.01 * step as f32);
        let stride = (unit * 0.12).max(1.0);
        let (x_lo, x_hi) = (left - unit * 0.3, right + 1.0 - layout.width * unit + unit * 0.3);
        let (y_lo, y_hi) = (top - unit * 0.3, bottom + 1.0 - layout.height * unit + unit * 0.3);
        if x_hi < x_lo || y_hi < y_lo {
            continue;
        }
        let mut y = y_lo;
        while y <= y_hi {
            let mut x = x_lo;
            while x <= x_hi {
                let s = score(unit, x, y);
                if s > best.score {
                    best = Fit { unit, x, y, score: s };
                }
                x += stride;
            }
            y += stride;
        }
    }
    if best.score < 0.0 {
        return None;
    }
    // Then finer, around the best.
    let coarse = best;
    for step in -6..=6 {
        let unit = coarse.unit * (1.0 + 0.0025 * step as f32);
        let reach = (coarse.unit * 0.12).max(1.0);
        let mut y = coarse.y - reach;
        while y <= coarse.y + reach {
            let mut x = coarse.x - reach;
            while x <= coarse.x + reach {
                let s = score(unit, x, y);
                if s > best.score {
                    best = Fit { unit, x, y, score: s };
                }
                x += 0.5;
            }
            y += 0.5;
        }
    }
    // Relative to how edgy the photo is anywhere on the body.
    let mut total = 0.0;
    let mut count = 0usize;
    for (i, &inside) in trace.body.iter().enumerate() {
        if inside {
            total += trace.field[i];
            count += 1;
        }
    }
    let baseline = (total / count.max(1) as f32).max(1e-4);
    best.score /= baseline;
    Some(best)
}

/// A good enough fit: keycap edges on edges well above the photo's average.
const GOOD_FIT: f32 = 1.6;
/// Keypads are photographed from an angle, where a flat layout fits badly
/// but can still seem to fit somewhere; they must fit clearly, or be drawn.
const GOOD_KEYPAD_FIT: f32 = 2.6;

fn keyboard(traced: Option<Trace>, leds: &Leds, keypad: bool, forced: Option<Variant>) -> Art {
    // Which layouts to try: the matrix says how big the board is, when there
    // is one; a board without one could be any size.
    let mut candidates: Vec<(Layout, Option<Variant>)> = Vec::new();
    if keypad {
        candidates.push((layout::keypad(4, 8), None));
    } else if leds.rows >= 5 && leds.columns >= 14 {
        let rows = leds.rows.min(6);
        for variant in [Variant::Ansi, Variant::Iso] {
            if forced.is_none_or(|f| f == variant) {
                candidates.push((layout::keyboard(rows, leds.columns, variant, false), Some(variant)));
            }
        }
    } else {
        for (rows, columns) in [(6, 22), (6, 18), (5, 15)] {
            candidates.push((layout::keyboard(rows, columns, Variant::Ansi, false), Some(Variant::Ansi)));
        }
    }

    let fitted = traced.as_ref().and_then(|trace| {
        candidates
            .iter()
            .enumerate()
            .filter_map(|(i, (layout, _))| fit_layout(trace, layout).map(|fit| (i, fit)))
            // ISO and ANSI differ by three keys: ANSI unless ISO is clearly better.
            .max_by(|a, b| {
                let bias = |i: usize| if candidates[i].1 == Some(Variant::Iso) { 0.97 } else { 1.0 };
                (a.1.score * bias(a.0)).total_cmp(&(b.1.score * bias(b.0)))
            })
            .filter(|(_, fit)| fit.score >= if keypad { GOOD_KEYPAD_FIT } else { GOOD_FIT })
    });

    let matrix_board = !keypad && leds.rows >= 5 && leds.columns >= 14;
    let (trace, layout, variant, fit, photo) = match (traced, fitted) {
        (Some(trace), Some((i, fit))) => {
            let (mut layout, variant) = candidates.swap_remove(i);
            // M1-M5: where the photo shows a column of keys left of the board.
            if matrix_board {
                if let Some(keys) = find_macro_keys(&trace, &fit, layout.height, leds.rows.min(6), leds.macro_keys) {
                    layout = layout::with_keys(&layout, &keys);
                }
            }
            (trace, layout, variant, fit, true)
        }
        _ => {
            let (mut layout, variant) = candidates.swap_remove(0);
            if matrix_board && leds.macro_keys {
                layout = layout::keyboard(leds.rows.min(6), leds.columns, variant.unwrap_or(Variant::Ansi), true);
            }
            let (trace, fit) = synthetic_keyboard(&layout);
            (trace, layout, variant, fit, false)
        }
    };

    let place = |x: f32, y: f32, w: f32, h: f32| {
        let gap = 0.06;
        Rect::from_min_size(pos2(fit.x + (x + gap) * fit.unit, fit.y + (y + gap) * fit.unit), vec2((w - 2.0 * gap) * fit.unit, (h - 2.0 * gap) * fit.unit))
    };
    let mut shapes = Vec::new();
    let zone_board = leds.rows == 1 && leds.columns > 1;
    for key in &layout.keys {
        let rect = place(key.x, key.y, key.w, key.h);
        let label = key.label.to_string();
        if leds.rows >= 5 {
            shapes.push(Shape { region: Region::Cell(key.row, key.col), geom: Geom::Rect(rect), label: label.clone() });
        } else if zone_board {
            // Laptops light in bands across the board, left to right.
            let band = (((key.x + key.w / 2.0) / layout.width) * leds.columns as f32).floor().min(leds.columns as f32 - 1.0) as u8;
            shapes.push(Shape { region: Region::Cell(0, band), geom: Geom::Rect(rect), label: label.clone() });
        }
        if leds.has("backlight") {
            shapes.push(Shape { region: Region::Zone("backlight"), geom: Geom::Rect(rect), label });
        }
    }

    // The logo: in the matrix at row 0, column 20 on full-size boards, and/or a zone.
    let keys_bottom = fit.y + layout.height * fit.unit;
    let below = trace.bbox[3] as f32 - keys_bottom;
    let logo_at = if below > fit.unit * 0.55 {
        pos2(fit.x + layout.width * fit.unit / 2.0, keys_bottom + below.min(fit.unit * 1.6) / 2.0)
    } else {
        pos2(fit.x + (layout.width - 2.0) * fit.unit, fit.y + 0.5 * fit.unit)
    };
    let logo_radius = fit.unit * 0.45;
    if leds.rows >= 1 && leds.columns > 20 && !layout.keys.iter().any(|k| k.row == 0 && k.col == 20) && !keypad {
        shapes.push(Shape { region: Region::Cell(0, 20), geom: Geom::Dot(logo_at, logo_radius), label: "Logo".into() });
    }
    for zone in &leds.zones {
        match *zone {
            "logo" => shapes.push(Shape { region: Region::Zone("logo"), geom: Geom::Dot(logo_at, logo_radius), label: "Logo".into() }),
            "scroll" => {
                let at = pos2(fit.x + (layout.width - 0.6) * fit.unit, fit.y - 0.2 * fit.unit);
                shapes.push(Shape { region: Region::Zone("scroll"), geom: Geom::Dot(at, fit.unit * 0.4), label: "Dial".into() });
            }
            "left" | "right" => {
                let run = side_run(&trace, *zone == "left");
                shapes.push(Shape { region: Region::Zone(zone), geom: Geom::Strip(run, fit.unit * 0.12), label: zone.to_string() });
            }
            _ => {}
        }
    }
    // Rows past the keys are light bars and underglow, spaced along the outline.
    if leds.rows > 6 {
        let extra: Vec<(u8, u8)> = (6..leds.rows).flat_map(|r| (0..leds.columns).map(move |c| (r as u8, c as u8))).collect();
        let points = spaced(&trace.contour, extra.len());
        for ((row, col), at) in extra.into_iter().zip(points) {
            shapes.push(Shape { region: Region::Cell(row, col), geom: Geom::Dot(at, fit.unit * 0.16), label: format!("LED {}·{}", row, col + 1) });
        }
    }
    Art::finish(trace, shapes, photo, variant, fit.unit)
}

/// Look left of a fitted board for a column of macro keys.
///
/// Boards place them differently -- a narrow column hard against the board, or
/// full keys a gap away -- so every likely spacing is tried, and the column is
/// kept if its keycap edges stand out as clearly as the board's did; or, for
/// a board that says it has macro keys, wherever they fit best.
fn find_macro_keys(trace: &Trace, fit: &Fit, _height: f32, rows: usize, declared: bool) -> Option<Vec<layout::KeyCap>> {
    let mut best: Option<(f32, Vec<layout::KeyCap>)> = None;
    let mut total = 0.0;
    let mut count = 0usize;
    for (i, &inside) in trace.body.iter().enumerate() {
        if inside {
            total += trace.field[i];
            count += 1;
        }
    }
    let baseline = (total / count.max(1) as f32).max(1e-4);
    for width in [0.75f32, 0.85, 1.0] {
        for step in 0..=12 {
            let gap = step as f32 * 0.05;
            let keys = layout::macro_keys(gap, width, rows);
            let probe = Layout { keys: keys.clone(), width: 0.0, height: 0.0 };
            let samples = edge_samples(&probe);
            let score = samples.iter().map(|&(u, v)| trace.field_at(fit.x + u * fit.unit, fit.y + v * fit.unit)).sum::<f32>() / samples.len().max(1) as f32 / baseline;
            let on_body = keys.iter().all(|k| trace.inside(fit.x + (k.x + k.w / 2.0) * fit.unit, fit.y + (k.y + k.h / 2.0) * fit.unit));
            if on_body && best.as_ref().is_none_or(|(s, _)| score > *s) {
                best = Some((score, keys));
            }
        }
    }
    let (score, keys) = best?;
    (score >= fit.score * 0.8 || (declared && score >= GOOD_FIT * 0.8)).then_some(keys)
}

/// A drawn keyboard: its body and keycaps, at a fixed scale.
fn synthetic_keyboard(layout: &Layout) -> (Trace, Fit) {
    let unit = 30.0f32;
    let margin = 0.45 * unit;
    let w = (layout.width * unit + 2.0 * margin + 8.0).ceil() as usize;
    let h = (layout.height * unit + 2.0 * margin + 8.0).ceil() as usize;
    let body_rect = Rect::from_min_size(pos2(4.0, 4.0), vec2(layout.width * unit + 2.0 * margin, layout.height * unit + 2.0 * margin));
    let mut canvas = Canvas::new(w, h);
    canvas.fill_rounded(body_rect, unit * 0.35);
    let fit = Fit { unit, x: body_rect.left() + margin, y: body_rect.top() + margin, score: 0.0 };
    for key in &layout.keys {
        let rect = Rect::from_min_size(pos2(fit.x + (key.x + 0.06) * unit, fit.y + (key.y + 0.06) * unit), vec2((key.w - 0.12) * unit, (key.h - 0.12) * unit));
        canvas.stroke_rect(rect, 120);
        canvas.stroke_rect(rect.shrink(unit * 0.12), 55);
    }
    (canvas.into_trace(), fit)
}

// -- mice, headsets, strips ------------------------------------------------------------

fn centre_x(trace: &Trace) -> f32 {
    (trace.bbox[0] + trace.bbox[2]) as f32 / 2.0
}

/// The geometry of a lit part: its own pixels where the photo shows it lit,
/// otherwise a round spot where it usually is.
fn spot(blob: Option<&outline::Blob>, fallback: Pos2, radius: f32) -> Geom {
    match blob {
        Some(blob) if !blob.pixels.is_empty() => Geom::Pixels(blob.pixels.clone(), pos2(blob.x, blob.y), radius),
        Some(blob) => Geom::Dot(pos2(blob.x, blob.y), radius),
        None => Geom::Dot(fallback, radius),
    }
}

/// Where the scroll wheel and logo are: lit in the photo, or where they usually are.
fn mouse_spots(trace: &Trace, scroll_r: f32, logo_r: f32) -> (Geom, Geom) {
    let (top, w, h) = (trace.bbox[1] as f32, trace.bbox_w(), trace.bbox_h());
    let cx = centre_x(trace);
    let upper = |b: &&outline::Blob| b.y < top + 0.45 * h && (b.x - cx).abs() < 0.22 * w;
    let lower = |b: &&outline::Blob| b.y >= top + 0.45 * h && (b.x - cx).abs() < 0.3 * w;
    let scroll = spot(trace.lit.iter().filter(upper).max_by_key(|b| b.area), pos2(cx, top + 0.2 * h), scroll_r);
    let logo = spot(trace.lit.iter().filter(lower).max_by_key(|b| b.area), pos2(cx, top + 0.72 * h), logo_r);
    (scroll, logo)
}

/// The longest stretch of the outline where `keep` holds, in order.
fn contour_run(trace: &Trace, keep: impl Fn(f32, f32) -> bool) -> Vec<Pos2> {
    let n = trace.contour.len();
    if n == 0 {
        return Vec::new();
    }
    let flags: Vec<bool> = trace.contour.iter().map(|&(x, y)| keep(x, y)).collect();
    if flags.iter().all(|&f| f) {
        return trace.contour.iter().map(|&(x, y)| pos2(x, y)).collect();
    }
    // Start just after a point that is not kept, so a run is never split.
    let start = flags.iter().position(|&f| !f).unwrap_or(0);
    let mut best: Vec<Pos2> = Vec::new();
    let mut run: Vec<Pos2> = Vec::new();
    for k in 1..=n {
        let i = (start + k) % n;
        if flags[i] {
            run.push(pos2(trace.contour[i].0, trace.contour[i].1));
        } else {
            if run.len() > best.len() {
                best = std::mem::take(&mut run);
            }
            run.clear();
        }
    }
    if run.len() > best.len() {
        best = run;
    }
    best
}

/// One side of the outline, between a quarter and nine tenths of the way down.
fn side_run(trace: &Trace, left: bool) -> Vec<Pos2> {
    let (top, h) = (trace.bbox[1] as f32, trace.bbox_h());
    let cx = centre_x(trace);
    let run = contour_run(trace, |x, y| (if left { x < cx } else { x > cx }) && y > top + 0.25 * h && y < top + 0.9 * h);
    thin(&run, 24)
}

/// At most `n` points of a run, evenly picked.
fn thin(points: &[Pos2], n: usize) -> Vec<Pos2> {
    if points.len() <= n {
        return points.to_vec();
    }
    (0..n).map(|i| points[i * (points.len() - 1) / (n - 1).max(1)]).collect()
}

/// `n` points spaced evenly along a closed outline.
fn spaced(contour: &[(f32, f32)], n: usize) -> Vec<Pos2> {
    if contour.is_empty() || n == 0 {
        return Vec::new();
    }
    (0..n).map(|i| contour[(i * contour.len()) / n]).map(|(x, y)| pos2(x, y)).collect()
}

fn mouse_shapes(trace: &Trace, leds: &Leds) -> Vec<Shape> {
    let w = trace.bbox_w();
    let (scroll_r, logo_r) = (w * 0.075, w * 0.14);
    let (scroll_geom, logo_geom) = mouse_spots(trace, scroll_r, logo_r);
    let mut shapes = Vec::new();
    for zone in &leds.zones {
        let shape = match *zone {
            "scroll" => Shape { region: Region::Zone("scroll"), geom: scroll_geom.clone(), label: "Scroll wheel".into() },
            "logo" => Shape { region: Region::Zone("logo"), geom: logo_geom.clone(), label: "Logo".into() },
            "left" | "right" => Shape { region: Region::Zone(zone), geom: Geom::Strip(side_run(trace, *zone == "left"), w * 0.025), label: zone.to_string() },
            "backlight" => Shape { region: Region::Zone("backlight"), geom: Geom::Body, label: "Backlight".into() },
            other => Shape { region: Region::Zone(other), geom: Geom::Dot(pos2(centre_x(trace), trace.bbox[3] as f32 - w * 0.12), w * 0.06), label: other.into() },
        };
        shapes.push(shape);
    }
    // The matrix: wheel, logo, then the rest around the sides and the bottom.
    // A single LED is the logo zone again, and too small for per-key layers.
    let n = leds.cells();
    if n > 1 {
        let mut spots: Vec<(Geom, String)> = vec![(scroll_geom, "Scroll wheel".into()), (logo_geom, "Logo".into())];
        let (top, h) = (trace.bbox[1] as f32, trace.bbox_h());
        let run = contour_run(trace, |_, y| y > top + 0.18 * h);
        for (k, p) in run_points(&run, n.saturating_sub(2)).into_iter().enumerate() {
            spots.push((Geom::Dot(p, w * 0.045), format!("LED {}", k + 3)));
        }
        for (index, (geom, label)) in spots.into_iter().take(n).enumerate() {
            shapes.push(Shape { region: Region::Cell(0, index as u8), geom, label });
        }
    }
    shapes
}

/// `n` points spread along an open run, ends included.
fn run_points(run: &[Pos2], n: usize) -> Vec<Pos2> {
    if run.is_empty() || n == 0 {
        return Vec::new();
    }
    if n == 1 {
        return vec![run[run.len() / 2]];
    }
    (0..n).map(|i| run[i * (run.len() - 1) / (n - 1)]).collect()
}

/// The lit part of a headset on one side, above the cups: a Kraken Kitty's
/// cat ear.  Its own lit pixels where the photo shows it lit; otherwise, on a
/// Kitty, the ear's shape itself -- the body standing above the headband.
fn cat_ear(trace: &Trace, left_side: bool, kitty: bool) -> Option<Geom> {
    let (top, w, h) = (trace.bbox[1] as f32, trace.bbox_w(), trace.bbox_h());
    let cx = centre_x(trace);
    let mut pixels: Vec<u32> = Vec::new();
    for blob in &trace.lit {
        if blob.y < top + 0.45 * h && (blob.x < cx) == left_side && (blob.x - cx).abs() > 0.12 * w {
            pixels.extend_from_slice(&blob.pixels);
        }
    }
    if pixels.is_empty() && kitty {
        // The ears stand at the top corners, out past the middle of the headband.
        let bottom = (top + 0.27 * h) as usize;
        for y in trace.bbox[1]..bottom.min(trace.bbox[3]) {
            for x in trace.bbox[0]..=trace.bbox[2] {
                let outward = if left_side { cx - x as f32 } else { x as f32 - cx };
                if outward > 0.17 * w && trace.body[y * trace.w + x] {
                    pixels.push((y * trace.w + x) as u32);
                }
            }
        }
    }
    if pixels.is_empty() {
        return None;
    }
    let n = pixels.len() as f32;
    let sx: f32 = pixels.iter().map(|&p| (p as usize % trace.w) as f32).sum();
    let sy: f32 = pixels.iter().map(|&p| (p as usize / trace.w) as f32).sum();
    Some(Geom::Pixels(pixels, pos2(sx / n, sy / n), w * 0.1))
}

fn headset_shapes(trace: &Trace, leds: &Leds, name: &str) -> Vec<Shape> {
    let (left, top, w, h) = (trace.bbox[0] as f32, trace.bbox[1] as f32, trace.bbox_w(), trace.bbox_h());
    // The Kitty's cat ears light with the cup on their side, left with left.
    let kitty = name.to_lowercase().contains("kitty");
    let ears = [cat_ear(trace, true, kitty), cat_ear(trace, false, kitty)];
    // Each ear cup: the middle of the body's lower part on that side.
    let cup = |left_side: bool| {
        let (mut sx, mut sy, mut n) = (0.0f64, 0.0f64, 0usize);
        for y in (top + 0.4 * h) as usize..trace.bbox[3] {
            for x in trace.bbox[0]..=trace.bbox[2] {
                let fx = (x as f32 - left) / w;
                let side = if left_side { fx < 0.32 } else { fx > 0.68 };
                if side && trace.body[y * trace.w + x] {
                    sx += x as f64;
                    sy += y as f64;
                    n += 1;
                }
            }
        }
        if n == 0 {
            pos2(left + w * if left_side { 0.18 } else { 0.82 }, top + 0.72 * h)
        } else {
            pos2((sx / n as f64) as f32, (sy / n as f64) as f32)
        }
    };
    let (l, r) = (cup(true), cup(false));
    let radius = w * 0.13;
    // Everything one side lights: its cup, and its cat ear if it has one.
    let side = |left_side: bool, region: Region, label: &str, shapes: &mut Vec<Shape>| {
        let at = if left_side { l } else { r };
        shapes.push(Shape { region, geom: Geom::Dot(at, radius), label: label.into() });
        if let Some(ear) = &ears[if left_side { 0 } else { 1 }] {
            shapes.push(Shape { region, geom: ear.clone(), label: label.into() });
        }
    };
    let mut shapes = Vec::new();
    for zone in &leds.zones {
        match *zone {
            "left" => side(true, Region::Zone("left"), "Left ear", &mut shapes),
            "right" => side(false, Region::Zone("right"), "Right ear", &mut shapes),
            "backlight" | "logo" => {
                side(true, Region::Zone(zone), "Both ears", &mut shapes);
                side(false, Region::Zone(zone), "Both ears", &mut shapes);
            }
            _ => {}
        }
    }
    for index in 0..leds.cells() {
        let label = format!("LED {}", index + 1);
        if index < 2 {
            side(index == 0, Region::Cell(0, index as u8), &label, &mut shapes);
        } else {
            let at = if index % 2 == 0 { l } else { r };
            let lifted = pos2(at.x, at.y - (index / 2) as f32 * radius * 1.2);
            shapes.push(Shape { region: Region::Cell(0, index as u8), geom: Geom::Dot(lifted, radius * 0.8), label });
        }
    }
    shapes
}

/// Mousepads, docks, stands and the rest: LEDs spaced round the outline.
fn strip_shapes(trace: &Trace, leds: &Leds) -> Vec<Shape> {
    let w = trace.bbox_w().max(trace.bbox_h());
    let mut shapes = Vec::new();
    for zone in &leds.zones {
        match *zone {
            "backlight" => shapes.push(Shape {
                region: Region::Zone("backlight"),
                geom: Geom::Strip(thin(&trace.contour.iter().map(|&(x, y)| pos2(x, y)).collect::<Vec<_>>(), 160), w * 0.012),
                label: "Backlight".into(),
            }),
            "logo" => {
                let geom = spot(trace.lit.iter().max_by_key(|b| b.area), pos2(centre_x(trace), trace.bbox[3] as f32 - w * 0.08), w * 0.05);
                shapes.push(Shape { region: Region::Zone("logo"), geom, label: "Logo".into() });
            }
            other => {
                let index = ZONES.iter().position(|z| *z == other).unwrap_or(0) as f32;
                let at = pos2(trace.bbox[0] as f32 + w * (0.1 + 0.08 * index), trace.bbox[3] as f32 - w * 0.06);
                shapes.push(Shape { region: Region::Zone(other), geom: Geom::Dot(at, w * 0.03), label: other.replace('_', " ") });
            }
        }
    }
    let n = leds.cells();
    if n > 1 {
        for (index, at) in spaced(&trace.contour, n).into_iter().enumerate() {
            shapes.push(Shape { region: Region::Cell(0, index as u8), geom: Geom::Dot(at, w * 0.018), label: format!("LED {}", index + 1) });
        }
    }
    shapes
}

/// Several rows of LEDs with nothing to trace -- an ARGB controller's
/// channels -- drawn as one strip per row.
fn strips(leds: &Leds) -> Art {
    let pitch = 14.0f32;
    let w = (leds.columns as f32 * pitch + 60.0) as usize;
    let h = (leds.rows as f32 * 34.0 + 30.0) as usize;
    let mut canvas = Canvas::new(w, h);
    let mut shapes = Vec::new();
    for row in 0..leds.rows {
        let y = 22.0 + row as f32 * 34.0;
        let rect = Rect::from_min_size(pos2(20.0, y - 9.0), vec2(leds.columns as f32 * pitch + 20.0, 18.0));
        canvas.fill_rounded(rect, 9.0);
        for col in 0..leds.columns {
            let at = pos2(30.0 + col as f32 * pitch + pitch / 2.0 - 4.0, y);
            shapes.push(Shape { region: Region::Cell(row as u8, col as u8), geom: Geom::Dot(at, 4.5), label: format!("{} · LED {}", row + 1, col + 1) });
        }
    }
    let trace = canvas.into_trace();
    Art::finish(trace, shapes, false, None, 14.0)
}

// -- drawing a device that has no photo -------------------------------------------------

#[derive(Clone, Copy)]
enum Silhouette {
    Mouse,
    Headset,
    Mat,
    Slab,
}

/// A simple raster to draw a stand-in outline on.
struct Canvas {
    w: usize,
    h: usize,
    body: Vec<bool>,
    lines: Vec<u8>,
}

impl Canvas {
    fn new(w: usize, h: usize) -> Canvas {
        Canvas { w, h, body: vec![false; w * h], lines: vec![0; w * h] }
    }

    fn fill_where(&mut self, inside: impl Fn(f32, f32) -> bool) {
        for y in 0..self.h {
            for x in 0..self.w {
                if inside(x as f32 + 0.5, y as f32 + 0.5) {
                    self.body[y * self.w + x] = true;
                }
            }
        }
    }

    fn fill_rounded(&mut self, rect: Rect, radius: f32) {
        self.fill_where(|x, y| {
            let dx = (rect.left() + radius - x).max(x - (rect.right() - radius)).max(0.0);
            let dy = (rect.top() + radius - y).max(y - (rect.bottom() - radius)).max(0.0);
            rect.contains(pos2(x, y)) && dx * dx + dy * dy <= radius * radius
        });
    }

    fn plot(&mut self, x: f32, y: f32, value: u8) {
        if x >= 0.0 && y >= 0.0 && (x as usize) < self.w && (y as usize) < self.h {
            let i = y as usize * self.w + x as usize;
            self.lines[i] = self.lines[i].max(value);
        }
    }

    fn line(&mut self, a: Pos2, b: Pos2, value: u8) {
        let steps = (b - a).length().ceil().max(1.0) as usize;
        for s in 0..=steps {
            let p = a + (b - a) * (s as f32 / steps as f32);
            self.plot(p.x, p.y, value);
        }
    }

    fn stroke_rect(&mut self, rect: Rect, value: u8) {
        let corners = [rect.left_top(), rect.right_top(), rect.right_bottom(), rect.left_bottom()];
        for i in 0..4 {
            self.line(corners[i], corners[(i + 1) % 4], value);
        }
    }

    fn into_trace(mut self) -> Trace {
        let (w, h) = (self.w, self.h);
        let (mut left, mut top, mut right, mut bottom) = (w, h, 0, 0);
        for y in 0..h {
            for x in 0..w {
                let i = y * w + x;
                if !self.body[i] {
                    continue;
                }
                (left, top, right, bottom) = (left.min(x), top.min(y), right.max(x), bottom.max(y));
                let edge = x == 0 || y == 0 || x + 1 == w || y + 1 == h || !self.body[i - 1] || !self.body[i + 1] || !self.body[i - w] || !self.body[i + w];
                if edge {
                    self.lines[i] = 255;
                }
            }
        }
        let field: Vec<f32> = self.lines.iter().map(|&l| l as f32 / 255.0).collect();
        let body_area = self.body.iter().filter(|&&b| b).count();
        let mut contour: Vec<(f32, f32)> = Vec::new();
        if body_area > 0 {
            // Walk the outline the way a traced photo's is walked.
            let mut image = image::RgbaImage::new(w as u32, h as u32);
            for (i, &inside) in self.body.iter().enumerate() {
                if inside {
                    image.put_pixel((i % w) as u32, (i / w) as u32, image::Rgba([40, 40, 40, 255]));
                }
            }
            if let Some(traced) = outline::trace(&image) {
                contour = traced.contour;
            }
        }
        Trace { w, h, body: self.body, lines: self.lines, field, bbox: [left, top, right.max(left), bottom.max(top)], contour, lit: Vec::new() }
    }
}

fn synthetic(kind: Silhouette) -> Trace {
    match kind {
        Silhouette::Mouse => {
            let (w, h) = (240usize, 360usize);
            let mut canvas = Canvas::new(w, h);
            // An egg, narrower at the front.
            canvas.fill_where(|x, y| {
                let ny = (y - 190.0) / 160.0;
                let half = 92.0 * (1.0 - ny * ny).max(0.0).sqrt() * (0.86 + 0.14 * ny.max(-1.0));
                ny.abs() <= 1.0 && (x - 120.0).abs() <= half
            });
            canvas.line(pos2(120.0, 32.0), pos2(120.0, 150.0), 150);
            canvas.line(pos2(40.0, 150.0), pos2(200.0, 150.0), 110);
            canvas.stroke_rect(Rect::from_center_size(pos2(120.0, 85.0), vec2(16.0, 44.0)), 170);
            canvas.into_trace()
        }
        Silhouette::Headset => {
            let (w, h) = (360usize, 320usize);
            let mut canvas = Canvas::new(w, h);
            canvas.fill_where(|x, y| {
                let band = {
                    let d = ((x - 180.0).powi(2) + (y - 190.0).powi(2)).sqrt();
                    (130.0..152.0).contains(&d) && y < 200.0
                };
                let cup = |cx: f32| ((x - cx) / 44.0).powi(2) + ((y - 215.0) / 70.0).powi(2) <= 1.0;
                band || cup(52.0) || cup(308.0)
            });
            canvas.into_trace()
        }
        Silhouette::Mat => {
            let mut canvas = Canvas::new(420, 300);
            canvas.fill_rounded(Rect::from_min_size(pos2(10.0, 10.0), vec2(400.0, 280.0)), 18.0);
            canvas.into_trace()
        }
        Silhouette::Slab => {
            let mut canvas = Canvas::new(300, 300);
            canvas.fill_rounded(Rect::from_min_size(pos2(20.0, 20.0), vec2(260.0, 260.0)), 40.0);
            canvas.into_trace()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn caps(kind: &str, dims: Option<[u64; 2]>, zones: &[&str]) -> Value {
        let zones: serde_json::Map<String, Value> = zones.iter().map(|z| (z.to_string(), json!({"effects": ["static"], "brightness": true}))).collect();
        json!({ "type": kind, "matrix_dimensions": dims, "custom_frame": dims.is_some(), "zones": zones })
    }

    #[test]
    fn a_drawn_keyboard_has_a_key_per_led_and_a_logo() {
        let art = Art::build("keyboard", &caps("keyboard", Some([6, 22]), &["backlight"]), None);
        assert!(!art.photo);
        let cells: std::collections::HashSet<_> = art.shapes.iter().filter_map(|s| match s.region {
            Region::Cell(r, c) => Some((r, c)),
            _ => None,
        }).collect();
        assert!(cells.contains(&(3, 2)), "the A key");
        assert!(cells.contains(&(0, 20)), "the logo");
        assert!(art.shapes.iter().any(|s| s.region == Region::Zone("backlight")));
    }

    #[test]
    fn a_drawn_mouse_has_its_zones_and_leds() {
        let art = Art::build("mouse", &caps("mouse", Some([1, 5]), &["logo", "scroll"]), None);
        assert!(art.shapes.iter().any(|s| s.region == Region::Zone("scroll")));
        assert!(art.shapes.iter().any(|s| s.region == Region::Zone("logo")));
        assert_eq!(art.shapes.iter().filter(|s| matches!(s.region, Region::Cell(..))).count(), 5);
    }

    #[test]
    fn lit_shapes_colour_the_drawing() {
        let art = Art::build("mouse", &caps("mouse", None, &["logo"]), None);
        let logo = art.shapes.iter().position(|s| s.region == Region::Zone("logo")).unwrap();
        let mut colours = vec![Color32::BLACK; art.shapes.len()];
        let dark = art.paint(&colours);
        colours[logo] = Color32::from_rgb(0, 255, 0);
        let lit = art.paint(&colours);
        let centre = art.shapes[logo].bounds(&art).center();
        let i = centre.y as usize * art.w + centre.x as usize;
        assert!(lit.pixels[i].g() > dark.pixels[i].g() + 60);
    }

    #[test]
    fn many_rows_of_leds_become_strips() {
        let art = Art::build("accessory", &caps("accessory", Some([6, 80]), &[]), None);
        assert_eq!(art.shapes.len(), 480);
    }

    /// Fits the layout to real product photos and writes what it drew, for
    /// looking at: `OPENRAZER_PHOTOS=<dir> cargo test -- --ignored photos`.
    #[test]
    #[ignore]
    fn photos() {
        let dir = std::path::PathBuf::from(std::env::var_os("OPENRAZER_PHOTOS").expect("OPENRAZER_PHOTOS"));
        for entry in std::fs::read_dir(&dir).unwrap().flatten() {
            let path = entry.path();
            let name = path.file_stem().unwrap().to_string_lossy().to_string();
            if name.starts_with("art-") || !matches!(path.extension().and_then(|e| e.to_str()), Some("png" | "jpg")) {
                continue;
            }
            let photo = image::open(&path).unwrap().thumbnail(crate::images::TRACE_SIZE, crate::images::TRACE_SIZE).to_rgba8();
            let photo = crate::images::cut_out_backdrop(&photo).unwrap_or(photo);
            let kind = if name.contains("viper") || name.contains("deathadder") || name.contains("basilisk") || name.contains("naga") {
                ("mouse", caps("mouse", Some([1, 3]), &["logo", "scroll"]))
            } else if name.contains("firefly") {
                ("mousemat", caps("mousemat", Some([1, 15]), &["backlight"]))
            } else if name.contains("tartarus") {
                ("keypad", caps("keypad", Some([4, 8]), &["backlight"]))
            } else if name.contains("kitty") || name.contains("kraken") {
                let mut caps = caps("headset", Some([1, 2]), &["backlight", "left", "right"]);
                caps["name"] = "Razer Kraken Kitty V2 BT".into();
                ("headset", caps)
            } else {
                ("keyboard", caps("keyboard", Some([6, 22]), &["backlight", "logo"]))
            };
            let started = std::time::Instant::now();
            let art = Art::build(kind.0, &kind.1, Some(&photo));
            let colours: Vec<Color32> = (0..art.shapes.len()).map(|i| crate::preview::hue(i as f32 * 0.037)).collect();
            let image = art.paint(&colours);
            let bytes: Vec<u8> = image.pixels.iter().flat_map(|p| p.to_array()).collect();
            let out = image::RgbaImage::from_raw(art.w as u32, art.h as u32, bytes).unwrap();
            out.save(dir.join(format!("art-{name}.png"))).unwrap();
            println!("{name}: photo {} variant {:?} unit {:.1} shapes {} {}x{} in {:?}", art.photo, art.variant, art.unit, art.shapes.len(), art.w, art.h, started.elapsed());
        }
    }
}
