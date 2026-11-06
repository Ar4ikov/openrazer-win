//! Just enough HTTP to talk to the daemon's Chroma REST API on the loopback.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::{Duration, Instant};

/// How long to wait for the daemon before giving a frame up.
const TIMEOUT: Duration = Duration::from_millis(800);

/// After failing to reach the daemon, how long before trying again: a game
/// sends frames many times a second, and each would otherwise try.
const RETRY: Duration = Duration::from_secs(2);

pub struct Client {
    address: SocketAddr,
    stream: Option<BufReader<TcpStream>>,
    failed_at: Option<Instant>,
}

impl Client {
    pub fn new(port: u16) -> Client {
        Client { address: SocketAddr::from(([127, 0, 0, 1], port)), stream: None, failed_at: None }
    }

    /// Send a request and return the status and the body of the answer.
    pub fn send(&mut self, method: &str, path: &str, body: &str) -> Option<(u16, String)> {
        if self.failed_at.is_some_and(|at| at.elapsed() < RETRY) {
            return None;
        }
        // A kept connection the daemon has since closed fails at once, so one
        // more try on a fresh one.
        for _ in 0..2 {
            if let Some(answer) = self.try_send(method, path, body) {
                self.failed_at = None;
                return Some(answer);
            }
            self.stream = None;
        }
        self.failed_at = Some(Instant::now());
        None
    }

    fn try_send(&mut self, method: &str, path: &str, body: &str) -> Option<(u16, String)> {
        if self.stream.is_none() {
            let stream = TcpStream::connect_timeout(&self.address, TIMEOUT).ok()?;
            stream.set_read_timeout(Some(TIMEOUT)).ok()?;
            stream.set_write_timeout(Some(TIMEOUT)).ok()?;
            let _ = stream.set_nodelay(true);
            self.stream = Some(BufReader::new(stream));
        }
        let reader = self.stream.as_mut()?;
        let request = format!(
            "{method} {path} HTTP/1.1\r\nHost: localhost:{}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n{body}",
            self.address.port(),
            body.len()
        );
        reader.get_mut().write_all(request.as_bytes()).ok()?;

        let mut line = String::new();
        reader.read_line(&mut line).ok()?;
        let status: u16 = line.split_whitespace().nth(1)?.parse().ok()?;
        let mut length = 0usize;
        let mut close = false;
        loop {
            line.clear();
            if reader.read_line(&mut line).ok()? == 0 {
                return None;
            }
            let header = line.trim_end();
            if header.is_empty() {
                break;
            }
            if let Some((name, value)) = header.split_once(':') {
                match name.trim().to_ascii_lowercase().as_str() {
                    "content-length" => length = value.trim().parse().ok()?,
                    "connection" => close = value.trim().eq_ignore_ascii_case("close"),
                    _ => {}
                }
            }
        }
        let mut data = vec![0u8; length.min(1 << 20)];
        reader.read_exact(&mut data).ok()?;
        if close {
            self.stream = None;
        }
        Some((status, String::from_utf8_lossy(&data).into_owned()))
    }
}

/// The string value of `key` in a flat JSON answer.
pub fn field(json: &str, key: &str) -> Option<String> {
    let start = json.find(&format!("\"{key}\""))? + key.len() + 2;
    let rest = json[start..].trim_start().strip_prefix(':')?.trim_start();
    if let Some(quoted) = rest.strip_prefix('"') {
        Some(quoted[..quoted.find('"')?].to_string())
    } else {
        let end = rest.find([',', '}']).unwrap_or(rest.len());
        Some(rest[..end].trim().to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fields_are_read_out_of_an_answer() {
        let answer = r#"{"sessionid": 3, "uri": "http://localhost:54235/sessions/3/chromasdk"}"#;
        assert_eq!(field(answer, "uri").as_deref(), Some("http://localhost:54235/sessions/3/chromasdk"));
        assert_eq!(field(answer, "sessionid").as_deref(), Some("3"));
        assert_eq!(field(answer, "nothing"), None);
    }
}
