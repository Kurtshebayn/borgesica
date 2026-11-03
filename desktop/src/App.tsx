import { useEffect, useRef, useState } from "react";
import { deriveSidecarStatus, type SidecarState, type SidecarStatus } from "./sidecarStatus";
import { startSidecar, stopSidecar } from "./sidecarClient";
import { providerRequiresKey, type Provider } from "./wizard";
import CompareScreen from "./CompareScreen";
import LibraryScreen from "./LibraryScreen";
import ProviderPanel from "./ProviderPanel";
import StartScreen from "./StartScreen";
import WizardScreen from "./WizardScreen";

const SIDECAR_STATUS_LABELS: Record<SidecarStatus, string> = {
  idle: "inactivo",
  starting: "iniciando",
  ready: "listo",
  error: "error",
};

/** The engine refuses to boot a hosted provider without some key. Entering
 * without one boots it with this stand-in: enough to read the library, and
 * any paid call fails until a real key is added (the translate screen asks
 * for it first). */
const NO_KEY = "sin-clave";

/** Which screen the ready app shows: the library, a translation run (new,
 * resumed or a draft picked up at its estimate), or a finished book read side
 * by side. */
type View =
  | { name: "library" }
  | { name: "translate"; resumeJobId?: string; draft?: boolean }
  | { name: "compare"; jobId: string; title: string };

/**
 * App shell: session-only API key entry + sidecar lifecycle (T7a), then
 * hands off to the translation wizard (T7b) once the sidecar reports
 * `ready` (spec: desktop-shell "UI waits for readiness").
 */
export default function App() {
  const [provider, setProvider] = useState<Provider>("anthropic");
  // Keys typed this session, by provider, so switching back and forth never
  // asks twice. Memory only: they vanish when the app closes.
  const [keys, setKeys] = useState<Partial<Record<Provider, string>>>({});
  // Whether the running engine got a real key (not the NO_KEY stand-in).
  const [keyed, setKeyed] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [state, setState] = useState<SidecarState>({
    baseUrl: null,
    error: null,
    token: null,
  });
  const [booting, setBooting] = useState(false);
  const [view, setView] = useState<View>({ name: "library" });
  const started = useRef(false);

  const status = deriveSidecarStatus(state);

  /** Boots the engine for `target` with `key` (empty/missing: no key). */
  async function launch(target: Provider, key: string) {
    if (started.current) return;
    started.current = true;
    setProvider(target);
    setBooting(true);
    const hasKey = !providerRequiresKey(target) || key.trim() !== "";
    try {
      const { baseUrl, token } = await startSidecar(target, hasKey ? key : NO_KEY);
      setState({ baseUrl, token, error: null });
      setKeyed(hasKey);
    } catch (err) {
      setState({ baseUrl: null, token: null, error: String(err) });
      started.current = false;
    } finally {
      setBooting(false);
    }
  }

  function setKeyFor(target: Provider, key: string) {
    setKeys((prev) => ({ ...prev, [target]: key }));
  }

  /** The engine is bound to one provider at boot: switching tears it down and
   * starts a new one, then lands on the library. */
  async function handleSwitch(target: Provider, key: string) {
    if (key.trim() !== "") setKeyFor(target, key);
    started.current = false;
    // Show the wait at once: stopping the old engine can take seconds.
    setBooting(true);
    setState({ baseUrl: null, token: null, error: null });
    setPanelOpen(false);
    setView({ name: "library" });
    await stopSidecar();
    await launch(target, key);
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
    setBooting(true);
    setState({ baseUrl: null, token: null, error: null });
    await stopSidecar();
    await launch(provider, keys[provider] ?? "");
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
          apiKey={keys[provider] ?? ""}
          booting={booting}
          error={state.error}
          onProviderChange={setProvider}
          onApiKeyChange={(key) => setKeyFor(provider, key)}
          onStart={() => void launch(provider, keys[provider] ?? "")}
          onStartWithoutKey={() => void launch(provider, "")}
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
              providerLabel={`${provider}${providerRequiresKey(provider) && !keyed ? " · sin clave" : ""} · clave`}
              onProvider={() => setPanelOpen((open) => !open)}
              panel={
                panelOpen && (
                  <ProviderPanel
                    active={provider}
                    activeKeyed={keyed || !providerRequiresKey(provider)}
                    keys={keys}
                    onUse={(target, key) => void handleSwitch(target, key)}
                    onClose={() => setPanelOpen(false)}
                  />
                )
              }
              onNew={() => setView({ name: "translate" })}
              onOpen={(ficha) =>
                setView(
                  ficha.action === "compare"
                    ? { name: "compare", jobId: ficha.job.id, title: ficha.job.title }
                    : {
                        name: "translate",
                        resumeJobId: ficha.job.id,
                        draft: ficha.status === "draft",
                      },
                )
              }
            />
          )}
          {view.name === "translate" && (
            <>
              <button type="button" className="link back" onClick={() => setView({ name: "library" })}>
                ← Biblioteca
              </button>
              {providerRequiresKey(provider) && !keyed ? (
                <section className="card provider-panel">
                  <h2>Falta la clave de {provider}</h2>
                  <p>
                    Entraste sin clave, así que puedes leer tus libros pero no traducir. Agrégala
                    para continuar.
                  </p>
                  <div className="actions">
                    <button
                      type="button"
                      className="primary"
                      onClick={() => {
                        setView({ name: "library" });
                        setPanelOpen(true);
                      }}
                    >
                      Agregar la clave
                    </button>
                  </div>
                </section>
              ) : (
                <WizardScreen
                  baseUrl={state.baseUrl}
                  token={state.token}
                  provider={provider}
                  resumeJobId={view.resumeJobId}
                  resumeDraft={view.draft}
                  onRestartSidecar={() => void handleRestartSidecar()}
                />
              )}
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
