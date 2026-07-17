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
  });
  const started = useRef(false);

  const status = deriveSidecarStatus(state);

  async function handleStart() {
    if (started.current) return;
    started.current = true;
    try {
      const baseUrl = await startSidecar(apiKeyInput);
      setState({ baseUrl, error: null });
    } catch (err) {
      setState({ baseUrl: null, error: String(err) });
      started.current = false;
    }
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
      {status === "ready" && state.baseUrl && <WizardScreen baseUrl={state.baseUrl} />}
    </main>
  );
}
