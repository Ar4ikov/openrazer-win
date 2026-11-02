//! Keeping Studio up to date from the project's GitHub releases.
//!
//! This stays out of the way. The first look comes a minute after start and
//! the next a day later, each carrying the ETag of the last answer so that an
//! unchanged list costs nothing, and a look that fails says nothing and tries
//! again hours later. A newer release is a quiet mark beside *Updates* in the
//! navigation; nothing pops up, nothing is downloaded and nothing is replaced
//! until the person asks.

use openrazer_studio_core::{install, paths, t, tf};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const CURRENT: &str = env!("CARGO_PKG_VERSION");

const FEED: &str = "https://api.github.com/repos/Ar4ikov/openrazer-win/releases?per_page=10";
/// Everything downloaded comes from under here.
const DOWNLOADS: &str = "https://github.com/Ar4ikov/openrazer-win/releases/download/";
/// A build made with this set looks there for releases instead, and takes
/// files from wherever that says: the whole path can then be tried without
/// publishing anything. It is decided when building, so nothing on a user's
/// machine can point a released Studio at another installer.
const TEST_FEED: Option<&str> = option_env!("OPENRAZER_STUDIO_TEST_FEED");

const SETUP_ASSET: &str = "openrazer-studio-setup.exe";
const SUMS_ASSET: &str = "SHA256SUMS.txt";

/// How long after start the first look comes: not while Windows is signing in.
const FIRST_LOOK: Duration = Duration::from_secs(60);
/// How often the thread wakes to see whether a look is due.
const WAKE: Duration = Duration::from_secs(3600);
/// Between looks.
const DAY: u64 = 86_400;
/// After a look that failed.
const RETRY: u64 = 6 * 3600;

pub fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// `1.5.0` as numbers, ignoring a leading `v` and anything after a `-`.
pub fn parse(version: &str) -> Vec<u32> {
    version.trim().trim_start_matches('v').split('-').next().unwrap_or("").split('.').map(|p| p.parse().unwrap_or(0)).collect()
}

pub fn newer(candidate: &str, current: &str) -> bool {
    parse(candidate) > parse(current)
}

/// One release, as much of it as Studio needs.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Release {
    pub version: String,
    /// Its page on GitHub.
    pub page: String,
    /// `2026-11-03`.
    pub published: String,
    /// Its text, in every language it came in: see [`crate::notes`].
    pub notes: String,
    /// `None` while the release is still being uploaded.
    pub installer: Option<Installer>,
    /// The URL of the checksums published with it.
    pub sums: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Installer {
    pub url: String,
    pub size: u64,
    /// The SHA-256 GitHub noted when the file was uploaded.
    pub sha256: Option<String>,
}

fn trusted(url: &str) -> bool {
    TEST_FEED.is_some() || url.starts_with(DOWNLOADS)
}

fn read_release(release: &Value) -> Option<Release> {
    let version = release["tag_name"].as_str()?.trim().trim_start_matches('v');
    // It ends up in a file name.
    if !version.starts_with(|c: char| c.is_ascii_digit()) || !version.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-')) {
        return None;
    }
    let assets = release["assets"].as_array()?;
    let url = |asset: &Value| asset["browser_download_url"].as_str().filter(|url| trusted(url)).map(str::to_string);
    let installer = assets.iter().find(|a| a["name"] == SETUP_ASSET).and_then(|asset| {
        let sha256 = asset["digest"].as_str().and_then(|d| d.strip_prefix("sha256:")).map(str::to_ascii_lowercase);
        Some(Installer { url: url(asset)?, size: asset["size"].as_u64()?, sha256 })
    });
    Some(Release {
        version: version.to_string(),
        page: release["html_url"].as_str().unwrap_or(install::RELEASES).to_string(),
        published: release["published_at"].as_str().unwrap_or_default().chars().take(10).collect(),
        notes: release["body"].as_str().unwrap_or_default().to_string(),
        installer,
        sums: assets.iter().find(|a| a["name"] == SUMS_ASSET).and_then(url),
    })
}

/// The published releases in GitHub's answer, newest first.
fn read_feed(feed: &Value) -> Vec<Release> {
    let mut releases: Vec<Release> = feed.as_array().into_iter().flatten().filter(|r| r["draft"] != true && r["prerelease"] != true).filter_map(read_release).collect();
    releases.sort_by_key(|r| std::cmp::Reverse(parse(&r.version)));
    releases
}

/// The line for `name` in a `sha256sum` listing.
fn from_sums(text: &str, name: &str) -> Option<String> {
    text.lines().find_map(|line| {
        let (hash, file) = line.trim().split_once(char::is_whitespace)?;
        let good = hash.len() == 64 && hash.chars().all(|c| c.is_ascii_hexdigit());
        (good && file.trim().trim_start_matches('*') == name).then(|| hash.to_ascii_lowercase())
    })
}

/// What the installer must hash to, from the checksums the release workflow
/// wrote and the note GitHub kept. Either will do; if both exist they must agree.
fn expected_hash(listed: Option<String>, noted: Option<String>) -> Result<String, String> {
    match (listed, noted) {
        (Some(listed), Some(noted)) if listed != noted => Err(t("The checksums published with this release disagree, so it will not be installed from here.").to_string()),
        (Some(hash), _) | (None, Some(hash)) => Ok(hash),
        (None, None) => Err(t("This release lists no checksum for its installer, so it will not be installed from here.").to_string()),
    }
}

fn sha256(path: &Path) -> std::io::Result<String> {
    let mut context = ring::digest::Context::new(&ring::digest::SHA256);
    let mut file = File::open(path)?;
    let mut buffer = vec![0; 64 * 1024];
    loop {
        match file.read(&mut buffer)? {
            0 => break,
            read => context.update(&buffer[..read]),
        }
    }
    Ok(context.finish().as_ref().iter().map(|byte| format!("{byte:02x}")).collect())
}

fn setup_path(version: &str) -> PathBuf {
    paths::updates_dir().join(format!("openrazer-studio-setup-{version}.exe"))
}

/// An installer in the updates folder for a version this copy already is, or has passed.
fn spent(file_name: &str) -> bool {
    let version = file_name.trim_start_matches("openrazer-studio-setup-").trim_end_matches(".exe").trim_end_matches(".partial");
    !newer(version, CURRENT)
}

// -- what is remembered between runs ------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
struct Ready {
    version: String,
    sha256: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
struct Saved {
    /// Of the last answer, so an unchanged list is one short reply.
    etag: String,
    releases: Vec<Release>,
    /// When GitHub last answered.
    checked: u64,
    /// When to look next.
    next: u64,
    /// The version the person said to leave alone.
    skipped: String,
    /// The installer downloaded and checked, waiting to be run.
    ready: Option<Ready>,
    /// The version the installer was started for, to say so if it worked.
    updating_to: String,
}

impl Saved {
    fn path() -> PathBuf {
        paths::studio_dir().join("updates.json")
    }

    fn load() -> Saved {
        std::fs::read_to_string(Self::path()).ok().and_then(|text| serde_json::from_str(&text).ok()).unwrap_or_default()
    }

    fn save(&self) {
        if let Ok(text) = serde_json::to_string_pretty(self) {
            let _ = paths::write_atomic(&Self::path(), text.as_bytes());
        }
    }

    /// Whether to look now. A date far ahead means the clock was wrong when it was set.
    fn due(&self, now: u64) -> bool {
        now >= self.next || self.next > now + 2 * DAY
    }
}

// -- what the window sees -----------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
pub struct Progress {
    pub done: u64,
    pub total: u64,
    pub per_second: f64,
}

#[derive(Clone, Debug, PartialEq, Default)]
pub enum Phase {
    #[default]
    Idle,
    /// Only when someone asked: a look of its own accord shows nothing.
    Checking,
    Downloading(Progress),
    /// The installer is here and checked.
    Ready,
    /// The installer has been started, and is about to close this program.
    Installing,
}

#[derive(Clone, Debug, Default)]
pub struct Status {
    pub phase: Phase,
    /// What went wrong with the last thing asked for.
    pub error: Option<String>,
    pub releases: Arc<Vec<Release>>,
    pub checked: u64,
    pub skipped: String,
    /// The install folder, when this is the installed copy and so can update itself.
    pub install_dir: Option<PathBuf>,
}

impl Status {
    /// The releases this copy could update to, newest first.
    pub fn pending(&self) -> impl Iterator<Item = &Release> {
        self.releases.iter().filter(|r| r.installer.is_some() && newer(&r.version, CURRENT))
    }

    pub fn newest(&self) -> Option<&Release> {
        self.pending().next()
    }

    /// Whether the navigation should mention it.
    pub fn worth_a_mention(&self) -> bool {
        self.newest().is_some_and(|r| r.version != self.skipped)
    }
}

enum Order {
    Check,
    Download,
    Install(PathBuf),
    Skip(String),
}

/// The window's handle on the thread that does all this.
pub struct Updater {
    status: Arc<Mutex<Status>>,
    orders: Sender<Order>,
    enabled: Arc<AtomicBool>,
    cancel: Arc<AtomicBool>,
    just_updated: Mutex<Option<String>>,
}

impl Updater {
    pub fn start(ctx: egui::Context, enabled: bool) -> Updater {
        let mut saved = Saved::load();
        let just_updated = (saved.updating_to == CURRENT).then(|| CURRENT.to_string());
        if !saved.updating_to.is_empty() {
            saved.updating_to.clear();
            saved.save();
        }
        let status = Arc::new(Mutex::new(Status { install_dir: install::running_installed().map(|i| i.dir), ..Default::default() }));
        let (orders, inbox) = mpsc::channel();
        let enabled = Arc::new(AtomicBool::new(enabled));
        let cancel = Arc::new(AtomicBool::new(false));
        let mut worker = Worker { saved, status: status.clone(), enabled: enabled.clone(), cancel: cancel.clone(), ctx };
        worker.publish();
        std::thread::Builder::new().name("updates".into()).spawn(move || worker.run(inbox)).ok();
        Updater { status, orders, enabled, cancel, just_updated: Mutex::new(just_updated) }
    }

    pub fn status(&self) -> Status {
        self.status.lock().unwrap().clone()
    }

    pub fn set_enabled(&self, enabled: bool) {
        self.enabled.store(enabled, Ordering::Relaxed);
    }

    pub fn check(&self) {
        self.status.lock().unwrap().phase = Phase::Checking;
        let _ = self.orders.send(Order::Check);
    }

    pub fn download(&self) {
        self.cancel.store(false, Ordering::Relaxed);
        let _ = self.orders.send(Order::Download);
    }

    /// Stop the download; what has arrived stays, for carrying on later.
    pub fn cancel(&self) {
        self.cancel.store(true, Ordering::Relaxed);
    }

    pub fn install(&self, dir: PathBuf) {
        self.status.lock().unwrap().phase = Phase::Installing;
        let _ = self.orders.send(Order::Install(dir));
    }

    /// Stop mentioning `version`.
    pub fn skip(&self, version: &str) {
        self.status.lock().unwrap().skipped = version.to_string();
        let _ = self.orders.send(Order::Skip(version.to_string()));
    }

    /// The version this run was just updated to, once.
    pub fn just_updated(&self) -> Option<String> {
        self.just_updated.lock().unwrap().take()
    }

    /// For the screenshot hook: look as if `state` were happening.
    pub fn pretend(&self, state: &str) {
        let mut status = self.status.lock().unwrap();
        status.install_dir.get_or_insert_with(|| PathBuf::from("."));
        match state {
            "downloading" => status.phase = Phase::Downloading(Progress { done: 14_200_000, total: 23_520_256, per_second: 3_100_000.0 }),
            "ready" => status.phase = Phase::Ready,
            "installing" => status.phase = Phase::Installing,
            "failed" => status.error = Some(tf("Could not reach GitHub: {}", &[&"connection timed out"])),
            _ => {}
        }
    }
}

// -- the thread ---------------------------------------------------------------------------

enum Failure {
    /// GitHub says to wait, until then.
    Limited(u64),
    Other(String),
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .user_agent(concat!("openrazer-studio/", env!("CARGO_PKG_VERSION")))
        .timeout_connect(Duration::from_secs(15))
        .timeout_read(Duration::from_secs(30))
        .try_proxy_from_env(true)
        .build()
}

/// The releases, unless they are the ones the ETag stands for.
fn fetch(etag: &str) -> Result<Option<(String, Vec<Release>)>, Failure> {
    let mut request = agent().get(TEST_FEED.unwrap_or(FEED)).set("Accept", "application/vnd.github+json").set("X-GitHub-Api-Version", "2022-11-28");
    if !etag.is_empty() {
        request = request.set("If-None-Match", etag);
    }
    let response = match request.call() {
        Ok(response) => response,
        Err(ureq::Error::Status(403 | 429, response)) => {
            let after = response.header("retry-after").and_then(|s| s.parse::<u64>().ok()).map(|seconds| now() + seconds);
            let reset = response.header("x-ratelimit-reset").and_then(|s| s.parse::<u64>().ok());
            return Err(Failure::Limited(after.or(reset).unwrap_or(0)));
        }
        Err(error) => return Err(Failure::Other(tf("Could not reach GitHub: {}", &[&error]))),
    };
    if response.status() == 304 {
        return Ok(None);
    }
    let etag = response.header("etag").unwrap_or_default().to_string();
    let text = response.into_string().map_err(|e| Failure::Other(tf("Could not reach GitHub: {}", &[&e])))?;
    let feed = serde_json::from_str(&text).map_err(|e| Failure::Other(tf("Could not reach GitHub: {}", &[&e])))?;
    Ok(Some((etag, read_feed(&feed))))
}

struct Worker {
    saved: Saved,
    status: Arc<Mutex<Status>>,
    enabled: Arc<AtomicBool>,
    cancel: Arc<AtomicBool>,
    ctx: egui::Context,
}

impl Worker {
    fn run(mut self, inbox: Receiver<Order>) {
        let started = Instant::now();
        let mut tidied = false;
        loop {
            let wait = if tidied { WAKE } else { FIRST_LOOK.saturating_sub(started.elapsed()) };
            match inbox.recv_timeout(wait) {
                Ok(Order::Check) => self.look(true),
                Ok(Order::Download) => self.download(),
                Ok(Order::Install(dir)) => self.install(&dir),
                Ok(Order::Skip(version)) => {
                    self.saved.skipped = version;
                    self.saved.save();
                }
                Err(RecvTimeoutError::Timeout) => {
                    if !tidied {
                        self.tidy();
                        tidied = true;
                    }
                    if self.enabled.load(Ordering::Relaxed) && self.saved.due(now()) {
                        self.look(false);
                    }
                }
                Err(RecvTimeoutError::Disconnected) => return,
            }
        }
    }

    /// The phase that follows from what is on disk, with nothing under way.
    fn resting_phase(&self) -> Phase {
        let newest = self.saved.releases.iter().find(|r| r.installer.is_some() && newer(&r.version, CURRENT));
        let waiting = self.saved.ready.as_ref().filter(|ready| newest.is_some_and(|r| r.version == ready.version) && setup_path(&ready.version).exists());
        if waiting.is_some() {
            Phase::Ready
        } else {
            Phase::Idle
        }
    }

    /// Show the window what is true now.
    fn publish(&mut self) {
        let phase = self.resting_phase();
        let mut status = self.status.lock().unwrap();
        status.releases = Arc::new(self.saved.releases.clone());
        status.checked = self.saved.checked;
        status.skipped = self.saved.skipped.clone();
        status.phase = phase;
        drop(status);
        self.ctx.request_repaint();
    }

    fn fail(&mut self, message: String) {
        self.publish();
        self.status.lock().unwrap().error = Some(message);
        self.ctx.request_repaint();
    }

    /// Take away installers for versions already reached. Not at start: the
    /// one that just ran this copy may still be closing.
    fn tidy(&self) {
        for entry in std::fs::read_dir(paths::updates_dir()).into_iter().flatten().flatten() {
            if spent(&entry.file_name().to_string_lossy()) {
                let _ = std::fs::remove_file(entry.path());
            }
        }
    }

    fn look(&mut self, asked: bool) {
        // With nothing kept, the list has to come in full whatever the ETag.
        let etag = if self.saved.releases.is_empty() { String::new() } else { self.saved.etag.clone() };
        let answer = fetch(&etag);
        let time = now();
        let mut trouble = None;
        match answer {
            Ok(fresh) => {
                if let Some((etag, releases)) = fresh {
                    self.saved.etag = etag;
                    self.saved.releases = releases;
                }
                self.saved.checked = time;
                self.saved.next = time + DAY;
            }
            Err(Failure::Limited(until)) => {
                self.saved.next = until.max(time + RETRY);
                trouble = Some(t("GitHub has had too many requests from this address. Try again later.").to_string());
            }
            Err(Failure::Other(message)) => {
                self.saved.next = time + RETRY;
                trouble = Some(message);
            }
        }
        self.saved.save();
        match trouble.filter(|_| asked) {
            Some(message) => self.fail(message),
            None => {
                self.publish();
                if asked {
                    self.status.lock().unwrap().error = None;
                }
            }
        }
    }

    fn set_progress(&self, progress: Progress) {
        self.status.lock().unwrap().phase = Phase::Downloading(progress);
        self.ctx.request_repaint();
    }

    fn download(&mut self) {
        let Some(release) = self.saved.releases.iter().find(|r| r.installer.is_some() && newer(&r.version, CURRENT)).cloned() else { return };
        self.status.lock().unwrap().error = None;
        // Going for it says the person has changed their mind about skipping it.
        if self.saved.skipped == release.version {
            self.saved.skipped.clear();
            self.status.lock().unwrap().skipped.clear();
        }
        self.set_progress(Progress { done: 0, total: release.installer.as_ref().map_or(0, |i| i.size), per_second: 0.0 });
        match self.fetch_installer(&release) {
            Ok(Some(sha256)) => {
                self.saved.ready = Some(Ready { version: release.version, sha256 });
                self.saved.save();
                self.publish();
            }
            Ok(None) => self.publish(),
            Err(message) => self.fail(message),
        }
    }

    /// Download and check the installer. Its SHA-256 when it is here, `None` if stopped.
    fn fetch_installer(&self, release: &Release) -> Result<Option<String>, String> {
        let installer = release.installer.as_ref().ok_or_else(|| t("This release has no installer yet.").to_string())?;
        let agent = agent();
        let listed = release.sums.as_ref().and_then(|url| agent.get(url).call().ok()).and_then(|r| r.into_string().ok()).and_then(|text| from_sums(&text, SETUP_ASSET));
        let wanted = expected_hash(listed, installer.sha256.clone())?;

        let cannot_write = |e: std::io::Error| tf("Could not save the download: {}", &[&e]);
        std::fs::create_dir_all(paths::updates_dir()).map_err(cannot_write)?;
        let file = setup_path(&release.version);
        let partial = file.with_extension("partial");
        // Carry on from what an earlier try left, if the server is willing.
        let mut done = std::fs::metadata(&partial).map(|m| m.len()).unwrap_or(0);
        if done >= installer.size {
            done = 0;
        }
        let mut request = agent.get(&installer.url);
        if done > 0 {
            request = request.set("Range", &format!("bytes={done}-"));
        }
        let response = request.call().map_err(|e| tf("Could not download the update: {}", &[&e]))?;
        let resumed = response.status() == 206;
        if !resumed {
            done = 0;
        }
        let mut output = OpenOptions::new().create(true).write(true).append(resumed).truncate(!resumed).open(&partial).map_err(cannot_write)?;
        let mut reader = response.into_reader();
        let mut buffer = vec![0; 64 * 1024];
        let (mut mark, mut mark_done, mut per_second) = (Instant::now(), done, 0.0);
        loop {
            if self.cancel.swap(false, Ordering::Relaxed) {
                return Ok(None);
            }
            let read = reader.read(&mut buffer).map_err(|_| t("The download stopped. Press Download to carry on from there.").to_string())?;
            if read == 0 {
                break;
            }
            output.write_all(&buffer[..read]).map_err(cannot_write)?;
            done += read as u64;
            if mark.elapsed() >= Duration::from_millis(200) {
                let instant = (done - mark_done) as f64 / mark.elapsed().as_secs_f64();
                per_second = if per_second == 0.0 { instant } else { per_second * 0.7 + instant * 0.3 };
                (mark, mark_done) = (Instant::now(), done);
                self.set_progress(Progress { done, total: installer.size, per_second });
            }
        }
        output.sync_all().map_err(cannot_write)?;
        drop(output);
        if done < installer.size {
            return Err(t("The download stopped. Press Download to carry on from there.").to_string());
        }
        let sha256 = sha256(&partial).map_err(cannot_write)?;
        if sha256 != wanted {
            let _ = std::fs::remove_file(&partial);
            return Err(t("The download does not match the checksum published with the release, so it was thrown away. Nothing was changed.").to_string());
        }
        std::fs::rename(&partial, &file).map_err(cannot_write)?;
        // Only the newest is worth keeping.
        for entry in std::fs::read_dir(paths::updates_dir()).into_iter().flatten().flatten() {
            if entry.path() != file {
                let _ = std::fs::remove_file(entry.path());
            }
        }
        Ok(Some(sha256))
    }

    /// Start the installer. It closes this program itself, once it is running
    /// and has something to show, so a refusal leaves everything as it was.
    fn install(&mut self, dir: &Path) {
        self.status.lock().unwrap().error = None;
        let Some(ready) = self.saved.ready.clone() else { return self.publish() };
        let file = setup_path(&ready.version);
        // It may have sat in a folder of the user's for days.
        if !sha256(&file).is_ok_and(|hash| hash == ready.sha256) {
            let _ = std::fs::remove_file(&file);
            self.saved.ready = None;
            self.saved.save();
            return self.fail(t("The downloaded installer has changed since it was checked, so it was thrown away. Download it again.").to_string());
        }
        self.saved.updating_to = ready.version;
        self.saved.save();
        match std::process::Command::new(&file).arg("--update").arg("--dir").arg(dir).spawn() {
            Ok(mut child) => {
                let _ = child.wait();
                // Still here, so it stopped before getting as far as closing us.
                self.saved.updating_to.clear();
                self.saved.save();
                self.fail(t("The installer closed before it finished. Nothing was changed.").to_string());
            }
            Err(error) => {
                self.saved.updating_to.clear();
                self.saved.save();
                self.fail(tf("Could not start {}: {}", &[&file.display(), &error]));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn versions_compare_as_numbers() {
        assert!(newer("v1.10.0", "1.9.3"));
        assert!(newer("1.5.1", "1.5.0"));
        assert!(!newer("v1.5.0", "1.5.0"));
        assert!(!newer("1.4.9", "1.5.0"));
        assert!(!newer("garbage", "1.5.0"));
    }

    fn release(tag: &str) -> Value {
        json!({
            "tag_name": tag,
            "html_url": format!("https://github.com/Ar4ikov/openrazer-win/releases/tag/{tag}"),
            "published_at": "2026-11-03T01:02:03Z",
            "body": "notes",
            "assets": [
                {"name": SETUP_ASSET, "size": 100, "digest": "sha256:ABC", "browser_download_url": format!("{DOWNLOADS}{tag}/{SETUP_ASSET}")},
                {"name": SUMS_ASSET, "size": 10, "browser_download_url": format!("{DOWNLOADS}{tag}/{SUMS_ASSET}")},
            ],
        })
    }

    #[test]
    fn the_feed_gives_published_releases_newest_first() {
        let mut draft = release("v9.0.0");
        draft["draft"] = json!(true);
        let mut prerelease = release("v8.0.0");
        prerelease["prerelease"] = json!(true);
        let feed = json!([release("v1.9.0"), release("v1.10.0"), draft, prerelease, release("nightly")]);
        let releases = read_feed(&feed);
        assert_eq!(releases.iter().map(|r| r.version.as_str()).collect::<Vec<_>>(), ["1.10.0", "1.9.0"]);
        assert_eq!(releases[0].published, "2026-11-03");
        assert_eq!(releases[0].installer.as_ref().unwrap().sha256.as_deref(), Some("abc"));
        assert!(releases[0].sums.as_ref().unwrap().ends_with(SUMS_ASSET));
    }

    #[test]
    fn a_tag_cannot_climb_out_of_the_updates_folder() {
        assert!(read_release(&release("v1.5.4")).is_some());
        assert!(read_release(&release("v1.5.4/../../evil")).is_none());
    }

    #[test]
    fn an_installer_from_anywhere_else_is_ignored() {
        let mut elsewhere = release("v1.5.4");
        elsewhere["assets"][0]["browser_download_url"] = json!("https://example.com/openrazer-studio-setup.exe");
        assert!(read_release(&elsewhere).unwrap().installer.is_none());
    }

    #[test]
    fn a_release_still_uploading_is_not_offered() {
        let mut uploading = release("v99.0.0");
        uploading["assets"] = json!([]);
        let status = Status { releases: Arc::new(read_feed(&json!([uploading]))), ..Default::default() };
        assert!(status.newest().is_none());
    }

    #[test]
    fn only_a_version_not_skipped_is_worth_a_mention() {
        let releases = Arc::new(read_feed(&json!([release("v99.0.1"), release("v99.0.0")])));
        let mut status = Status { releases, ..Default::default() };
        assert!(status.worth_a_mention());
        status.skipped = "99.0.1".into();
        assert!(!status.worth_a_mention());
        // A later one is mentioned again.
        status.skipped = "99.0.0".into();
        assert!(status.worth_a_mention());
    }

    #[test]
    fn checksums_are_read_from_a_sha256sum_listing() {
        let hash = "a".repeat(64);
        let listing = format!("{} *other.exe\n{hash}  {SETUP_ASSET}\n", "b".repeat(64));
        assert_eq!(from_sums(&listing, SETUP_ASSET), Some(hash));
        assert_eq!(from_sums("nonsense  openrazer-studio-setup.exe", SETUP_ASSET), None);
    }

    #[test]
    fn a_checksum_is_required_and_the_two_must_agree() {
        assert_eq!(expected_hash(Some("a".into()), Some("a".into())), Ok("a".to_string()));
        assert_eq!(expected_hash(Some("a".into()), None), Ok("a".to_string()));
        assert_eq!(expected_hash(None, Some("b".into())), Ok("b".to_string()));
        assert!(expected_hash(Some("a".into()), Some("b".into())).is_err());
        assert!(expected_hash(None, None).is_err());
    }

    #[test]
    fn sha256_of_a_known_text() {
        let file = std::env::temp_dir().join(format!("openrazer-sha-test-{}", std::process::id()));
        std::fs::write(&file, "abc").unwrap();
        assert_eq!(sha256(&file).unwrap(), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        std::fs::remove_file(&file).unwrap();
    }

    #[test]
    fn a_look_is_due_when_its_time_comes_or_the_clock_was_wrong() {
        let saved = Saved { next: 1_000_000, ..Default::default() };
        assert!(!saved.due(999_999));
        assert!(saved.due(1_000_000));
        assert!(saved.due(1_000_000 - 3 * DAY));
    }

    #[test]
    fn installers_for_versions_reached_are_spent() {
        assert!(spent(&format!("openrazer-studio-setup-{CURRENT}.exe")));
        assert!(spent("openrazer-studio-setup-1.0.0.partial"));
        assert!(!spent("openrazer-studio-setup-99.0.0.exe"));
    }
}
