//! Tauri-facing sidecar lifecycle glue: locates the packaged engine binary
//! (or the dev-mode Python fallback), spawns it, hands off the API key over
//! stdin, discovers the port from the ready line, health-checks it, and
//! guarantees teardown on app exit.
//!
//! All *decisions* (argv shape, ready-line parsing, timing) live in the
//! dependency-free `sidecar_logic` crate so they can be unit tested without
//! spawning a real process. This module owns only the I/O.

use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::time::{Duration, Instant};

use sidecar_logic::{
    args_contain_secret, base_url, build_serve_args, health_check_url, parse_ready_line,
    ready_deadline_exceeded, should_force_kill,
};

const READY_TIMEOUT: Duration = Duration::from_secs(15);
const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

#[derive(Debug)]
pub enum SidecarError {
    SpawnFailed(String),
    StdinUnavailable,
    StdoutUnavailable,
    HandshakeTimeout,
    ReadyLineUnparseable,
    HealthCheckFailed,
}

impl std::fmt::Display for SidecarError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SidecarError::SpawnFailed(e) => write!(f, "failed to spawn sidecar process: {e}"),
            SidecarError::StdinUnavailable => write!(f, "sidecar stdin was not piped"),
            SidecarError::StdoutUnavailable => write!(f, "sidecar stdout was not piped"),
            SidecarError::HandshakeTimeout => {
                write!(f, "sidecar did not print a ready line within the timeout")
            }
            SidecarError::ReadyLineUnparseable => {
                write!(f, "sidecar stdout ended before a valid ready line arrived")
            }
            SidecarError::HealthCheckFailed => write!(f, "sidecar health check did not succeed"),
        }
    }
}

/// A running sidecar process plus the base URL the frontend should call.
///
/// Intentionally holds no copy of the API key: it is written to stdin once,
/// during `spawn_sidecar`, and never retained afterwards.
pub struct SidecarHandle {
    child: Child,
    pub base_url: String,
}

/// Resolves the (program, leading-args) pair used to launch the engine.
///
/// Packaging spike (T1) produces a frozen `borgesica`/`borgesica.exe`
/// binary; in dev, no packaged binary exists yet, so we fall back to
/// running the venv's Python module directly, matching how `__main__.py`
/// is already invoked by the test suite and packaging spike.
pub fn resolve_engine_command(packaged_binary: Option<&PathBuf>) -> (String, Vec<String>) {
    if let Some(path) = packaged_binary {
        if path.exists() {
            return (path.to_string_lossy().to_string(), Vec::new());
        }
    }
    // Dev fallback: `python -m borgesica`. Callers running from a repo
    // checkout are expected to have the project's venv active/selected.
    (
        "python".to_string(),
        vec!["-m".to_string(), "borgesica".to_string()],
    )
}

/// Spawns the sidecar, delivers the API key over stdin, waits for the ready
/// line, and health-checks the resulting base URL before returning.
pub fn spawn_sidecar(
    packaged_binary: Option<&PathBuf>,
    api_key: &str,
) -> Result<SidecarHandle, SidecarError> {
    let (program, mut leading_args) = resolve_engine_command(packaged_binary);
    let mut args = build_serve_args(0);
    // Guard: the key must never end up in argv. This is a defensive runtime
    // assertion backing the "key never in argv" spec requirement, mirrored
    // by sidecar_logic's own unit tests.
    debug_assert!(!args_contain_secret(&args, api_key));
    leading_args.append(&mut args);

    let mut child = Command::new(&program)
        .args(&leading_args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| SidecarError::SpawnFailed(e.to_string()))?;

    write_key_to_stdin(&mut child, api_key)?;

    let port = read_ready_port(&mut child)?;
    let url = base_url(port);

    wait_for_health(&url)?;

    Ok(SidecarHandle {
        child,
        base_url: url,
    })
}

fn write_key_to_stdin(child: &mut Child, api_key: &str) -> Result<(), SidecarError> {
    let stdin: &mut ChildStdin = child.stdin.as_mut().ok_or(SidecarError::StdinUnavailable)?;
    let line = serde_json::json!({ "api_key": api_key }).to_string();
    stdin
        .write_all(line.as_bytes())
        .and_then(|_| stdin.write_all(b"\n"))
        .map_err(|_| SidecarError::StdinUnavailable)
}

fn read_ready_port(child: &mut Child) -> Result<u16, SidecarError> {
    let stdout = child.stdout.take().ok_or(SidecarError::StdoutUnavailable)?;
    let mut reader = BufReader::new(stdout);
    let start = Instant::now();

    loop {
        if ready_deadline_exceeded(start.elapsed(), READY_TIMEOUT) {
            return Err(SidecarError::HandshakeTimeout);
        }

        let mut line = String::new();
        let bytes_read = reader
            .read_line(&mut line)
            .map_err(|_| SidecarError::ReadyLineUnparseable)?;

        if bytes_read == 0 {
            // stdout closed before a ready line arrived.
            return Err(SidecarError::ReadyLineUnparseable);
        }

        if let Ok(port) = parse_ready_line(&line) {
            return Ok(port);
        }
        // Non-ready lines (banner/log noise) are ignored; keep reading.
    }
}

fn wait_for_health(base: &str) -> Result<(), SidecarError> {
    let url = health_check_url(base);
    let deadline = Instant::now() + READY_TIMEOUT;
    loop {
        if ureq::get(&url).call().is_ok() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(SidecarError::HealthCheckFailed);
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// Tears down the sidecar: attempts a graceful `/shutdown` call, gives it
/// `SHUTDOWN_GRACE` to exit, then force-kills if it hasn't. Guarantees the
/// child does not outlive the app.
pub fn shutdown_sidecar(mut handle: SidecarHandle) {
    let shutdown_url = format!("{}/shutdown", handle.base_url);
    let _ = ureq::post(&shutdown_url).call();

    let start = Instant::now();
    loop {
        match handle.child.try_wait() {
            Ok(Some(_status)) => return, // exited cleanly
            Ok(None) => {
                if should_force_kill(start.elapsed(), SHUTDOWN_GRACE) {
                    let _ = handle.child.kill();
                    let _ = handle.child.wait();
                    return;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => {
                let _ = handle.child.kill();
                return;
            }
        }
    }
}
