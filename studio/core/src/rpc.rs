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
        let stream = TcpStream::connect_timeout(&address, CONNECT_TIMEOUT)
            .map_err(|_| RpcError::Unavailable(crate::t("the daemon is not running (its endpoint file is stale)").into()))?;
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
