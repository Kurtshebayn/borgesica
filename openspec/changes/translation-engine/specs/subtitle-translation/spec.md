# Spec: subtitle-translation

Change: `translation-engine` · Capability: `subtitle-translation`
Phase: spec · Status: draft · Artifact store: openspec

---

## ADDED Requirements

### Requirement: SRT parsing preserves structure

The SRT reader SHALL parse a well-formed `.srt` file into an ordered list of `Chunk` objects, one per cue, with the cue index, start timestamp, end timestamp, and raw text (including inline tags) captured in the chunk's `meta` dict under the keys `cue_index`, `start`, `end`. The domain model `Chunk.source_text` SHALL contain the raw cue text with inline tags present.

#### Scenario: standard cue parsed correctly

Given a `.srt` file containing the cue:
```
42
00:01:05,000 --> 00:01:08,500
Hello, <i>world</i>.
```

When `SrtReader.read(path, config)` is called,

Then the result SHALL contain a `Chunk` where:
- `chunk.index == 41` (0-based)
- `chunk.meta["cue_index"] == 42`
- `chunk.meta["start"] == "00:01:05,000"`
- `chunk.meta["end"] == "00:01:08,500"`
- `chunk.source_text == "Hello, <i>world</i>."`

#### Scenario: multi-line cue text is preserved

Given a cue with two lines of text:
```
7
00:00:30,000 --> 00:00:33,000
Line one
Line two
```

When `SrtReader.read(path, config)` is called,

Then `chunk.source_text` SHALL equal `"Line one\nLine two"` and `chunk.meta["cue_index"] == 7`.

#### Scenario: empty SRT file returns empty list

Given a `.srt` file with zero cues,

When `SrtReader.read(path, config)` is called,

Then the result SHALL be an empty list and no exception SHALL be raised.

---

### Requirement: cues are batched into chunks, never split

The chunker SHALL group SRT cues into batches of at most `JobConfig.chunk_size` cues (default 25). A single cue SHALL NEVER be split across two chunks. Chunk boundaries SHALL fall on cue boundaries only.

#### Scenario: 60 cues produce 3 batches of 20 (chunk_size=20)

Given a `.srt` file with 60 cues and `JobConfig.chunk_size = 20`,

When the chunker partitions the cue list,

Then there SHALL be exactly 3 `Chunk` objects, each containing exactly 20 cues' text concatenated in order, with all `meta` keys for each cue preserved.

#### Scenario: last batch is smaller than chunk_size

Given a `.srt` file with 55 cues and `JobConfig.chunk_size = 25`,

When the chunker partitions the cue list,

Then there SHALL be 3 chunks: the first two containing 25 cues each, the third containing 5 cues.

#### Scenario: single-cue file produces one chunk

Given a `.srt` file with exactly 1 cue,

When the chunker partitions the cue list,

Then the result SHALL be exactly 1 chunk containing that cue.

---

### Requirement: inline tags are preserved in place (tags-in-text), with deterministic strip/reinsert as fallback

The engine SHALL preserve inline SRT tags (`<i>`, `<b>`, `<u>`, and their closing forms) by keeping them IN the `source_text` sent to the provider and instructing the model — via the system prompt — to carry each tag with the word(s) it wraps, preserving tag count and nesting. The `markup` module SHALL validate the result with `markup.validate_tags` (tag count in output equals tag count in source). On mismatch the orchestrator SHALL retry the provider call (≤2 additional attempts). If validation still fails after retries, the engine SHALL fall back to the deterministic `markup.strip` → translate-plain → `markup.reinsert` path. Only if the deterministic fallback also fails validation SHALL the chunk be marked FAILED and the job PAUSED.

Rationale: proportional `reinsert` placed tags by character fraction, which drifts across multi-cue chunks (a tag opened in one cue closed in the next — reproduced in the 2026-06-25 live test). Keeping tags in the text lets the model anchor them to the translated words. `strip`/`reinsert` is retained as a deterministic fallback for weak/local models; hardening that fallback's placement heuristic is tracked for M4.

#### Scenario: tags travel with the translated words (primary path)

Given `source_text = "The <i>quick</i> fox."`,

When the chunk is translated with tags kept in the text,

Then the output SHALL contain exactly 2 tags (`<i>` and `</i>`) wrapping the translated equivalent word (e.g. `"El zorro <i>rápido</i>."`), and `markup.strip` SHALL NOT have removed the tags before the provider call.

#### Scenario: system prompt instructs the model to preserve inline tags

Given a chunk whose `source_text` contains inline tags,

When the `ContextManager` builds the system prompt,

Then the prompt SHALL contain an explicit instruction to keep every inline tag, move it with the word(s) it wraps, and preserve the exact tag count.

#### Scenario: tag count mismatch triggers retry

Given a translated chunk where `markup.validate_tags(source, translated)` returns `False`,

When validation fails,

Then the orchestrator SHALL retry the provider call for that chunk, up to 2 additional attempts (3 total).

#### Scenario: retries exhausted — deterministic strip/reinsert fallback

Given a chunk whose tag-count validation fails on all 3 provider attempts,

When the retries are exhausted,

Then the engine SHALL apply `markup.strip` → translate the plain text → `markup.reinsert`, and if the reinserted result passes `markup.validate_tags`, the chunk SHALL be completed as `DONE` using the fallback output.

#### Scenario: fallback also fails — chunk fails, job pauses

Given a chunk where both the tags-in-text path (3 attempts) and the deterministic strip/reinsert fallback fail validation,

When the final validation fails,

Then the chunk `status` SHALL be set to `ChunkStatus.FAILED`, the job `status` SHALL be set to `JobStatus.PAUSED`, and no further chunks SHALL be translated until the job is resumed.

---

### Requirement: translated SRT is reflowed to the line-length limit

After tag reinsertion, the SRT writer SHALL reflow each cue's translated text to at most `JobConfig.line_length` characters per line (default 42). The reflow target is 2 lines; a 3-line fallback is permitted when no 2-line split fits within the limit. A cue SHALL NEVER be extended to 4 or more lines by the reflow step.

#### Scenario: short translation fits on one line (no split)

Given a translated cue text of 30 characters (no newline),

When the SRT writer reflows with `line_length = 42`,

Then the output cue SHALL contain exactly 1 line with no inserted newline.

#### Scenario: translation exceeds limit, splits into 2 lines

Given a translated cue text `"Esta es una frase bastante larga para mostrar el reflejo."` (57 chars),

When the SRT writer reflows with `line_length = 42`,

Then the output SHALL contain exactly 2 lines, each ≤ 42 characters, with the split at a word boundary (no mid-word hyphenation introduced).

#### Scenario: 2-line split impossible, fallback to 3 lines

Given a translated cue text where no word-boundary split yields two lines ≤ 42 chars,

When the SRT writer reflows with `line_length = 42`,

Then the output SHALL contain exactly 3 lines, each ≤ 42 characters, with splits at word boundaries.

#### Scenario: configurable line_length is respected

Given `JobConfig.line_length = 30`,

When the SRT writer reflows a 50-character cue text,

Then all output lines SHALL be ≤ 30 characters.

---

### Requirement: SRT writer faithfully reconstructs the file

The SRT writer SHALL produce a `.srt` file where every cue preserves its original `cue_index` and timestamps from `chunk.meta`, and the translated+reflowed text replaces the original text. The output file SHALL be parseable by the `srt` library without errors.

#### Scenario: round-trip index and timing integrity

Given a job with 10 translated chunks each containing 5 cues (50 cues total),

When `SrtWriter.write(chunks, src_path, out_path)` is called,

Then the output `.srt` file SHALL contain exactly 50 cues, cue indices SHALL run 1–50 in order (matching original), and every cue's timestamp SHALL equal the original timestamp from `meta`.

#### Scenario: output is parseable

Given any completed SRT job,

When the output `.srt` file is parsed by `import srt; list(srt.parse(open(out_path).read()))`,

Then no `srt.SRTParseError` SHALL be raised and the parsed cue count SHALL equal the input cue count.
