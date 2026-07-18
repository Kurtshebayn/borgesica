/**
 * Thin glue over `@tauri-apps/api`'s `invoke`. Kept separate from
 * `sidecarStatus.ts` (pure logic, unit tested) so the untestable
 * IPC/process boundary is not disguised as tested logic.
 */
import { invoke } from "@tauri-apps/api/core";

export interface SidecarStartResult {
  baseUrl: string;
  token: string;
}

/**
 * Starts the sidecar for this session, passing the API key once. The key
 * is never stored by this module; the caller is responsible for holding it
 * only in memory for the lifetime of the session (spec: desktop-shell "Key
 * not persisted").
 *
 * Returns both the base URL and the per-session auth token
 * (RISK-001/002/003) — the Rust side generates the token and exposes it
 * here the same controlled way base_url always was; the frontend must
 * attach it to every subsequent serve API request.
 */
export async function startSidecar(apiKey: string): Promise<SidecarStartResult> {
  const result = await invoke<{ base_url: string; token: string }>(
    "start_sidecar",
    { apiKey },
  );
  return { baseUrl: result.base_url, token: result.token };
}

/**
 * Requests a graceful sidecar shutdown. Safe to call even if no sidecar is
 * currently running.
 */
export async function stopSidecar(): Promise<void> {
  await invoke("stop_sidecar");
}
