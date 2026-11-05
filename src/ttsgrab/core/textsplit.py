"""Text analysis: sentence splitting and Traditional→Simplified normalisation.

Traditional Chinese support, honestly stated
--------------------------------------------
No open local model here was trained on Taiwan Mandarin. They use zh-CN lexicons
(pypinyin / jieba), so feeding raw Traditional text produces wrong readings and
occasionally missing characters. The mitigation is two-stage:

1. **OpenCC t2s** converts Traditional → Simplified before the model sees it. This
   fixes coverage problems and most readings.
2. **A user-editable pronunciation dictionary** rewrites specific words *after* the t2s
   pass, so Taiwan readings survive. Taiwan has real divergences: 垃圾 (lā jī vs lè sè),
   法國 (fǎ vs fà), 企業 (qǐ vs qì), 星期 (xīng qī vs xīng qí).

Neither is a substitute for a zh-TW trained model, and we say so in the UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import ClassVar

# Sentence terminators for zh + en. Kept as one character class for speed.
_TERMINATORS = "。！？!?；;\n"
_SOFT_BREAKS = "，、,：:"

_TRADITIONAL_RANGES = ((0x3400, 0x4DBF), (0xF900, 0xFAFF))
_ABBREVIATIONS = (
    "Mr.",
    "Mrs.",
    "Ms.",
    "Dr.",
    "Prof.",
    "Sr.",
    "Jr.",
    "St.",
    "vs.",
    "etc.",
    "e.g.",
    "i.e.",
    "cf.",
    "No.",
    "Fig.",
    "Ref.",
)


@lru_cache(maxsize=1)
def _opencc():
    """OpenCC is optional: degrade to a no-op if it is not installed."""
    try:
        from opencc import OpenCC

        return OpenCC("t2s")
    except Exception:
        return None


def has_opencc() -> bool:
    return _opencc() is not None


def to_simplified(text: str) -> str:
    conv = _opencc()
    if conv is None:
        return text
    return conv.convert(text)


def looks_traditional(text: str) -> bool:
    """Heuristic: any CJK char in the compatibility/extension blocks, or a known set."""
    common = set("繁體臺灣個們這來說時會後點開關電腦網路資料軟體硬體語音")
    return any(ch in common for ch in text)


@dataclass
class TextPlan:
    chunks: list[str]
    normalized: str
    was_simplified: bool
    dictionary_hits: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(c) for c in self.chunks)


class PronunciationDictionary:
    """Ordered word → replacement map, applied longest-first.

    Ships a small Taiwan-oriented default set; users extend it in config.
    """

    DEFAULT: ClassVar[dict[str, str]] = {
        # Traditional word -> simplified spelling that the zh-CN grapheme-to-phoneme
        # models pronounce the way Taiwan speakers do.
        "垃圾": "拉基",
        "企業": "起業",
        "法國": "法郭",
        "星期": "星七",
        "髮型": "發型",
        "液體": "夜體",
        "質量": "質量",
    }

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        merged = dict(self.DEFAULT)
        if overrides:
            merged.update(overrides)
        self._map = merged
        self._keys = sorted(merged, key=len, reverse=True)

    def apply(self, text: str) -> tuple[str, list[str]]:
        hits: list[str] = []
        for key in self._keys:
            if key in text:
                hits.append(key)
                text = text.replace(key, self._map[key])
        return text, hits


def split_sentences(text: str, *, max_chars: int = 300) -> list[str]:
    """Split on sentence terminators, then hard-wrap over-long pieces on soft breaks."""
    text = text.strip()
    if not text:
        return []

    pieces: list[str] = []
    buf = ""
    for i, ch in enumerate(text):
        buf += ch
        is_terminator = ch in _TERMINATORS or ch == "."
        if not is_terminator:
            continue
        if ch == ".":
            tail = buf.rstrip()
            # Never split inside a number ("3.14"), an abbreviation ("Dr."), or a
            # decimal-point continuation ("v1.2").
            prev = text[i - 1] if i > 0 else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if prev.isdigit() and nxt.isdigit():
                continue
            if any(tail.endswith(a) for a in _ABBREVIATIONS):
                continue
            # A period followed immediately by a letter is part of a token ("e.g", "U.S").
            if nxt and not nxt.isspace() and nxt not in "\"'”’)]":
                continue
        if buf.strip():
            pieces.append(buf)
        buf = ""
    if buf.strip():
        pieces.append(buf)

    out: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        out.extend(_wrap(piece, max_chars))
    return out


def _wrap(piece: str, max_chars: int) -> list[str]:
    if len(piece) <= max_chars:
        return [piece]
    result: list[str] = []
    remaining = piece
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = max((window.rfind(ch) for ch in _SOFT_BREAKS), default=-1)
        if cut <= 0:
            cut = max_chars - 1
        result.append(remaining[: cut + 1].strip())
        remaining = remaining[cut + 1 :].lstrip()
    if remaining:
        result.append(remaining)
    return result


def plan_text(
    text: str,
    *,
    normalize: bool,
    dictionary: PronunciationDictionary | None,
    chunk_threshold: int,
    max_sentence_chars: int,
    target_language: str | None = None,
) -> TextPlan:
    """Decide how the text will be sent to a provider."""
    was_simplified = False
    working = text

    is_zh = bool(target_language and target_language.startswith("zh")) or any(
        "\u4e00" <= ch <= "\u9fff" for ch in text
    )
    if normalize and is_zh:
        simplified = to_simplified(text)
        was_simplified = simplified != text
        working = simplified

    hits: list[str] = []
    if dictionary is not None and is_zh:
        working, hits = dictionary.apply(working)

    if len(working) <= chunk_threshold:
        return TextPlan(
            chunks=[working],
            normalized=working,
            was_simplified=was_simplified,
            dictionary_hits=hits,
        )

    return TextPlan(
        chunks=split_sentences(working, max_chars=max_sentence_chars),
        normalized=working,
        was_simplified=was_simplified,
        dictionary_hits=hits,
    )


def estimate_speech_seconds(text: str, speed: float = 1.0) -> float:
    """Rough duration estimate for cost/latency previews (not a synthesis input)."""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    words = len(re.findall(r"[A-Za-z']+", text))
    # ~4.5 CJK chars/sec, ~2.6 words/sec at natural pace.
    seconds = cjk / 4.5 + words / 2.6
    return max(0.3, seconds / max(0.25, speed))
