//! The daemon's wire protocol: newline-delimited JSON-RPC 2.0 over loopback TCP.
//!
//! The daemon writes where it listens, and the bearer token to talk to it, to
//! `%LOCALAPPDATA%\openrazer-win\daemon.json`. Only the user who started it
//! can read that file, which is the whole of the trust boundary.

use serde::Deserialize;
use serde_json::{json, Value};
use std::fmt;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::Duration;

/// Long enough for a Bluetooth call, which can take seconds on a cold link.
const READ_TIMEOUT: Duration = Duration::from_secs(40);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
/// A live daemon on loopback accepts at once. Windows takes about 2 s to refuse
/// a connection to a closed loopback port -- what a stale endpoint file at
/// sign-in points at -- so on loopback this is how long to wait before asking
/// whether the daemon is even alive.
const LOOPBACK_CONNECT_TIMEOUT: Duration = Duration::from_millis(250);

/// A connect that merely took longer than the short loopback wait is not a
/// daemon that has gone: it is retried with the full time. Any other failure
/// (refused) is final.
fn slow_not_gone(kind: std::io::ErrorKind) -> bool {
    kind == std::io::ErrorKind::TimedOut
}

/// Whether a failed short loopback connect earns the full-time retry: it must
/// have been a timeout, and the process the file names must not have gone in
/// the meantime (a pid of 0 or none is not recorded, so it is trusted).
fn retry_after_short_connect(kind: std::io::ErrorKind, pid: Option<u32>, alive: impl Fn(u32) -> bool) -> bool {
    slow_not_gone(kind) && pid.is_none_or(|pid| pid == 0 || alive(pid))
}

#[derive(Clone, Debug, Deserialize)]
pub struct Endpoint {
    pub host: String,
    pub port: u16,
    pub token: String,
    #[serde(default)]
    pub pid: Option<u32>,
}

impl Endpoint {
    pub fn path() -> PathBuf {
        crate::paths::data_dir().join("daemon.json")
    }

    pub fn read() -> Option<Endpoint> {
        let text = std::fs::read_to_string(Self::path()).ok()?;
        serde_json::from_str(&text).ok()
    }
}

#[derive(Debug, Clone)]
pub enum RpcError {
    /// No daemon to talk to, or the connection broke.
    Unavailable(String),
    /// The daemon answered with an error.
    Remote(String),
}

impl fmt::Display for RpcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RpcError::Unavailable(reason) => write!(f, "{reason}"),
            RpcError::Remote(message) => write!(f, "{message}"),
        }
    }
}

pub struct Client {
    endpoint: Endpoint,
    reader: BufReader<TcpStream>,
    writer: TcpStream,
    next_id: u64,
}

impl Client {
    /// Connect to the daemon named in the endpoint file.
    pub fn connect() -> Result<Client, RpcError> {
        let endpoint = Endpoint::read().ok_or_else(|| RpcError::Unavailable(crate::t("the daemon is not running").into()))?;
        Self::connect_to(endpoint)
    }

    pub fn connect_to(endpoint: Endpoint) -> Result<Client, RpcError> {
        let address = (endpoint.host.as_str(), endpoint.port)
            .to_socket_addrs()
            .map_err(|e| RpcError::Unavailable(e.to_string()))?
            .next()
            .ok_or_else(|| RpcError::Unavailable("bad endpoint address".into()))?;
        let stale = || RpcError::Unavailable(crate::t("the daemon is not running (its endpoint file is stale)").into());
        let stream = if address.ip().is_loopback() {
            // The process the file names is gone: nothing to wait for.
            if endpoint.pid.is_some_and(|pid| pid != 0 && !crate::process::is_alive(pid)) {
                return Err(stale());
            }
            match TcpStream::connect_timeout(&address, LOOPBACK_CONNECT_TIMEOUT) {
                Ok(stream) => stream,
                // Slow to accept is not the same as not there. Only a daemon
                // known to be gone is given up on early: a false "not running"
                // would have Studio start a second one.
                Err(e) if retry_after_short_connect(e.kind(), endpoint.pid, crate::process::is_alive) => TcpStream::connect_timeout(&address, CONNECT_TIMEOUT).map_err(|_| stale())?,
                Err(_) => return Err(stale()),
            }
        } else {
            TcpStream::connect_timeout(&address, CONNECT_TIMEOUT).map_err(|_| stale())?
        };
        stream.set_read_timeout(Some(READ_TIMEOUT)).ok();
        stream.set_nodelay(true).ok();
        let writer = stream.try_clone().map_err(|e| RpcError::Unavailable(e.to_string()))?;
        Ok(Client { endpoint, reader: BufReader::new(stream), writer, next_id: 1 })
    }

    pub fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    pub fn call(&mut self, method: &str, params: Value) -> Result<Value, RpcError> {
        let id = self.next_id;
        self.next_id += 1;
        let request = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
            "token": self.endpoint.token,
        });
        let mut line = serde_json::to_vec(&request).expect("a request always serialises");
        line.push(b'\n');
        self.writer.write_all(&line).map_err(|e| RpcError::Unavailable(format!("lost the daemon: {e}")))?;
        let mut answer = String::new();
        let read = self.reader.read_line(&mut answer).map_err(|e| RpcError::Unavailable(format!("lost the daemon: {e}")))?;
        if read == 0 {
            return Err(RpcError::Unavailable("the daemon closed the connection".into()));
        }
        let message: Value = serde_json::from_str(&answer).map_err(|e| RpcError::Unavailable(format!("unreadable answer: {e}")))?;
        if let Some(error) = message.get("error") {
            return Err(RpcError::Remote(error.get("message").and_then(Value::as_str).unwrap_or("unknown error").to_string()));
        }
        Ok(message.get("result").cloned().unwrap_or(Value::Null))
    }

    /// `device.call`: one allowlisted method on one device.
    pub fn device(&mut self, serial: &str, method: &str, args: Vec<Value>, zone: Option<&str>) -> Result<Value, RpcError> {
        let kwargs = match zone {
            Some(zone) => json!({ "zone": zone }),
            None => json!({}),
        };
        self.call("device.call", json!({ "serial": serial, "method": method, "args": args, "kwargs": kwargs }))
    }
}

/// Ask whatever daemon is running to stop, and wait up to `wait` for it to go.
///
/// For the installer, which has to replace the daemon's executable.
pub fn shutdown_daemon(wait: Duration) -> bool {
    let Ok(mut client) = Client::connect() else { return false };
    if client.call("daemon.shutdown", json!({})).is_err() {
        return false;
    }
    let deadline = std::time::Instant::now() + wait;
    while std::time::Instant::now() < deadline {
        if Client::connect().and_then(|mut c| c.call("daemon.version", json!({}))).is_err() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::time::Instant;

    fn endpoint(port: u16, pid: Option<u32>) -> Endpoint {
        Endpoint { host: "127.0.0.1".into(), port, token: "t".into(), pid }
    }

    /// A port nothing listens on: bound, then let go of.
    fn closed_port() -> u16 {
        TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
    }

    #[test]
    fn a_daemon_that_is_listening_is_reached() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(Client::connect_to(endpoint(port, Some(std::process::id()))).is_ok());
        // An endpoint file with no pid, as an older daemon wrote it, still works.
        assert!(Client::connect_to(endpoint(port, None)).is_ok());
    }

    #[cfg(windows)]
    #[test]
    fn a_stale_endpoint_naming_a_dead_process_fails_at_once() {
        let started = Instant::now();
        // No process has an id this large.
        assert!(Client::connect_to(endpoint(closed_port(), Some(0xFFFF_FFF0))).is_err());
        assert!(started.elapsed() < Duration::from_millis(200), "took {:?}", started.elapsed());
    }

    #[test]
    fn only_a_timeout_is_given_the_long_wait() {
        use std::io::ErrorKind::*;
        // Slow to accept: the daemon may be busy, so it is not given up on.
        assert!(slow_not_gone(TimedOut));
        // Refused is an answer.
        assert!(!slow_not_gone(ConnectionRefused));
        assert!(!slow_not_gone(Other));
    }

    #[test]
    fn the_long_retry_needs_a_timeout_and_a_process_that_may_be_alive() {
        use std::io::ErrorKind::*;
        assert!(retry_after_short_connect(TimedOut, Some(7), |_| true));
        assert!(retry_after_short_connect(TimedOut, None, |_| false));
        assert!(retry_after_short_connect(TimedOut, Some(0), |_| false));
        // Gone while we waited: no second wait.
        assert!(!retry_after_short_connect(TimedOut, Some(7), |_| false));
        // Refused is final, however alive the process.
        assert!(!retry_after_short_connect(ConnectionRefused, Some(7), |_| true));
        assert!(!retry_after_short_connect(ConnectionRefused, None, |_| true));
    }

    #[test]
    fn a_closed_port_is_still_refused_when_the_process_may_be_alive() {
        // The pid is ours, so it is alive: the full timeout applies, and the
        // answer is the same -- not running.
        assert!(Client::connect_to(endpoint(closed_port(), Some(std::process::id()))).is_err());
    }
}
