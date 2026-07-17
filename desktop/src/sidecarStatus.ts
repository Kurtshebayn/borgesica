/**
 * Pure, side-effect-free helpers for the sidecar lifecycle UI state.
 *
 * Deliberately has no dependency on `@tauri-apps/api` or the DOM so it can
 * be unit tested in isolation (vitest, node environment) without a running
 * webview or sidecar process. All actual `invoke()` calls live in
 * `sidecarClient.ts`.
 */

export type SidecarStatus = "idle" | "starting" | "ready" | "error";

export interface SidecarState {
  baseUrl: string | null;
  error: string | null;
}

/**
 * Derives the UI-facing status from the raw sidecar state. The UI MUST NOT
 * send create/run/estimate requests before `ready` (spec: desktop-shell
 * "UI waits for readiness").
 */
export function deriveSidecarStatus(state: SidecarState): SidecarStatus {
  if (state.error) return "error";
  if (state.baseUrl) return "ready";
  return "starting";
}

/**
 * True only once the sidecar is ready to receive requests.
 */
export function canSendRequests(state: SidecarState): boolean {
  return deriveSidecarStatus(state) === "ready";
}

/**
 * Defensive guard mirroring the Rust-side `args_contain_secret`: asserts a
 * piece of text that might get logged (e.g. an error message surfaced to
 * the UI) does not leak the raw API key value.
 */
export function textLeaksSecret(text: string, secret: string): boolean {
  if (!secret) return false;
  return text.includes(secret);
}
