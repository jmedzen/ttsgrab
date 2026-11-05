"""The worker client must be forgiving about spelling, strict about behaviour.

A worker is often someone else's container. If one unexpected value in its voice list
makes the whole provider fail to load, the gateway silently loses a backend — the
catalogue comes back empty and nothing says why.
"""

from __future__ import annotations

import pytest

from ttsgrab.workers.client import _normalise_gender


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("female", "female"),
        ("Female", "female"),
        ("FEMALE", "female"),
        ("f", "female"),
        ("feminine", "female"),
        ("male", "male"),
        ("M", "male"),
        ("masculine", "male"),
        ("neutral", "neutral"),
        ("", "unknown"),
        (None, "unknown"),
        ("robot", "unknown"),
        (123, "unknown"),
    ],
)
def test_gender_spellings_normalise(raw, expected) -> None:
    assert _normalise_gender(raw) == expected


def test_worker_voice_with_odd_gender_still_loads() -> None:
    """A single odd value must not take the whole provider's catalogue down."""
    from ttsgrab.core.types import Voice

    # What the client builds after normalisation: always one of the four literals.
    for raw in ("Female", "MAN", "n/a", None):
        voice = Voice(
            provider="w",
            voice_id=f"v-{raw}",
            name="x",
            languages=["en-US"],
            gender=_normalise_gender(raw),
        )
        assert voice.gender in {"male", "female", "neutral", "unknown"}
