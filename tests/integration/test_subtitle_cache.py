"""Subtitles must survive a cache hit, or the same request behaves differently twice.

`synthesize()` returns timings computed during synthesis. A cache hit returns stored audio
and skips that path entirely, so the second identical request had no `segments` — and
`/v1/tts?subtitles=srt` therefore worked once and then silently stopped returning subtitles.
Worse, `want_segments` was not part of the cache key, so a request *without* subtitles could
poison the entry for one *with* them.
"""

from __future__ import annotations


async def test_subtitles_are_returned_from_a_cache_hit(stub_client):
    body = {
        "text": "One. Two. Three.",
        "voice": "stub-tone",
        "provider": "stub",
        "subtitles": "srt",
        "response": "json",
    }
    # First call populates the cache; `bypass_cache` would skip the *write* as well as the
    # read, so the second call would be a second synthesis and prove nothing.
    first = await stub_client.post("/v1/tts", json=body)
    assert first.status_code == 200
    assert first.json().get("subtitles"), "the uncached request must have timings"

    second = await stub_client.post("/v1/tts", json=body)
    assert second.status_code == 200
    assert second.json()["cached"] is True, "this test is worthless unless the hit is real"
    assert second.json().get("subtitles"), (
        "a cache hit returned no timings, so subtitles worked only the first time"
    )


async def test_a_request_without_subtitles_cannot_poison_one_with_them(stub_client):
    """The cache key did not include `want_segments`."""
    plain = {"text": "A. B. C.", "voice": "stub-tone", "provider": "stub", "response": "json"}
    first = await stub_client.post("/v1/tts", json=plain)
    assert first.status_code == 200

    with_subs = await stub_client.post("/v1/tts", json={**plain, "subtitles": "srt"})
    assert with_subs.status_code == 200
    assert with_subs.json().get("subtitles"), (
        "a plain request cached an entry that a subtitle request then received"
    )


async def test_loudness_targets_do_not_share_a_cache_entry(stub_client):
    """`loudness` was missing from the cache key.

    Loudness is applied after synthesis, which is why it was easy to overlook — but two
    requests differing only in target shared an entry, so the second caller received audio
    normalised to the first one's target. A wrong result, no error, nothing in the response
    to indicate it.
    """
    text = "Loudness check."
    loud = {"text": text, "voice": "stub-tone", "provider": "stub", "response": "json"}
    quiet = {**loud, "loudness": -23.0}
    loud = {**loud, "loudness": -16.0}

    first = await stub_client.post("/v1/tts", json=loud)
    assert first.status_code == 200

    second = await stub_client.post("/v1/tts", json=quiet)
    assert second.status_code == 200
    assert second.json()["cached"] is False, "a -23 LUFS request was served the -16 LUFS entry"

    # And the same target still hits: the fix must not disable caching altogether.
    again = await stub_client.post("/v1/tts", json=loud)
    assert again.json()["cached"] is True
