import {
  describeGender,
  FLAG_TEXT,
  formatPronounEvidence,
  formatProposals,
  genderChoiceOf,
  genderChoices,
  genderFromChoice,
  showsGenderControl,
  triageGroups,
  type GenderChoice,
  type IndexedReviewEntry,
} from "./glossaryReview";
import type { GlossaryEntry, GlossaryReviewResponse } from "./apiTypes";

type EntryPatch = Partial<Pick<GlossaryEntry, "translation" | "gender" | "locked">>;

/**
 * The glossary triage list: entries grouped by what needs a look, each with
 * its flags, the renderings the extraction proposed and the gender evidence,
 * in plain words. Presentational only — grouping, wording and the save
 * payload live in the tested `glossaryReview.ts`; edits go up through
 * `onEdit` to the wizard reducer.
 */
export default function GlossaryTriage({
  review,
  glossary,
  disabled,
  onEdit,
}: {
  review: GlossaryReviewResponse;
  /** The editable entries, index-aligned with `review.entries`. */
  glossary: GlossaryEntry[];
  disabled: boolean;
  onEdit: (index: number, patch: EntryPatch) => void;
}) {
  return (
    <div>
      {triageGroups(review.entries).map(({ group, label, items }) => (
        // The long tail (no observations, weak signal) starts folded so the
        // entries that need a decision are what the reader sees first.
        <details key={group} open={group === "attention" || group === "gender"}>
          <summary>
            {label} ({items.length})
          </summary>
          <ul>
            {items.map(({ index, item }) => (
              <TriageEntry
                key={item.entry.term}
                indexed={{ index, item }}
                current={glossary[index] ?? item.entry}
                review={review}
                disabled={disabled}
                onEdit={onEdit}
              />
            ))}
          </ul>
        </details>
      ))}
    </div>
  );
}

function TriageEntry({
  indexed: { index, item },
  current,
  review,
  disabled,
  onEdit,
}: {
  indexed: IndexedReviewEntry;
  current: GlossaryEntry;
  review: GlossaryReviewResponse;
  disabled: boolean;
  onEdit: (index: number, patch: EntryPatch) => void;
}) {
  const proposals = formatProposals(item.proposals);
  return (
    <li>
      <p>
        <strong>{item.entry.term}</strong> →{" "}
        <input
          type="text"
          aria-label={`Traducción de ${item.entry.term}`}
          value={current.translation}
          disabled={disabled}
          onChange={(e) => onEdit(index, { translation: e.target.value })}
        />
      </p>
      {item.flags.map((flag) => (
        <p key={flag}>
          <strong>{FLAG_TEXT[flag].label}.</strong> {FLAG_TEXT[flag].explanation}
        </p>
      ))}
      {proposals && <p>Propuestas de la preparación: {proposals}</p>}
      {showsGenderControl(item.entry) && (
        <div>
          <label>
            Género:{" "}
            <select
              value={genderChoiceOf(current)}
              disabled={disabled}
              onChange={(e) =>
                onEdit(index, { gender: genderFromChoice(e.target.value as GenderChoice) })
              }
            >
              {genderChoices(item.entry).map(({ value, label }) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <p>{describeGender(item, review)}</p>
          <p>{formatPronounEvidence(item)}</p>
        </div>
      )}
      <label>
        <input
          type="checkbox"
          checked={current.locked}
          disabled={disabled}
          onChange={(e) => onEdit(index, { locked: e.target.checked })}
        />{" "}
        Fijar esta traducción (el proceso automático no la cambiará)
      </label>
    </li>
  );
}
