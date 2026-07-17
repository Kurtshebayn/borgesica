/**
 * Thin glue over `@tauri-apps/api`'s `invoke`. Kept separate from
 * `sidecarStatus.ts` (pure logic, unit tested) so the untestable
 * IPC/process boundary is not disguised as tested logic.
 */
import { invoke } from "@tauri-apps/api/core";

/**
 * Starts the sidecar for this session, passing the API key once. The key
 * is never stored by this module; the caller is responsible for holding it
 * only in memory for the lifetime of the session (spec: desktop-shell "Key
 * not persisted").
 */
export async function startSidecar(apiKey: string): Promise<string> {
  return invoke<string>("start_sidecar", { apiKey });
}

/**
 * Requests a graceful sidecar shutdown. Safe to call even if no sidecar is
 * currently running.
 */
export async function stopSidecar(): Promise<void> {
  await invoke("stop_sidecar");
}
