"""Split contract text into clause-sized chunks.

The first design of this module hardcoded `第X条`, taken from the single sample
supplied on 2026-08-12. Measuring the eight largest real attachments on
2026-08-13 showed that marker appears **zero times** in production documents:

| document      | chars  | markers found        |
| ------------- | ------ | -------------------- |
| 763KB docx    | 53,899 | `一、` x495          |
| 4.68MB pdf    |  3,393 | `1.1` x53, `1.` x12  |
| 117KB pdf     |  1,039 | `一、` x9            |
| 242KB docx    |    813 | `一、` x6            |

A hardcoded marker would therefore have dropped 100% of real documents into the
paragraph fallback: the pipeline would appear to work while emitting chunks
that do not correspond to clauses at all.

So the splitter detects which marker family a document actually uses, and says
which one it picked. Two further guards matter as much as the detection:

- **Merging.** 495 markers across 935 lines is a numbered list, not 495
  clauses. Splitting naively yields shrapnel, and shrapnel retrieves badly.
- **An explicit fallback signal.** If no family qualifies we split on blank
  lines, and the caller is told, because "clause-level retrieval" over
  arbitrary paragraph blocks is a different and weaker claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# A family must appear at least this often to be believed. Two matches are as
# easily a coincidence in prose as a structure.
MIN_MARKER_HITS = 3
# Chunks below this are merged forward. Retrieval over one-line fragments
# returns matches with no surrounding obligation, which is worse than useless
# for a reader trying to judge a clause.
MIN_CHUNK_CHARS = 200
# Hard ceiling per chunk, so one unsplit document cannot dominate a payload.
MAX_CHUNK_CHARS = 4000

STRATEGY_PARAGRAPH = "paragraph_fallback"
SIGNAL_FALLBACK = "clause_split_fell_back_to_paragraphs"

# Ordered by how strongly the marker implies a real clause boundary. `第X条` is
# unambiguous; a bare `1.` is frequently just a list item, so it ranks last and
# only wins if nothing better qualifies.
MARKER_FAMILIES: tuple[tuple[str, str], ...] = (
    ("article", r"第[一二三四五六七八九十百零〇\d]{1,6}条"),
    ("chapter", r"第[一二三四五六七八九十百零〇\d]{1,6}章"),
    ("cn_ordinal", r"^[一二三四五六七八九十]{1,3}[、．.]"),
    ("decimal", r"^\d{1,2}\.\d{1,2}"),
    ("digit", r"^\d{1,2}[．.、]\s"),
)
# `article` outranks everything once it qualifies at all; the rest compete on
# how many times they appear.
PREFERRED_STRATEGY = "article"


@dataclass
class Clause:
    index: int
    heading: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        # Body text is deliberately included: this object stays local. The
        # payload builder decides what may leave the machine.
        return {"index": self.index, "heading": self.heading, "text": self.text}


@dataclass
class SplitResult:
    clauses: list[Clause] = field(default_factory=list)
    strategy: str = STRATEGY_PARAGRAPH
    marker_counts: dict[str, int] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Metadata only. Clause bodies are not serialised here."""
        return {
            "strategy": self.strategy,
            "clause_count": len(self.clauses),
            "marker_counts": dict(self.marker_counts),
            "signals": list(self.signals),
        }


def count_markers(text: str) -> dict[str, int]:
    """How often each family appears. Exposed so the choice can be inspected."""
    counts: dict[str, int] = {}
    for name, pattern in MARKER_FAMILIES:
        counts[name] = len(re.findall(pattern, text, re.MULTILINE))
    return counts


def choose_strategy(counts: dict[str, int]) -> str:
    """Pick the family to split on, or the paragraph fallback."""
    qualifying = {name: n for name, n in counts.items() if n >= MIN_MARKER_HITS}
    if not qualifying:
        return STRATEGY_PARAGRAPH
    if PREFERRED_STRATEGY in qualifying:
        return PREFERRED_STRATEGY
    # Ties break toward the family declared earlier, which is the one whose
    # marker more strongly implies a clause boundary.
    order = [name for name, _ in MARKER_FAMILIES]
    return max(qualifying, key=lambda name: (qualifying[name], -order.index(name)))


def split_clauses(text: str) -> SplitResult:
    """Split contract text into clause-sized chunks, reporting how."""
    result = SplitResult()
    if not text or not text.strip():
        return result

    result.marker_counts = count_markers(text)
    result.strategy = choose_strategy(result.marker_counts)

    if result.strategy == STRATEGY_PARAGRAPH:
        result.signals.append(SIGNAL_FALLBACK)
        blocks = _split_paragraphs(text)
    else:
        pattern = dict(MARKER_FAMILIES)[result.strategy]
        blocks = _split_on_marker(text, pattern)

    merged = _merge_small(blocks)
    result.clauses = [
        Clause(index=position, heading=_heading_of(block), text=block)
        for position, block in enumerate(merged, start=1)
    ]
    return result


def _split_on_marker(text: str, pattern: str) -> list[str]:
    """Cut immediately before each marker occurrence."""
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if not matches:
        return _split_paragraphs(text)
    blocks: list[str] = []
    # Anything before the first marker is preamble (title, parties). Keep it:
    # dropping it would lose the contract's own identification.
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            blocks.append(preamble)
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        block = text[match.start() : end].strip()
        if block:
            blocks.append(block)
    return blocks


def _split_paragraphs(text: str) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    return blocks or ([text.strip()] if text.strip() else [])


def _merge_small(blocks: list[str]) -> list[str]:
    """Merge undersized blocks forward, then hard-cap oversized ones.

    Merging forward rather than backward keeps a heading attached to the body
    that follows it, which is the pairing a reader needs.
    """
    merged: list[str] = []
    buffer = ""
    for block in blocks:
        buffer = f"{buffer}\n{block}".strip() if buffer else block
        if len(buffer) >= MIN_CHUNK_CHARS:
            merged.append(buffer)
            buffer = ""
    if buffer:
        # The tail is too short to stand alone. Append it to the previous chunk
        # rather than emitting a fragment or, worse, dropping it.
        if merged:
            merged[-1] = f"{merged[-1]}\n{buffer}".strip()
        else:
            merged.append(buffer)
    return [piece for block in merged for piece in _cap(block)]


def _cap(block: str) -> list[str]:
    if len(block) <= MAX_CHUNK_CHARS:
        return [block]
    return [block[start : start + MAX_CHUNK_CHARS] for start in range(0, len(block), MAX_CHUNK_CHARS)]


def _heading_of(block: str) -> str:
    """First line, bounded. Used to label a clause without quoting its body."""
    first = block.splitlines()[0].strip() if block.splitlines() else ""
    return first[:40]
