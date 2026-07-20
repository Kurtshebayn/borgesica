import { useEffect, useReducer, useRef, useState } from "react";
import {
  cancelJob,
  createJob,
  getEstimate,
  getGlossary,
  getStatus,
  putGlossary,
  resumeJob,
  runJob,
} from "./apiClient";
import { subscribeToJobEvents, type SseSubscription } from "./sseClient";
import {
  defaultModelForProvider,
  formatBestEffortSummary,
  formatFailureSummary,
  formatProgress,
  initialWizardState,
  runRecoveryAction,
  wizardReducer,
  type Provider,
} from "./wizard";
import type { GlossaryEntry } from "./apiTypes";

/**
 * The T7b translation wizard: pick -> estimate -> glossary -> run -> done,
 * plus a resume path for an interrupted job (spec: desktop-shell). Screen
 * transitions and guards live in the pure `wizardReducer`; this component
 * is the untested glue wiring it to the sidecar's REST/SSE API.
 *
 * Presentational restraint (batch instructions): functional, minimal
 * styling — v1 happy path, not a design showcase.
 */
export default function WizardScreen({
  baseUrl,
  token,
  provider,
  onRestartSidecar,
}: {
  baseUrl: string;
  token: string;
  /** The provider the sidecar was launched with — seeds a provider-appropriate
   * default model so a DeepSeek/Ollama session is not stuck with an Anthropic
   * model id (which would fail at create/estimate). */
  provider: Provider;
  /** RES-001 (second correction round): the real stop+start sidecar
   * respawn seam, owned by App.tsx (which alone holds the API key and the
   * Tauri invoke calls). Called only when recovering from a genuine
   * persistent-connection-loss "error" screen — see `runRecoveryAction`. */
  onRestartSidecar: () => void;
}) {
  const [state, dispatch] = useReducer(wizardReducer, initialWizardState);
  // Provider-appropriate default model (anthropic -> claude-sonnet-5). This
  // supersedes the retired claude-3-5-sonnet-latest default, whose failures
  // used to look like a silent $0 "success" on untranslated output.
  const [model, setModel] = useState(() => defaultModelForProvider(provider));
  const [outPath, setOutPath] = useState("");
  const [resumeJobId, setResumeJobId] = useState("");
  const subscriptionRef = useRef<SseSubscription | null>(null);

  useEffect(() => {
    return () => {
      subscriptionRef.current?.close();
    };
  }, []);

  async function handlePick() {
    if (state.fileError || !state.filePath) return;
    try {
      const job = await createJob(baseUrl, token, {
        source_path: state.filePath,
        model,
      });
      dispatch({ type: "JOB_CREATED", jobId: job.id });
      const estimate = await getEstimate(baseUrl, token, job.id);
      dispatch({ type: "ESTIMATE_RECEIVED", estimate });
    } catch (err) {
      dispatch({ type: "JOB_CREATE_FAILED", message: String(err) });
    }
  }

  async function handleProceedToGlossary() {
    dispatch({ type: "PROCEED_TO_GLOSSARY" });
    if (!state.jobId) return;
    try {
      const glossary = await getGlossary(baseUrl, token, state.jobId);
      dispatch({ type: "GLOSSARY_LOADED", entries: glossary.entries });
    } catch {
      // Best-effort load; an empty glossary is a valid pre-run state.
    }
  }

  function handleGlossaryEntryChange(index: number, next: GlossaryEntry) {
    const entries = state.glossary.map((entry, i) => (i === index ? next : entry));
    dispatch({ type: "GLOSSARY_CHANGED", entries });
  }

  async function handleLockAndSaveGlossary() {
    if (!state.jobId) return;
    try {
      await putGlossary(baseUrl, token, state.jobId, state.glossary);
    } finally {
      dispatch({ type: "GLOSSARY_LOCKED" });
    }
  }

  function startWatching(jobId: string) {
    subscriptionRef.current?.close();
    subscriptionRef.current = subscribeToJobEvents(baseUrl, token, jobId, dispatch);
  }

  async function handleStartRun() {
    if (!state.jobId || !outPath) return;
    try {
      const job = await runJob(baseUrl, token, state.jobId, outPath);
      dispatch({ type: "RUN_STARTED", job });
      startWatching(state.jobId);
    } catch (err) {
      dispatch({ type: "RUN_START_FAILED", message: String(err) });
    }
  }

  function handleRecoverFromError() {
    // RES-001 (second correction round): recovery must be REAL, not
    // cosmetic. `runRecoveryAction` (pure/tested) decides whether the
    // underlying sidecar needs an actual respawn (only true when *state*
    // reflects a genuine persistent-failure "error" screen) and drives the
    // seams accordingly — a plain screen reset alone used to leave the
    // dead sidecar process, and its stale baseUrl/token, untouched.
    runRecoveryAction(state, {
      closeSubscription: () => {
        subscriptionRef.current?.close();
        subscriptionRef.current = null;
      },
      dispatch,
      restartSidecar: onRestartSidecar,
    });
  }

  async function handleCancel() {
    if (!state.jobId) return;
    dispatch({ type: "CANCEL_REQUESTED" });
    try {
      await cancelJob(baseUrl, token, state.jobId);
    } catch {
      // Cancellation is cooperative (chunk-granular); a transient error
      // here does not need to surface — the SSE/poll stream will reflect
      // the true terminal state once the run actually stops.
    }
  }

  async function handleResume() {
    if (!resumeJobId || !outPath) return;
    try {
      const status = await getStatus(baseUrl, token, resumeJobId);
      const estimate = await getEstimate(baseUrl, token, resumeJobId).catch(
        () => null,
      );
      if (estimate) dispatch({ type: "ESTIMATE_RECEIVED", estimate });
      if (status.status === "DONE") {
        dispatch({ type: "RESUME_JOB", job: status });
        return;
      }
      const job = await resumeJob(baseUrl, token, resumeJobId, outPath);
      dispatch({ type: "RESUME_JOB", job });
      startWatching(resumeJobId);
    } catch (err) {
      dispatch({ type: "RUN_START_FAILED", message: String(err) });
    }
  }

  const bestEffortSummary = state.job
    ? formatBestEffortSummary(state.job.best_effort_count)
    : null;
  const failureSummary = state.job
    ? formatFailureSummary(state.job.failed_count, state.job.total_chunks)
    : null;

  return (
    <section>
      {state.screen === "pick" && (
        <div>
          <h2>1. Choose a file</h2>
          <input
            type="text"
            placeholder="Path to .epub or .srt file"
            value={state.filePath}
            onChange={(e) =>
              dispatch({ type: "FILE_PATH_CHANGED", path: e.target.value })
            }
          />
          {state.fileError && <p role="alert">{state.fileError}</p>}
          <input
            type="text"
            placeholder="Model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
          <button
            onClick={() => void handlePick()}
            disabled={!state.filePath || !!state.fileError}
          >
            Create job &amp; estimate
          </button>
          {state.createError && <p role="alert">{state.createError}</p>}

          <h3>Resume an interrupted job</h3>
          <input
            type="text"
            placeholder="Existing job id"
            value={resumeJobId}
            onChange={(e) => setResumeJobId(e.target.value)}
          />
          <input
            type="text"
            placeholder="Output destination path"
            value={outPath}
            onChange={(e) => setOutPath(e.target.value)}
          />
          <button onClick={() => void handleResume()} disabled={!resumeJobId || !outPath}>
            Resume
          </button>
        </div>
      )}

      {state.screen === "estimate" && state.estimate && (
        <div>
          <h2>2. Estimate</h2>
          <p>
            Estimated cost: ${state.estimate.usd_low.toFixed(2)} – $
            {state.estimate.usd_high.toFixed(2)}
          </p>
          {!state.estimate.within_budget && (
            <p role="alert">This estimate exceeds the configured budget.</p>
          )}
          <button onClick={() => void handleProceedToGlossary()}>
            Continue to glossary
          </button>
        </div>
      )}

      {state.screen === "glossary" && (
        <div>
          <h2>3. Glossary (locked before run)</h2>
          <ul>
            {state.glossary.map((entry, index) => (
              <li key={entry.term}>
                <input
                  type="text"
                  value={entry.term}
                  disabled={state.glossaryLocked}
                  onChange={(e) =>
                    handleGlossaryEntryChange(index, { ...entry, term: e.target.value })
                  }
                />
                <input
                  type="text"
                  value={entry.translation}
                  disabled={state.glossaryLocked}
                  onChange={(e) =>
                    handleGlossaryEntryChange(index, {
                      ...entry,
                      translation: e.target.value,
                    })
                  }
                />
              </li>
            ))}
          </ul>
          <button
            onClick={() =>
              dispatch({
                type: "GLOSSARY_CHANGED",
                entries: [...state.glossary, { term: "", translation: "", locked: false }],
              })
            }
            disabled={state.glossaryLocked}
          >
            Add entry
          </button>
          {!state.glossaryLocked && (
            <button onClick={() => void handleLockAndSaveGlossary()}>
              Lock glossary &amp; continue
            </button>
          )}
          {state.glossaryLocked && (
            <div>
              <input
                type="text"
                placeholder="Output destination path"
                value={outPath}
                onChange={(e) => setOutPath(e.target.value)}
              />
              <button onClick={() => void handleStartRun()} disabled={!outPath}>
                Start run
              </button>
              {state.runError && <p role="alert">{state.runError}</p>}
            </div>
          )}
        </div>
      )}

      {state.screen === "run" && (
        <div>
          <h2>4. Running</h2>
          <p>{state.progress ? formatProgress(state.progress) : "Starting…"}</p>
          <button onClick={() => void handleCancel()} disabled={state.cancelRequested}>
            {state.cancelRequested ? "Cancelling… (takes effect between chunks)" : "Cancel"}
          </button>
        </div>
      )}

      {state.screen === "error" && (
        <div>
          <h2>Connection lost</h2>
          <p role="alert">{state.connectionError}</p>
          <button onClick={handleRecoverFromError}>Back to start</button>
        </div>
      )}

      {state.screen === "done" && (
        <div>
          <h2>{failureSummary ? "5. Finished with errors" : "5. Done"}</h2>
          <p>Status: {state.job?.status ?? "unknown"}</p>
          {/* Error-surfacing: a run can end status=DONE at $0 while every
              chunk FAILED (provider auth/model error) and the output holds
              untranslated source text. This alert makes that the headline
              instead of a silent success. */}
          {failureSummary && <p role="alert">⚠ {failureSummary}</p>}
          {state.runError && <p role="alert">{state.runError}</p>}
          {bestEffortSummary && <p>{bestEffortSummary}</p>}
          <p>Output written to: {outPath || "(destination chosen at run start)"}</p>
          <ExportPanel outPath={outPath} />
        </div>
      )}
    </section>
  );
}

/**
 * Export = choosing a destination for output already produced (design
 * decision #11); it never re-invokes translation. Copying the file to the
 * chosen destination itself requires an OS-level file-copy command that is
 * out of scope for this frontend-only work unit (no new Tauri fs plugin was
 * registered on the Rust side in T7b) — this panel intentionally limits
 * itself to confirming the already-written location instead of silently
 * claiming a copy it cannot perform.
 */
function ExportPanel({ outPath }: { outPath: string }) {
  const [destination, setDestination] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);

  return (
    <div>
      <input
        type="text"
        placeholder="Copy destination"
        value={destination}
        onChange={(e) => setDestination(e.target.value)}
      />
      <button onClick={() => setAcknowledged(true)} disabled={!destination}>
        Export
      </button>
      {acknowledged && (
        <p>
          The translated output is available at <code>{outPath}</code>. Copy it to{" "}
          <code>{destination}</code> using your file manager (no re-translation occurs).
        </p>
      )}
    </div>
  );
}
