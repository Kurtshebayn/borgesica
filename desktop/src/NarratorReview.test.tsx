import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import NarratorReview from "./NarratorReview";
import type { NarratorChapter, NarratorFact } from "./apiTypes";
import type { NarratorDraft } from "./narrators";

function chapter(
  key: string,
  title: string,
  firstPerson: boolean,
  narrator: NarratorFact | null = null,
): NarratorChapter {
  return {
    key,
    title,
    opening: `Opening of ${title}`,
    first_chunk: 0,
    last_chunk: 0,
    first_person: firstPerson,
    first_person_per_1k: firstPerson ? 30 : 0,
    narrator,
  };
}

const vis: NarratorFact = { name: "Vis", gender: "masculine" };
const chapters = [
  chapter("ack.xhtml", "Agradecimientos", true),
  chapter("ch1.xhtml", "Capítulo uno", true, vis),
  chapter("ch2.xhtml", "Capítulo dos", false),
];
const draft: NarratorDraft = { "ack.xhtml": null, "ch1.xhtml": vis, "ch2.xhtml": null };
const noop = () => {};

function render(overrides: Partial<Parameters<typeof NarratorReview>[0]> = {}) {
  return renderToStaticMarkup(
    <NarratorReview
      chapters={chapters}
      loadError={null}
      draft={draft}
      bulk={{ name: "", gender: null }}
      saving={false}
      hasChanges={false}
      saveError={null}
      onBulkChange={noop}
      onApplyToFirstPerson={noop}
      onChapterChange={noop}
      onApplyOnward={noop}
      onSave={noop}
      {...overrides}
    />,
  );
}

describe("NarratorReview", () => {
  it("is not shown at all for a job without chapters (subtitles)", () => {
    expect(render({ chapters: [] })).toBe("");
  });

  it("is not shown while the chapters are still loading", () => {
    expect(render({ chapters: null })).toBe("");
  });

  it("a failed load shows only a short error", () => {
    const html = render({ chapters: null, loadError: "No se pudieron cargar: HTTP 500" });
    expect(html).toContain('role="alert"');
    expect(html).toContain("HTTP 500");
    expect(html).not.toContain("Agradecimientos");
  });

  it("lists every chapter with its title and opening, marking the first-person ones", () => {
    const html = render();
    for (const title of ["Agradecimientos", "Capítulo uno", "Capítulo dos"]) {
      expect(html).toContain(title);
      expect(html).toContain(`Opening of ${title}`);
    }
    expect(html.match(/Primera persona/g)).toHaveLength(2);
  });

  it("offers to write the narrator once for the first-person chapters, once a gender is chosen", () => {
    const blank = render();
    expect(blank).toContain("Aplicar a los 2 capítulos en primera persona");
    expect(blank).toMatch(/<button[^>]*disabled=""[^>]*>Aplicar a los 2/);
    const chosen = render({ bulk: { name: "Vis", gender: "masculine" } });
    expect(chosen).not.toMatch(/<button[^>]*disabled=""[^>]*>Aplicar a los 2/);
  });

  it("shows each chapter's narrator, with no narrator as an explicit choice", () => {
    const html = render();
    expect(html).toContain('value="Vis"');
    expect(html).toContain("Sin narrador");
    // ch1 has a masculine narrator; the others have none selected.
    expect(html.match(/<option value="masculine" selected="">/g)).toHaveLength(1);
    expect(html.match(/<option value="" selected="">Sin narrador/g)).toHaveLength(2);
  });

  it("offers 'from here onward' only on a chapter that has a narrator", () => {
    const html = render();
    expect(html.match(/Aplicar desde aquí en adelante/g)).toHaveLength(1);
  });

  it("saves only when there are changes, and shows a failed save's error", () => {
    expect(render()).toMatch(/<button[^>]*disabled=""[^>]*>Guardar narradores/);
    const changed = render({ hasChanges: true, saveError: "No se pudieron guardar: HTTP 422" });
    expect(changed).not.toMatch(/<button[^>]*disabled=""[^>]*>Guardar narradores/);
    expect(changed).toContain("Hay cambios sin guardar");
    expect(changed).toContain("HTTP 422");
  });

  it("disables editing while saving", () => {
    const html = render({ saving: true, hasChanges: true });
    expect(html).toContain("Guardando…");
    expect(html).not.toMatch(/<select(?![^>]*disabled)/);
  });
});
