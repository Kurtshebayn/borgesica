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
            dispatch({ type: "RUN_TERMINAL", status: job.status, error: null });
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
      dispatch(sseEventToAction(parsed));
      if (parsed.type === "terminal") {
        source?.close();
        source = null;
      }
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
