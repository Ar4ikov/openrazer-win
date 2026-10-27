//! Product pictures, fetched once from Razer's site and kept on disk.

use egui::{ColorImage, TextureHandle, TextureOptions};
use std::collections::HashMap;
use std::io::Read;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

type Ready = Arc<Mutex<Vec<(String, Option<ColorImage>)>>>;

#[derive(Default)]
pub struct Images {
    textures: HashMap<String, Option<TextureHandle>>,
    ready: Ready,
}

impl Images {
    /// The picture at `url`, once it has arrived; `None` until then, or if it never will.
    pub fn get(&mut self, ctx: &egui::Context, url: &str) -> Option<TextureHandle> {
        let arrived: Vec<_> = std::mem::take(&mut *self.ready.lock().unwrap());
        for (key, image) in arrived {
            let texture = image.map(|image| ctx.load_texture(key.clone(), image, TextureOptions::LINEAR));
            self.textures.insert(key, texture);
        }
        if let Some(entry) = self.textures.get(url) {
            return entry.clone();
        }
        // Reserve the slot so it is only fetched once; the thread fills it in.
        self.textures.insert(url.to_string(), None);
        let ready = self.ready.clone();
        let url = url.to_string();
        let ctx = ctx.clone();
        std::thread::Builder::new()
            .name("image".into())
            .spawn(move || {
                let image = fetch(&url).and_then(|bytes| decode(&bytes));
                ready.lock().unwrap().push((url, image));
                ctx.request_repaint();
            })
            .ok();
        None
    }
}

fn cache_path(url: &str) -> PathBuf {
    // FNV-1a: stable across runs, which the std hasher is not.
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in url.bytes() {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    crate::rpc::config_dir().join("images").join(format!("{hash:016x}.img"))
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

fn decode(bytes: &[u8]) -> Option<ColorImage> {
    let image = image::load_from_memory(bytes).ok()?.thumbnail(480, 480).to_rgba8();
    let image = cut_out_backdrop(&image).unwrap_or(image);
    let image = image::DynamicImage::ImageRgba8(image).thumbnail(320, 320).to_rgba8();
    let size = [image.width() as usize, image.height() as usize];
    Some(ColorImage::from_rgba_unmultiplied(size, image.as_raw()))
}

/// Take a photo off its flat dark backdrop, and trim it to what is left.
///
/// Most product pictures come cut out already. Razer's photos of the Kraken
/// Kitty V2 BT sit on near-black instead, which showed as a dark box in every
/// tile. The backdrop is peeled off from the edges inwards, so a dark device
/// keeps its body even where it is nearly the backdrop's colour; the margins
/// are tight because a black headset's outline is only a few levels lighter.
/// Anything already transparent, or not on one flat dark colour, is left as it is.
fn cut_out_backdrop(image: &image::RgbaImage) -> Option<image::RgbaImage> {
    const NEAR: i32 = 4;
    const FAR: i32 = 10;
    let (w, h) = image.dimensions();
    if w < 3 || h < 3 {
        return None;
    }
    let backdrop = *image.get_pixel(0, 0);
    if backdrop[3] < 255 || backdrop.0[..3].iter().any(|&c| c > 48) {
        return None;
    }
    let distance = |p: &image::Rgba<u8>| (0..3).map(|i| (p[i] as i32 - backdrop[i] as i32).abs()).max().unwrap_or(0);
    let border: Vec<(u32, u32)> = (0..w).flat_map(|x| [(x, 0), (x, h - 1)]).chain((0..h).flat_map(|y| [(0, y), (w - 1, y)])).collect();
    let flat = border.iter().filter(|&&(x, y)| distance(image.get_pixel(x, y)) <= NEAR + 2).count();
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
        if d >= FAR {
            continue;
        }
        out.get_pixel_mut(x, y)[3] = if d <= NEAR { 0 } else { (255 * (d - NEAR) / (FAR - NEAR)) as u8 };
        if d > NEAR {
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

    fn photo(backdrop: [u8; 4]) -> RgbaImage {
        let mut image = RgbaImage::from_pixel(60, 40, Rgba(backdrop));
        for x in 20..40 {
            for y in 10..30 {
                // A dark device, only a few levels off the backdrop at its rim.
                let rim = x == 20 || x == 39 || y == 10 || y == 29;
                image.put_pixel(x, y, if rim { Rgba([31, 33, 40, 255]) } else { Rgba([22, 22, 24, 255]) });
            }
        }
        image
    }

    #[test]
    fn a_dark_backdrop_is_peeled_off_and_trimmed() {
        let cut = cut_out_backdrop(&photo([17, 17, 17, 255])).expect("a flat dark backdrop");
        assert_eq!(cut.dimensions(), (20, 20));
        // The device's own body is as dark as the backdrop's noise, yet kept.
        assert_eq!(cut.get_pixel(10, 10)[3], 255);
        assert_eq!(cut.get_pixel(0, 0)[3], 255);
    }

    #[test]
    fn pictures_that_are_cut_out_or_on_white_are_left_alone() {
        assert!(cut_out_backdrop(&photo([0, 0, 0, 0])).is_none());
        assert!(cut_out_backdrop(&photo([255, 255, 255, 255])).is_none());
        let mut busy = photo([17, 17, 17, 255]);
        for x in 0..60 {
            busy.put_pixel(x, 0, Rgba([200, 50, 50, 255]));
        }
        assert!(cut_out_backdrop(&busy).is_none());
    }
}
