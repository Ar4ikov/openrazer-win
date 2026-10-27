//! Product pictures, fetched once from Razer's site and kept on disk.
//!
//! Each picture is kept twice: a small texture for tiles and headers, and the
//! cut-out photo itself, which the lighting editor traces into the device's
//! outline (see `outline`).

use egui::{ColorImage, TextureHandle, TextureOptions};
use image::RgbaImage;
use std::collections::HashMap;
use std::io::Read;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

/// The longest side of the photo kept for tracing.
pub const TRACE_SIZE: u32 = 720;

type Arrived = Arc<Mutex<Vec<(String, Option<(ColorImage, Arc<RgbaImage>)>)>>>;

#[derive(Default)]
pub struct Images {
    textures: HashMap<String, Option<TextureHandle>>,
    /// Asked for, and not arrived yet.
    waiting: std::collections::HashSet<String>,
    photos: HashMap<String, Arc<RgbaImage>>,
    arrived: Arrived,
}

impl Images {
    fn collect(&mut self, ctx: &egui::Context) {
        let arrived: Vec<_> = std::mem::take(&mut *self.arrived.lock().unwrap());
        for (url, image) in arrived {
            self.waiting.remove(&url);
            match image {
                Some((thumbnail, photo)) => {
                    self.textures.insert(url.clone(), Some(ctx.load_texture(url.clone(), thumbnail, TextureOptions::LINEAR)));
                    self.photos.insert(url, photo);
                }
                None => {
                    self.textures.insert(url, None);
                }
            }
        }
    }

    fn request(&mut self, ctx: &egui::Context, url: &str) {
        if self.textures.contains_key(url) {
            return;
        }
        // Reserve the slot so it is only fetched once; the thread fills it in.
        self.textures.insert(url.to_string(), None);
        self.waiting.insert(url.to_string());
        let arrived = self.arrived.clone();
        let url = url.to_string();
        let ctx = ctx.clone();
        std::thread::Builder::new()
            .name("image".into())
            .spawn(move || {
                let image = fetch(&url).and_then(|bytes| decode(&bytes));
                arrived.lock().unwrap().push((url, image));
                ctx.request_repaint();
            })
            .ok();
    }

    /// The picture at `url`, once it has arrived; `None` until then, or if it never will.
    pub fn get(&mut self, ctx: &egui::Context, url: &str) -> Option<TextureHandle> {
        self.collect(ctx);
        self.request(ctx, url);
        self.textures.get(url).cloned().flatten()
    }

    /// The cut-out photo at `url`, at tracing size.
    pub fn photo(&mut self, ctx: &egui::Context, url: &str) -> Option<Arc<RgbaImage>> {
        self.collect(ctx);
        self.request(ctx, url);
        self.photos.get(url).cloned()
    }

    /// Whether the picture at `url` has been tried and could not be had.
    pub fn failed(&self, url: &str) -> bool {
        self.textures.contains_key(url) && !self.waiting.contains(url) && !self.photos.contains_key(url)
    }
}

fn cache_path(url: &str) -> PathBuf {
    // FNV-1a: stable across runs, which the std hasher is not.  The same file
    // names as the classic control panel, so the two share one cache.
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in url.bytes() {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    openrazer_studio_core::paths::data_dir().join("images").join(format!("{hash:016x}.img"))
}

fn fetch(url: &str) -> Option<Vec<u8>> {
    let path = cache_path(url);
    if let Ok(bytes) = std::fs::read(&path) {
        return Some(bytes);
    }
    if !url.starts_with("https://") {
        return None;
    }
    let response = ureq::get(url).timeout(std::time::Duration::from_secs(15)).call().ok()?;
    let mut bytes = Vec::new();
    response.into_reader().take(8 * 1024 * 1024).read_to_end(&mut bytes).ok()?;
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(&path, &bytes);
    Some(bytes)
}

fn decode(bytes: &[u8]) -> Option<(ColorImage, Arc<RgbaImage>)> {
    let image = image::load_from_memory(bytes).ok()?.thumbnail(TRACE_SIZE, TRACE_SIZE).to_rgba8();
    let photo = cut_out_backdrop(&image).unwrap_or(image);
    let thumbnail = image::DynamicImage::ImageRgba8(photo.clone()).thumbnail(360, 360).to_rgba8();
    let size = [thumbnail.width() as usize, thumbnail.height() as usize];
    Some((ColorImage::from_rgba_unmultiplied(size, thumbnail.as_raw()), Arc::new(photo)))
}

/// Take a photo off a flat backdrop, dark or light, and trim it to what is left.
///
/// Most product pictures come cut out already. Some sit on near-black (the
/// Kraken Kitty V2 BT) and some on white (the BlackWidow Chroma), which show
/// as boxes on a dark window and would trace as a rectangle. The backdrop is
/// peeled off from the edges inwards, so a dark device keeps its body even
/// where it is nearly the backdrop's colour. Anything already transparent, or
/// not on one flat colour, is left as it is.
pub fn cut_out_backdrop(image: &RgbaImage) -> Option<RgbaImage> {
    let (w, h) = image.dimensions();
    if w < 3 || h < 3 {
        return None;
    }
    let backdrop = *image.get_pixel(0, 0);
    if backdrop[3] < 255 {
        return None;
    }
    // A black device on a near-black backdrop is only a few levels off it, so
    // dark backdrops get tight margins; white ones carry more JPEG noise.
    let dark = backdrop.0[..3].iter().all(|&c| c < 64);
    let (near, far): (i32, i32) = if dark { (3, 8) } else { (6, 16) };
    let distance = |p: &image::Rgba<u8>| (0..3).map(|i| (p[i] as i32 - backdrop[i] as i32).abs()).max().unwrap_or(0);
    let border: Vec<(u32, u32)> = (0..w).flat_map(|x| [(x, 0), (x, h - 1)]).chain((0..h).flat_map(|y| [(0, y), (w - 1, y)])).collect();
    let flat = border.iter().filter(|&&(x, y)| distance(image.get_pixel(x, y)) <= near + 2).count();
    if flat * 50 < border.len() * 49 {
        return None;
    }

    let mut out = image.clone();
    let mut seen = vec![false; (w * h) as usize];
    let mut queue: std::collections::VecDeque<(u32, u32)> = border.into_iter().collect();
    while let Some((x, y)) = queue.pop_front() {
        let index = (y * w + x) as usize;
        if std::mem::replace(&mut seen[index], true) {
            continue;
        }
        let d = distance(image.get_pixel(x, y));
        if d >= far {
            continue;
        }
        out.get_pixel_mut(x, y)[3] = if d <= near { 0 } else { (255 * (d - near) / (far - near)) as u8 };
        if d > near {
            continue; // an edge: faded, but the backdrop does not spread past it
        }
        for (dx, dy) in [(1i64, 0i64), (-1, 0), (0, 1), (0, -1)] {
            let (nx, ny) = (x as i64 + dx, y as i64 + dy);
            if nx >= 0 && ny >= 0 && nx < w as i64 && ny < h as i64 && !seen[(ny as u32 * w + nx as u32) as usize] {
                queue.push_back((nx as u32, ny as u32));
            }
        }
    }

    let (mut left, mut top, mut right, mut bottom) = (w, h, 0, 0);
    for (x, y, pixel) in out.enumerate_pixels() {
        if pixel[3] > 0 {
            (left, top, right, bottom) = (left.min(x), top.min(y), right.max(x), bottom.max(y));
        }
    }
    if left > right {
        return None; // nothing but backdrop
    }
    Some(image::imageops::crop_imm(&out, left, top, right - left + 1, bottom - top + 1).to_image())
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{Rgba, RgbaImage};

    fn photo(backdrop: [u8; 4], body: [u8; 4]) -> RgbaImage {
        let mut image = RgbaImage::from_pixel(60, 40, Rgba(backdrop));
        for x in 20..40 {
            for y in 10..30 {
                image.put_pixel(x, y, Rgba(body));
            }
        }
        image
    }

    #[test]
    fn dark_and_light_backdrops_are_peeled_off_and_trimmed() {
        let cut = cut_out_backdrop(&photo([17, 17, 17, 255], [40, 44, 50, 255])).expect("a flat dark backdrop");
        assert_eq!(cut.dimensions(), (20, 20));
        let cut = cut_out_backdrop(&photo([255, 255, 255, 255], [20, 20, 22, 255])).expect("a flat white backdrop");
        assert_eq!(cut.dimensions(), (20, 20));
        assert_eq!(cut.get_pixel(5, 5)[3], 255);
    }

    #[test]
    fn pictures_that_are_cut_out_or_busy_are_left_alone() {
        assert!(cut_out_backdrop(&photo([0, 0, 0, 0], [20, 20, 20, 255])).is_none());
        let mut busy = photo([17, 17, 17, 255], [40, 40, 40, 255]);
        for x in 0..60 {
            busy.put_pixel(x, 0, Rgba([200, 50, 50, 255]));
        }
        assert!(cut_out_backdrop(&busy).is_none());
    }
}
