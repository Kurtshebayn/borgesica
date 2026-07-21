/**
 * `fetch`-stream SSE consumer for a single job's progress stream, with a
 * status-polling fallback on connection drop (spec: "Reconnection after drop"
 * — must resume via a status poll without restarting the job).
 *
 * Why not native `EventSource`: it cannot set request headers, which forced
 * the auth token into a `?token=` query parameter (RISK-001/002/003). Reading
 * the stream with `fetch` + a `ReadableStream` reader lets the token ride the
 * `X-Borgesica-Token` header like every other route, so it never appears in a
 * URL. Untested by design: this module is pure DOM/network glue; the frame
 * parsing and action mapping it delegates to (`extractSseData`, `parseSseData`,
 * `sseEventToAction`, `isRunningStatus`) are pure and covered by
 * `wizard.test.ts`.
 */
import { getStatus } from "./apiClient";
import type { JobStatus } from "./apiTypes";
import {
  extractSseData,
  isPersistentConnectionFailure,
  isRunningStatus,
  parseSseData,
  sseEventToAction,
  type WizardAction,
} from "./wizard";

const POLL_INTERVAL_MS = 2000;
const TOKEN_HEADER = "X-Borgesica-Token";

export interface SseSubscription {
  close(): void;
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
              chunkIndex: job.completed_chunks,
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
    const resp = await fetch(`${baseUrl}/jobs/${jobId}/events`, {
      headers: { [TOKEN_HEADER]: token },
      signal: controller.signal,
    });
    // A non-OK response (e.g. auth/404) or a bodiless response cannot be
    // streamed — fall back to polling, which surfaces a recoverable error if
    // the sidecar is truly unreachable.
    if (!resp.ok || !resp.body) {
      startPolling();
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Events are separated by a blank line (server emits "\n\n"). Process
      // every complete frame; keep the trailing partial in the buffer.
      let sep = buffer.indexOf("\n\n");
      while (sep !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        if (handleFrame(frame)) {
          controller.abort(); // stop the stream once terminal is seen
          return;
        }
        sep = buffer.indexOf("\n\n");
      }
    }
    // The stream ended before a terminal event: reconnect via status polling
    // (spec: reconnection falls back to status polling), unless we're closing.
    if (!closed && !terminalSeen) startPolling();
  }

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
