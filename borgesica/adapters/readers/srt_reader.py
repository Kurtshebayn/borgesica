"""SRT reader adapter (M1-9).

Parses an SRT file into one Chunk per cue using the `srt` library.
Each Chunk carries:
  - index: 0-based sequential index (NOT the SRT cue number)
  - source_text: cue content (inline tags preserved, lines joined with \n)
  - meta: {
        "cue_index": int,    # original SRT 1-based cue number
        "start": str,        # HH:MM:SS,mmm
        "end": str,          # HH:MM:SS,mmm
    }

Dependency rule: only stdlib + domain models + srt library allowed here.
"""
from __future__ import annotations

import re

import srt

from borgesica.domain.models import Chunk, ChunkStatus, JobConfig


def _normalize_cue_text(content: str) -> str:
    """Collapse blank lines inside a cue's content and trim the edges.

    The srt parser can absorb trailing blank lines at EOF into the last
    cue's content (e.g. 'Ahhh\\n\\n'). Downstream, SrtChunker joins cue
    texts with '\\n\\n' as the segment delimiter — an internal blank line
    makes segments outnumber cues, and that chunk can never pass segment
    validation (it burns every retry on every run).
    """
    return re.sub(r"\n\s*\n", "\n", content).strip()


def _td_to_srt_ts(td: object) -> str:
    """Convert a timedelta to SRT timestamp string HH:MM:SS,mmm."""
    total_seconds = int(td.total_seconds())  # type: ignore[union-attr]
    millis = int(td.microseconds / 1000)  # type: ignore[union-attr]
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


class SrtReader:
    """DocumentReader implementation for .srt files."""

    def read(self, path: str, config: JobConfig) -> list[Chunk]:  # noqa: ARG002
        """Parse path into one Chunk per SRT cue.

        config is accepted (satisfies Protocol) but only line_length would
        be used by the writer — reader is format-agnostic.
        """
        with open(path, encoding="utf-8") as f:
            raw = f.read()

        if not raw.strip():
            return []

        chunks: list[Chunk] = []
        for seq, subtitle in enumerate(srt.parse(raw)):
            chunks.append(
                Chunk(
                    index=seq,
                    source_text=_normalize_cue_text(subtitle.content),
                    status=ChunkStatus.PENDING,
                    meta={
                        "cue_index": subtitle.index,
                        "start": _td_to_srt_ts(subtitle.start),
                        "end": _td_to_srt_ts(subtitle.end),
                    },
                )
            )
        return chunks
