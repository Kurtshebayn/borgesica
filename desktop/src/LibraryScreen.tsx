import { useEffect, useState } from "react";
import { listJobs } from "./apiClient";
import type { JobListItem } from "./apiTypes";
import { librarySummary, nextSignature, toFichas, type Ficha } from "./library";

interface Props {
  baseUrl: string;
  token: string;
  onOpen: (ficha: Ficha) => void;
  onNew: () => void;
}

/** Every stored job as a catalogue card, plus a blank one for the next book. */
export default function LibraryScreen({ baseUrl, token, onOpen, onNew }: Props) {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listJobs(baseUrl, token)
      .then((r) => !cancelled && setJobs(r.jobs))
      .catch((err) => !cancelled && setError(String(err)));
    return () => {
      cancelled = true;
    };
  }, [baseUrl, token]);

  const fichas = jobs ? toFichas(jobs) : [];

  return (
    <section className="library">
      <header className="library-head">
        <div>
          <p className="kicker">{jobs ? librarySummary(fichas) : "Cargando…"}</p>
          <h2>Biblioteca</h2>
        </div>
        <button type="button" className="primary" onClick={onNew}>
          Traducir un libro →
        </button>
      </header>
      {error && <p role="alert">{error}</p>}
      {jobs && (
        <ul className="ficha-grid">
          {fichas.map((f) => (
            <li key={f.job.id}>
              <FichaCard ficha={f} onOpen={onOpen} />
            </li>
          ))}
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
        </ul>
      )}
    </section>
  );
}

function FichaCard({ ficha, onOpen }: { ficha: Ficha; onOpen: (f: Ficha) => void }) {
  const body = (
    <>
      <span className="ficha-top">
        <span>{ficha.signature}</span>
        <span className="ficha-status">{ficha.label}</span>
      </span>
      <span className="ficha-title">{ficha.job.title}</span>
      <span className="ficha-meta">{ficha.meta}</span>
      <span className="ficha-foot">
        <span>{ficha.footer}</span>
        {ficha.action === "resume" && <span className="ficha-cta">Reanudar</span>}
      </span>
    </>
  );
  if (!ficha.action) {
    return (
      <div className="ficha" data-status={ficha.status}>
        {body}
      </div>
    );
  }
  return (
    <button
      type="button"
      className="ficha"
      data-status={ficha.status}
      onClick={() => onOpen(ficha)}
    >
      {body}
    </button>
  );
}
