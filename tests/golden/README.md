# Golden Fixtures — Schema and Usage

This directory contains curated (source, expected-translation) fixture pairs used
by the M4-2 LLM-as-judge harness and its deterministic Tier-1 guard tests.

## Fixture format (YAML)

Each `.yaml` file defines one fixture with exactly four top-level keys:

```yaml
source: |
  The English source text, verbatim (including inline tags for SRT fixtures).
  Multi-line values use YAML block scalar (|).

expected: |
  The gold-standard neutral-Spanish translation.
  Must obey all five neutral-Spanish rules:
    1. No voseo (no "vos" / voseo conjugations — use "tú" or impersonal constructions)
    2. No regional slang or localisms (no "che", "tío", "órale", etc.)
    3. No leísmo (use "lo/la" for direct object, not "le")
    4. Register-consistent throughout
    5. Consistent number/unit style

glossary:
  # List of relevant glossary entries. Locked entries (locked: true) MUST appear
  # verbatim in `expected`. Empty list ([]) is valid for fixtures without glossary terms.
  - term: SourceTerm
    translation: Traducción
    locked: true        # or false
    note: "Optional context note"

notes: |
  Explain what this fixture tests and any translation decisions worth documenting.
  For calque samples, document the idiom trap and the correct approach.
```

## Fixture index

| File | Type | What it tests |
|------|------|---------------|
| `srt_01_standard_dialogue.yaml` | SRT | Standard dialogue cue — basic accuracy and neutral register |
| `srt_02_inline_tags.yaml` | SRT | Inline `<i>` tag preservation through translation |
| `srt_03_three_line_reflow.yaml` | SRT | Long cue that triggers 3-line reflow (>42 chars, no 2-line split) |
| `srt_04_glossary_proper_noun.yaml` | SRT | Proper noun locked in glossary must appear verbatim |
| `srt_05_regional_neutralization.yaml` | SRT | Region-specific English idiom requiring neutral-Spanish rendering |
| `prose_01_literary_register.yaml` | Prose | Literary register consistency across a formal paragraph |
| `prose_02_glossary_consistency.yaml` | Prose | Glossary term used consistently across two paragraphs |
| `prose_03_named_entity.yaml` | Prose | Named entity preserved verbatim in prose context |
| `calque_01_locking_beam.yaml` | Calque | Marquee calque trap: "locking wooden beam" ≠ "viga con cerradura" |

## Rules for authoring

- `expected` MUST be a genuine neutral-Spanish translation — no voseo, no leísmo, no localisms.
- Locked glossary terms in `expected` MUST appear exactly as specified in the `glossary` block.
- Fixtures are static ground truth — they MUST NOT require a live LLM to evaluate.
- When you add a fixture that involves a non-obvious translation decision, document it in `notes`.
