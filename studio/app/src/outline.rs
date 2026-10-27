//! A device's outline, traced from its product photo.
//!
//! The lighting editor draws each device the way Razer's own software does:
//! as a line drawing of that very product, its LEDs glowing in place. Razer
//! draws those by hand; there are hundreds of devices here, so the drawing is
//! traced from the product photo instead. The silhouette gives the outer
//! line, edges inside it give the keycaps, buttons and seams, and the parts
//! that are lit in the photo -- the logo, the scroll wheel -- say where those
//! LEDs are.

use image::RgbaImage;

#[derive(Clone, Debug)]
pub struct Blob {
    pub x: f32,
    pub y: f32,
    pub area: usize,
    /// The lit pixels themselves (index = y * w + x): the shape of a logo.
    pub pixels: Vec<u32>,
}

/// What tracing a photo found, in the photo's pixels.
#[derive(Clone, Debug, Default)]
pub struct Trace {
    pub w: usize,
    pub h: usize,
    /// The device's body: no cable, no glow around it, holes filled.
    pub body: Vec<bool>,
    /// Line intensity, 0-255: the outer line at full strength, detail fainter.
    pub lines: Vec<u8>,
    /// Edges inside the body, softened, for fitting a key layout to.
    pub field: Vec<f32>,
    /// The body's bounds: left, top, right, bottom (inclusive).
    pub bbox: [usize; 4],
    /// The outer edge of the body, in order, evenly spaced.
    pub contour: Vec<(f32, f32)>,
    /// Bright, coloured patches on the body: lit logos and wheels.
    pub lit: Vec<Blob>,
}

impl Trace {
    pub fn bbox_w(&self) -> f32 {
        (self.bbox[2] - self.bbox[0] + 1) as f32
    }
    pub fn bbox_h(&self) -> f32 {
        (self.bbox[3] - self.bbox[1] + 1) as f32
    }
    pub fn inside(&self, x: f32, y: f32) -> bool {
        let (x, y) = (x.round(), y.round());
        x >= 0.0 && y >= 0.0 && (x as usize) < self.w && (y as usize) < self.h && self.body[y as usize * self.w + x as usize]
    }
    pub fn field_at(&self, x: f32, y: f32) -> f32 {
        let (xi, yi) = (x as isize, y as isize);
        if xi < 0 || yi < 0 || xi as usize >= self.w || yi as usize >= self.h {
            return 0.0;
        }
        self.field[yi as usize * self.w + xi as usize]
    }
}

fn hsv(r: u8, g: u8, b: u8) -> (f32, f32) {
    let (r, g, b) = (r as f32 / 255.0, g as f32 / 255.0, b as f32 / 255.0);
    let max = r.max(g).max(b);
    let min = r.min(g).min(b);
    let sat = if max <= 0.0 { 0.0 } else { (max - min) / max };
    (sat, max)
}

/// Flood the background in from the border; whatever it cannot reach is body.
fn fill_holes(mask: &mut [bool], w: usize, h: usize) {
    let mut outside = vec![false; w * h];
    let mut stack: Vec<usize> = Vec::new();
    for x in 0..w {
        stack.push(x);
        stack.push((h - 1) * w + x);
    }
    for y in 0..h {
        stack.push(y * w);
        stack.push(y * w + w - 1);
    }
    while let Some(i) = stack.pop() {
        if mask[i] || outside[i] {
            continue;
        }
        outside[i] = true;
        let (x, y) = (i % w, i / w);
        if x > 0 {
            stack.push(i - 1);
        }
        if x + 1 < w {
            stack.push(i + 1);
        }
        if y > 0 {
            stack.push(i - w);
        }
        if y + 1 < h {
            stack.push(i + w);
        }
    }
    for (m, o) in mask.iter_mut().zip(outside) {
        *m = !o;
    }
}

/// Square erosion (`grow` false) or dilation (`grow` true) of radius `r`, separably.
fn morph(mask: &[bool], w: usize, h: usize, r: usize, grow: bool) -> Vec<bool> {
    let mut rows = vec![false; w * h];
    for y in 0..h {
        for x in 0..w {
            let (a, b) = (x.saturating_sub(r), (x + r).min(w - 1));
            let row = &mask[y * w + a..=y * w + b];
            rows[y * w + x] = if grow { row.iter().any(|&m| m) } else { row.iter().all(|&m| m) };
        }
    }
    let mut out = vec![false; w * h];
    for y in 0..h {
        let (a, b) = (y.saturating_sub(r), (y + r).min(h - 1));
        for x in 0..w {
            out[y * w + x] = if grow { (a..=b).any(|yy| rows[yy * w + x]) } else { (a..=b).all(|yy| rows[yy * w + x]) };
        }
    }
    out
}

/// Labels of 4-connected components of `mask`, and each one's size.
fn components(mask: &[bool], w: usize, h: usize) -> (Vec<u32>, Vec<usize>) {
    let mut label = vec![0u32; w * h];
    let mut sizes = vec![0usize];
    let mut stack = Vec::new();
    for start in 0..w * h {
        if !mask[start] || label[start] != 0 {
            continue;
        }
        let id = sizes.len() as u32;
        sizes.push(0);
        stack.push(start);
        label[start] = id;
        while let Some(i) = stack.pop() {
            sizes[id as usize] += 1;
            let (x, y) = (i % w, i / w);
            let mut visit = |j: usize| {
                if mask[j] && label[j] == 0 {
                    label[j] = id;
                    stack.push(j);
                }
            };
            if x > 0 {
                visit(i - 1);
            }
            if x + 1 < w {
                visit(i + 1);
            }
            if y > 0 {
                visit(i - w);
            }
            if y + 1 < h {
                visit(i + w);
            }
        }
    }
    (label, sizes)
}

/// Box blur of radius `r`, twice: close enough to a Gaussian.
pub fn blur(values: &[f32], w: usize, h: usize, r: usize) -> Vec<f32> {
    let mut a = values.to_vec();
    let mut b = vec![0.0f32; w * h];
    for _ in 0..2 {
        for y in 0..h {
            let row = &a[y * w..(y + 1) * w];
            let mut sum: f32 = row[..(r + 1).min(w)].iter().sum();
            for x in 0..w {
                let lo = x as isize - r as isize;
                let hi = x + r;
                let count = (hi.min(w - 1) as isize - lo.max(0) + 1) as f32;
                b[y * w + x] = sum / count;
                if hi + 1 < w {
                    sum += row[hi + 1];
                }
                if lo >= 0 {
                    sum -= row[lo as usize];
                }
            }
        }
        for x in 0..w {
            let mut sum = 0.0;
            for y in 0..(r + 1).min(h) {
                sum += b[y * w + x];
            }
            for y in 0..h {
                let lo = y as isize - r as isize;
                let hi = y + r;
                let count = (hi.min(h - 1) as isize - lo.max(0) + 1) as f32;
                a[y * w + x] = sum / count;
                if hi + 1 < h {
                    sum += b[(hi + 1) * w + x];
                }
                if lo >= 0 {
                    sum -= b[lo as usize * w + x];
                }
            }
        }
    }
    a
}

/// The outer boundary of the body, walked in order (Moore neighbour tracing).
fn walk_contour(body: &[bool], w: usize, h: usize) -> Vec<(f32, f32)> {
    let at = |x: isize, y: isize| x >= 0 && y >= 0 && (x as usize) < w && (y as usize) < h && body[y as usize * w + x as usize];
    let Some(start) = body.iter().position(|&b| b) else { return Vec::new() };
    let start = ((start % w) as isize, (start / w) as isize);
    // Clockwise neighbours, starting west.
    const AROUND: [(isize, isize); 8] = [(-1, 0), (-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1)];
    let mut points = vec![start];
    let mut current = start;
    let mut from = 0usize; // the direction we entered from
    for _ in 0..(w * h * 2) {
        let mut found = None;
        for step in 0..8 {
            let d = (from + 1 + step) % 8;
            let next = (current.0 + AROUND[d].0, current.1 + AROUND[d].1);
            if at(next.0, next.1) {
                found = Some((next, d));
                break;
            }
        }
        let Some((next, d)) = found else { break };
        // Look back towards where we came from on the next step.
        from = (d + 4) % 8;
        current = next;
        if current == start && points.len() > 2 {
            break;
        }
        points.push(current);
    }
    resample(&points.iter().map(|&(x, y)| (x as f32, y as f32)).collect::<Vec<_>>(), 2.0)
}

/// Points every `spacing` pixels along a closed polyline, lightly smoothed.
pub fn resample(points: &[(f32, f32)], spacing: f32) -> Vec<(f32, f32)> {
    if points.len() < 3 {
        return points.to_vec();
    }
    // Smooth the staircase of pixel steps first.
    let n = points.len();
    let k = 3usize.min(n / 2);
    let smooth: Vec<(f32, f32)> = (0..n)
        .map(|i| {
            let mut sx = 0.0;
            let mut sy = 0.0;
            for j in 0..=2 * k {
                let p = points[(i + n + j - k) % n];
                sx += p.0;
                sy += p.1;
            }
            let c = (2 * k + 1) as f32;
            (sx / c, sy / c)
        })
        .collect();
    let mut out = vec![smooth[0]];
    let mut carry = 0.0f32;
    for i in 0..n {
        let a = smooth[i];
        let b = smooth[(i + 1) % n];
        let len = ((b.0 - a.0).powi(2) + (b.1 - a.1).powi(2)).sqrt();
        let mut t = spacing - carry;
        while t <= len {
            out.push((a.0 + (b.0 - a.0) * t / len, a.1 + (b.1 - a.1) * t / len));
            t += spacing;
        }
        carry = len - (t - spacing);
    }
    out
}

/// Trace `photo`, a cut-out product picture.
pub fn trace(photo: &RgbaImage) -> Option<Trace> {
    let (w, h) = (photo.width() as usize, photo.height() as usize);
    if w < 16 || h < 16 {
        return None;
    }
    let pixels = photo.as_raw();
    let mut lum = vec![0.0f32; w * h];
    let mut alpha = vec![0.0f32; w * h];
    let mut glowing = vec![false; w * h];
    for i in 0..w * h {
        let [r, g, b, a] = [pixels[i * 4], pixels[i * 4 + 1], pixels[i * 4 + 2], pixels[i * 4 + 3]];
        alpha[i] = a as f32 / 255.0;
        lum[i] = (0.2126 * r as f32 + 0.7152 * g as f32 + 0.0722 * b as f32) / 255.0 * alpha[i];
        let (sat, val) = hsv(r, g, b);
        glowing[i] = sat > 0.45 && val > 0.4;
    }

    // The body: solid pixels, minus soft coloured glow around the device.
    let mut body: Vec<bool> = (0..w * h).map(|i| alpha[i] >= 0.8 || (alpha[i] >= 0.5 && !glowing[i])).collect();
    fill_holes(&mut body, w, h);
    // An opening takes off the cable and other thin strands: a braided mouse
    // cable is a twentieth of the mouse's width.
    let extent = {
        let (mut l, mut r) = (w, 0);
        for y in 0..h {
            for x in 0..w {
                if body[y * w + x] {
                    l = l.min(x);
                    r = r.max(x);
                }
            }
        }
        r.saturating_sub(l).max(1)
    };
    let r = ((extent as f32) * 0.03).round().clamp(3.0, 14.0) as usize;
    let opened = morph(&morph(&body, w, h, r, false), w, h, r, true);
    // But a cable is a loose end, joined to the body at one end only. A thin
    // part that joins two pieces of the body -- the side of a headband, from
    // the ears down to a cup -- is the device itself, and is put back.
    let taken: Vec<bool> = (0..w * h).map(|i| body[i] && !opened[i]).collect();
    let (strands, strand_sizes) = components(&taken, w, h);
    let (pieces, _) = components(&opened, w, h);
    let mut joins: Vec<Vec<u32>> = vec![Vec::new(); strand_sizes.len()];
    for (i, &strand) in strands.iter().enumerate() {
        let strand = strand as usize;
        if strand == 0 {
            continue;
        }
        let (x, y) = (i % w, i / w);
        let around = [(x > 0).then(|| i - 1), (x + 1 < w).then(|| i + 1), (y > 0).then(|| i - w), (y + 1 < h).then(|| i + w)];
        for j in around.into_iter().flatten() {
            let piece = pieces[j];
            if piece != 0 && !joins[strand].contains(&piece) {
                joins[strand].push(piece);
            }
        }
    }
    let opened: Vec<bool> = (0..w * h).map(|i| opened[i] || (strands[i] != 0 && joins[strands[i] as usize].len() >= 2)).collect();
    let (labels, sizes) = components(&opened, w, h);
    let largest = sizes.iter().copied().max().unwrap_or(0);
    if largest < w * h / 50 {
        return None;
    }
    let keep: Vec<bool> = sizes.iter().map(|&s| s > 0 && s * 7 >= largest).collect();
    let mut body: Vec<bool> = labels.iter().map(|&l| l != 0 && keep[l as usize]).collect();
    fill_holes(&mut body, w, h);
    let body_area = body.iter().filter(|&&b| b).count();

    let (mut left, mut top, mut right, mut bottom) = (w, h, 0, 0);
    for y in 0..h {
        for x in 0..w {
            if body[y * w + x] {
                (left, top, right, bottom) = (left.min(x), top.min(y), right.max(x), bottom.max(y));
            }
        }
    }

    // Edges inside the body: Sobel on a lightly softened picture, so the weave
    // of a mousepad's cloth does not come out as noise, then thinned to one pixel.
    let lum = blur(&lum, w, h, 1);
    let mut magnitude = vec![0.0f32; w * h];
    let mut direction = vec![0u8; w * h];
    for y in 1..h - 1 {
        for x in 1..w - 1 {
            let p = |dx: isize, dy: isize| lum[(y as isize + dy) as usize * w + (x as isize + dx) as usize];
            let gx = p(1, -1) + 2.0 * p(1, 0) + p(1, 1) - p(-1, -1) - 2.0 * p(-1, 0) - p(-1, 1);
            let gy = p(-1, 1) + 2.0 * p(0, 1) + p(1, 1) - p(-1, -1) - 2.0 * p(0, -1) - p(1, -1);
            magnitude[y * w + x] = (gx * gx + gy * gy).sqrt();
            let angle = gy.atan2(gx).to_degrees().rem_euclid(180.0);
            direction[y * w + x] = ((angle + 22.5) / 45.0) as u8 % 4;
        }
    }
    let mut thin = vec![0.0f32; w * h];
    for y in 1..h - 1 {
        for x in 1..w - 1 {
            let i = y * w + x;
            if !body[i] {
                continue;
            }
            let (a, b) = match direction[i] {
                0 => (i - 1, i + 1),
                1 => (i - w - 1, i + w + 1),
                2 => (i - w, i + w),
                _ => (i - w + 1, i + w - 1),
            };
            if magnitude[i] >= magnitude[a] && magnitude[i] >= magnitude[b] {
                thin[i] = magnitude[i];
            }
        }
    }
    let mut strengths: Vec<f32> = thin.iter().copied().filter(|&m| m > 0.02).collect();
    strengths.sort_by(|a, b| a.total_cmp(b));
    let strong = strengths.get(strengths.len() * 92 / 100).copied().unwrap_or(1.0).max(0.05);
    let detail: Vec<f32> = thin.iter().map(|&m| ((m / strong - 0.2) / 0.8).clamp(0.0, 1.0)).collect();

    // The outer line, softened so its pixel steps do not show when the
    // drawing is scaled up.
    let mut edge = vec![0.0f32; w * h];
    for y in 0..h {
        for x in 0..w {
            let i = y * w + x;
            if body[i] && (x == 0 || y == 0 || x + 1 == w || y + 1 == h || !body[i - 1] || !body[i + 1] || !body[i - w] || !body[i + w]) {
                edge[i] = 1.0;
            }
        }
    }
    let edge = blur(&edge, w, h, 1);
    let mut lines = vec![0u8; w * h];
    for i in 0..w * h {
        let outer = (edge[i] * 2.6).min(1.0);
        lines[i] = (outer * 255.0).max(detail[i] * 175.0) as u8;
    }
    let field = blur(&detail, w, h, ((w as f32) / 400.0).round().max(1.0) as usize);

    // Lit patches on the body.  A logo drawn in thin lines comes apart into
    // specks, so they are joined up before being counted.
    // Not near the outline: glow round a device bleeds onto its edge, and
    // would pass for a ring-shaped "logo".
    let inner = morph(&body, w, h, ((extent as f32) * 0.035).round().max(2.0) as usize, false);
    let lit_mask: Vec<bool> = (0..w * h).map(|i| inner[i] && glowing[i] && alpha[i] > 0.5).collect();
    let joined = morph(&lit_mask, w, h, ((w as f32) / 180.0).round().max(2.0) as usize, true);
    let (lit_labels, lit_sizes) = components(&joined, w, h);
    let mut sums = vec![(0.0f64, 0.0f64); lit_sizes.len()];
    let mut own: Vec<Vec<u32>> = vec![Vec::new(); lit_sizes.len()];
    for (i, &l) in lit_labels.iter().enumerate() {
        if l != 0 {
            sums[l as usize].0 += (i % w) as f64;
            sums[l as usize].1 += (i / w) as f64;
            if lit_mask[i] {
                own[l as usize].push(i as u32);
            }
        }
    }
    let lit = lit_sizes
        .iter()
        .enumerate()
        .skip(1)
        .filter(|&(_, &size)| size * 1200 >= body_area)
        .map(|(l, &size)| Blob {
            x: (sums[l].0 / size as f64) as f32,
            y: (sums[l].1 / size as f64) as f32,
            area: size,
            pixels: std::mem::take(&mut own[l]),
        })
        .collect();

    let contour = walk_contour(&body, w, h);
    Some(Trace { w, h, body, lines, field, bbox: [left, top, right, bottom], contour, lit })
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::Rgba;

    /// A dark rounded slab with a cable and a lit logo, on transparency.
    fn mouse() -> RgbaImage {
        let mut image = RgbaImage::from_pixel(200, 300, Rgba([0, 0, 0, 0]));
        for y in 0..300 {
            for x in 0..200 {
                let (dx, dy) = ((x as f32 - 100.0) / 70.0, (y as f32 - 170.0) / 110.0);
                if dx * dx + dy * dy <= 1.0 {
                    image.put_pixel(x, y, Rgba([30, 30, 34, 255]));
                }
                if (98..102).contains(&x) && y < 70 {
                    image.put_pixel(x, y, Rgba([20, 20, 20, 255])); // the cable
                }
                let (lx, ly) = (x as f32 - 100.0, y as f32 - 220.0);
                if lx * lx + ly * ly < 100.0 {
                    image.put_pixel(x, y, Rgba([40, 230, 40, 255])); // the logo
                }
            }
        }
        image
    }

    #[test]
    fn the_body_loses_its_cable_and_keeps_its_logo() {
        let trace = trace(&mouse()).expect("a body");
        assert!(trace.bbox[1] >= 55, "the cable is not part of the outline: {:?}", trace.bbox);
        assert!(trace.inside(100.0, 220.0), "the lit logo is part of the body");
        assert_eq!(trace.lit.len(), 1);
        assert!((trace.lit[0].y - 220.0).abs() < 2.0);
    }

    #[test]
    fn the_contour_goes_round_the_body() {
        let trace = trace(&mouse()).expect("a body");
        assert!(trace.contour.len() > 200);
        let xs: Vec<f32> = trace.contour.iter().map(|p| p.0).collect();
        let min = xs.iter().copied().fold(f32::INFINITY, f32::min);
        let max = xs.iter().copied().fold(0.0, f32::max);
        assert!(min < 35.0 && max > 165.0, "{min} {max}");
    }

    #[test]
    fn the_outer_line_is_drawn_strongest() {
        let trace = trace(&mouse()).expect("a body");
        let edge = trace.contour[0];
        let near = (-2..=2)
            .flat_map(|dy| (-2..=2).map(move |dx| (dx, dy)))
            .any(|(dx, dy)| trace.lines[((edge.1 as isize + dy) as usize) * trace.w + (edge.0 as isize + dx) as usize] >= 200);
        assert!(near);
    }
}
