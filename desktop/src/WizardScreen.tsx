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
  updateJobModel,
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
  formatBookEstimate,
  formatFailureSummary,
  formatOutputLocation,
  formatGlossaryProgress,
  formatProgress,
  initialWizardState,
  loadGlossaryReview,
  loadNarrators,
  modelsForProvider,
  runRecoveryAction,
  saveGlossary,
  saveNarrators,
  wizardReducer,
  type Provider,
  type WizardState,
} from "./wizard";
import { glossaryChangesToSave } from "./glossaryReview";
import { hasNarratorChanges } from "./narrators";
import { pickSourceFile } from "./filePicker";
import EstimateSummary from "./EstimateSummary";
import GlossaryTriage from "./GlossaryTriage";
import NarratorReview from "./NarratorReview";
import type { GlossaryEntry, JobResponse, JobStatus } from "./apiTypes";

const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  CREATED: "creado",
  ESTIMATING: "estimando",
  RUNNING: "en curso",
  PAUSED: "pausado",
  DONE: "listo",
  FAILED: "con errores",
  CANCELLED: "cancelado",
};

const STEP_LABELS = ["Archivo", "Estimación", "Glosario", "Traducción", "Listo"];

/** The steps of the bar that can be looked at again while a resume waits. */
const PEEK_OF_STEP: Record<number, "estimate" | "glossary" | undefined> = {
  1: "estimate",
  2: "glossary",
};
const PEEK_STEP = { estimate: 1, glossary: 2 } as const;

/** Which step of the bar each screen belongs to. */
const STEP_OF_SCREEN: Record<WizardState["screen"], number> = {
  pick: 0,
  estimate: 1,
  preparing: 2,
  glossary: 2,
  run: 3,
  error: 3,
  done: 4,
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
  resumeJobId,
  resumeDraft,
  onChangeProvider,
}: {
  /** Asks the app to swap the provider (restarting the engine) and come back
   * to this draft's estimate. Offered only while the draft has not started. */
  onChangeProvider?: (jobId: string) => void;
  /** An interrupted job (chosen in the library) to pick up on mount. */
  resumeJobId?: string;
  /** The job never ran: pick it up at its estimate instead of resuming a run. */
  resumeDraft?: boolean;
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
  const subscriptionRef = useRef<SseSubscription | null>(null);
  const resumedRef = useRef(false);
  // A stopped run picked from the library waits here for an explicit
  // "Reanudar traducción": resuming spends money, so a click on the ficha
  // alone must not start it.
  const [pendingResume, setPendingResume] = useState<JobResponse | null>(null);
  // While that confirmation waits, the earlier steps can be looked at again
  // (read-only: nothing there spends money or changes the job).
  const [peek, setPeek] = useState<"estimate" | "glossary" | null>(null);
  const [peekGlossary, setPeekGlossary] = useState<GlossaryEntry[] | null>(null);
  const [peekError, setPeekError] = useState<string | null>(null);
  const [modelBusy, setModelBusy] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      subscriptionRef.current?.close();
    };
  }, []);

  // Resuming starts a paid run, so it happens once per mount even when the
  // dev build mounts effects twice.
  useEffect(() => {
    if (!resumeJobId || resumedRef.current) return;
    resumedRef.current = true;
    void handleResume(resumeJobId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeJobId]);

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

  async function handleChooseFile() {
    try {
      const path = await pickSourceFile();
      if (path) dispatch({ type: "FILE_PATH_CHANGED", path });
    } catch {
      // No native dialog (plain browser, dev): the path field still works.
    }
  }

  async function handleResume(jobId: string) {
    try {
      if (resumeDraft) {
        dispatch({ type: "JOB_CREATED", jobId });
        try {
          // The draft keeps the model it was made with; after a provider
          // change that model belongs to the old provider, so it is swapped
          // for this provider's default before anything is priced.
          const draft = await getStatus(baseUrl, token, jobId);
          const options = modelsForProvider(provider);
          if (options.length > 0 && !options.includes(draft.model)) {
            const fallback = defaultModelForProvider(provider);
            await updateJobModel(baseUrl, token, jobId, fallback);
            setModel(fallback);
          } else {
            setModel(draft.model);
          }
          dispatch({ type: "ESTIMATE_RECEIVED", estimate: await getEstimate(baseUrl, token, jobId) });
        } catch (err) {
          dispatch({ type: "ESTIMATE_FAILED", message: String(err) });
        }
        return;
      }
      const status = await getStatus(baseUrl, token, jobId);
      const estimate = await getEstimate(baseUrl, token, jobId).catch(
        () => null,
      );
      if (estimate) dispatch({ type: "ESTIMATE_RECEIVED", estimate });
      if (status.status === "DONE") {
        dispatch({ type: "RESUME_JOB", job: status });
        return;
      }
      setPendingResume(status);
    } catch (err) {
      dispatch({ type: "RUN_START_FAILED", message: String(err) });
    }
  }

  async function handlePeek(target: "estimate" | "glossary" | null) {
    setPeek(target);
    if (target !== "glossary" || peekGlossary || !pendingResume) return;
    setPeekError(null);
    try {
      setPeekGlossary((await getGlossary(baseUrl, token, pendingResume.id)).entries);
    } catch (err) {
      setPeekError(String(err));
    }
  }

  /** A draft's model can change until it starts: the engine stores it, and
   * the estimate is asked again because every figure follows the model. */
  async function handleModelChange(next: string) {
    if (!state.jobId || next.trim() === "" || next === model) return;
    const jobId = state.jobId;
    const previous = model;
    setModel(next);
    setModelBusy(true);
    setModelError(null);
    try {
      await updateJobModel(baseUrl, token, jobId, next);
      dispatch({ type: "ESTIMATE_RECEIVED", estimate: await getEstimate(baseUrl, token, jobId) });
    } catch (err) {
      setModel(previous);
      setModelError(`No se pudo cambiar el modelo: ${String(err)}`);
    } finally {
      setModelBusy(false);
    }
  }

  async function handleConfirmResume() {
    if (!pendingResume) return;
    const jobId = pendingResume.id;
    setPendingResume(null);
    try {
      const job = await resumeJob(baseUrl, token, jobId);
      dispatch({ type: "RESUME_JOB", job });
      startWatching(jobId);
    } catch (err) {
      dispatch({ type: "RUN_START_FAILED", message: String(err) });
    }
  }

  const currentStep = pendingResume ? STEP_OF_SCREEN.run : STEP_OF_SCREEN[state.screen];
  const modelOptions = modelsForProvider(provider);
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
    <section className="wizard">
      <ol className="steps" aria-label="Proceso">
        {STEP_LABELS.map((label, i) => {
          // Only the steps already behind a waiting resume can be revisited.
          const target = pendingResume ? PEEK_OF_STEP[i] : undefined;
          const here = pendingResume ? (peek ? PEEK_STEP[peek] : currentStep) === i : i === currentStep;
          return (
            <li
              key={label}
              data-state={here ? "current" : i < currentStep ? "done" : "todo"}
              aria-current={here ? "step" : undefined}
            >
              <span className="step-num">{String(i + 1).padStart(2, "0")}</span>
              {target ? (
                <button type="button" className="step-name step-link" onClick={() => void handlePeek(target)}>
                  {label}
                </button>
              ) : pendingResume && i === currentStep ? (
                <button type="button" className="step-name step-link" onClick={() => void handlePeek(null)}>
                  {label}
                </button>
              ) : (
                <span className="step-name">{label}</span>
              )}
              {here && <span className="step-here" aria-hidden="true" />}
            </li>
          );
        })}
      </ol>
      {pendingResume && state.screen === "pick" && peek === "estimate" && (
        <div>
          <h2>Estimación</h2>
          {state.estimate ? (
            <>
              <p>{formatBookEstimate(state.estimate)}</p>
              {!state.estimate.within_budget && (
                <p role="alert">Esta estimación supera el presupuesto configurado.</p>
              )}
            </>
          ) : (
            <p>No hay estimación disponible para este libro.</p>
          )}
          <button onClick={() => void handlePeek(null)}>← Volver a reanudar</button>
        </div>
      )}
      {pendingResume && state.screen === "pick" && peek === "glossary" && (
        <div>
          <h2>Glosario</h2>
          {peekError && <p role="alert">{peekError}</p>}
          {!peekGlossary && !peekError && <p>Cargando…</p>}
          {peekGlossary && peekGlossary.length === 0 && <p>
              Este libro no tiene glosario. El glosario solo se prepara antes de empezar a
              traducir; con la traducción ya empezada no se puede crear sin dejar el principio y
              el resto del libro con términos distintos.
            </p>}
          {peekGlossary && peekGlossary.length > 0 && (
            <ul className="peek-glossary">
              {peekGlossary.map((entry, i) => (
                <li key={i}>
                  <span>{entry.term}</span> → <span>{entry.translation}</span>
                </li>
              ))}
            </ul>
          )}
          <button onClick={() => void handlePeek(null)}>← Volver a reanudar</button>
        </div>
      )}
      {pendingResume && state.screen === "pick" && !peek && (
        <div>
          <h2>Reanudar la traducción</h2>
          <p>
            Esta traducción quedó a medias: {pendingResume.completed_chunks} de{" "}
            {pendingResume.total_chunks} fragmentos hechos, ${pendingResume.cost_usd.toFixed(3)} gastados.
            Al reanudar se vuelve a llamar a {provider} y se paga lo que falta.
          </p>
          <div className="actions">
            <button className="primary" onClick={() => void handleConfirmResume()}>
              Reanudar traducción
            </button>
            <button onClick={() => void handlePeek("estimate")}>Ver estimación</button>
            <button onClick={() => void handlePeek("glossary")}>Ver glosario</button>
          </div>
        </div>
      )}

      {state.screen === "pick" && !pendingResume && (
        <div>
          <h2>¿Qué libro vamos a traducir?</h2>
          <div className="file-row">
            <input
              type="text"
              placeholder="Ruta al archivo .epub o .srt"
              value={state.filePath}
              onChange={(e) =>
                dispatch({ type: "FILE_PATH_CHANGED", path: e.target.value })
              }
            />
            <button type="button" onClick={() => void handleChooseFile()}>
              Elegir archivo
            </button>
          </div>
          {state.fileError && <p role="alert">{state.fileError}</p>}
          <label>
            Modelo
            {modelOptions.length > 0 ? (
              <select value={model} onChange={(e) => setModel(e.target.value)}>
                {modelOptions.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                placeholder="Modelo local, p. ej. llama3"
                value={model}
                onChange={(e) => setModel(e.target.value)}
              />
            )}
          </label>
          <button
            className="primary"
            onClick={() => void handlePick()}
            disabled={!state.filePath || !!state.fileError}
          >
            Crear trabajo y estimar
          </button>
          {state.createError && <p role="alert">{state.createError}</p>}
        </div>
      )}

      {state.screen === "estimate" && state.estimate && (
        <EstimateSummary
          settings={
            <div className="estimate-settings">
              <label>
                Modelo
                {modelOptions.length > 0 ? (
                  <select
                    value={model}
                    disabled={modelBusy}
                    onChange={(e) => void handleModelChange(e.target.value)}
                  >
                    {!modelOptions.includes(model) && <option value={model}>{model}</option>}
                    {modelOptions.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    defaultValue={model}
                    disabled={modelBusy}
                    onBlur={(e) => void handleModelChange(e.target.value)}
                  />
                )}
              </label>
              {onChangeProvider && state.jobId && (
                <div>
                  <span>Proveedor: {provider}</span>
                  <button type="button" onClick={() => onChangeProvider(state.jobId!)}>
                    Cambiar proveedor
                  </button>
                </div>
              )}
              {modelError && <p role="alert">{modelError}</p>}
            </div>
          }
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
          <h2>Glosario</h2>
          <p className="hint">Se bloquea antes de traducir.</p>
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
            <button className="primary" onClick={() => void handleSaveGlossary("lock")}>
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
              <button className="primary" onClick={() => void handleStartRun()} disabled={narratorsUnsaved}>
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
          <h2>Traduciendo</h2>
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
          <h2>{failureSummary ? "Terminado con errores" : "Listo"}</h2>
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
