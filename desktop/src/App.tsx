import { useEffect, useRef, useState } from "react";
import { deriveSidecarStatus, type SidecarState, type SidecarStatus } from "./sidecarStatus";
import { startSidecar, stopSidecar } from "./sidecarClient";
import { PROVIDERS, providerRequiresKey, type Provider } from "./wizard";
import WizardScreen from "./WizardScreen";

const SIDECAR_STATUS_LABELS: Record<SidecarStatus, string> = {
  idle: "inactivo",
  starting: "iniciando",
  ready: "listo",
  error: "error",
};

/**
 * App shell: session-only API key entry + sidecar lifecycle (T7a), then
 * hands off to the translation wizard (T7b) once the sidecar reports
 * `ready` (spec: desktop-shell "UI waits for readiness").
 */
export default function App() {
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [provider, setProvider] = useState<Provider>("anthropic");
  const [state, setState] = useState<SidecarState>({
    baseUrl: null,
    error: null,
    token: null,
  });
  const started = useRef(false);

  const status = deriveSidecarStatus(state);

  async function handleStart() {
    if (started.current) return;
    // Pre-spawn guard: hosted providers (anthropic/deepseek) authenticate with
    // an API key. Spawning the sidecar with an empty key just crashes it
    // (serve --key-stdin exits(1) on a keyless init line) and surfaces a raw
    // handshake error. Block Start with a clear inline message instead.
    if (providerRequiresKey(provider) && apiKeyInput.trim() === "") {
      setState({ baseUrl: null, token: null, error: "Este proveedor necesita una clave de API." });
      return;
    }
    started.current = true;
    try {
      const { baseUrl, token } = await startSidecar(provider, apiKeyInput);
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
      <h1>Borgésica</h1>
      <p>Estado del motor: {SIDECAR_STATUS_LABELS[status]}</p>
      {status !== "ready" && (
        <div>
          <label>
            Proveedor:{" "}
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value as Provider)}
            >
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <input
            type="password"
            placeholder={
              provider === "ollama"
                ? "Clave de API (no hace falta para Ollama)"
                : "Clave de API (solo para esta sesión, nunca se guarda)"
            }
            value={apiKeyInput}
            onChange={(e) => setApiKeyInput(e.target.value)}
          />
          <button onClick={handleStart} disabled={status === "starting" && started.current}>
            Iniciar
          </button>
        </div>
      )}
      {state.error && <p role="alert">{state.error}</p>}
      {status === "ready" && state.baseUrl && state.token && (
        <WizardScreen
          baseUrl={state.baseUrl}
          token={state.token}
          provider={provider}
          onRestartSidecar={() => void handleRestartSidecar()}
        />
      )}
    </main>
  );
}
