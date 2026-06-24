"""Chunking module — SRT cue-batch and prose paragraph strategies.

Pure domain module: stdlib only, no I/O, no external dependencies.

SrtChunker.chunk(cues, config)
    Groups individual cue Chunks (as produced by SrtReader) into batches of
    at most config.chunk_size cues.  A single cue is NEVER split across two
    batches.  Chunk boundaries fall on cue boundaries only.

    Each output Chunk:
      - source_text: cues' source_text joined with "\\n\\n" (SRT block separator)
      - meta["cue_batches"]: list of {cue_index, start, end, text} dicts
      - index: 0-based batch index
      - status: ChunkStatus.PENDING

chunk_prose(paragraphs, config, provider)
    EPUB/PDF prose chunker — implemented in M2.  Raises NotImplementedError.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from borgesica.domain.models import Chunk, ChunkStatus, JobConfig

if TYPE_CHECKING:
    from borgesica.domain.ports import TranslationProvider


class SrtChunker:
    """Groups individual SRT cue Chunks into batch Chunks."""

    @staticmethod
    def chunk(cues: list[Chunk], config: JobConfig) -> list[Chunk]:
        """Partition *cues* into batches of at most ``config.chunk_size``.

        Args:
            cues:   Ordered list of individual cue Chunks (one per SRT cue).
            config: Job configuration; only ``chunk_size`` is used here.

        Returns:
            Ordered list of batch Chunks, each aggregating up to
            ``config.chunk_size`` cues.
        """
        if not cues:
            return []

        size = config.chunk_size
        batches: list[Chunk] = []

        for batch_index, offset in enumerate(range(0, len(cues), size)):
            batch_cues = cues[offset : offset + size]

            source_text = "\n\n".join(c.source_text for c in batch_cues)

            cue_batches: list[dict[str, Any]] = []
            for cue in batch_cues:
                cue_batches.append(
                    {
                        "cue_index": cue.meta.get("cue_index", cue.index),
                        "start": cue.meta.get("start", ""),
                        "end": cue.meta.get("end", ""),
                        "text": cue.source_text,
                    }
                )

            batches.append(
                Chunk(
                    index=batch_index,
                    source_text=source_text,
                    status=ChunkStatus.PENDING,
                    meta={"cue_batches": cue_batches},
                )
            )

        return batches


def chunk_prose(
    paragraphs: list[str],
    config: JobConfig,
    provider: "TranslationProvider",
) -> list[Chunk]:
    """Prose chunker for EPUB/PDF — implemented in M2.

    Raises:
        NotImplementedError: Always — this is a stub for M2.
    """
    raise NotImplementedError("chunk_prose is not implemented until M2")
