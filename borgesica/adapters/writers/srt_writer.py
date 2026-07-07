"""SRT writer adapter (M1-9).

Reassembles translated Chunk batches into a valid SRT file.

Reflow strategy:
  - 1 line if text fits within line_length.
  - 2 lines (target): split at the word boundary nearest the midpoint,
    both lines ≤ line_length.
  - 3 lines (fallback): if no 2-line split satisfies line_length.
  - NEVER 4+ lines (spec constraint).

Each Chunk carries cue-batch metadata in meta["cue_batches"]:
  [{"cue_index": int, "start": str, "end": str, "text": str}, ...]

The writer expands each batch's translated_text back into per-cue subtitles
by splitting on "\n\n" and aligning positionally with the original cue metadata.
"""
from __future__ import annotations

import logging
import textwrap
from datetime import timedelta

import srt

from borgesica.domain.models import Chunk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public reflow function (also used by tests directly)
# ---------------------------------------------------------------------------


def reflow(text: str, line_length: int) -> str:
    """Break text into ≤ 3 lines, each ≤ line_length characters.

    Strategy:
      1. If len(text) ≤ line_length → single line.
      2. Try 2-line split: find the best word boundary near the midpoint.
      3. If no 2-line split satisfies line_length → use textwrap for 3 lines.
      NEVER returns 4+ lines.
    """
    text = text.strip()
    if len(text) <= line_length:
        return text

    # Try 2-line split: iterate word boundaries around the midpoint.
    words = text.split()
    best_split = None
    mid = len(text) / 2

    # Find the split index (number of words in line 1) that minimises
    # the distance from the midpoint, subject to both lines fitting.
    for i in range(1, len(words)):
        line1 = " ".join(words[:i])
        line2 = " ".join(words[i:])
        if len(line1) <= line_length and len(line2) <= line_length:
            # This split works; measure distance from midpoint
            dist = abs(len(line1) - mid)
            if best_split is None or dist < best_split[0]:
                best_split = (dist, line1, line2)

    if best_split is not None:
        return f"{best_split[1]}\n{best_split[2]}"

    # 3-line fallback: use textwrap to break into chunks of line_length.
    # textwrap may produce more than 3 lines for very long text; cap at 3.
    #
    # If any single word is longer than line_length it CANNOT be split without
    # hyphenation, so we allow that one line to overflow and log a warning.
    words = text.split()
    overlong_tokens = [w for w in words if len(w) > line_length]
    if overlong_tokens:
        logger.warning(
            "reflow: token(s) exceed line_length=%d (overlong: %s); "
            "line will overflow — hyphenation not supported.",
            line_length,
            ", ".join(repr(t) for t in overlong_tokens),
        )

    lines = textwrap.wrap(text, width=line_length)
    if not lines:
        # textwrap returned nothing (e.g. all-whitespace input)
        return text.strip()

    if len(lines) <= 3:
        return "\n".join(lines)

    # Merge excess lines into the last allowed line (may exceed line_length
    # by a small amount — still better than violating the 3-line spec).
    return "\n".join([lines[0], lines[1], " ".join(lines[2:])])


# ---------------------------------------------------------------------------
# SrtWriter
# ---------------------------------------------------------------------------


def _parse_ts(ts: str) -> timedelta:
    """Parse SRT timestamp string HH:MM:SS,mmm → timedelta."""
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return timedelta(hours=int(h), minutes=int(m), seconds=int(s), milliseconds=int(ms))


class SrtWriter:
    """DocumentWriter implementation for .srt files."""

    def write(self, chunks: list[Chunk], src_path: str, out_path: str) -> None:  # noqa: ARG002
        """Write translated chunks to out_path as a valid SRT file.

        Each Chunk may represent a *batch* of original SRT cues
        (when produced by SrtChunker) or a single cue (when Chunks come
        directly from SrtReader without batching).

        Per-cue translation text is recovered by splitting chunk.translated_text
        on "\\n\\n" and aligning positionally with cue_batches metadata.
        If the counts don't match, each cue falls back to its original
        source text (graceful degradation).
        """
        # Detect mode: batched (meta has "cue_batches") or single-cue
        # (meta has "cue_index").
        subtitles: list[srt.Subtitle] = []

        for chunk in chunks:
            meta = chunk.meta
            translated_text = chunk.translated_text or chunk.source_text

            if "cue_batches" in meta:
                cue_batches = meta["cue_batches"]
                # Split translated text back into per-cue parts
                parts = translated_text.split("\n\n")
                if len(parts) != len(cue_batches):
                    # Graceful degradation: fall back to each cue's ORIGINAL
                    # source text (untranslated but readable, correct timing).
                    # Duplicating the whole batch into every cue produces
                    # walls of identical text and line_length violations.
                    logger.warning(
                        "SrtWriter: chunk %d translated into %d parts but has "
                        "%d cues — falling back to per-cue source text.",
                        chunk.index,
                        len(parts),
                        len(cue_batches),
                    )
                    parts = [cb["text"] for cb in cue_batches]

                for cue_meta, part in zip(cue_batches, parts):
                    # Detect line_length from chunk meta if stored, else 42
                    line_length = meta.get("line_length", 42)
                    reflowed = reflow(part.strip(), line_length=line_length)
                    subtitles.append(
                        srt.Subtitle(
                            index=cue_meta["cue_index"],
                            start=_parse_ts(cue_meta["start"]),
                            end=_parse_ts(cue_meta["end"]),
                            content=reflowed,
                        )
                    )
            else:
                # Single-cue mode (from SrtReader directly, not batched)
                cue_index = meta.get("cue_index", len(subtitles) + 1)
                line_length = meta.get("line_length", 42)
                reflowed = reflow(translated_text.strip(), line_length=line_length)
                subtitles.append(
                    srt.Subtitle(
                        index=cue_index,
                        start=_parse_ts(meta["start"]),
                        end=_parse_ts(meta["end"]),
                        content=reflowed,
                    )
                )

        # Sort by cue index before composing (order may vary if chunks reordered)
        subtitles.sort(key=lambda s: s.index)

        # reindex=False: srt.compose() defaults to reindex=True, which RE-SORTS
        # by start time and renumbers 1..N, silently discarding the sort above.
        # Real transcripts are not always chronological (a post-credits/bonus
        # scene can restart its own timestamp track) — one such cue is enough
        # for the default time-based reindex to scramble everything after it.
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(srt.compose(subtitles, reindex=False))
