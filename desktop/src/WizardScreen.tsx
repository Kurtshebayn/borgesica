import { useEffect, useReducer, useRef, useState } from "react";
import {
  cancelJob,
  createJob,
  extractGlossary,
  getEstimate,
  getGlossary,
  getGlossaryReview,
  getNarrators,
  getStatus,
  putGlossary,
  putNarrators,
  resumeJob,
  runJob,
  stopGlossaryExtraction,
} from "./apiClient";
import {
  subscribeToGlossaryExtraction,
  subscribeToJobEvents,
  type SseSubscription,
} from "./sseClient";
import {
  defaultModelForProvider,
  formatBestEffortSummary,
  formatFailureSummary,
  formatOutputLocation,
  formatGlossaryProgress,
  formatProgress,
  initialWizardState,
  loadGlossaryReview,
  loadNarrators,
  runRecoveryAction,
  saveGlossary,
  saveNarrators,
  wizardReducer,
  type Provider,
} from "./wizard";
import { glossaryChangesToSave } from "./glossaryReview";
import { hasNarratorChanges } from "./narrators";
import EstimateSummary from "./EstimateSummary";
import GlossaryTriage from "./GlossaryTriage";
import NarratorReview from "./NarratorReview";
import type { GlossaryEntry, JobStatus } from "./apiTypes";

const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  CREATED: "creado",
  ESTIMATING: "estimando",
  RUNNING: "en curso",
  PAUSED: "pausado",
  DONE: "listo",
  FAILED: "con errores",
  CANCELLED: "cancelado",
};

/**
 * The T7b translation wizard: pick -> estimate -> [preparing] -> glossary ->
 * run -> done, plus a resume path for an interrupted job (spec:
 * desktop-shell). "preparing" is the optional, paid glossary extraction
 * pass, started only by the user's click on the estimate screen. Screen
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
  const [resumeJobId, setResumeJobId] = useState("");
  const subscriptionRef = useRef<SseSubscription | null>(null);

  useEffect(() => {
    return () => {
      subscriptionRef.current?.close();
    };
  }, []);

  // The triage review is loaded on entering the glossary screen, whichever
  // way it was reached (skipping the preparation or finishing it).
  const needsReview =
    state.screen === "glossary" &&
    !!state.jobId &&
    !state.glossaryReview &&
    !state.glossaryReviewError &&
    !state.glossaryLocked;
  useEffect(() => {
    if (needsReview && state.jobId) void reloadGlossaryReview(state.jobId);
  }, [needsReview]);

  function reloadGlossaryReview(jobId: string): Promise<void> {
    return loadGlossaryReview(() => getGlossaryReview(baseUrl, token, jobId), dispatch);
  }

  // The chapters (for the narrator section) load on the same screen, once;
  // a failure is shown and blocks neither the glossary nor the run.
  const needsNarrators =
    state.screen === "glossary" &&
    !!state.jobId &&
    state.narratorChapters === null &&
    !state.narratorLoadError;
  useEffect(() => {
    if (needsNarrators && state.jobId) {
      const jobId = state.jobId;
      void loadNarrators(jobId, () => getNarrators(baseUrl, token, jobId), dispatch);
    }
  }, [needsNarrators]);

  // A run reads the narrators stored on the server, so unsaved edits would be
  // silently left out of the translation: the run waits until they are saved.
  const narratorsUnsaved =
    !!state.narratorChapters &&
    hasNarratorChanges(state.narratorChapters, state.narratorDraft);

  /** The payload and the state changes are the tested `saveNarrators`; this
   * only wires the API call. */
  async function handleSaveNarrators() {
    if (!state.jobId) return;
    const jobId = state.jobId;
    await saveNarrators(state, {
      put: (narrators) => putNarrators(baseUrl, token, jobId, narrators),
      dispatch,
    });
  }

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

  /** The paid pass: only ever reached from the "prepare glossary" button,
   * which is shown only next to its cost line. The reducer leaves the
   * estimate screen first, so the button cannot be clicked twice. */
  async function handlePrepareGlossary() {
    if (!state.jobId) return;
    const jobId = state.jobId;
    dispatch({ type: "GLOSSARY_EXTRACTION_STARTED" });
    try {
      await extractGlossary(baseUrl, token, jobId);
    } catch (err) {
      dispatch({ type: "GLOSSARY_EXTRACTION_START_FAILED", message: String(err) });
      return;
    }
    subscriptionRef.current?.close();
    subscriptionRef.current = subscribeToGlossaryExtraction(baseUrl, token, jobId, dispatch);
  }

  async function handleStopPreparation() {
    if (!state.jobId) return;
    dispatch({ type: "GLOSSARY_STOP_REQUESTED" });
    try {
      // The stop route never cancels the job (unlike /cancel): the pass stops
      // before its next batch, the job stays CREATED, and the stream reports
      // the end.
      await stopGlossaryExtraction(baseUrl, token, state.jobId);
    } catch (err) {
      // The pass may still be running (or just ended — the stream then
      // reports it): re-enable the button instead of leaving it disabled.
      dispatch({ type: "GLOSSARY_STOP_FAILED", message: String(err) });
    }
  }

  function handleGlossaryEntryChange(index: number, next: GlossaryEntry) {
    const entries = state.glossary.map((entry, i) => (i === index ? next : entry));
    dispatch({ type: "GLOSSARY_CHANGED", entries });
  }

  /** "save" or "lock": the decision (what to send, when to lock) is the
   * tested `saveGlossary`; this only wires the API calls. */
  async function handleSaveGlossary(mode: "save" | "lock") {
    if (!state.jobId) return;
    const jobId = state.jobId;
    await saveGlossary(state, mode, {
      put: (entries) => putGlossary(baseUrl, token, jobId, entries),
      loadReview: () => reloadGlossaryReview(jobId),
      dispatch,
    });
  }

  function startWatching(jobId: string) {
    subscriptionRef.current?.close();
    subscriptionRef.current = subscribeToJobEvents(baseUrl, token, jobId, dispatch);
  }

  async function handleStartRun() {
    if (!state.jobId) return;
    try {
      const job = await runJob(baseUrl, token, state.jobId);
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
    if (!resumeJobId) return;
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
      const job = await resumeJob(baseUrl, token, resumeJobId);
      dispatch({ type: "RESUME_JOB", job });
      startWatching(resumeJobId);
    } catch (err) {
      dispatch({ type: "RUN_START_FAILED", message: String(err) });
    }
  }

  const pendingGlossaryChanges = state.glossaryReview
    ? glossaryChangesToSave(state.glossaryReview, state.glossary).length
    : 0;
  const bestEffortSummary = state.job
    ? formatBestEffortSummary(state.job.best_effort_count)
    : null;
  const failureSummary = state.job
    ? formatFailureSummary(state.job.failed_count, state.job.total_chunks)
    : null;
  const outputLocation = state.job
    ? formatOutputLocation(state.job.status, state.job.out_path, state.runError)
    : null;

  return (
    <section>
      {state.screen === "pick" && (
        <div>
          <h2>1. Elegir un archivo</h2>
          <input
            type="text"
            placeholder="Ruta al archivo .epub o .srt"
            value={state.filePath}
            onChange={(e) =>
              dispatch({ type: "FILE_PATH_CHANGED", path: e.target.value })
            }
          />
          {state.fileError && <p role="alert">{state.fileError}</p>}
          <input
            type="text"
            placeholder="Modelo"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
          <button
            onClick={() => void handlePick()}
            disabled={!state.filePath || !!state.fileError}
          >
            Crear trabajo y estimar
          </button>
          {state.createError && <p role="alert">{state.createError}</p>}

          <h3>Reanudar un trabajo interrumpido</h3>
          <input
            type="text"
            placeholder="Id de un trabajo existente"
            value={resumeJobId}
            onChange={(e) => setResumeJobId(e.target.value)}
          />
          <button onClick={() => void handleResume()} disabled={!resumeJobId}>
            Reanudar
          </button>
        </div>
      )}

      {state.screen === "estimate" && state.estimate && (
        <EstimateSummary
          estimate={state.estimate}
          preparationMessage={state.preparationMessage}
          onStart={() => void handlePrepareGlossary()}
          onContinue={() => void handleProceedToGlossary()}
        />
      )}

      {state.screen === "preparing" && (
        <div>
          <h2>Preparando el glosario</h2>
          <p>
            {state.glossaryProgress
              ? formatGlossaryProgress(state.glossaryProgress)
              : "Iniciando…"}
          </p>
          <button onClick={() => void handleStopPreparation()} disabled={state.stopRequested}>
            {state.stopRequested ? "Deteniendo… (se aplica entre lotes)" : "Detener"}
          </button>
          {state.stopError && <p role="alert">{state.stopError}</p>}
        </div>
      )}

      {state.screen === "glossary" && (
        <div>
          <h2>3. Glosario (se bloquea antes de traducir)</h2>
          {state.glossaryNotice && <p>{state.glossaryNotice}</p>}
          {state.glossaryReviewError && <p role="alert">{state.glossaryReviewError}</p>}
          {state.glossaryReview && (
            <GlossaryTriage
              review={state.glossaryReview}
              glossary={state.glossary}
              disabled={state.glossaryLocked}
              onEdit={(index, patch) => dispatch({ type: "GLOSSARY_ENTRY_EDITED", index, patch })}
            />
          )}
          <ul>
            {/* With a review, only the entries added here are listed (the
                reviewed ones are in the triage above); without one, every
                entry is, as a plain list. */}
            {state.glossary.map((entry, index) =>
              index < (state.glossaryReview?.entries.length ?? 0) ? null : (
              <li key={index}>
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
              ),
            )}
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
            Agregar entrada
          </button>
          {!state.glossaryLocked && pendingGlossaryChanges > 0 && state.glossaryReview && (
            <button onClick={() => void handleSaveGlossary("save")}>
              Guardar {pendingGlossaryChanges === 1 ? "1 cambio" : `${pendingGlossaryChanges} cambios`}{" "}
              y actualizar la revisión
            </button>
          )}
          {!state.glossaryLocked && (
            <button onClick={() => void handleSaveGlossary("lock")}>
              Bloquear glosario y continuar
            </button>
          )}
          {state.glossarySaveError && <p role="alert">{state.glossarySaveError}</p>}
          <NarratorReview
            chapters={state.narratorChapters}
            loadError={state.narratorLoadError}
            draft={state.narratorDraft}
            bulk={state.narratorBulk}
            saving={state.narratorSaving}
            hasChanges={narratorsUnsaved}
            saveError={state.narratorSaveError}
            onBulkChange={(form) => dispatch({ type: "NARRATOR_BULK_CHANGED", form })}
            onApplyToFirstPerson={() => dispatch({ type: "NARRATORS_APPLIED_TO_FIRST_PERSON" })}
            onChapterChange={(key, narrator) =>
              dispatch({ type: "NARRATOR_CHAPTER_EDITED", key, narrator })
            }
            onApplyOnward={(key) => dispatch({ type: "NARRATOR_APPLIED_ONWARD", key })}
            onSave={() => void handleSaveNarrators()}
          />
          {state.glossaryLocked && (
            <div>
              <button onClick={() => void handleStartRun()} disabled={narratorsUnsaved}>
                Iniciar traducción
              </button>
              {narratorsUnsaved && (
                <p>Guarda los narradores antes de iniciar la traducción.</p>
              )}
              {state.runError && <p role="alert">{state.runError}</p>}
            </div>
          )}
        </div>
      )}

      {state.screen === "run" && (
        <div>
          <h2>4. Traduciendo</h2>
          <p>{state.progress ? formatProgress(state.progress) : "Iniciando…"}</p>
          <button onClick={() => void handleCancel()} disabled={state.cancelRequested}>
            {state.cancelRequested ? "Cancelando… (se aplica entre fragmentos)" : "Cancelar"}
          </button>
        </div>
      )}

      {state.screen === "error" && (
        <div>
          <h2>Se perdió la conexión</h2>
          <p role="alert">{state.connectionError}</p>
          <button onClick={handleRecoverFromError}>Volver al inicio</button>
        </div>
      )}

      {state.screen === "done" && (
        <div>
          <h2>{failureSummary ? "5. Terminado con errores" : "5. Listo"}</h2>
          <p>Estado: {state.job ? JOB_STATUS_LABELS[state.job.status] : "desconocido"}</p>
          {/* Error-surfacing: a run can end status=DONE at $0 while every
              chunk FAILED (provider auth/model error) and the output holds
              untranslated source text. This alert makes that the headline
              instead of a silent success. */}
          {failureSummary && <p role="alert">⚠ {failureSummary}</p>}
          {state.runError && <p role="alert">{state.runError}</p>}
          {bestEffortSummary && <p>{bestEffortSummary}</p>}
          {outputLocation && <p>{outputLocation}</p>}
        </div>
      )}
    </section>
  );
}
