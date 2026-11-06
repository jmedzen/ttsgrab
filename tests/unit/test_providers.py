"""Provider adapter tests. Upstream HTTP is mocked with respx; nothing touches the network."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from ttsgrab.core.types import AudioFormat, SynthRequest
from ttsgrab.providers.azure import AzureProvider
from ttsgrab.providers.google_ai_studio import GoogleAIStudioProvider
from ttsgrab.providers.google_cloud import GoogleCloudProvider, tier_from_voice_id

# --------------------------------------------------------------------------------------
# Google Cloud TTS
# --------------------------------------------------------------------------------------


def _google(settings, **overrides) -> GoogleCloudProvider:
    settings.google_cloud.enabled = True
    settings.google_cloud.api_key = "test-key"
    for key, value in overrides.items():
        setattr(settings.google_cloud, key, value)
    return GoogleCloudProvider(settings)


def test_tier_parsing() -> None:
    assert tier_from_voice_id("cmn-TW-Wavenet-A") == "Wavenet"
    assert tier_from_voice_id("en-US-Neural2-C") == "Neural2"
    assert tier_from_voice_id("en-US-Chirp3-HD-Achernar") == "Chirp3-HD"
    assert tier_from_voice_id("cmn-TW-Standard-A") == "Standard"
    assert tier_from_voice_id("en-US-Studio-O") == "Studio"
    assert tier_from_voice_id("weird") == "Standard"


def test_google_disabled_without_credentials(settings) -> None:
    settings.google_cloud.enabled = True
    settings.google_cloud.api_key = None
    settings.google_cloud.credentials_file = None
    provider = GoogleCloudProvider(settings)
    assert provider.enabled is False
    assert "credentials" in (provider.disabled_reason or "").lower()


@pytest.mark.asyncio
@respx.mock
async def test_google_synthesize_reports_cost_and_unsupported(settings) -> None:
    provider = _google(settings)
    await provider.startup()
    respx.get("https://texttospeech.googleapis.com/v1/voices").mock(
        return_value=httpx.Response(
            200,
            json={
                "voices": [
                    {
                        "name": "cmn-TW-Wavenet-A",
                        "languageCodes": ["cmn-TW"],
                        "ssmlGender": "FEMALE",
                    },
                ]
            },
        )
    )
    await provider.refresh_voices()

    audio = b"\xff\xfb" + b"0" * 100
    respx.post("https://texttospeech.googleapis.com/v1/text:synthesize").mock(
        return_value=httpx.Response(200, json={"audioContent": base64.b64encode(audio).decode()})
    )
    req = SynthRequest(
        text="你好",
        voice_id="cmn-TW-Wavenet-A",
        provider="google_cloud",
        format=AudioFormat.MP3,
        volume=0.5,
    )
    result = await provider.synthesize(req)
    assert result.audio == audio
    assert result.billed_characters == 2
    assert "volume" in result.unsupported  # Cloud TTS has no per-request gain
    await provider.shutdown()


@pytest.mark.asyncio
@respx.mock
async def test_google_strict_mode_raises_on_unsupported(settings) -> None:
    from ttsgrab.providers.base import ParamSupportError

    provider = _google(settings)
    await provider.startup()
    provider._voices = []
    req = SynthRequest(
        text="x", voice_id="cmn-TW-Wavenet-A", format=AudioFormat.MP3, volume=0.2, strict=True
    )
    with pytest.raises(ParamSupportError):
        await provider.synthesize(req)
    await provider.shutdown()


@pytest.mark.asyncio
@respx.mock
async def test_google_chirp3_rejects_speed_and_ssml(settings) -> None:
    from ttsgrab.providers.base import ParamSupportError

    provider = _google(settings)
    await provider.startup()
    respx.post("https://texttospeech.googleapis.com/v1/text:synthesize").mock(
        return_value=httpx.Response(200, json={"audioContent": base64.b64encode(b"aud").decode()})
    )
    req = SynthRequest(
        text="hi",
        voice_id="en-US-Chirp3-HD-Kore",
        format=AudioFormat.MP3,
        speed=1.2,
        ssml=True,
        strict=True,
    )
    with pytest.raises(ParamSupportError) as exc:
        await provider.synthesize(req)
    assert "speed" in exc.value.params
    assert "ssml" in exc.value.params
    await provider.shutdown()


@pytest.mark.asyncio
@respx.mock
async def test_google_429_becomes_rate_limited(settings) -> None:
    from ttsgrab.core.errors import UpstreamRateLimited

    provider = _google(settings)
    await provider.startup()
    respx.post("https://texttospeech.googleapis.com/v1/text:synthesize").mock(
        return_value=httpx.Response(429, text="quota")
    )
    with pytest.raises(UpstreamRateLimited):
        await provider.synthesize(
            SynthRequest(text="x", voice_id="cmn-TW-Wavenet-A", format=AudioFormat.MP3)
        )
    await provider.shutdown()


# --------------------------------------------------------------------------------------
# Gemini TTS key pool
# --------------------------------------------------------------------------------------


def _studio(settings, free: list[str], paid: list[str] | None = None) -> GoogleAIStudioProvider:
    settings.google_ai_studio.enabled = True
    settings.google_ai_studio.free_api_keys = free
    settings.google_ai_studio.paid_api_keys = paid or []
    settings.google_ai_studio.model = "gemini-3.8-flash-tts"
    return GoogleAIStudioProvider(settings)


@pytest.mark.asyncio
@respx.mock
async def test_studio_rotates_to_next_key_on_429(settings) -> None:
    provider = _studio(settings, ["free-key-1", "free-key-2"])
    await provider.startup()

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        calls.append(key)
        if key == "free-key-1":
            return httpx.Response(429, text="quota")
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/wav",
                                    "data": base64.b64encode(b"wavdata").decode(),
                                }
                            }
                        ]
                    }
                }
            ]
        }
        return httpx.Response(200, json=payload)

    respx.post(url__regex=r".*generateContent$").mock(side_effect=handler)
    result = await provider.synthesize(
        SynthRequest(text="你好", voice_id="Kore", provider="google_ai_studio")
    )
    assert result.audio == b"wavdata"
    assert len(calls) >= 2
    assert set(calls[:2]) == {"free-key-1", "free-key-2"}
    await provider.shutdown()


@pytest.mark.asyncio
@respx.mock
async def test_studio_key_never_leaks_into_result(settings) -> None:
    provider = _studio(settings, ["secret-key-abc"])
    await provider.startup()
    respx.post(url__regex=r".*generateContent$").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/wav",
                                        "data": base64.b64encode(b"a").decode(),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )
    )
    result = await provider.synthesize(SynthRequest(text="hi", voice_id="Kore"))
    joined = json.dumps(
        {"extra": result.extra, "voice": result.voice_id, "provider": result.provider}
    )
    assert "secret-key-abc" not in joined
    await provider.shutdown()


@pytest.mark.asyncio
@respx.mock
async def test_studio_reports_unsupported_params(settings) -> None:
    provider = _studio(settings, ["k"])
    await provider.startup()
    respx.post(url__regex=r".*generateContent$").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/wav",
                                        "data": base64.b64encode(b"a").decode(),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )
    )
    result = await provider.synthesize(
        SynthRequest(text="hi", voice_id="Kore", pitch=3.0, ssml=True)
    )
    assert "pitch" in result.unsupported
    assert "ssml" in result.unsupported
    await provider.shutdown()


@pytest.mark.asyncio
@respx.mock
async def test_studio_400_does_not_burn_other_keys(settings) -> None:
    from ttsgrab.core.errors import ProviderError

    provider = _studio(settings, ["k1", "k2", "k3"])
    await provider.startup()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("x-goog-api-key", ""))
        return httpx.Response(400, text="bad model")

    respx.post(url__regex=r".*generateContent$").mock(side_effect=handler)
    with pytest.raises(ProviderError):
        await provider.synthesize(SynthRequest(text="hi", voice_id="Kore"))
    assert len(calls) == 1, "a 400 is a request problem, not a key problem"
    await provider.shutdown()


# --------------------------------------------------------------------------------------
# Azure
# --------------------------------------------------------------------------------------


def _azure(settings) -> AzureProvider:
    settings.azure.enabled = True
    settings.azure.api_key = "azure-key"
    settings.azure.region = "eastasia"
    return AzureProvider(settings)


@pytest.mark.asyncio
@respx.mock
async def test_azure_429_is_reported_as_no_charge_occurred(settings) -> None:
    """F0 fails closed; the message must say so, because that is a feature."""
    from ttsgrab.core.errors import UpstreamRateLimited

    provider = _azure(settings)
    await provider.startup()
    respx.post("https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1").mock(
        return_value=httpx.Response(429, text="throttled")
    )
    with pytest.raises(UpstreamRateLimited) as exc:
        await provider.synthesize(
            SynthRequest(text="hi", voice_id="zh-TW-HsiaoChenNeural", format=AudioFormat.MP3)
        )
    assert "no charge" in str(exc.value).lower()
    await provider.shutdown()


@pytest.mark.asyncio
@respx.mock
async def test_azure_builds_ssml_with_prosody(settings) -> None:
    provider = _azure(settings)
    await provider.startup()
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, content=b"mp3bytes")

    respx.post("https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1").mock(
        side_effect=handler
    )
    await provider.synthesize(
        SynthRequest(
            text="你好",
            voice_id="zh-TW-HsiaoChenNeural",
            format=AudioFormat.MP3,
            speed=1.2,
            pitch=2.0,
            language="zh-TW",
        )
    )
    body = captured["body"]
    assert 'rate="+20%"' in body
    assert 'pitch="+2.0st"' in body
    assert "zh-TW-HsiaoChenNeural" in body
    await provider.shutdown()


@pytest.mark.asyncio
@respx.mock
async def test_azure_escapes_xml(settings) -> None:
    provider = _azure(settings)
    await provider.startup()
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, content=b"x")

    respx.post("https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1").mock(
        side_effect=handler
    )
    await provider.synthesize(
        SynthRequest(
            text="a < b & c > d",
            voice_id="zh-TW-HsiaoChenNeural",
            format=AudioFormat.MP3,
            language="zh-TW",
        )
    )
    assert "&lt;" in captured["body"]
    assert "&amp;" in captured["body"]
    await provider.shutdown()


# --------------------------------------------------------------------------------------
# Edge (no network: patch the edge_tts module surface)
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge_maps_speed_and_pitch_to_ssml_prosody(settings, monkeypatch) -> None:
    from ttsgrab.providers import edge as edge_mod

    captured: dict[str, object] = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            captured["text"] = text
            captured["voice"] = voice
            captured["kwargs"] = kwargs

        async def stream(self):
            yield {"type": "audio", "data": b"mp3"}

    monkeypatch.setattr(edge_mod, "edge_tts", None, raising=False)
    import sys
    import types

    fake_module = types.SimpleNamespace(
        Communicate=FakeCommunicate,
        list_voices=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "edge_tts", fake_module)

    settings.edge.enabled = True
    provider = edge_mod.EdgeProvider(settings)
    provider._voices = provider._voices or []
    result = await provider.synthesize(
        SynthRequest(
            text="hello",
            voice_id="en-US-AriaNeural",
            format=AudioFormat.MP3,
            speed=1.2,
            pitch=2.0,
        )
    )
    assert result.audio == b"mp3"
    kwargs = captured["kwargs"]
    assert kwargs["rate"] == "+20%"
    assert kwargs["pitch"] == "+24Hz"


@pytest.mark.asyncio
async def test_edge_reports_transcode_need_for_wav(settings, monkeypatch) -> None:
    import sys
    import types

    from ttsgrab.providers import edge as edge_mod

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"mp3"}

    fake = types.SimpleNamespace(Communicate=FakeCommunicate, list_voices=lambda: None)
    monkeypatch.setitem(sys.modules, "edge_tts", fake)

    settings.edge.enabled = True
    provider = edge_mod.EdgeProvider(settings)
    result = await provider.synthesize(
        SynthRequest(text="hello", voice_id="en-US-AriaNeural", format=AudioFormat.WAV)
    )
    assert "format" in result.unsupported
    # It still returns audio — the pipeline transcodes downstream.
    assert result.format is AudioFormat.MP3


def test_every_builtin_module_actually_registers_its_provider() -> None:
    """A provider module that imports cleanly but never calls @register is invisible.

    That failure is silent: no warning, no error, the provider simply never appears in
    /v1/providers and the fallback chain quietly skips it. This caught exactly that when
    the Gemini adapter was renamed to Google AI Studio.
    """
    import importlib

    from ttsgrab.core.registry import BUILTIN_MODULES, registered_classes

    for mod_name in BUILTIN_MODULES:
        importlib.import_module(mod_name)  # a raise here is the failure we want

    registered = set(registered_classes())

    # Modules whose provider is registered under a different name than the module.
    aliases = {
        "ttsgrab.providers.gtts": {"gtts", "translate_tts"},
    }
    for mod_name in BUILTIN_MODULES:
        expected = aliases.get(mod_name, {mod_name.rsplit(".", 1)[1]})
        assert expected & registered, (
            f"{mod_name} imported but registered none of {sorted(expected)}; "
            "is the @register decorator missing?"
        )
