# Delta for book-translation

Change: `continue-on-error` · Capability: `book-translation`
Phase: spec · Status: draft · Artifact store: openspec

---

## MODIFIED Requirements

### Requirement: inline EPUB tags are preserved in place (tags-in-text), with strip/reinsert fallback

EPUB text nodes SHALL use the same tag-handling pipeline as SRT (see subtitle-translation): inline formatting tags (`<em>`, `<strong>`, `<span>`, `<a>`, and similar inline HTML elements) are kept IN the text sent to the provider, the model is instructed to carry them with the translated words, and `markup.validate_tags` checks the count. On mismatch the orchestrator retries (≤2), then falls back to deterministic `markup.strip` → translate-plain → `markup.reinsert`. Only if the fallback also fails does the chunk become `ChunkStatus.FAILED`. Whether `ChunkStatus.FAILED` also sets `JobStatus.PAUSED` is governed by `JobConfig.continue_on_error` (see job-lifecycle's "run_job applies the continue_on_error gate on chunk failure"): the job pauses ONLY when `continue_on_error=False`; by default (`continue_on_error=True`) the run continues past the chunk, and the chunk's original text rides through to the writer as a pass-through (writers already fall back to `source_text` when `translated_text is None` — no writer change needed). Because EPUB prose is markup-dense, this shared behavior is the reason the tag-rework is a prerequisite for the EPUB reader/writer.

(Previously: the requirement ended "only if the fallback also fails does the chunk become `ChunkStatus.FAILED` and the job `JobStatus.PAUSED`" — pausing was unconditional on fallback failure. The `continue_on_error` gate now makes pausing conditional; by default the job continues.)

#### Scenario: EPUB italic tag round-trips

- GIVEN an EPUB text node `"A <em>critical</em> point."`
- WHEN the markup pipeline processes it through a translation cycle
- THEN the output SHALL contain exactly 2 inline tags (`<em>` and `</em>`) wrapping the translated equivalent word(s), and the surrounding translated text SHALL be coherent Spanish

#### Scenario: fallback exhaustion with continue_on_error=True — chunk FAILED, job continues

- GIVEN an EPUB text node whose tags-in-text attempts AND strip/reinsert fallback both fail, and `JobConfig.continue_on_error=True` (default)
- WHEN the orchestrator finishes processing this chunk
- THEN the chunk SHALL become `ChunkStatus.FAILED` with `translated_text=None`, the job SHALL NOT pause, and the run SHALL proceed to the next chunk

#### Scenario: fallback exhaustion with continue_on_error=False — chunk FAILED, job PAUSED (unchanged prior contract)

- GIVEN an EPUB text node whose tags-in-text attempts AND strip/reinsert fallback both fail, and `JobConfig.continue_on_error=False` (`--strict`)
- WHEN the orchestrator finishes processing this chunk
- THEN the chunk SHALL become `ChunkStatus.FAILED` with `translated_text=None`, and `job.status` SHALL be set to `JobStatus.PAUSED`, stopping the run — identical to the pre-existing contract

#### Scenario: FAILED chunk's original text is written by the EPUB writer

- GIVEN a `FAILED` chunk with `translated_text=None` and `source_text` containing the original node text, produced under `continue_on_error=True`
- WHEN `EpubWriter.write` reinserts this chunk
- THEN the writer SHALL emit `source_text` in place of the missing translation (existing `None → source_text` fallback in `epub_writer.py`, no writer code change required)
