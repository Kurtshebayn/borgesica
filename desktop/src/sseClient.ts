/**
 * `fetch`-stream SSE consumer for a single job's progress stream, with a
 * status-polling fallback on connection drop (spec: "Reconnection after drop"
 * — must resume via a status poll without restarting the job).
 *
 * Why not native `EventSource`: it cannot set request headers, which forced
 * the auth token into a `?token=` query parameter (RISK-001/002/003). Reading
 * the stream with `fetch` + a `ReadableStream` reader lets the token ride the
 * `X-Borgesica-Token` header like every other route, so it never appears in a
 * URL. The frame parsing and action mapping it delegates to (`extractSseData`,
 * `parseSseData`, `parseGlossarySseData`, `sseEventToAction`,
 * `isRunningStatus`) are pure and covered by `wizard.test.ts`; the
 * subscriptions themselves are exercised against a stubbed `fetch` in
 * `sseClient.test.ts`.
 *
 * A glossary preparation is watched by its own subscription
 * (`subscribeToGlossaryExtraction`): the job stays CREATED throughout, so the
 * run's status-polling fallback cannot tell how the pass is going. It
 * reconnects to the stream instead, which replays the pass's end.
 */
import { getEstimate, getGlossary, getStatus } from "./apiClient";
import type { EstimateResponse, GlossaryEntry, JobStatus } from "./apiTypes";
import {
  extractSseData,
  glossaryResultFromEvent,
  isPersistentConnectionFailure,
  isRunningStatus,
  parseGlossarySseData,
  parseSseData,
  sseEventToAction,
  type GlossaryExtractionResult,
  type WizardAction,
} from "./wizard";

const POLL_INTERVAL_MS = 2000;
const TOKEN_HEADER = "X-Borgesica-Token";

export interface SseSubscription {
  close(): void;
}

/** Reads job_id's event stream frame by frame until *onFrame* returns true
 * (a terminal event: the stream is then aborted) or the server ends it.
 * Rejects when the stream cannot be opened (non-OK or bodiless response) or
 * drops — callers treat that like an early end. */
async function readSseFrames(
  baseUrl: string,
  token: string,
  jobId: string,
  controller: AbortController,
  onFrame: (frame: string) => boolean,
): Promise<"terminal" | "ended"> {
  const resp = await fetch(`${baseUrl}/jobs/${jobId}/events`, {
    headers: { [TOKEN_HEADER]: token },
    signal: controller.signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`event stream unavailable (HTTP ${resp.status})`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return "ended";
    buffer += decoder.decode(value, { stream: true });
    // Events are separated by a blank line (server emits "\n\n"). Process
    // every complete frame; keep the trailing partial in the buffer.
    let sep = buffer.indexOf("\n\n");
    while (sep !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      if (onFrame(frame)) {
        controller.abort(); // stop the stream once terminal is seen
        return "terminal";
      }
      sep = buffer.indexOf("\n\n");
    }
  }
}

export function subscribeToJobEvents(
  baseUrl: string,
  token: string,
  jobId: string,
  dispatch: (action: WizardAction) => void,
): SseSubscription {
  let closed = false;
  let terminalSeen = false;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let consecutiveFailures = 0;
  const controller = new AbortController();

  function stopPolling(): void {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  /** On a terminal SSE event, re-fetch GET /jobs/{id} so the done screen sees
   * the accurate failed_count/best_effort_count (the RUNNING run-ack that
   * seeded state.job carried zeros). The SSE event's own `error` string is
   * preserved — the status payload does not carry it. Falls back to a
   * status-less terminal dispatch if the fetch fails, so a done screen still
   * appears rather than the run hanging. */
  function finalizeTerminal(
    statusFromEvent: JobStatus,
    errorFromEvent: string | null,
  ): void {
    getStatus(baseUrl, token, jobId)
      .then((job) => {
        if (closed) return;
        dispatch({ type: "RUN_TERMINAL", status: job.status, error: errorFromEvent, job });
      })
      .catch(() => {
        if (closed) return;
        dispatch({ type: "RUN_TERMINAL", status: statusFromEvent, error: errorFromEvent });
      });
  }

  function startPolling(): void {
    if (closed || terminalSeen || pollTimer !== null) return;
    pollTimer = setInterval(() => {
      getStatus(baseUrl, token, jobId)
        .then((job) => {
          consecutiveFailures = 0;
          dispatch({
            type: "PROGRESS_RECEIVED",
            progress: {
              completed: job.completed_chunks,
              totalChunks: job.total_chunks,
              costUsd: job.cost_usd,
            },
          });
          if (!isRunningStatus(job.status)) {
            // Poll already holds the full job — pass it so failed/best-effort
            // counts reach the done screen (error-surfacing).
            terminalSeen = true;
            dispatch({ type: "RUN_TERMINAL", status: job.status, error: null, job });
            stopPolling();
          }
        })
        .catch(() => {
          // RES-001: a single failed poll is a transient network blip — keep
          // retrying silently. But if the sidecar has actually died, every
          // subsequent poll will keep failing forever with no user-visible
          // feedback (the dead end this fixes). Once the failure run is
          // judged persistent, stop looping and surface a recoverable error
          // instead of retrying forever.
          consecutiveFailures += 1;
          if (isPersistentConnectionFailure(consecutiveFailures)) {
            stopPolling();
            dispatch({
              type: "CONNECTION_LOST",
              message:
                "Se perdió la conexión con el motor de traducción. La traducción puede seguir en curso, pero esta app ya no puede comunicarse con ella.",
            });
          }
        });
    }, POLL_INTERVAL_MS);
  }

  /** Handle one complete SSE frame. Returns true once a terminal event was
   * seen, so the reader loop can stop. */
  function handleFrame(frame: string): boolean {
    const data = extractSseData(frame);
    if (data === null) return false; // heartbeat / comment — ignore
    const parsed = parseSseData(data);
    if (!parsed) return false; // malformed / unknown — ignore, keep streaming
    if (parsed.type === "terminal") {
      terminalSeen = true;
      // Re-fetch status so the done screen surfaces failed/best-effort chunks
      // (the count-less terminal frame is not dispatched directly).
      finalizeTerminal(parsed.status as JobStatus, parsed.error);
      return true;
    }
    dispatch(sseEventToAction(parsed));
    return false;
  }

  async function streamEvents(): Promise<void> {
    const outcome = await readSseFrames(baseUrl, token, jobId, controller, handleFrame);
    // The stream ended before a terminal event: reconnect via status polling
    // (spec: reconnection falls back to status polling), unless we're closing.
    if (outcome === "ended" && !closed && !terminalSeen) startPolling();
  }

  // A non-OK response (e.g. auth/404) or a bodiless response cannot be
  // streamed and rejects like a dropped connection — both fall back to
  // polling, which surfaces a recoverable error if the sidecar is truly
  // unreachable.
  streamEvents().catch(() => {
    // The fetch/stream errored (dropped connection, abort). If we aborted on
    // purpose (terminal seen or close()) there is nothing to do; otherwise
    // fall back to polling like EventSource's onerror did.
    if (!closed && !terminalSeen) startPolling();
  });

  return {
    close(): void {
      closed = true;
      controller.abort();
      stopPolling();
    },
  };
}

export interface GlossarySubscriptionOptions {
  /** Wait before reopening a stream that dropped or ended early. */
  reconnectDelayMs?: number;
}

/** Watches job_id's glossary preparation: dispatches each batch's progress
 * and, once the pass ends, one GLOSSARY_EXTRACTION_ENDED carrying what the
 * next screen needs — the glossary after "done", the re-fetched estimate
 * otherwise (cost so far and the extraction line both come from the job).
 *
 * A stream that drops or ends without the pass's end is reopened after
 * `reconnectDelayMs`: the server then streams the pass still in flight, or
 * replays how it ended. Consecutive attempts that bring no event count as
 * connection failures; once `isPersistentConnectionFailure` judges them
 * persistent, CONNECTION_LOST is dispatched instead of retrying forever. */
export function subscribeToGlossaryExtraction(
  baseUrl: string,
  token: string,
  jobId: string,
  dispatch: (action: WizardAction) => void,
  { reconnectDelayMs = POLL_INTERVAL_MS }: GlossarySubscriptionOptions = {},
): SseSubscription {
  let closed = false;
  let finished = false;
  let consecutiveFailures = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let controller = new AbortController();

  /** Fetch what the next screen shows, then end the preparation. A failed
   * fetch still ends it — the screen copes with a missing glossary or
   * estimate — so the wizard never stays on "preparing". */
  function finish(result: GlossaryExtractionResult): void {
    finished = true;
    const followUp: Promise<{ glossary?: GlossaryEntry[]; estimate?: EstimateResponse }> =
      result.status === "done"
        ? getGlossary(baseUrl, token, jobId).then(({ entries }) => ({ glossary: entries }))
        : getEstimate(baseUrl, token, jobId).then((estimate) => ({ estimate }));
    followUp
      .catch(() => ({}))
      .then((extra) => {
        if (closed) return;
        dispatch({ type: "GLOSSARY_EXTRACTION_ENDED", result, ...extra });
      });
  }

  function handleFrame(frame: string): boolean {
    const data = extractSseData(frame);
    if (data === null) return false; // heartbeat / comment — ignore
    const event = parseGlossarySseData(data);
    if (!event) return false; // malformed / unknown — ignore, keep streaming
    consecutiveFailures = 0;
    if (event.type === "glossary_progress") {
      dispatch({
        type: "GLOSSARY_PROGRESS_RECEIVED",
        progress: {
          batch: event.batch,
          totalBatches: event.total_batches,
          costUsd: event.cost_usd,
        },
      });
      return false;
    }
    finish(glossaryResultFromEvent(event));
    return true;
  }

  function reconnectLater(): void {
    if (closed || finished) return;
    consecutiveFailures += 1;
    if (isPersistentConnectionFailure(consecutiveFailures)) {
      finished = true;
      dispatch({
        type: "CONNECTION_LOST",
        message:
          "Se perdió la conexión con el motor de traducción durante la preparación del glosario. La preparación puede seguir en curso, pero esta app ya no puede comunicarse con ella.",
      });
      return;
    }
    retryTimer = setTimeout(connect, reconnectDelayMs);
  }

  function connect(): void {
    retryTimer = null;
    if (closed || finished) return;
    controller = new AbortController();
    readSseFrames(baseUrl, token, jobId, controller, handleFrame)
      .then((outcome) => {
        if (outcome === "ended") reconnectLater();
      })
      .catch(() => reconnectLater());
  }

  connect();

  return {
    close(): void {
      closed = true;
      controller.abort();
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
    },
  };
}
