"""Integration tests against the real application and the real HTTP surface.

Network providers are replaced with fakes, so these are deterministic: they verify the
wiring between the API layer, the pipeline, the database and the cache.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeBehaviour, FakeProvider, install
from ttsgrab.core.types import CostClass


def a_fake(ctx, **kw) -> FakeProvider:
    provider = FakeProvider(ctx.settings, name="fake", **kw)
    install(ctx, provider)
    return provider


@pytest.mark.asyncio
async def test_healthz_and_readyz(client) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = await client.get("/readyz")
    body = r.json()
    assert r.status_code == 200, body
    assert body["checks"]["database"] == "ok"


@pytest.mark.asyncio
async def test_voice_catalogue_returns_voices(client, settings) -> None:
    r = await client.get("/v1/voices")
    assert r.status_code == 200
    data = r.json()
    assert "voices" in data
    assert "total" in data
    # Edge is enabled by default, so the catalogue is never empty in a clean checkout.
    assert data["total"] > 0
    voice = data["voices"][0]
    for field in ("provider", "voice_id", "name", "languages", "cost_class"):
        assert field in voice


@pytest.mark.asyncio
async def test_voice_filters(client) -> None:
    r = await client.get("/v1/voices", params={"language": "zh-TW", "limit": 5})
    data = r.json()
    for v in data["voices"]:
        assert any(lg.startswith("zh-TW") for lg in v["languages"])

    r = await client.get("/v1/voices", params={"q": "zzzznotavoice"})
    assert r.json()["count"] == 0
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_providers_reports_disabled_reason(client) -> None:
    r = await client.get("/v1/providers")
    assert r.status_code == 200
    providers = {p["name"]: p for p in r.json()["providers"]}
    assert "edge" in providers
    assert providers["edge"]["enabled"] is True
    assert providers["edge"]["official"] is False  # honesty about unofficial interfaces
    assert providers["edge"]["stability"] == "fragile"
    # Google is not configured, so it must be explicitly disabled with a reason.
    g = providers["google_cloud"]
    assert g["enabled"] is False
    assert g["reason"]


@pytest.mark.asyncio
async def test_capabilities(client) -> None:
    r = await client.get("/v1/capabilities")
    body = r.json()
    assert "mp3" in body["formats"]
    assert "cheapest" in body["policies"]
    assert body["max_text_chars"] > 0


@pytest.mark.asyncio
async def test_estimate_does_not_synthesise(client) -> None:
    r = await client.post(
        "/v1/tts/estimate",
        json={"text": "這是一段估算用的文字。", "voice": "zh-TW-HsiaoChenNeural"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["estimated_duration_ms"] > 0
    assert body["characters"] == len("這是一段估算用的文字。")
    assert body["estimated_cost_micro_usd"] == 0  # edge is free


@pytest.mark.asyncio
async def test_tts_json_response_shape(client, ctx) -> None:
    a_fake(ctx)
    r = await client.post(
        "/v1/tts",
        json={"text": "hello", "voice": "fake-voice", "response": "json", "format": "wav"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "fake"
    assert body["audio_base64"]
    assert body["bytes"] > 0
    assert body["characters"] == 5
    assert body["history_id"]


@pytest.mark.asyncio
async def test_tts_audio_response_has_headers(client, ctx) -> None:
    a_fake(ctx)
    # Unique text: the disk cache is shared across a test session, so reusing a string
    # would make this assertion depend on test order.
    r = await client.post(
        "/v1/tts",
        json={"text": "headers check unique text", "voice": "fake-voice", "format": "wav"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/")
    assert r.headers["x-ttsgrab-provider"] == "fake"
    assert r.headers["x-ttsgrab-cached"] == "false"
    assert len(r.content) > 100


@pytest.mark.asyncio
async def test_second_request_is_cached(client, ctx) -> None:
    a_fake(ctx)
    payload = {"text": "cache me", "voice": "fake-voice", "format": "wav"}
    first = await client.post("/v1/tts", json=payload)
    second = await client.post("/v1/tts", json=payload)
    assert first.headers["x-ttsgrab-cached"] == "false"
    assert second.headers["x-ttsgrab-cached"] == "true"
    assert first.content == second.content


@pytest.mark.asyncio
async def test_bypass_cache_forces_synthesis(client, ctx) -> None:
    provider = a_fake(ctx)
    payload = {"text": "fresh", "voice": "fake-voice", "format": "wav"}
    await client.post("/v1/tts", json=payload)
    r = await client.post("/v1/tts", json={**payload, "bypass_cache": True})
    assert r.headers["x-ttsgrab-cached"] == "false"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_strict_mode_returns_422_for_unsupported(client, ctx) -> None:
    provider = FakeProvider(ctx.settings, name="fake")
    provider.behaviour = FakeBehaviour()
    provider.capabilities.__class__  # noqa: B018 - keep the reference explicit
    install(ctx, provider)

    # The fake claims ssml support is absent; request SSML with strict on.
    r = await client.post(
        "/v1/tts",
        json={"text": "x", "voice": "fake-voice", "ssml": True, "strict": True},
    )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "unsupported_param"
    assert body["retryable"] is False
    assert "ssml" in body["details"]["params"]


@pytest.mark.asyncio
async def test_text_over_limit_is_rejected(client, settings) -> None:
    limit = settings.security.max_text_chars
    r = await client.post(
        "/v1/tts", json={"text": "a" * (limit + 1), "voice": "zh-TW-HsiaoChenNeural"}
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "text_too_long"


@pytest.mark.asyncio
async def test_empty_text_is_a_validation_error(client) -> None:
    r = await client.post("/v1/tts", json={"text": "", "voice": "zh-TW-HsiaoChenNeural"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] in {"invalid_request", "unsupported_param"}


@pytest.mark.asyncio
async def test_unknown_voice_fails_cleanly(client, ctx) -> None:
    a_fake(ctx)
    r = await client.post("/v1/tts", json={"text": "x", "voice": "no-such-voice"})
    assert r.status_code in (404, 503)
    assert r.json()["error"]["code"] in {"not_found", "all_providers_failed"}


@pytest.mark.asyncio
async def test_history_records_and_replays(client, ctx) -> None:
    a_fake(ctx)
    r = await client.post(
        "/v1/tts", json={"text": "history me", "voice": "fake-voice", "format": "wav"}
    )
    history_id = r.headers["x-ttsgrab-history-id"]

    listing = await client.get("/v1/history")
    assert listing.status_code == 200
    ids = [item["id"] for item in listing.json()["items"]]
    assert history_id in ids

    audio = await client.get(f"/v1/history/{history_id}/audio")
    assert audio.status_code == 200
    assert audio.content == r.content

    removed = await client.delete(f"/v1/history/{history_id}")
    assert removed.status_code == 200
    assert (await client.get(f"/v1/history/{history_id}/audio")).status_code == 404


@pytest.mark.asyncio
async def test_usage_reflects_synthesis(client, ctx) -> None:
    a_fake(ctx)
    await client.post(
        "/v1/tts", json={"text": "usage test", "voice": "fake-voice", "format": "wav"}
    )
    r = await client.get("/v1/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["requests"] >= 1
    assert body["totals"]["characters"] >= 10
    assert "fake" in body["by_provider"]


@pytest.mark.asyncio
async def test_metrics_expose_counters(client, ctx) -> None:
    a_fake(ctx)
    await client.post("/v1/tts", json={"text": "metrics", "voice": "fake-voice", "format": "wav"})
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "ttsgrab_synthesis_requests_total" in r.text


@pytest.mark.asyncio
async def test_llms_txt_and_agents_md_are_served(client) -> None:
    for path in ("/llms.txt", "/agents.md"):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "ttsgrab" in r.text.lower()
        assert "/v1/tts" in r.text


@pytest.mark.asyncio
async def test_llms_txt_lists_enabled_providers(client) -> None:
    r = await client.get("/llms.txt")
    assert "edge" in r.text


@pytest.mark.asyncio
async def test_openapi_schema_is_valid(client) -> None:
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["openapi"].startswith("3.")
    assert "/v1/tts" in schema["paths"]
    assert "/v1/voices" in schema["paths"]


@pytest.mark.asyncio
async def test_api_key_crud_and_auth(client, settings) -> None:
    created = await client.post("/v1/keys", json={"name": "test-agent"})
    assert created.status_code == 200
    token = created.json()["token"]
    key_id = created.json()["id"]
    assert token.startswith("ttsg_")

    # The token must never appear in the list endpoint again.
    listing = await client.get("/v1/keys")
    assert token not in listing.text
    assert key_id in [k["id"] for k in listing.json()["keys"]]

    revoked = await client.delete(f"/v1/keys/{key_id}")
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] == key_id


@pytest.mark.asyncio
async def test_openai_speech_endpoint(client, ctx) -> None:
    a_fake(ctx)
    r = await client.post(
        "/v1/audio/speech",
        json={
            "model": "tts-1",
            "input": "hello there",
            "voice": "fake-voice",
            "response_format": "wav",
        },
    )
    assert r.status_code == 200, r.text
    assert len(r.content) > 100
    assert r.headers["x-ttsgrab-provider"] == "fake"


@pytest.mark.asyncio
async def test_openai_endpoint_returns_openai_error_shape(client, ctx) -> None:
    a_fake(ctx)
    r = await client.post(
        "/v1/audio/speech",
        json={"model": "tts-1", "input": "x", "voice": "no-such-voice-at-all"},
    )
    assert r.status_code >= 400
    body = r.json()
    assert "error" in body
    assert "message" in body["error"]


@pytest.mark.asyncio
async def test_openai_models_endpoint(client) -> None:
    r = await client.get("/v1/models")
    assert r.status_code == 200
    assert r.json()["object"] == "list"


@pytest.mark.asyncio
async def test_compare_returns_metrics_per_target(client, ctx) -> None:
    a = FakeProvider(ctx.settings, name="a")
    b = FakeProvider(ctx.settings, name="b")
    a._voices[0] = a._voices[0].model_copy(update={"voice_id": "a-voice"})
    b._voices[0] = b._voices[0].model_copy(update={"voice_id": "b-voice"})
    install(ctx, a, b)

    r = await client.post(
        "/v1/compare",
        json={
            "text": "compare",
            "format": "wav",
            "targets": [
                {"provider": "a", "voice": "a-voice"},
                {"provider": "b", "voice": "b-voice"},
            ],
        },
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 2
    assert {res["provider"] for res in results} == {"a", "b"}
    for res in results:
        assert "error" not in res
        assert res["bytes"] > 0
        assert res["audio_url"]


@pytest.mark.asyncio
async def test_compare_one_bad_target_does_not_fail_the_rest(client, ctx) -> None:
    a_fake(ctx)
    r = await client.post(
        "/v1/compare",
        json={
            "text": "x",
            "format": "wav",
            "targets": [
                {"provider": "fake", "voice": "fake-voice"},
                {"provider": "nonexistent", "voice": "nope"},
            ],
        },
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert any("error" in res for res in results)
    assert any("error" not in res for res in results)


@pytest.mark.asyncio
async def test_job_lifecycle(client, ctx) -> None:
    a_fake(ctx)
    created = await client.post(
        "/v1/jobs", json={"text": "async job", "voice": "fake-voice", "format": "wav"}
    )
    assert created.status_code == 200
    job_id = created.json()["id"]
    assert created.json()["status"] in {"queued", "running", "succeeded"}

    for _ in range(50):
        polled = await client.get(f"/v1/jobs/{job_id}")
        assert polled.status_code == 200
        if polled.json()["status"] in {"succeeded", "failed"}:
            break
        await __import__("asyncio").sleep(0.05)
    body = polled.json()
    assert body["status"] == "succeeded", body
    assert body["result"]["provider"] == "fake"


@pytest.mark.asyncio
async def test_worker_registration_and_removal(client, ctx) -> None:
    # Registration validates the URL, instantiates the provider, then tries a health check.
    # Point at a closed port so the un-reachable path is exercised.
    created = await client.post(
        "/v1/workers",
        json={
            "name": "ghost",
            "base_url": "http://127.0.0.1:59999",
            "kind": "tts",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["healthy"] is False
    assert body["last_error"]

    listing = await client.get("/v1/workers")
    assert "ghost" in [w["name"] for w in listing.json()["workers"]]

    removed = await client.delete(f"/v1/workers/{body['id']}")
    assert removed.status_code == 200
    assert (await client.get("/v1/workers")).json()["workers"] == []


@pytest.mark.asyncio
async def test_worker_url_is_ssrf_checked(client) -> None:
    r = await client.post(
        "/v1/workers",
        json={"name": "metadata", "base_url": "http://169.254.169.254/", "kind": "tts"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_duplicate_worker_name_is_rejected(client) -> None:
    payload = {"name": "dup", "base_url": "http://127.0.0.1:59998", "kind": "tts"}
    first = await client.post("/v1/workers", json=payload)
    assert first.status_code == 200
    second = await client.post("/v1/workers", json=payload)
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_reference_upload_requires_consent_note(client) -> None:
    wav = b"RIFF" + b"\x00" * 40 + b"WAVE"
    r = await client.post(
        "/v1/references",
        files={"file": ("ref.wav", wav, "audio/wav")},
        data={"name": "no-consent", "provider": "fake"},
    )
    assert r.status_code == 400
    assert "consent" in r.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_reference_upload_validates_magic_bytes(client) -> None:
    r = await client.post(
        "/v1/references",
        files={"file": ("fake.wav", b"not audio at all", "audio/wav")},
        data={"name": "bad", "provider": "fake", "consent_note": "mine"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_provider_refresh_reloads_voices(client) -> None:
    r = await client.post("/v1/providers/edge/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["voices"] > 0


@pytest.mark.asyncio
async def test_provider_refresh_unknown_is_404(client) -> None:
    r = await client.post("/v1/providers/does-not-exist/refresh")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_request_id_present_on_errors(client, ctx) -> None:
    a_fake(ctx)
    r = await client.post("/v1/tts", json={"text": "x", "voice": "nope"})
    if r.status_code >= 400:
        assert r.json()["error"]["request_id"]


@pytest.mark.asyncio
async def test_fallback_into_an_over_budget_provider_is_refused(client, ctx) -> None:
    """A budget must not be escapable by falling back.

    The dangerous shape: the first provider fails, the router moves to an expensive one,
    and the cap was only ever evaluated against the provider that was *requested*.
    """
    from ttsgrab.db.session import session_scope

    # The generic check reads `monthly_budget_chars` off the config section named after the
    # provider, so a fake provider named "azure" exercises the same code path a real one does.
    ctx.settings.azure.monthly_budget_chars = 10
    async with session_scope() as session:
        await ctx.usage.record(
            session,
            provider="azure",
            voice_id="x",
            tier="neural",
            characters=100,
            cost_micro_usd=0,
            free_tier=True,
            cached=False,
            api_key_id=None,
            job_id=None,
        )

    flaky = FakeProvider(ctx.settings, name="flaky", behaviour=FakeBehaviour(failures=99))
    flaky._voices[0] = flaky._voices[0].model_copy(update={"voice_id": "shared-voice"})
    paid = FakeProvider(ctx.settings, name="azure", cost_class=CostClass.FREE_TIER)
    paid._voices[0] = paid._voices[0].model_copy(
        update={"voice_id": "shared-voice", "tier": "neural"}
    )
    install(ctx, flaky, paid)
    # azure must be reachable as a fallback; the default chain now includes it.
    assert "azure" in ctx.settings.router.fallback_chain

    r = await client.post(
        "/v1/tts",
        json={
            "text": "fallback please",
            "voice": "shared-voice",
            "policy": "cheapest",
            "allow_fallback": True,
        },
    )
    assert r.status_code == 402, f"expected a refusal, got {r.status_code}: {r.text}"
    assert r.json()["error"]["code"] == "budget_exceeded"
    assert r.json()["error"]["details"]["provider"] == "azure"
    assert paid.calls == [], "the over-budget provider must never be called"


@pytest.mark.asyncio
async def test_every_provider_with_a_budget_enforces_it(ctx) -> None:
    """Generic, not special-cased: a provider that declares a budget must honour it."""
    from ttsgrab.core.errors import BudgetExceeded
    from ttsgrab.db.session import session_scope

    for provider in ("google_cloud", "gemini", "azure"):
        cfg = getattr(ctx.settings, provider)
        cfg.monthly_budget_chars = 10
        async with session_scope() as session:
            await ctx.usage.record(
                session,
                provider=provider,
                voice_id="x",
                tier=None,
                characters=100,
                cost_micro_usd=0,
                free_tier=True,
                cached=False,
                api_key_id=None,
                job_id=None,
            )
            with pytest.raises(BudgetExceeded):
                await ctx.usage.check_budget(session, provider=provider, tier=None, characters=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["alloy", "nova", "shimmer", "echo", "fable", "onyx"])
async def test_openai_voice_aliases_resolve_to_existing_voices(client, ctx, alias) -> None:
    """The SDK's default voice is "alloy", which means nothing to any real engine.

    An unchanged OpenAI client sends that name, so it has to map to something the
    catalogue actually contains — otherwise the first request anyone makes fails.
    """
    from ttsgrab.api.v1.openai_compat import ALIASES, _resolve_voice

    a_fake(ctx)
    provider = ctx.registry.get("fake")
    assert provider is not None

    # Publish every voice the alias table can point at, so the table itself is under test.
    from ttsgrab.core.types import AudioFormat, Voice

    candidates = {v for mapping in ALIASES.values() for v in mapping.values()}
    provider._voices = [
        Voice(
            provider="fake",
            voice_id=voice_id,
            name=voice_id,
            languages=["en-US"],
            formats=[AudioFormat.WAV],
        )
        for voice_id in sorted(candidates)
    ]
    available = {v.voice_id for v in provider._voices}

    for language in ("en-US", "zh-TW"):
        resolved = _resolve_voice(ctx, alias, language, None)
        assert resolved in available, (
            f"{alias} resolved to {resolved!r}, which no provider offers "
            f"(available: {sorted(available)})"
        )


@pytest.mark.asyncio
async def test_openai_real_voice_id_beats_an_alias(client, ctx) -> None:
    """If someone names a real voice that happens to collide with an alias, the real voice
    must win — otherwise they silently get a different speaker."""
    from ttsgrab.api.v1.openai_compat import _resolve_voice

    a_fake(ctx)
    provider = ctx.registry.get("fake")
    assert provider is not None
    from ttsgrab.core.types import AudioFormat, Voice

    provider._voices = [
        Voice(
            provider="fake",
            voice_id="alloy",
            name="Someone named their voice alloy",
            languages=["en-US"],
            formats=[AudioFormat.WAV],
        )
    ]
    assert _resolve_voice(ctx, "alloy", "en-US", None) == "alloy"


@pytest.mark.asyncio
async def test_openai_unknown_voice_is_refused_clearly(client, ctx) -> None:
    a_fake(ctx)
    r = await client.post(
        "/v1/audio/speech",
        json={"model": "tts-1", "input": "x", "voice": "definitely-not-a-voice"},
    )
    assert r.status_code >= 400
    body = r.json()
    assert "error" in body and "message" in body["error"]


@pytest.mark.asyncio
async def test_openai_alias_request_succeeds_end_to_end(client, ctx) -> None:
    """The whole point: an unchanged SDK call with voice="alloy" returns audio."""
    a_fake(ctx)
    provider = ctx.registry.get("fake")
    assert provider is not None
    from ttsgrab.core.types import AudioFormat, Voice

    provider._voices = [
        Voice(
            provider="fake",
            voice_id="en-US-AriaNeural",
            name="Aria",
            languages=["en-US"],
            formats=[AudioFormat.WAV],
        )
    ]
    r = await client.post(
        "/v1/audio/speech",
        json={
            "model": "tts-1",
            "input": "hello from an unchanged SDK",
            "voice": "alloy",
            "response_format": "wav",
        },
    )
    assert r.status_code == 200, r.text
    assert len(r.content) > 100


@pytest.mark.asyncio
async def test_compare_reports_a_metric_row_per_target(client, ctx) -> None:
    """The Compare page is only useful if every target comes back with numbers attached —
    a row that is all nulls looks like a working comparison and tells you nothing."""
    from ttsgrab.core.types import AudioFormat, Voice

    providers = []
    for name in ("a", "b", "c"):
        provider = FakeProvider(ctx.settings, name=name)
        provider._voices = [
            Voice(
                provider=name,
                voice_id=f"{name}-v",
                name=name,
                languages=["en-US"],
                formats=[AudioFormat.WAV],
            )
        ]
        providers.append(provider)
    install(ctx, *providers)

    r = await client.post(
        "/v1/compare",
        json={
            "text": "compare me",
            "targets": [{"provider": n, "voice_id": f"{n}-v"} for n in ("a", "b", "c")],
        },
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert len(results) == 3
    for row in results:
        # A successful row omits `error` entirely rather than sending null.
        assert not row.get("error"), row
        assert row["bytes"] > 0
        assert row["latency_ms"] is not None
        assert row["rtf"] is not None
        assert "first_byte_ms" in row


@pytest.mark.asyncio
async def test_compare_isolates_a_failing_target(client, ctx) -> None:
    """One dead provider must not lose the other two results."""
    from tests.fakes import FakeBehaviour
    from ttsgrab.core.types import AudioFormat, Voice

    good = FakeProvider(ctx.settings, name="good")
    good._voices = [
        Voice(
            provider="good",
            voice_id="g-v",
            name="g",
            languages=["en-US"],
            formats=[AudioFormat.WAV],
        )
    ]
    bad = FakeProvider(ctx.settings, name="bad", behaviour=FakeBehaviour(failures=99))
    bad._voices = [
        Voice(
            provider="bad",
            voice_id="b-v",
            name="b",
            languages=["en-US"],
            formats=[AudioFormat.WAV],
        )
    ]
    install(ctx, good, bad)

    r = await client.post(
        "/v1/compare",
        json={
            "text": "half broken",
            "targets": [
                {"provider": "good", "voice_id": "g-v"},
                {"provider": "bad", "voice_id": "b-v"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    rows = {row["provider"]: row for row in r.json()["results"]}
    assert rows["good"]["bytes"] > 0
    assert rows["bad"].get("error")
