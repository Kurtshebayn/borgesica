import { useEffect, useRef, useState } from "react";
import { deriveSidecarStatus, type SidecarState } from "./sidecarStatus";
import { startSidecar, stopSidecar } from "./sidecarClient";
import WizardScreen from "./WizardScreen";

/**
 * App shell: session-only API key entry + sidecar lifecycle (T7a), then
 * hands off to the translation wizard (T7b) once the sidecar reports
 * `ready` (spec: desktop-shell "UI waits for readiness").
 */
export default function App() {
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [state, setState] = useState<SidecarState>({
    baseUrl: null,
    error: null,
    token: null,
  });
  const started = useRef(false);

  const status = deriveSidecarStatus(state);

  async function handleStart() {
    if (started.current) return;
    started.current = true;
    try {
      const { baseUrl, token } = await startSidecar(apiKeyInput);
      setState({ baseUrl, token, error: null });
    } catch (err) {
      setState({ baseUrl: null, token: null, error: String(err) });
      started.current = false;
    }
  }

  /**
   * RES-001 (second correction round): the REAL respawn seam behind the
   * wizard's "Back to start" recovery action after a genuine persistent
   * connection loss (`runRecoveryAction`/`recoveryRequiresRespawn` in
   * wizard.ts). A prior version only reset the wizard's own screen state
   * while this App-level `started` ref stayed true and `state.baseUrl`/
   * `state.token` kept pointing at the dead process — the next
   * create/estimate call just hit the same corpse. This tears the old
   * (possibly already-dead) sidecar down, clears the stale lifecycle
   * state, and re-spawns a fresh one with the still-held session API key
   * — no full Tauri app restart is required, only this in-app respawn.
   */
  async function handleRestartSidecar() {
    started.current = false;
    setState({ baseUrl: null, token: null, error: null });
    await stopSidecar();
    await handleStart();
  }

  useEffect(() => {
    return () => {
      if (started.current) {
        void stopSidecar();
      }
    };
  }, []);

  return (
    <main>
      <h1>Borgesica</h1>
      <p>Sidecar status: {status}</p>
      {status !== "ready" && (
        <div>
          <input
            type="password"
            placeholder="API key (session only, never saved)"
            value={apiKeyInput}
            onChange={(e) => setApiKeyInput(e.target.value)}
          />
          <button onClick={handleStart} disabled={status === "starting" && started.current}>
            Start
          </button>
        </div>
      )}
      {state.error && <p role="alert">{state.error}</p>}
      {status === "ready" && state.baseUrl && state.token && (
        <WizardScreen
          baseUrl={state.baseUrl}
          token={state.token}
          onRestartSidecar={() => void handleRestartSidecar()}
        />
      )}
    </main>
  );
}
