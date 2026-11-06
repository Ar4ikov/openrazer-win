//! A stand-in for Razer's Chroma SDK DLL.
//!
//! A game lights Razer devices by loading `RzChromaSDK64.dll` (or, 32-bit,
//! `RzChromaSDK.dll`), which Synapse installs. Without Synapse there is none,
//! and the game lights nothing. This is a DLL of the same name with the same
//! functions; it passes what the game shows on to the openrazer-win daemon,
//! through the same REST API Razer's SDK serves, and the daemon draws it.
//!
//! It never makes a game wait: calls only note what is to be shown, and a
//! thread of its own sends it. With the daemon not running, frames are simply
//! dropped. Nothing here may panic -- a panic would end the game -- so there
//! is no `unwrap` outside the tests.

#![allow(non_snake_case)]

pub mod effects;
mod http;

use effects::{Kind, RzResult, INVALID_PARAMETER, NOT_FOUND, NOT_SUPPORTED, SUCCESS};
use std::collections::HashMap;
use std::ffi::c_void;
use std::sync::{Condvar, Mutex, MutexGuard, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Where Razer's SDK listens, and so the daemon too.
pub const PORT: u16 = 54235;

/// What tells this DLL from Razer's own: Studio takes back only a DLL that
/// carries it. Exported, so that no linker can leave it out.
#[no_mangle]
pub static OPENRAZER_CHROMA: [u8; 16] = *b"openrazer-chroma";

/// How often the session is said to be alive while nothing is shown: the
/// daemon lets go of a game it has not heard from for fifteen seconds.
const HEARTBEAT: Duration = Duration::from_secs(1);

/// A Windows `GUID`: how the SDK names effects and devices.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Guid {
    pub data1: u32,
    pub data2: u16,
    pub data3: u16,
    pub data4: [u8; 8],
}

/// `ChromaSDK::APPINFOTYPE`.
#[repr(C)]
pub struct AppInfo {
    pub title: [u16; 256],
    pub description: [u16; 1024],
    pub author_name: [u16; 256],
    pub author_contact: [u16; 256],
    pub supported_device: u32,
    pub category: u32,
}

/// `ChromaSDK::DEVICE_INFO_TYPE`.
#[repr(C)]
pub struct DeviceInfo {
    pub device_type: i32,
    pub connected: u32,
}

#[derive(Default)]
struct State {
    running: bool,
    /// Asked to stop: the thread ends the session and goes.
    leaving: bool,
    title: String,
    description: String,
    /// What to show next on each kind of device; only the latest counts.
    pending: Vec<(Kind, String)>,
    /// Effects made to be shown later.
    effects: HashMap<Guid, (Kind, String)>,
    made: u64,
}

struct Shared {
    state: Mutex<State>,
    wake: Condvar,
}

fn shared() -> &'static Shared {
    static SHARED: OnceLock<Shared> = OnceLock::new();
    SHARED.get_or_init(|| Shared { state: Mutex::new(State::default()), wake: Condvar::new() })
}

fn lock() -> MutexGuard<'static, State> {
    shared().state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn port() -> u16 {
    std::env::var("OPENRAZER_CHROMA_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(PORT)
}

/// The program's own name, for a game that does not give one.
fn program_name() -> String {
    std::env::current_exe().ok().and_then(|p| p.file_stem().map(|s| s.to_string_lossy().into_owned())).unwrap_or_else(|| "A game".into())
}

fn wide(text: &[u16]) -> String {
    let end = text.iter().position(|&c| c == 0).unwrap_or(text.len());
    String::from_utf16_lossy(&text[..end])
}

fn start(title: String, description: String) -> RzResult {
    let mut state = lock();
    state.title = title;
    state.description = description;
    state.leaving = false;
    if state.running {
        return SUCCESS;
    }
    state.running = true;
    drop(state);
    if std::thread::Builder::new().name("openrazer-chroma".into()).spawn(run).is_err() {
        lock().running = false;
        return effects::NOT_SUPPORTED;
    }
    SUCCESS
}

/// The thread that talks to the daemon.
fn run() {
    let mut client = http::Client::new(port());
    let mut session: Option<String> = None;
    loop {
        let (pending, leaving, title, description) = {
            let mut state = lock();
            if state.pending.is_empty() && !state.leaving {
                state = shared().wake.wait_timeout(state, HEARTBEAT).map(|(s, _)| s).unwrap_or_else(|p| p.into_inner().0);
            }
            (std::mem::take(&mut state.pending), state.leaving, state.title.clone(), state.description.clone())
        };
        if leaving {
            if let Some(path) = session.take() {
                let _ = client.send("DELETE", &path, "");
            }
            let mut state = lock();
            state.running = false;
            state.leaving = false;
            return;
        }
        if pending.is_empty() {
            if let Some(path) = &session {
                if client.send("PUT", &format!("{path}/heartbeat"), "").is_none_or(|(status, _)| status == 404) {
                    session = None;
                }
            }
            continue;
        }
        if session.is_none() {
            session = open(&mut client, &title, &description);
        }
        let Some(path) = session.clone() else { continue };
        for (kind, body) in pending {
            match client.send("PUT", &format!("{path}/{}", kind.name()), &body) {
                // The daemon was restarted, or let go: a new session next time.
                Some((404, _)) | None => session = None,
                Some(_) => {}
            }
        }
    }
}

fn open(client: &mut http::Client, title: &str, description: &str) -> Option<String> {
    let kinds: Vec<String> = Kind::ALL.iter().map(|k| effects::quoted(k.name())).collect();
    let body = format!(
        r#"{{"title":{},"description":{},"author":{{"name":"","contact":""}},"device_supported":[{}],"category":"game"}}"#,
        effects::quoted(title),
        effects::quoted(description),
        kinds.join(",")
    );
    let (status, answer) = client.send("POST", "/razer/chromasdk", &body)?;
    if status != 200 {
        return None;
    }
    let uri = http::field(&answer, "uri")?;
    // Only the path: the daemon is on the loopback, whatever host it names.
    let path = uri.splitn(4, '/').nth(3).map(|p| format!("/{p}"))?;
    Some(path)
}

fn show(kind: Kind, body: String) {
    let mut state = lock();
    state.pending.retain(|(k, _)| *k != kind);
    state.pending.push((kind, body));
    drop(state);
    shared().wake.notify_one();
}

/// An id for an effect, unlike any other in this process.
fn new_id(state: &mut State) -> Guid {
    state.made += 1;
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0);
    let mut x = nanos ^ (state.made << 32) ^ u64::from(std::process::id());
    let mut next = || {
        // splitmix64
        x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = x;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    };
    let (a, b) = (next(), next());
    Guid { data1: (a >> 32) as u32, data2: (a >> 16) as u16, data3: ((a as u16) & 0x0FFF) | 0x4000, data4: (b | (state.made << 56)).to_le_bytes() }
}

/// Show `body` now, or keep it under a new id for later.
///
/// # Safety
/// `id` must be null or point at a writable `GUID`.
unsafe fn made(kind: Kind, body: Result<String, RzResult>, id: *mut Guid) -> RzResult {
    let body = match body {
        Ok(body) => body,
        Err(code) => return code,
    };
    if id.is_null() {
        show(kind, body);
    } else {
        let mut state = lock();
        let new = new_id(&mut state);
        state.effects.insert(new, (kind, body));
        *id = new;
    }
    SUCCESS
}

// -- what games call ---------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn Init() -> RzResult {
    start(program_name(), String::new())
}

/// # Safety
/// `info` must be null or point at an `APPINFOTYPE`.
#[no_mangle]
pub unsafe extern "C" fn InitSDK(info: *const AppInfo) -> RzResult {
    match info.as_ref() {
        Some(info) => {
            let title = wide(&info.title);
            start(if title.is_empty() { program_name() } else { title }, wide(&info.description))
        }
        None => Init(),
    }
}

#[no_mangle]
pub extern "C" fn UnInit() -> RzResult {
    let mut state = lock();
    state.effects.clear();
    state.pending.clear();
    if state.running {
        state.leaving = true;
    }
    drop(state);
    shared().wake.notify_one();
    SUCCESS
}

/// # Safety
/// As for each kind's own call below.
#[no_mangle]
pub unsafe extern "C" fn CreateEffect(device: Guid, effect: i32, param: *const c_void, id: *mut Guid) -> RzResult {
    match effects::kind_of_device(device.data1) {
        Some(kind) => made(kind, effects::generic_body(kind, effect, param), id),
        None => NOT_SUPPORTED,
    }
}

/// # Safety
/// `param` must point at the structure `effect` takes, or be null where it
/// takes none; `id` must be null or point at a writable `GUID`.
#[no_mangle]
pub unsafe extern "C" fn CreateKeyboardEffect(effect: i32, param: *const c_void, id: *mut Guid) -> RzResult {
    made(Kind::Keyboard, effects::body(Kind::Keyboard, effect, param), id)
}

/// # Safety
/// As for [`CreateKeyboardEffect`].
#[no_mangle]
pub unsafe extern "C" fn CreateMouseEffect(effect: i32, param: *const c_void, id: *mut Guid) -> RzResult {
    made(Kind::Mouse, effects::body(Kind::Mouse, effect, param), id)
}

/// # Safety
/// As for [`CreateKeyboardEffect`].
#[no_mangle]
pub unsafe extern "C" fn CreateHeadsetEffect(effect: i32, param: *const c_void, id: *mut Guid) -> RzResult {
    made(Kind::Headset, effects::body(Kind::Headset, effect, param), id)
}

/// # Safety
/// As for [`CreateKeyboardEffect`].
#[no_mangle]
pub unsafe extern "C" fn CreateMousepadEffect(effect: i32, param: *const c_void, id: *mut Guid) -> RzResult {
    made(Kind::Mousepad, effects::body(Kind::Mousepad, effect, param), id)
}

/// # Safety
/// As for [`CreateKeyboardEffect`].
#[no_mangle]
pub unsafe extern "C" fn CreateKeypadEffect(effect: i32, param: *const c_void, id: *mut Guid) -> RzResult {
    made(Kind::Keypad, effects::body(Kind::Keypad, effect, param), id)
}

/// # Safety
/// As for [`CreateKeyboardEffect`].
#[no_mangle]
pub unsafe extern "C" fn CreateChromaLinkEffect(effect: i32, param: *const c_void, id: *mut Guid) -> RzResult {
    made(Kind::ChromaLink, effects::body(Kind::ChromaLink, effect, param), id)
}

#[no_mangle]
pub extern "C" fn SetEffect(id: Guid) -> RzResult {
    let found = lock().effects.get(&id).cloned();
    match found {
        Some((kind, body)) => {
            show(kind, body);
            SUCCESS
        }
        None => NOT_FOUND,
    }
}

#[no_mangle]
pub extern "C" fn DeleteEffect(id: Guid) -> RzResult {
    if lock().effects.remove(&id).is_some() {
        SUCCESS
    } else {
        NOT_FOUND
    }
}

/// Synapse posts messages to this window when, say, a device is plugged in;
/// nothing is posted here, which a game takes as nothing having happened.
#[no_mangle]
pub extern "C" fn RegisterEventNotification(_window: *mut c_void) -> RzResult {
    SUCCESS
}

#[no_mangle]
pub extern "C" fn UnregisterEventNotification() -> RzResult {
    SUCCESS
}

/// # Safety
/// `info` must be null or point at a writable `DEVICE_INFO_TYPE`.
#[no_mangle]
pub unsafe extern "C" fn QueryDevice(device: Guid, info: *mut DeviceInfo) -> RzResult {
    let Some(info) = info.as_mut() else { return INVALID_PARAMETER };
    match effects::kind_of_device(device.data1) {
        Some(kind) => {
            // Whether one is plugged in is the daemon's business: a game that
            // draws on a device that is not there costs nothing.
            info.device_type = kind.device_type();
            info.connected = 1;
            SUCCESS
        }
        None => NOT_FOUND,
    }
}
