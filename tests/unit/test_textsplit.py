"""Text planning: sentence splitting, Traditional→Simplified, pronunciation overrides."""

from __future__ import annotations

import pytest

from ttsgrab.core.textsplit import (
    PronunciationDictionary,
    estimate_speech_seconds,
    has_opencc,
    looks_traditional,
    plan_text,
    split_sentences,
    to_simplified,
)


def test_split_on_cjk_terminators() -> None:
    assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]


def test_split_on_english_terminators() -> None:
    assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


def test_does_not_split_after_common_abbreviations() -> None:
    chunks = split_sentences("Dr. Smith went home.")
    assert chunks == ["Dr. Smith went home."]


def test_long_piece_is_wrapped_on_soft_breaks() -> None:
    text = "這是一段很長的話，" * 30
    chunks = split_sentences(text, max_chars=50)
    assert all(len(c) <= 52 for c in chunks), [len(c) for c in chunks]
    assert len(chunks) > 1


def test_hard_wrap_when_no_soft_break_exists() -> None:
    chunks = split_sentences("字" * 120, max_chars=40)
    assert len(chunks) == 3
    assert all(len(c) <= 40 for c in chunks)


def test_empty_text_yields_no_chunks() -> None:
    assert split_sentences("   ") == []


def test_opencc_is_available() -> None:
    assert has_opencc() is True


@pytest.mark.parametrize(
    ("traditional", "simplified"),
    [
        ("語音", "语音"),
        ("臺灣", "台湾"),
        ("軟體", "软体"),
        ("網路", "网路"),
    ],
)
def test_t2s_conversion(traditional: str, simplified: str) -> None:
    assert to_simplified(traditional) == simplified


def test_looks_traditional_heuristic() -> None:
    assert looks_traditional("這是繁體")
    assert not looks_traditional("hello world")


def test_dictionary_applies_taiwan_readings() -> None:
    d = PronunciationDictionary()
    out, hits = d.apply("垃圾車來了")
    assert out.startswith("拉基")
    assert "垃圾" in hits


def test_dictionary_override_wins(tmp_path) -> None:
    d = PronunciationDictionary({"企業": "企業X"})
    out, hits = d.apply("企業")
    assert out == "企業X"
    assert hits == ["企業"]


def test_dictionary_longest_match_first() -> None:
    d = PronunciationDictionary({"AB": "1", "ABC": "2"})
    out, hits = d.apply("ABC")
    assert out == "2"
    assert hits == ["ABC"]


def test_plan_is_single_chunk_below_threshold() -> None:
    plan = plan_text(
        "短句。",
        normalize=True,
        dictionary=None,
        chunk_threshold=400,
        max_sentence_chars=300,
        target_language="zh-TW",
    )
    assert len(plan.chunks) == 1


def test_plan_chunks_above_threshold() -> None:
    text = "第一句話。第二句話。第三句話。" * 20
    plan = plan_text(
        text,
        normalize=True,
        dictionary=None,
        chunk_threshold=100,
        max_sentence_chars=300,
        target_language="zh-TW",
    )
    assert len(plan.chunks) > 1
    assert plan.total_chars > 0


def test_plan_normalises_traditional_for_chinese() -> None:
    plan = plan_text(
        "這是繁體中文的測試。",
        normalize=True,
        dictionary=None,
        chunk_threshold=400,
        max_sentence_chars=300,
        target_language="zh-TW",
    )
    assert plan.was_simplified is True
    assert "這" not in plan.normalized


def test_plan_leaves_english_alone() -> None:
    plan = plan_text(
        "This stays as is.",
        normalize=True,
        dictionary=None,
        chunk_threshold=400,
        max_sentence_chars=300,
        target_language="en-US",
    )
    assert plan.was_simplified is False
    assert plan.normalized == "This stays as is."


def test_plan_can_skip_normalisation() -> None:
    plan = plan_text(
        "這是繁體",
        normalize=False,
        dictionary=None,
        chunk_threshold=400,
        max_sentence_chars=300,
        target_language="zh-TW",
    )
    assert plan.was_simplified is False
    assert plan.normalized == "這是繁體"


def test_estimate_scales_with_speed() -> None:
    normal = estimate_speech_seconds("這是一段中文測試文字", 1.0)
    fast = estimate_speech_seconds("這是一段中文測試文字", 2.0)
    assert fast < normal
