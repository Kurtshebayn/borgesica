mod sidecar;

use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;
use sidecar::{shutdown_sidecar, spawn_sidecar, SidecarHandle};
use tauri::{Manager, State};

/// Returned to the frontend by `start_sidecar`: the base URL AND the
/// per-session auth token (RISK-001/002/003), exposed together the same
/// controlled way `base_url` alone used to be.
#[derive(Serialize)]
struct SidecarStartResult {
    base_url: String,
    token: String,
}

/// App-managed state holding the (at most one) running sidecar handle.
/// `None` before the frontend calls `start_sidecar` and after teardown.
struct SidecarState(Mutex<Option<SidecarHandle>>);

/// Resolves the packaged sidecar binary path relative to the app's resource
/// directory, if one has been bundled. Returns `None` in dev, where the
/// Python fallback in `sidecar::resolve_engine_command` is used instead.
fn packaged_binary_path(app: &tauri::AppHandle) -> Option<PathBuf> {
    let resource_dir = app.path().resource_dir().ok()?;
    let candidate = resource_dir.join("borgesica").join(if cfg!(windows) {
        "borgesica.exe"
    } else {
        "borgesica"
    });
    if candidate.exists() {
        Some(candidate)
    } else {
        None
    }
}

/// Spawns the sidecar with the session-only API key and returns its base
/// URL to the frontend. The key is never persisted by this command: it is
/// consumed by `spawn_sidecar` and dropped once written to the child's
/// stdin.
#[tauri::command]
fn start_sidecar(
    app: tauri::AppHandle,
    state: State<SidecarState>,
    api_key: String,
) -> Result<SidecarStartResult, String> {
    let mut guard = state.0.lock().map_err(|_| "sidecar state poisoned")?;
    if guard.is_some() {
        return Err("sidecar already running".to_string());
    }

    let binary = packaged_binary_path(&app);
    let handle = spawn_sidecar(binary.as_ref(), &api_key).map_err(|e| e.to_string())?;
    let result = SidecarStartResult {
        base_url: handle.base_url.clone(),
        token: handle.token.clone(),
    };
    *guard = Some(handle);
    Ok(result)
}

/// Gracefully stops the sidecar if one is running. Safe to call multiple
/// times; a second call with no active sidecar is a no-op.
#[tauri::command]
fn stop_sidecar(state: State<SidecarState>) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|_| "sidecar state poisoned")?;
    if let Some(handle) = guard.take() {
        shutdown_sidecar(handle);
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(SidecarState(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![start_sidecar, stop_sidecar])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let state: State<SidecarState> = window.state();
                let taken = state.0.lock().ok().and_then(|mut guard| guard.take());
                if let Some(handle) = taken {
                    shutdown_sidecar(handle);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running the borgesica desktop shell");
}
