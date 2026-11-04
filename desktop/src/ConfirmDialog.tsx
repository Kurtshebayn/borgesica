import { useEffect, useRef, type ReactNode } from "react";

interface Props {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  /** Disables both buttons while the action runs. */
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/** A modal "are you sure?" over the page, built on the native <dialog> so the
 * focus trap, Escape and the backdrop come with it. Cancel is the default. */
export default function ConfirmDialog({
  title,
  children,
  confirmLabel,
  busy,
  error,
  onConfirm,
  onCancel,
}: Props) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  return (
    <dialog
      ref={ref}
      className="confirm"
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <h2>{title}</h2>
      <div className="confirm-body">{children}</div>
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button type="button" autoFocus disabled={busy} onClick={onCancel}>
          Cancelar
        </button>
        <button type="button" className="danger" disabled={busy} onClick={onConfirm}>
          {busy ? "Borrando…" : confirmLabel}
        </button>
      </div>
    </dialog>
  );
}
