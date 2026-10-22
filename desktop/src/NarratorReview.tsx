import {
  chapterLabel,
  withGenderChoice,
  withName,
  type NarratorBulkForm,
  type NarratorDraft,
  type NarratorGenderChoice,
} from "./narrators";
import type { NarratorChapter, NarratorFact, NarratorGender } from "./apiTypes";

/**
 * Who the first-person "I" is, per chapter: a "write it once" form for the
 * chapters the server marked as first-person, then every chapter with its
 * own narrator (changeable, clearable, or applied from it onward). Display
 * only — the rules live in the tested `narrators.ts`, the draft in the wizard
 * reducer. Renders nothing for a job without chapters (subtitles) or while
 * the chapters load; a failed load is a short notice that blocks nothing.
 */
export default function NarratorReview({
  chapters,
  loadError,
  draft,
  bulk,
  saving,
  hasChanges,
  saveError,
  onBulkChange,
  onApplyToFirstPerson,
  onChapterChange,
  onApplyOnward,
  onSave,
}: {
  chapters: NarratorChapter[] | null;
  loadError: string | null;
  draft: NarratorDraft;
  bulk: NarratorBulkForm;
  saving: boolean;
  hasChanges: boolean;
  saveError: string | null;
  onBulkChange: (form: NarratorBulkForm) => void;
  onApplyToFirstPerson: () => void;
  onChapterChange: (key: string, narrator: NarratorFact | null) => void;
  onApplyOnward: (key: string) => void;
  onSave: () => void;
}) {
  if (loadError) return <p role="alert">{loadError}</p>;
  if (!chapters || chapters.length === 0) return null;
  const firstPersonCount = chapters.filter((c) => c.first_person).length;

  return (
    <div>
      <h3>Narrador en primera persona</h3>
      <p>
        En los capítulos narrados en primera persona, indicar quién es el «yo» y su género ayuda a
        que la traducción concuerde (por ejemplo, «estoy cansado» o «estoy cansada»).
      </p>
      {firstPersonCount > 0 && (
        <div>
          <label>
            Nombre del narrador (opcional):{" "}
            <input
              type="text"
              value={bulk.name}
              disabled={saving}
              onChange={(e) => onBulkChange({ ...bulk, name: e.target.value })}
            />
          </label>{" "}
          <label>
            Género:{" "}
            <select
              value={bulk.gender ?? ""}
              disabled={saving}
              onChange={(e) =>
                onBulkChange({ ...bulk, gender: (e.target.value || null) as NarratorGender | null })
              }
            >
              <option value="">Elegir…</option>
              <option value="masculine">Masculino</option>
              <option value="feminine">Femenino</option>
            </select>
          </label>{" "}
          <button onClick={onApplyToFirstPerson} disabled={saving || !bulk.gender}>
            Aplicar a {firstPersonCount === 1 ? "el capítulo" : `los ${firstPersonCount} capítulos`}{" "}
            en primera persona
          </button>
        </div>
      )}
      <p>
        «Sin narrador» es para los capítulos sin un «yo» del relato (por ejemplo, agradecimientos
        escritos por el autor).
      </p>
      <ol>
        {chapters.map((chapter) => (
          <ChapterRow
            key={chapter.key}
            chapter={chapter}
            narrator={draft[chapter.key] ?? null}
            disabled={saving}
            onChange={(narrator) => onChapterChange(chapter.key, narrator)}
            onApplyOnward={() => onApplyOnward(chapter.key)}
          />
        ))}
      </ol>
      {hasChanges && !saving && <p>Hay cambios sin guardar.</p>}
      <button onClick={onSave} disabled={saving || !hasChanges}>
        {saving ? "Guardando…" : "Guardar narradores"}
      </button>
      {saveError && <p role="alert">{saveError}</p>}
    </div>
  );
}

function ChapterRow({
  chapter,
  narrator,
  disabled,
  onChange,
  onApplyOnward,
}: {
  chapter: NarratorChapter;
  narrator: NarratorFact | null;
  disabled: boolean;
  onChange: (narrator: NarratorFact | null) => void;
  onApplyOnward: () => void;
}) {
  const label = chapterLabel(chapter);
  return (
    <li>
      <p>
        <strong>{label}</strong>
        {chapter.first_person && <> — Primera persona</>}
      </p>
      {chapter.opening && <p>«{chapter.opening}»</p>}
      <label>
        Narrador:{" "}
        <select
          aria-label={`Género del narrador de ${label}`}
          value={narrator?.gender ?? ""}
          disabled={disabled}
          onChange={(e) =>
            onChange(withGenderChoice(narrator, e.target.value as NarratorGenderChoice))
          }
        >
          <option value="">Sin narrador</option>
          <option value="masculine">Masculino</option>
          <option value="feminine">Femenino</option>
        </select>
      </label>{" "}
      <input
        type="text"
        aria-label={`Nombre del narrador de ${label}`}
        placeholder="Nombre (opcional)"
        value={narrator?.name ?? ""}
        disabled={disabled || !narrator}
        onChange={(e) => onChange(withName(narrator, e.target.value))}
      />
      {narrator && (
        <button onClick={onApplyOnward} disabled={disabled}>
          Aplicar desde aquí en adelante
        </button>
      )}
    </li>
  );
}
