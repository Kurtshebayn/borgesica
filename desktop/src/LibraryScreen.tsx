import { useEffect, useState, type ReactNode } from "react";
import { deleteJob, listJobs } from "./apiClient";
import type { JobListItem } from "./apiTypes";
import {
  FICHA_FILTERS,
  filterCounts,
  filterFichas,
  librarySummary,
  nextSignature,
  toFichas,
  type Ficha,
  type FichaFilter,
} from "./library";
import ConfirmDialog from "./ConfirmDialog";

interface Props {
  baseUrl: string;
  token: string;
  /** What the provider button says, e.g. "anthropic · sin clave". */
  providerLabel: string;
  /** Rendered under the header (the provider/key panel when it is open). */
  panel?: ReactNode;
  onOpen: (ficha: Ficha) => void;
  onNew: () => void;
  onProvider: () => void;
}

/** Every stored job as a catalogue card, plus a blank one for the next book. */
export default function LibraryScreen({
  baseUrl,
  token,
  providerLabel,
  panel,
  onOpen,
  onNew,
  onProvider,
}: Props) {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FichaFilter>("all");
  const [toDelete, setToDelete] = useState<Ficha | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listJobs(baseUrl, token)
      .then((r) => !cancelled && setJobs(r.jobs))
      .catch((err) => !cancelled && setError(String(err)));
    return () => {
      cancelled = true;
    };
  }, [baseUrl, token]);

  // Signatures come from the whole library, so a filter never renumbers a ficha.
  const fichas = jobs ? toFichas(jobs) : [];
  const counts = filterCounts(fichas);
  const shown = filterFichas(fichas, filter);

  async function confirmDelete() {
    if (!toDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteJob(baseUrl, token, toDelete.job.id);
      setJobs((prev) => prev && prev.filter((j) => j.id !== toDelete.job.id));
      setToDelete(null);
    } catch (err) {
      setDeleteError(String(err));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <section className="library">
      <header className="library-head">
        <div>
          <p className="kicker">{jobs ? librarySummary(fichas) : "Cargando…"}</p>
          <h2>Biblioteca</h2>
        </div>
        <div className="library-actions">
          <button type="button" onClick={onProvider}>
            {providerLabel}
          </button>
          <button type="button" className="primary" onClick={onNew}>
            Traducir un libro →
          </button>
        </div>
      </header>
      {panel}
      {error && <p role="alert">{error}</p>}
      {jobs && (
        <>
          <div className="filters" role="group" aria-label="Filtrar por estado">
            {FICHA_FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                aria-pressed={filter === f.id}
                disabled={counts[f.id] === 0 && f.id !== "all"}
                onClick={() => setFilter(f.id)}
              >
                {f.label} <span>{counts[f.id]}</span>
              </button>
            ))}
          </div>
          <ul className="ficha-grid">
            {shown.map((f) => (
              <li key={f.job.id}>
                <FichaCard
                  ficha={f}
                  onOpen={onOpen}
                  onDelete={() => {
                    setDeleteError(null);
                    setToDelete(f);
                  }}
                />
              </li>
            ))}
            {filter === "all" && (
              <li>
                <button
                  type="button"
                  className="ficha ficha-blank"
                  aria-label="Traducir un libro nuevo"
                  onClick={onNew}
                >
                  <span className="ficha-plus">+</span>
                  <span>Ficha nueva</span>
                  <span className="ficha-sig">{nextSignature(jobs)}</span>
                </button>
              </li>
            )}
          </ul>
        </>
      )}
      {toDelete && (
        <ConfirmDialog
          title={`¿Borrar ${toDelete.signature}?`}
          confirmLabel="Borrar"
          busy={deleting}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onCancel={() => setToDelete(null)}
        >
          <p>
            «{toDelete.job.title}» se quita de la biblioteca junto con su glosario y su avance, y
            si su traducción está en la carpeta Biblioteca, ese archivo también se elimina.
          </p>
          <p>El libro original no se toca. Esto no se puede deshacer.</p>
        </ConfirmDialog>
      )}
    </section>
  );
}

function FichaCard({
  ficha,
  onOpen,
  onDelete,
}: {
  ficha: Ficha;
  onOpen: (f: Ficha) => void;
  onDelete: () => void;
}) {
  return (
    <div className="ficha-wrap">
      <button
        type="button"
        className="ficha"
        data-status={ficha.status}
        onClick={() => onOpen(ficha)}
      >
        <span className="ficha-top">
          <span>{ficha.signature}</span>
          <span className="ficha-status">{ficha.label}</span>
        </span>
        <span className="ficha-title">{ficha.job.title}</span>
        <span className="ficha-meta">{ficha.meta}</span>
        <span className="ficha-foot">
          <span>{ficha.footer}</span>
          {ficha.action === "resume" && (
            <span className="ficha-cta">{ficha.status === "draft" ? "Continuar" : "Reanudar"}</span>
          )}
        </span>
      </button>
      <button
        type="button"
        className="ficha-delete"
        aria-label={`Borrar ${ficha.signature}`}
        onClick={onDelete}
      >
        Borrar
      </button>
    </div>
  );
}
