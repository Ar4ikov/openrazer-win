//! The DLL's calls, made the way a game makes them, against a stand-in daemon.

use rzchromasdk::*;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::mpsc;
use std::time::Duration;

/// Answer like the daemon, and report each request.
fn daemon() -> (u16, mpsc::Receiver<(String, String, String)>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { break };
            let tx = tx.clone();
            std::thread::spawn(move || {
                let mut reader = BufReader::new(stream);
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 {
                        return;
                    }
                    let mut parts = line.split_whitespace();
                    let (method, path) = (parts.next().unwrap_or("").to_string(), parts.next().unwrap_or("").to_string());
                    let mut length = 0;
                    loop {
                        let mut header = String::new();
                        reader.read_line(&mut header).unwrap();
                        if header.trim().is_empty() {
                            break;
                        }
                        if let Some(value) = header.to_ascii_lowercase().strip_prefix("content-length:") {
                            length = value.trim().parse().unwrap();
                        }
                    }
                    let mut body = vec![0; length];
                    reader.read_exact(&mut body).unwrap();
                    let answer = if path == "/razer/chromasdk" {
                        format!(r#"{{"sessionid": 7, "uri": "http://localhost:{port}/sessions/7/chromasdk"}}"#)
                    } else {
                        r#"{"result": 0}"#.to_string()
                    };
                    let reply = format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{answer}", answer.len());
                    reader.get_mut().write_all(reply.as_bytes()).unwrap();
                    tx.send((method, path, String::from_utf8(body).unwrap())).unwrap();
                }
            });
        }
    });
    (port, rx)
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().collect()
}

#[test]
fn a_game_s_session_reaches_the_daemon() {
    let (port, requests) = daemon();
    std::env::set_var("OPENRAZER_CHROMA_PORT", port.to_string());

    let mut info = AppInfo { title: [0; 256], description: [0; 1024], author_name: [0; 256], author_contact: [0; 256], supported_device: 0x3F, category: 2 };
    let title = wide("Test Game");
    info.title[..title.len()].copy_from_slice(&title);
    assert_eq!(unsafe { InitSDK(&info) }, 0);

    // Made ahead, shown by id -- the way most games do it.
    let red: u32 = 0x0000FF;
    let mut id = Guid::default();
    assert_eq!(unsafe { CreateKeyboardEffect(4, (&red as *const u32).cast(), &mut id) }, 0);
    assert_ne!(id, Guid::default());
    assert_eq!(SetEffect(id), 0);
    assert_eq!(DeleteEffect(id), 0);
    assert_eq!(SetEffect(id), 1168);

    // Shown at once, with no id asked for.
    let ears: [u32; 5] = [0x0000FF, 0xFF0000, 0, 0, 0];
    assert_eq!(unsafe { CreateHeadsetEffect(4, ears.as_ptr().cast(), std::ptr::null_mut()) }, 0);

    let mut device = DeviceInfo { device_type: 0, connected: 0 };
    let kraken = Guid { data1: 0xCD1E09A5, ..Guid::default() };
    assert_eq!(unsafe { QueryDevice(kraken, &mut device) }, 0);
    assert_eq!((device.device_type, device.connected), (3, 1));

    let mut seen = Vec::new();
    let mut take = |until: &str| {
        while let Ok(request) = requests.recv_timeout(Duration::from_secs(5)) {
            let done = request.1.ends_with(until) || request.0 == until;
            seen.push(request);
            if done {
                break;
            }
        }
    };
    take("/headset");
    // A game going away ends its session, so its devices are given back at once.
    assert_eq!(UnInit(), 0);
    take("DELETE");
    let opened = &seen[0];
    assert_eq!((opened.0.as_str(), opened.1.as_str()), ("POST", "/razer/chromasdk"));
    assert!(opened.2.contains(r#""title":"Test Game""#));
    assert!(seen.iter().any(|(m, p, b)| m == "PUT" && p == "/sessions/7/chromasdk/keyboard" && b == r#"{"effect":"CHROMA_STATIC","param":{"color":255}}"#));
    assert!(seen.iter().any(|(m, p, b)| m == "PUT" && p == "/sessions/7/chromasdk/headset" && b == r#"{"effect":"CHROMA_CUSTOM","param":[255,16711680,0,0,0]}"#));
    assert_eq!(seen.last().map(|r| (r.0.as_str(), r.1.as_str())), Some(("DELETE", "/sessions/7/chromasdk")));
}
