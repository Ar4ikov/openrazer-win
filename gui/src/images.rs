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
    let image = image::load_from_memory(bytes).ok()?;
    let image = image.thumbnail(320, 320).to_rgba8();
    let size = [image.width() as usize, image.height() as usize];
    Some(ColorImage::from_rgba_unmultiplied(size, image.as_raw()))
}
