import { Fragment, useEffect, useState } from "react";
import { acceptFailedChunk, getChunks, getNarrators, getStatus } from "./apiClient";
import type { ChunksResponse, JobResponse, NarratorChapter } from "./apiTypes";
import {
  COMPARE_PAGE_SIZE,
  KIND_LABELS,
  candidateWarning,
  explainFailure,
  chunkKind,
  displayNumber,
  nextOffset,
  offsetForChunk,
  pageSummary,
  parseInline,
  previousOffset,
  translationText,
} from "./compare";

interface Props {
  baseUrl: string;
  token: string;
  jobId: string;
  /** Shown above the pages; the job id when the caller has no title. */
  title?: string;
  onBack: () => void;
  /** Re-translate the failed chunks (opens the run screen and starts it). */
  onRetryFailed?: () => void;
}

/** Original beside translation, chunk by chunk, paged from the engine. */
export default function CompareScreen({ baseUrl, token, jobId, title, onBack, onRetryFailed }: Props) {
  const [job, setJob] = useState<JobResponse | null>(null);
  const [chapters, setChapters] = useState<NarratorChapter[]>([]);
  const [page, setPage] = useState<ChunksResponse | null>(null);
  const [offset, setOffset] = useState(0);
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepting, setAccepting] = useState<number | null>(null);
  // Bumped after keeping a candidate so the page and the counts are re-read.
  const [reload, setReload] = useState(0);

  async function keepCandidate(index: number) {
    setAccepting(index);
    setError(null);
    try {
      await acceptFailedChunk(baseUrl, token, jobId, index);
      setReload((n) => n + 1);
    } catch (err) {
      setError(`No se pudo usar esa versión: ${String(err)}`);
    } finally {
      setAccepting(null);
    }
  }

  useEffect(() => {
    let cancelled = false;
    getStatus(baseUrl, token, jobId)
      .then((j) => !cancelled && setJob(j))
      .catch((err) => !cancelled && setError(String(err)));
    // Chapters are a convenience: a job without them (subtitles) just has no index.
    getNarrators(baseUrl, token, jobId)
      .then((r) => !cancelled && setChapters(r.chapters))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [baseUrl, token, jobId, reload]);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getChunks(baseUrl, token, jobId, { offset, limit: COMPARE_PAGE_SIZE, onlyProblems })
      .then((p) => !cancelled && setPage(p))
      .catch((err) => !cancelled && setError(String(err)));
    return () => {
      cancelled = true;
    };
  }, [baseUrl, token, jobId, offset, onlyProblems, reload]);

  const chunks = page?.chunks ?? [];
  const summary = page ? pageSummary(page.total, page.offset, chunks.length) : null;
  const next = page ? nextOffset(page.total, offset, COMPARE_PAGE_SIZE) : null;
  const previous = previousOffset(offset, COMPARE_PAGE_SIZE);
  const problems = job ? job.failed_count + job.best_effort_count : 0;

  function toggleProblems(checked: boolean) {
    setOnlyProblems(checked);
    setOffset(0);
  }

  return (
    <section className="compare">
      <div className="compare-head">
        <button type="button" className="link" onClick={onBack}>
          ← Biblioteca
        </button>
        <div className="compare-head-main">
          <div>
          <p className="kicker">
            {job ? `${job.completed_chunks}/${job.total_chunks} fragmentos · $${job.cost_usd.toFixed(3)}` : "Cargando…"}
            {problems > 0 && <span className="flag"> · {problems} con problemas</span>}
          </p>
          <h2>{title ?? jobId}</h2>
          </div>
          {job && job.failed_count > 0 && onRetryFailed && (
            <div className="compare-retry">
              <button type="button" className="danger" onClick={onRetryFailed}>
                Reintentar fragmentos fallidos
              </button>
              <span className="hint">
                Solo se vuelven a traducir los {job.failed_count} fallidos (unos centavos).
              </span>
            </div>
          )}
        </div>
      </div>

      {error && <p role="alert">{error}</p>}

      <div className="compare-body">
        <aside className="compare-side">
          {chapters.length > 0 && !onlyProblems && (
            <>
              <h3>Capítulos</h3>
              <ol className="chapter-list">
                {chapters.map((c, i) => (
                  <li key={c.key}>
                    <button
                      type="button"
                      className="chapter"
                      data-active={c.first_chunk <= offset && offset <= c.last_chunk}
                      onClick={() => setOffset(offsetForChunk(c.first_chunk, COMPARE_PAGE_SIZE))}
                    >
                      <span>{String(i + 1).padStart(2, "0")}</span>
                      {c.title}
                    </button>
                  </li>
                ))}
              </ol>
            </>
          )}
          <label className="check">
            <input
              type="checkbox"
              checked={onlyProblems}
              onChange={(e) => toggleProblems(e.target.checked)}
            />
            Solo problemas
            {job && <span className="flag">{problems}</span>}
          </label>
        </aside>

        <div className="compare-main">
          <div className="compare-cols compare-cols-head">
            <span />
            <span>Original</span>
            <span>Traducción</span>
          </div>
          {page && chunks.length === 0 && (
            <p className="compare-empty">
              {onlyProblems ? "No hay fragmentos con problemas." : "Todavía no hay fragmentos."}
            </p>
          )}
          {chunks.map((c) => {
            const kind = chunkKind(c);
            const label = KIND_LABELS[kind];
            return (
              <Fragment key={c.index}>
              {kind === "failed" && (
                <div className="compare-notice" role="note">
                  <strong>Por qué falló el fragmento {displayNumber(c)}</strong>
                  <span>{explainFailure(c.failure_reasons)}</span>
                </div>
              )}
              <div className="compare-cols compare-row" data-kind={kind}>
                <span className="compare-num">{String(displayNumber(c)).padStart(2, "0")}</span>
                <p className="compare-source">
                  <InlineText text={c.source} />
                </p>
                <div className="compare-target">
                  {label && <span className="compare-tag">{label}</span>}
                  <p>
                    <InlineText text={translationText(c)} />
                  </p>
                  {kind === "failed" && c.candidate && (
                    <div className="compare-candidate">
                      <span className="compare-tag">VERSIÓN DEL MODELO · NO SE USÓ</span>
                      <p>
                        <InlineText text={c.candidate} />
                      </p>
                      {candidateWarning(c) && (
                        <p role="alert" className="compare-reason">
                          ⚠ {candidateWarning(c)}
                        </p>
                      )}
                      <button
                        type="button"
                        disabled={accepting !== null}
                        onClick={() => void keepCandidate(c.index)}
                      >
                        {accepting === c.index ? "Guardando…" : "Usar esta versión"}
                      </button>
                    </div>
                  )}
                </div>
              </div>
              </Fragment>
            );
          })}
          <div className="compare-foot">
            <span>
              {summary ? `Fragmentos ${summary.from}–${summary.to} de ${summary.total}` : ""}
            </span>
            <span className="pager">
              <button
                type="button"
                disabled={previous === null}
                onClick={() => previous !== null && setOffset(previous)}
              >
                ← Anterior
              </button>
              <button
                type="button"
                className="primary"
                disabled={next === null}
                onClick={() => next !== null && setOffset(next)}
              >
                Siguiente →
              </button>
            </span>
          </div>
        </div>
      </div>
    </section>
  );
}

/** Chunk text with its bold and italic shown as such; the rest of the markup is dropped. */
function InlineText({ text }: { text: string }) {
  return (
    <>
      {parseInline(text).map((s, i) => {
        if (s.bold && s.italic) return <strong key={i}><em>{s.text}</em></strong>;
        if (s.bold) return <strong key={i}>{s.text}</strong>;
        if (s.italic) return <em key={i}>{s.text}</em>;
        return s.text;
      })}
    </>
  );
}
