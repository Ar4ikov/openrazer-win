//! Keys that only the daemon sees, for the lighting preview.
//!
//! Windows never hears of a keyboard's macro keys or its dial: they come on
//! Razer's own report, which the daemon reads.  While the preview is on
//! screen, a thread of its own asks the daemon for them a dozen times a
//! second (issue #19).  It does not share the worker's connection, so a slow
//! Bluetooth call there cannot hold a key press back.

use openrazer_studio_core::rpc::Client;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

/// How often the daemon is asked while the preview is showing.
const POLL: Duration = Duration::from_millis(80);
/// How long the preview counts as showing after its last frame.
const LINGER: Duration = Duration::from_millis(500);

#[derive(Default)]
struct State {
    /// Until when someone is looking.
    wanted: Option<Instant>,
    /// Presses not yet taken: (serial, name the daemon gives the key).
    queue: Vec<(String, String)>,
}

fn shared() -> &'static Arc<Mutex<State>> {
    static SHARED: OnceLock<Arc<Mutex<State>>> = OnceLock::new();
    SHARED.get_or_init(|| {
        let state = Arc::new(Mutex::new(State::default()));
        let thread = state.clone();
        std::thread::Builder::new().name("presses".into()).spawn(move || follow(thread)).ok();
        state
    })
}

/// Called each frame the preview shows: the names of keys pressed on this
/// device since the last call.
pub fn take(serial: &str) -> Vec<String> {
    let mut state = shared().lock().unwrap_or_else(|e| e.into_inner());
    state.wanted = Some(Instant::now() + LINGER);
    let (mine, rest): (Vec<_>, Vec<_>) = std::mem::take(&mut state.queue).into_iter().partition(|(s, _)| s == serial);
    state.queue = rest;
    mine.into_iter().map(|(_, name)| name).collect()
}

fn follow(state: Arc<Mutex<State>>) {
    let mut client: Option<Client> = None;
    let mut cursor: Option<u64> = None;
    loop {
        let wanted = state.lock().map(|s| s.wanted.is_some_and(|until| Instant::now() < until)).unwrap_or(false);
        if !wanted {
            // Nobody is looking: forget the place, so a press made meanwhile
            // does not light up when the preview comes back.
            cursor = None;
            std::thread::sleep(Duration::from_millis(250));
            continue;
        }
        if client.is_none() {
            client = Client::connect().ok();
        }
        let Some(c) = client.as_mut() else {
            std::thread::sleep(Duration::from_secs(1));
            continue;
        };
        let params = match cursor {
            Some(since) => json!({ "since": since }),
            None => json!({}),
        };
        match c.call("keys.presses", params) {
            Ok(answer) => {
                let fresh = parse(&answer, cursor.is_some());
                cursor = answer["cursor"].as_u64().or(cursor);
                if !fresh.is_empty() {
                    if let Ok(mut s) = state.lock() {
                        s.queue.extend(fresh);
                        s.queue.truncate(64);
                    }
                }
            }
            // An older daemon has no such call, or the daemon went away: try
            // again later rather than every 80 ms.
            Err(_) => {
                client = None;
                std::thread::sleep(Duration::from_secs(2));
            }
        }
        std::thread::sleep(POLL);
    }
}

/// The presses in one answer, or none on the first one (that only finds
/// the place to start from).
fn parse(answer: &Value, started: bool) -> Vec<(String, String)> {
    if !started {
        return Vec::new();
    }
    answer["presses"]
        .as_array()
        .map(|list| {
            list.iter()
                .filter_map(|p| Some((p["serial"].as_str()?.to_string(), p["name"].as_str()?.to_string())))
                .collect()
        })
        .unwrap_or_default()
}

/// The cap a key the daemon names is drawn as: `M1` is `M1`, `DIAL` the dial.
/// Keys Windows also sees (media keys) are left to it, so none lights twice.
pub fn cap_label(name: &str) -> Option<&'static str> {
    Some(match name {
        "M1" => "M1",
        "M2" => "M2",
        "M3" => "M3",
        "M4" => "M4",
        "M5" => "M5",
        "M6" => "M6",
        "DIAL" => "Dial",
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_first_answer_only_finds_the_place() {
        let answer = json!({ "cursor": 5, "presses": [{ "seq": 5, "serial": "A", "code": 32, "name": "M1" }] });
        assert!(parse(&answer, false).is_empty());
        assert_eq!(parse(&answer, true), vec![("A".to_string(), "M1".to_string())]);
    }

    #[test]
    fn macro_keys_and_the_dial_have_caps() {
        assert_eq!(cap_label("M3"), Some("M3"));
        assert_eq!(cap_label("DIAL"), Some("Dial"));
        assert_eq!(cap_label("KEY_MUTE"), None);
    }
}
