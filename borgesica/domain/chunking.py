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

chunk_prose(paragraphs_by_chapter, config, provider)
    EPUB/PDF prose chunker.

    Signature:
        chunk_prose(
            paragraphs_by_chapter: list[list[str]],
            config: JobConfig,
            provider: TranslationProvider,
        ) -> list[Chunk]

    The caller (EpubReader / PdfReader) groups paragraphs per chapter into a
    list of lists.  chunk_prose processes each chapter independently, so no
    chunk ever crosses a chapter boundary.

    Within a chapter the following strategy is applied:
      1. Paragraphs are accumulated into a running chunk while the cumulative
         token count stays within config.prose_chunk_tokens.
      2. When adding the next paragraph would exceed the budget, the current
         accumulation is emitted as a chunk and a new one starts.
      3. If a single paragraph exceeds the budget it is split at sentence
         boundaries (.  !  ? followed by whitespace or end-of-string).
      4. If a single sentence exceeds the budget it is hard-split at the
         nearest word boundary at or below the budget, and a WARNING is logged
         containing the chunk index and the sentence character length.

    Token counting is delegated to provider.count_tokens(text, config.model)
    so the domain layer never imports any LLM SDK.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from borgesica.domain.models import Chunk, ChunkStatus, JobConfig

if TYPE_CHECKING:
    from borgesica.domain.ports import TranslationProvider

logger = logging.getLogger(__name__)


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
                    meta={"cue_batches": cue_batches, "line_length": config.line_length},
                )
            )

        return batches


def chunk_prose(
    paragraphs_by_chapter: list[list[str]],
    config: JobConfig,
    provider: "TranslationProvider",
) -> list[Chunk]:
    """Prose chunker for EPUB/PDF.

    Args:
        paragraphs_by_chapter: Outer list = chapters; inner list = paragraphs
            for that chapter.  Chapter boundaries are NEVER crossed when
            building chunks — an EpubReader or PdfReader provides this grouping.
        config: Job configuration; ``prose_chunk_tokens`` sets the token budget
            per chunk.  ``model`` is forwarded to ``provider.count_tokens``.
        provider: Used ONLY for ``count_tokens(text, model)``; no translation
            calls are made here (pure domain, no SDK imports).

    Returns:
        Ordered list of Chunks.  Each chunk stays within ``prose_chunk_tokens``
        OR, when a single sentence exceeds the budget, is as close to the budget
        as a word boundary allows (with a WARNING logged).
    """
    budget = config.prose_chunk_tokens
    model = config.model
    chunks: list[Chunk] = []
    chunk_index = 0

    for chapter_paragraphs in paragraphs_by_chapter:
        # Accumulate paragraphs for this chapter into running chunks.
        accumulated: list[str] = []
        accumulated_tokens = 0

        def _flush(texts: list[str]) -> None:
            nonlocal chunk_index
            if not texts:
                return
            text = "\n\n".join(texts)
            chunks.append(
                Chunk(
                    index=chunk_index,
                    source_text=text,
                    status=ChunkStatus.PENDING,
                    meta={"chunk_type": "prose"},
                )
            )
            chunk_index += 1

        for paragraph in chapter_paragraphs:
            para_tokens = provider.count_tokens(paragraph, model)

            if para_tokens <= budget:
                # Happy path: paragraph fits within budget.
                if accumulated_tokens + para_tokens > budget and accumulated:
                    # Flushing current accumulation before adding this paragraph.
                    _flush(accumulated)
                    accumulated = []
                    accumulated_tokens = 0
                accumulated.append(paragraph)
                accumulated_tokens += para_tokens
            else:
                # Paragraph exceeds budget — must split at sentence boundaries.
                # First flush any pending accumulation.
                if accumulated:
                    _flush(accumulated)
                    accumulated = []
                    accumulated_tokens = 0

                # Split paragraph into sentences.
                sentences = _split_sentences(paragraph)
                sent_accumulated: list[str] = []
                sent_tokens = 0

                for sentence in sentences:
                    s_tokens = provider.count_tokens(sentence, model)

                    if s_tokens > budget:
                        # Single sentence exceeds budget — hard-split required.
                        # Flush current sentence accumulation first.
                        if sent_accumulated:
                            _flush(sent_accumulated)
                            sent_accumulated = []
                            sent_tokens = 0

                        # Hard-split the sentence at nearest word boundary ≤ budget.
                        for fragment in _hard_split(sentence, budget, provider, model):
                            frag_tokens = provider.count_tokens(fragment, model)
                            logger.warning(
                                "Hard-splitting oversized sentence at chunk %d "
                                "(sentence char length=%d, fragment tokens=%d, budget=%d)",
                                chunk_index,
                                len(sentence),
                                frag_tokens,
                                budget,
                            )
                            chunks.append(
                                Chunk(
                                    index=chunk_index,
                                    source_text=fragment,
                                    status=ChunkStatus.PENDING,
                                    meta={"chunk_type": "prose", "hard_split": True},
                                )
                            )
                            chunk_index += 1
                    else:
                        # Sentence fits — accumulate or flush-and-start.
                        if sent_tokens + s_tokens > budget and sent_accumulated:
                            _flush(sent_accumulated)
                            sent_accumulated = []
                            sent_tokens = 0
                        sent_accumulated.append(sentence)
                        sent_tokens += s_tokens

                if sent_accumulated:
                    _flush(sent_accumulated)
                    sent_accumulated = []

        # End of chapter — flush whatever is left.
        if accumulated:
            _flush(accumulated)
            accumulated = []

    return chunks


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """Split *text* into sentences at .  !  ? followed by whitespace or EOS.

    The delimiter is kept at the END of each sentence (it belongs to the
    sentence that closes it).
    """
    # Pattern: a sentence-ending punctuation followed by whitespace or EOS.
    # We keep the delimiter with the left part using a non-consuming lookahead on the space.
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def _hard_split(
    text: str,
    budget: int,
    provider: "TranslationProvider",
    model: str,
) -> list[str]:
    """Split *text* into fragments each ≤ *budget* tokens, at word boundaries.

    Greedy: pack as many words as possible into each fragment without exceeding
    the budget.  Falls back to single-word fragments if a single word exceeds
    the budget (pathological edge case).
    """
    words = text.split()
    fragments: list[str] = []
    current_words: list[str] = []

    for word in words:
        candidate = " ".join([*current_words, word])
        if provider.count_tokens(candidate, model) > budget:
            if current_words:
                fragments.append(" ".join(current_words))
                current_words = [word]
            else:
                # Single word already exceeds budget — emit it alone.
                fragments.append(word)
                current_words = []
        else:
            current_words.append(word)

    if current_words:
        fragments.append(" ".join(current_words))

    return fragments
