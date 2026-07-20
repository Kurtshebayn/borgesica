/**
 * `EventSource`-based SSE consumer (design decision #13) for a single job's
 * progress stream, with a status-polling fallback on connection drop
 * (spec: "Reconnection after drop" — must resume via a status poll without
 * restarting the job). Untested by design: this module is pure DOM/network
 * glue; the event parsing and action mapping it delegates to
 * (`parseSseData`, `sseEventToAction`, `isRunningStatus`) are pure and
 * covered by `wizard.test.ts`.
 */
import { getStatus } from "./apiClient";
import type { JobStatus } from "./apiTypes";
import {
  isPersistentConnectionFailure,
  isRunningStatus,
  parseSseData,
  sseEventToAction,
  type WizardAction,
} from "./wizard";

const POLL_INTERVAL_MS = 2000;

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
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let consecutiveFailures = 0;
  // RISK-001/002/003: native EventSource cannot set custom request headers,
  // so the auth token travels as a query parameter for this one read-only
  // route (documented deviation — every mutating route below/elsewhere
  // requires the X-Borgesica-Token header exclusively).
  let source: EventSource | null = new EventSource(
    `${baseUrl}/jobs/${jobId}/events?token=${encodeURIComponent(token)}`,
  );

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
    if (closed || pollTimer !== null) return;
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
                "Lost connection to the translation sidecar. The run may still be in progress, but this app can no longer reach it.",
            });
          }
        });
    }, POLL_INTERVAL_MS);
  }

  if (source) {
    source.onmessage = (event: MessageEvent<string>) => {
      const parsed = parseSseData(event.data);
      if (!parsed) return;
      if (parsed.type === "terminal") {
        source?.close();
        source = null;
        // Do NOT dispatch the count-less terminal event directly — re-fetch
        // status first so the done screen surfaces failed/best-effort chunks.
        finalizeTerminal(parsed.status as JobStatus, parsed.error);
        return;
      }
      dispatch(sseEventToAction(parsed));
    };
    source.onerror = () => {
      // The stream dropped before a terminal event arrived. Do not rely on
      // EventSource's own auto-reconnect (it would re-open the SSE
      // handshake against a job that may already have finished) — fall
      // back to polling GET /jobs/{id} instead (spec: reconnection falls
      // back to status polling).
      source?.close();
      source = null;
      startPolling();
    };
  }

  return {
    close(): void {
      closed = true;
      source?.close();
      source = null;
      stopPolling();
    },
  };
}
