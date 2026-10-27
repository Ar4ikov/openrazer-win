//! Asking GitHub whether a newer release is out.

use std::sync::{Arc, Mutex};

const LATEST: &str = "https://api.github.com/repos/Ar4ikov/openrazer-win/releases/latest";

#[derive(Clone, Debug)]
pub struct Update {
    pub version: String,
    pub url: String,
}

pub type Found = Arc<Mutex<Option<Update>>>;

/// `1.5.0` as numbers, ignoring a leading `v` and anything after a `-`.
pub fn parse(version: &str) -> Vec<u32> {
    version.trim().trim_start_matches('v').split('-').next().unwrap_or("").split('.').map(|p| p.parse().unwrap_or(0)).collect()
}

pub fn newer(candidate: &str, current: &str) -> bool {
    parse(candidate) > parse(current)
}

/// Look once, in the background; `found` is filled in if there is something newer.
pub fn check(found: Found, ctx: egui::Context) {
    std::thread::Builder::new()
        .name("updates".into())
        .spawn(move || {
            let Ok(response) = ureq::get(LATEST)
                .set("User-Agent", concat!("openrazer-studio/", env!("CARGO_PKG_VERSION")))
                .set("Accept", "application/vnd.github+json")
                .timeout(std::time::Duration::from_secs(10))
                .call()
            else {
                return;
            };
            let Ok(release) = response.into_json::<serde_json::Value>() else { return };
            let tag = release["tag_name"].as_str().unwrap_or_default();
            if newer(tag, env!("CARGO_PKG_VERSION")) {
                let url = release["html_url"].as_str().unwrap_or(openrazer_studio_core::install::RELEASES).to_string();
                *found.lock().unwrap() = Some(Update { version: tag.trim_start_matches('v').to_string(), url });
                ctx.request_repaint();
            }
        })
        .ok();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_compare_as_numbers() {
        assert!(newer("v1.10.0", "1.9.3"));
        assert!(newer("1.5.1", "1.5.0"));
        assert!(!newer("v1.5.0", "1.5.0"));
        assert!(!newer("1.4.9", "1.5.0"));
        assert!(!newer("garbage", "1.5.0"));
    }
}
