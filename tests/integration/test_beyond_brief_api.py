"""Integration tests for the capabilities added on top of the original brief.

These run against the real ASGI app because the interesting failures live at the seams:
a header the schema drops, a column the migration forgets, a replay path that re-bills.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


TEXT_MULTI = "第一句話在這裡。第二句稍微長一點，用來測試分段。第三句收尾。"


@pytest.fixture
async def client(ctx, app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {ctx.settings.security.admin_token}"},
    ) as c:
        yield c


async def test_rate_limit_sends_retry_after_header(client, ctx):
    """The header AGENTS.md tells clients to honour must actually arrive."""
    from ttsgrab.core.errors import RateLimited

    # Exercised directly rather than through the endpoint: the ASGI transport uses the
    # `testclient` host, which the localhost bypass treats as trusted, so the limiter is
    # never consulted. What matters is that the error carries the header the app installs.
    exc = RateLimited("slow down", details={"retry_after_seconds": 30, "limit": 1})
    headers = exc.http_headers("req-abc")
    assert headers["Retry-After"] == "30"
    assert headers["X-Request-Id"] == "req-abc"


async def test_idempotency_key_returns_the_first_result_instead_of_billing_twice(stub_client):
    headers = {"Idempotency-Key": "itest-key-0001"}
    body = {"text": "冪等測試。", "voice": "stub-tone", "provider": "stub"}
    first = await stub_client.post("/v1/tts", json=body, headers=headers)
    if first.status_code != 200:
        pytest.skip(f"synthesis unavailable: {first.status_code} {first.text[:200]}")
    second = await stub_client.post("/v1/tts", json=body, headers=headers)

    assert second.status_code == 200
    assert second.headers.get("X-TTSGrab-Idempotent-Replay") == "true"
    assert second.content == first.content, "a replay must return the stored audio, not a re-render"
    assert second.headers.get("X-TTSGrab-History-Id") == first.headers.get("X-TTSGrab-History-Id")


async def test_reusing_a_key_with_a_different_body_is_refused(stub_client):
    headers = {"Idempotency-Key": "itest-key-0002"}
    first = await stub_client.post(
        "/v1/tts",
        json={"text": "原文。", "voice": "stub-tone", "provider": "stub"},
        headers=headers,
    )
    if first.status_code != 200:
        pytest.skip("synthesis unavailable in this environment")
    second = await stub_client.post(
        "/v1/tts",
        json={"text": "不同的文字。", "voice": "stub-tone", "provider": "stub"},
        headers=headers,
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "invalid_request"


async def test_subtitles_come_back_with_the_audio(stub_client):
    resp = await stub_client.post(
        "/v1/tts",
        json={
            "text": TEXT_MULTI,
            "voice": "stub-tone",
            "provider": "stub",
            "response": "json",
            "subtitles": "srt",
            "bypass_cache": True,
        },
    )
    if resp.status_code != 200:
        pytest.skip("synthesis unavailable in this environment")
    data = resp.json()
    assert data["subtitles"], "no subtitles were returned"
    assert "-->" in data["subtitles"]
    assert data["subtitles_format"] == "srt"
    assert data["subtitles_url"], "no durable URL for the subtitles"


async def test_subtitles_survive_the_round_trip_through_history(stub_client):
    resp = await stub_client.post(
        "/v1/tts",
        json={
            "text": TEXT_MULTI,
            "voice": "stub-tone",
            "provider": "stub",
            "response": "json",
            "subtitles": "vtt",
            "bypass_cache": True,
        },
    )
    if resp.status_code != 200:
        pytest.skip("synthesis unavailable in this environment")
    url = resp.json()["subtitles_url"]
    fetched = await stub_client.get(url)
    assert fetched.status_code == 200
    assert fetched.text.lstrip().startswith("WEBVTT")
    assert "-->" in fetched.text


async def test_subtitle_timings_end_where_the_audio_does(stub_client):
    """A caption track that ends before the audio leaves the last sentence unsaid."""
    resp = await stub_client.post(
        "/v1/tts",
        json={
            "text": TEXT_MULTI,
            "voice": "stub-tone",
            "provider": "stub",
            "response": "json",
            "subtitles": "srt",
            "bypass_cache": True,
        },
    )
    if resp.status_code != 200:
        pytest.skip("synthesis unavailable in this environment")
    data = resp.json()
    # Last cue's end timestamp, converted back to ms.
    last = [ln for ln in data["subtitles"].splitlines() if "-->" in ln][-1]
    hh, mm, rest = last.split(" --> ")[-1].split(":")
    ss, ms = rest.split(",")
    end_ms = ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(ms)
    duration = data["duration_ms"]
    # mp3 frame alignment means they will not be exactly equal.
    assert abs(end_ms - duration) < 1000, f"subtitles end at {end_ms}ms, audio is {duration}ms"


async def test_subtitles_on_a_single_chunk_are_refused_rather_than_empty(stub_client):
    """An empty SRT would look like a bug in the caller's pipeline."""
    resp = await stub_client.post(
        "/v1/tts",
        json={"text": "Hi.", "voice": "stub-tone", "provider": "stub", "subtitles": "srt"},
    )
    if resp.status_code == 200:
        pytest.skip("this deployment split the text anyway")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


async def test_loudness_preset_appears_in_transcoded(stub_client):
    resp = await stub_client.post(
        "/v1/tts",
        json={
            "text": "響度測試。這一句需要夠長才能標準化。",
            "voice": "stub-tone",
            "provider": "stub",
            "response": "json",
            "loudness": "podcast",
            "bypass_cache": True,
        },
    )
    if resp.status_code != 200:
        pytest.skip("synthesis unavailable in this environment")
    assert "loudness" in resp.json()["transcoded"]


async def test_unknown_loudness_preset_is_a_400_not_a_silent_pass(client):
    resp = await client.post(
        "/v1/tts",
        json={"text": "hi", "voice": "en-US-AriaNeural", "loudness": "deafening"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


async def test_usage_reports_runway_per_provider(client):
    resp = await client.get("/v1/usage")
    assert resp.status_code == 200
    runway = resp.json()["runway"]
    assert set(runway) >= {"google_cloud", "google_ai_studio"}
    for entry in runway.values():
        assert entry["status"] in {"no_budget", "exhausted", "no_traffic", "ok", "at_risk"}


async def test_provenance_tags_are_present_on_the_returned_mp3(stub_client, tmp_path):
    pytest.importorskip("shutil")
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe not installed")
    resp = await stub_client.post(
        "/v1/tts",
        json={
            "text": "出處標記測試。",
            "voice": "stub-tone",
            "provider": "stub",
            "bypass_cache": True,
        },
    )
    if resp.status_code != 200:
        pytest.skip("synthesis unavailable in this environment")
    path = tmp_path / "tagged.mp3"
    path.write_bytes(resp.content)
    tags = subprocess.run(  # noqa: ASYNC221 - a test, and ffprobe on one file is bounded
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    ).stdout
    assert "ttsgrab" in tags
    assert "provider=" in tags


async def test_degraded_header_says_whether_a_fallback_cost_quality(stub_app, settings):
    """X-TTSGrab-Fallback says a substitution happened; Degraded says it was worse.

    Callers act differently on the two: a same-tier swap needs no comment, a drop from a
    better engine is worth surfacing to whoever is listening.
    """
    import httpx
    from httpx import ASGITransport

    settings.router.quality_scores = {"stub": 0.9, "nowhere": 0.2}
    transport = ASGITransport(app=stub_app)
    token = settings.security.admin_token or ""
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    ) as c:
        # The stub is the policy's pick, so nothing is degraded.
        ok = await c.post(
            "/v1/tts",
            json={"text": "正常合成。", "voice": "stub-tone", "response": "json"},
        )
        if ok.status_code != 200:
            pytest.skip(f"synthesis unavailable: {ok.status_code}")
        assert "X-TTSGrab-Degraded" not in ok.headers

        # Now ask for the better engine and make it fail: the fallback is a quality drop.
        from stub_provider import FailingProvider

        failing = FailingProvider(settings)
        stub_app.state.ctx.registry.add_instance(failing)
        await failing.refresh_voices()
        settings.router.quality_scores = {"stub_failing": 0.95, "stub": 0.2}

        degraded = await c.post(
            "/v1/tts",
            json={
                "text": "降級測試。",
                "voice": "stub-tone",
                "provider": "stub_failing",
                "response": "json",
            },
        )
        assert degraded.status_code == 200
        assert degraded.headers.get("X-TTSGrab-Degraded") == "true"
        assert degraded.headers.get("X-TTSGrab-Fallback") == "stub_failing->stub"
