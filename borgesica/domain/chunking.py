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

chunk_prose(node_chunks, config, provider)
    EPUB prose chunker — M2-2R (provenance rework).

    Signature:
        chunk_prose(
            node_chunks: list[Chunk],
            config: JobConfig,
            provider: TranslationProvider,
        ) -> list[Chunk]

    Each input Chunk is ONE text node as produced by EpubReader, with:
        meta = {"epub_item_href": str, "node_path": str, "chapter_index": int}
        source_text = the node's raw text (inline tags like <em> preserved)

    Behavior:
      - Empty/whitespace-only nodes are SKIPPED.
      - Nodes are grouped by meta["chapter_index"]; NEVER batched across chapters.
      - Within a chapter, nodes are greedily accumulated while cumulative tokens
        stay within config.prose_chunk_tokens (default 800).
      - An over-budget SINGLE node is split at sentence boundaries (_split_sentences);
        a sentence over budget is hard-split at the nearest word boundary ≤ budget
        (_hard_split).  Exactly ONE WARNING is logged per oversized SENTENCE
        (containing chunk index + sentence char length) — NOT once per fragment.
      - Each output Chunk:
          source_text = batched node texts joined with "\\n\\n"
          meta["prose_nodes"] = ordered list[{"epub_item_href": str, "node_path": str}]
                                 one entry per "\\n\\n"-separated segment of source_text
          index = 0-based output index
          status = ChunkStatus.PENDING
          meta["hard_split"] = True  ← only present on hard-split chunks

    Token counting is delegated to provider.count_tokens(text, config.model)
    so the domain layer never imports any LLM SDK.
"""
from __future__ import annotations

import logging
import re
from itertools import groupby
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
    node_chunks: list[Chunk],
    config: JobConfig,
    provider: "TranslationProvider",
) -> list[Chunk]:
    """Prose chunker for EPUB — M2-2R provenance-aware implementation.

    Mirrors the SrtChunker pattern: takes per-node Chunks from EpubReader
    and batches them while preserving provenance so EpubWriter can reinstate
    each translated segment into its original XHTML node.

    Args:
        node_chunks: Ordered list of per-node Chunks from EpubReader.
                     Each chunk must have meta["epub_item_href"],
                     meta["node_path"], and meta["chapter_index"].
        config: Job configuration; ``prose_chunk_tokens`` sets the token budget
                per chunk.  ``model`` is forwarded to ``provider.count_tokens``.
        provider: Used ONLY for ``count_tokens(text, model)``; no translation
                  calls are made here (pure domain, no SDK imports).

    Returns:
        Ordered list of output Chunks.  Each chunk:
          - source_text: node texts joined with "\\n\\n"
          - meta["prose_nodes"]: list[{"epub_item_href": str, "node_path": str}]
                                 one entry per segment of source_text
          - index: 0-based output index
          - status: PENDING
          - meta["hard_split"]: True (only on hard-split chunks)
    """
    budget = config.prose_chunk_tokens
    model = config.model
    chunks: list[Chunk] = []
    chunk_index = 0

    # --- Skip empty/whitespace nodes up front ---
    valid_nodes = [n for n in node_chunks if n.source_text.strip()]

    # --- Group by chapter_index; preserve input order within each group ---
    # itertools.groupby preserves ordering so we get consecutive groups.
    for _ch_idx, chapter_node_iter in groupby(
        valid_nodes, key=lambda n: n.meta.get("chapter_index", 0)
    ):
        chapter_nodes = list(chapter_node_iter)

        # Accumulators for the current running batch within this chapter
        accumulated_texts: list[str] = []
        accumulated_nodes: list[dict[str, str]] = []  # {"epub_item_href", "node_path"}
        accumulated_tokens: int = 0

        def _flush_batch(texts: list[str], nodes: list[dict[str, str]]) -> None:
            nonlocal chunk_index
            if not texts:
                return
            source_text = "\n\n".join(texts)
            chunks.append(
                Chunk(
                    index=chunk_index,
                    source_text=source_text,
                    status=ChunkStatus.PENDING,
                    meta={"prose_nodes": nodes},
                )
            )
            chunk_index += 1

        for node in chapter_nodes:
            text = node.source_text
            node_ref = {
                "epub_item_href": node.meta.get("epub_item_href", ""),
                "node_path": node.meta.get("node_path", ""),
            }
            node_tokens = provider.count_tokens(text, model)

            if node_tokens <= budget:
                # Happy path: node fits within budget.
                if accumulated_tokens + node_tokens > budget and accumulated_texts:
                    # Flush current batch before starting a new one with this node.
                    _flush_batch(accumulated_texts, accumulated_nodes)
                    accumulated_texts = []
                    accumulated_nodes = []
                    accumulated_tokens = 0
                accumulated_texts.append(text)
                accumulated_nodes.append(node_ref)
                accumulated_tokens += node_tokens
            else:
                # Node exceeds budget — flush pending batch first, then split node.
                if accumulated_texts:
                    _flush_batch(accumulated_texts, accumulated_nodes)
                    accumulated_texts = []
                    accumulated_nodes = []
                    accumulated_tokens = 0

                # Split node at sentence boundaries.
                sentences = _split_sentences(text)
                sent_accumulated_texts: list[str] = []
                sent_accumulated_nodes: list[dict[str, str]] = []
                sent_accumulated_tokens: int = 0

                for sentence in sentences:
                    s_tokens = provider.count_tokens(sentence, model)

                    if s_tokens > budget:
                        # Single sentence exceeds budget — hard-split required.
                        # Flush any pending sentence accumulation first.
                        if sent_accumulated_texts:
                            _flush_batch(sent_accumulated_texts, sent_accumulated_nodes)
                            sent_accumulated_texts = []
                            sent_accumulated_nodes = []
                            sent_accumulated_tokens = 0

                        # Log exactly ONE WARNING for this oversized sentence.
                        logger.warning(
                            "Hard-splitting oversized sentence at chunk %d "
                            "(sentence char length=%d, budget=%d)",
                            chunk_index,
                            len(sentence),
                            budget,
                        )

                        # Hard-split at word boundaries ≤ budget.
                        for fragment in _hard_split(sentence, budget, provider, model):
                            chunks.append(
                                Chunk(
                                    index=chunk_index,
                                    source_text=fragment,
                                    status=ChunkStatus.PENDING,
                                    meta={
                                        "prose_nodes": [node_ref],
                                        "hard_split": True,
                                    },
                                )
                            )
                            chunk_index += 1
                    else:
                        # Sentence fits — accumulate or flush-and-start.
                        if (
                            sent_accumulated_tokens + s_tokens > budget
                            and sent_accumulated_texts
                        ):
                            _flush_batch(sent_accumulated_texts, sent_accumulated_nodes)
                            sent_accumulated_texts = []
                            sent_accumulated_nodes = []
                            sent_accumulated_tokens = 0
                        sent_accumulated_texts.append(sentence)
                        # All sentences from a single over-budget node reference the same node_ref
                        sent_accumulated_nodes.append(node_ref)
                        sent_accumulated_tokens += s_tokens

                if sent_accumulated_texts:
                    _flush_batch(sent_accumulated_texts, sent_accumulated_nodes)

        # End of chapter — flush remainder.
        if accumulated_texts:
            _flush_batch(accumulated_texts, accumulated_nodes)

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
