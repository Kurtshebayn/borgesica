//! Pure decision logic for the borgesica sidecar lifecycle.
//!
//! Deliberately has zero dependency on `tauri`/`tokio`/process-spawning so it
//! compiles and tests in isolation (seconds, not minutes) and can be unit
//! tested without ever spawning a real child process. The Tauri-facing glue
//! (`src-tauri/src/sidecar.rs`) calls into these functions but owns all I/O.

use std::time::Duration;

/// Errors produced while parsing the sidecar's stdout "ready" line.
#[derive(Debug, PartialEq, Eq)]
pub enum ReadyLineError {
    /// The line was not valid JSON.
    InvalidJson,
    /// The JSON parsed but was not a ready event (e.g. a log line).
    NotReadyEvent,
    /// The ready event was missing a usable, non-zero port.
    MissingPort,
}

/// Parses one line of the sidecar's stdout looking for the ready handshake
/// emitted by `borgesica serve` after binding: `{"event":"ready","port":N}`.
///
/// Any other line (banner text, log noise, partial JSON) is rejected rather
/// than panicking, since stdout may interleave unrelated output before the
/// ready line arrives.
pub fn parse_ready_line(line: &str) -> Result<u16, ReadyLineError> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Err(ReadyLineError::InvalidJson);
    }

    let value: serde_json::Value =
        serde_json::from_str(trimmed).map_err(|_| ReadyLineError::InvalidJson)?;

    let event = value.get("event").and_then(|v| v.as_str());
    if event != Some("ready") {
        return Err(ReadyLineError::NotReadyEvent);
    }

    let port = value
        .get("port")
        .and_then(|v| v.as_u64())
        .filter(|p| *p > 0 && *p <= u16::MAX as u64);

    port.map(|p| p as u16).ok_or(ReadyLineError::MissingPort)
}

/// Builds the argv for spawning the sidecar. The API key is deliberately
/// never included here: it is written to the child's stdin by the caller
/// after spawn, per the desktop-shell spec's "key never in argv/env"
/// requirement.
pub fn build_serve_args(port: u16) -> Vec<String> {
    vec![
        "serve".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--key-stdin".to_string(),
    ]
}

/// Guard usable both in tests and defensively at spawn time: asserts the
/// literal API key text never appears in the argv that will be handed to
/// the OS process launcher.
pub fn args_contain_secret(args: &[String], secret: &str) -> bool {
    if secret.is_empty() {
        return false;
    }
    args.iter().any(|a| a.contains(secret))
}

/// Handshake timeout decision: has waiting for the ready line exceeded the
/// allowed budget? Design specifies a 15s ready timeout.
pub fn ready_deadline_exceeded(elapsed: Duration, timeout: Duration) -> bool {
    elapsed >= timeout
}

/// Teardown decision: after requesting a graceful shutdown, has enough time
/// passed that the child should be force-killed instead of waited on?
/// Design specifies a 5s grace period, mirroring the server's own
/// `shutdown_join_timeout`.
pub fn should_force_kill(elapsed_since_shutdown: Duration, grace_period: Duration) -> bool {
    elapsed_since_shutdown >= grace_period
}

/// Builds the base URL the frontend should use once the port is known.
pub fn base_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

/// Builds the health-check URL from a base URL.
pub fn health_check_url(base: &str) -> String {
    format!("{}/health", base.trim_end_matches('/'))
}

/// Formats two OS-sourced pseudo-random u64 seeds into a 32-hex-char session
/// token (RISK-001/002/003). Pure formatting only — the caller (glue) is
/// responsible for sourcing the seeds from real entropy
/// (`std::collections::hash_map::RandomState`, itself OS-seeded).
pub fn format_token(seed1: u64, seed2: u64) -> String {
    format!("{seed1:016x}{seed2:016x}")
}

/// Builds the stdin init line handed to the sidecar process: the API key
/// (existing "key never in argv" contract) plus the per-session auth token
/// (RISK-001/002/003), carried over the SAME trusted stdin channel — never
/// argv, never logged. Returns the raw JSON line (caller appends the
/// newline when writing).
pub fn build_stdin_init_line(api_key: &str, token: &str) -> String {
    serde_json::json!({ "api_key": api_key, "token": token }).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_ready_line_extracts_port_from_valid_json() {
        let line = r#"{"event":"ready","port":54321}"#;
        assert_eq!(parse_ready_line(line), Ok(54321));
    }

    #[test]
    fn parse_ready_line_tolerates_surrounding_whitespace_and_newline() {
        let line = "  {\"event\":\"ready\",\"port\":8080}\n";
        assert_eq!(parse_ready_line(line), Ok(8080));
    }

    #[test]
    fn parse_ready_line_rejects_invalid_json() {
        let line = "not json at all";
        assert_eq!(parse_ready_line(line), Err(ReadyLineError::InvalidJson));
    }

    #[test]
    fn parse_ready_line_rejects_non_ready_event() {
        let line = r#"{"event":"log","message":"booting"}"#;
        assert_eq!(parse_ready_line(line), Err(ReadyLineError::NotReadyEvent));
    }

    #[test]
    fn parse_ready_line_rejects_missing_port() {
        let line = r#"{"event":"ready"}"#;
        assert_eq!(parse_ready_line(line), Err(ReadyLineError::MissingPort));
    }

    #[test]
    fn parse_ready_line_rejects_zero_port() {
        let line = r#"{"event":"ready","port":0}"#;
        assert_eq!(parse_ready_line(line), Err(ReadyLineError::MissingPort));
    }

    #[test]
    fn parse_ready_line_rejects_empty_line() {
        assert_eq!(parse_ready_line(""), Err(ReadyLineError::InvalidJson));
        assert_eq!(parse_ready_line("   "), Err(ReadyLineError::InvalidJson));
    }

    #[test]
    fn build_serve_args_never_includes_the_key() {
        let args = build_serve_args(54321);
        assert_eq!(args, vec!["serve", "--port", "54321", "--key-stdin"]);
        assert!(!args_contain_secret(&args, "sk-super-secret-key"));
    }

    #[test]
    fn args_contain_secret_detects_leaked_key() {
        let leaked = vec!["serve".to_string(), "--key".to_string(), "sk-abc".to_string()];
        assert!(args_contain_secret(&leaked, "sk-abc"));
    }

    #[test]
    fn args_contain_secret_ignores_empty_secret() {
        let args = build_serve_args(1234);
        assert!(!args_contain_secret(&args, ""));
    }

    #[test]
    fn ready_deadline_exceeded_true_once_timeout_reached() {
        let timeout = Duration::from_secs(15);
        assert!(!ready_deadline_exceeded(Duration::from_secs(14), timeout));
        assert!(ready_deadline_exceeded(Duration::from_secs(15), timeout));
        assert!(ready_deadline_exceeded(Duration::from_secs(20), timeout));
    }

    #[test]
    fn should_force_kill_true_once_grace_period_elapsed() {
        let grace = Duration::from_secs(5);
        assert!(!should_force_kill(Duration::from_millis(4900), grace));
        assert!(should_force_kill(Duration::from_secs(5), grace));
    }

    #[test]
    fn base_url_formats_loopback_with_port() {
        assert_eq!(base_url(54321), "http://127.0.0.1:54321");
    }

    #[test]
    fn health_check_url_appends_health_path() {
        assert_eq!(
            health_check_url("http://127.0.0.1:54321"),
            "http://127.0.0.1:54321/health"
        );
        assert_eq!(
            health_check_url("http://127.0.0.1:54321/"),
            "http://127.0.0.1:54321/health"
        );
    }

    // -----------------------------------------------------------------
    // RISK-001/002/003 — session token formatting + stdin handshake
    // -----------------------------------------------------------------

    #[test]
    fn format_token_produces_a_32_char_lowercase_hex_string() {
        let token = format_token(0x0123_4567_89ab_cdef, 0xfedc_ba98_7654_3210);
        assert_eq!(token.len(), 32);
        assert!(token.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        assert_eq!(token, "0123456789abcdeffedcba9876543210");
    }

    #[test]
    fn format_token_differs_for_different_seeds() {
        assert_ne!(format_token(1, 2), format_token(3, 4));
    }

    #[test]
    fn build_stdin_init_line_carries_both_api_key_and_token() {
        let line = build_stdin_init_line("sk-secret", "tok-12345");
        let parsed: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(parsed["api_key"], "sk-secret");
        assert_eq!(parsed["token"], "tok-12345");
    }

    #[test]
    fn session_token_never_leaks_into_serve_argv() {
        // RISK-001/002/003: the token travels over stdin only (mirrors the
        // existing api_key contract) — build_serve_args must never be able
        // to embed it, and args_contain_secret (already generic) confirms it.
        let token = format_token(0xdead_beef, 0xfeed_face);
        let args = build_serve_args(54321);
        assert!(!args_contain_secret(&args, &token));
    }
}
