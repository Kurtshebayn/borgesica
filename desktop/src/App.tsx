import { useEffect, useRef, useState } from "react";
import { deriveSidecarStatus, type SidecarState, type SidecarStatus } from "./sidecarStatus";
import { startSidecar, stopSidecar } from "./sidecarClient";
import { providerRequiresKey, type Provider } from "./wizard";
import CompareScreen from "./CompareScreen";
import LibraryScreen from "./LibraryScreen";
import StartScreen from "./StartScreen";
import WizardScreen from "./WizardScreen";

const SIDECAR_STATUS_LABELS: Record<SidecarStatus, string> = {
  idle: "inactivo",
  starting: "iniciando",
  ready: "listo",
  error: "error",
};

/** Which screen the ready app shows: the library, a translation run (new or
 * resumed), or a finished book read side by side. */
type View =
  | { name: "library" }
  | { name: "translate"; resumeJobId?: string }
  | { name: "compare"; jobId: string; title: string };

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
  const [booting, setBooting] = useState(false);
  const [view, setView] = useState<View>({ name: "library" });
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
    setBooting(true);
    try {
      const { baseUrl, token } = await startSidecar(provider, apiKeyInput);
      setState({ baseUrl, token, error: null });
    } catch (err) {
      setState({ baseUrl: null, token: null, error: String(err) });
      started.current = false;
    } finally {
      setBooting(false);
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
    <main className="app">
      {status !== "ready" && (
        <StartScreen
          provider={provider}
          apiKey={apiKeyInput}
          booting={booting}
          error={state.error}
          onProviderChange={setProvider}
          onApiKeyChange={setApiKeyInput}
          onStart={() => void handleStart()}
        />
      )}
      {status === "ready" && state.baseUrl && state.token && (
        <>
          <header className="app-header">
            <h1>Borgésica</h1>
            <span className="engine-status" data-status={status}>
              Motor: {SIDECAR_STATUS_LABELS[status]}
            </span>
          </header>
          {view.name === "library" && (
            <LibraryScreen
              baseUrl={state.baseUrl}
              token={state.token}
              onNew={() => setView({ name: "translate" })}
              onOpen={(ficha) =>
                setView(
                  ficha.action === "compare"
                    ? { name: "compare", jobId: ficha.job.id, title: ficha.job.title }
                    : { name: "translate", resumeJobId: ficha.job.id },
                )
              }
            />
          )}
          {view.name === "translate" && (
            <>
              <button type="button" className="link back" onClick={() => setView({ name: "library" })}>
                ← Biblioteca
              </button>
              <WizardScreen
                baseUrl={state.baseUrl}
                token={state.token}
                provider={provider}
                resumeJobId={view.resumeJobId}
                onRestartSidecar={() => void handleRestartSidecar()}
              />
            </>
          )}
          {view.name === "compare" && (
            <CompareScreen
              baseUrl={state.baseUrl}
              token={state.token}
              jobId={view.jobId}
              title={view.title}
              onBack={() => setView({ name: "library" })}
            />
          )}
        </>
      )}
    </main>
  );
}
