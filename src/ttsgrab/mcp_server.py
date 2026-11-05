"""MCP server — the same gateway, addressed by an agent instead of a browser.

Why this exists on top of REST: an MCP client (Claude Desktop, Cursor, an agent runtime)
gets tool schemas with descriptions, so the model does not have to be told the API shape
in every prompt. The tools are thin wrappers over the *same* synthesis pipeline, so
routing, caching, budgeting and history all behave identically.

Transport: streamable HTTP at ``/mcp``. That is what current clients support and it needs
no stdio subprocess, which matters for a containerised deployment.

Run standalone (optional, for clients that prefer a separate process):
    pip install "ttsgrab[mcp]"
    python -m ttsgrab.mcp_server --url http://127.0.0.1:8000 --token ...
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from typing import Any

import httpx


# The MCP Python SDK renamed FastMCP to MCPServer in 2.0. Support both so the gateway
# works with whatever version a user's environment resolves to.
def _load_sdk() -> tuple[type, str]:
    """Return the SDK's server class, tolerating the 1.x → 2.x rename.

    A local import keeps the optional dependency optional, and probing the package rather
    than importing both keeps mypy from seeing two definitions of the same name.
    """
    try:
        import mcp.server as pkg

        if hasattr(pkg, "mcpserver"):
            from mcp.server.mcpserver import MCPServer

            return MCPServer, "2.x"
        from mcp.server.fastmcp import FastMCP  # type: ignore[attr-defined]

        return FastMCP, "1.x"
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            'The MCP server needs the optional dependency: pip install "ttsgrab[mcp]"'
        ) from exc


_ServerClass, _SDK = _load_sdk()

MCP = _ServerClass(
    name="ttsgrab",
    instructions=(
        "Unified text-to-speech gateway. Call list_voices before synthesising: never "
        "invent a voice id. Prefer policy='cheapest' or free_only=true for bulk work. "
        "Check the returned 'unsupported' list — it names parameters the provider ignored."
    ),
)


# When mounted inside the gateway, tools call the app in-process instead of over
# loopback HTTP. That removes a whole class of "works on my machine" failures — wrong
# port, container-localhost, TLS, a reverse proxy rewriting paths — and it is faster.
_MOUNTED_APP = None
_ADMIN_TOKEN: str | None = None


def _client() -> tuple[httpx.AsyncClient, str]:
    token = (
        os.environ.get("TTSGRAB_MCP_TOKEN")
        or os.environ.get("TTSGRAB_ADMIN_TOKEN")
        or _ADMIN_TOKEN
        or ""
    )
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    if _MOUNTED_APP is not None:
        return (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_MOUNTED_APP),
                base_url="http://ttsgrab.internal",
                headers=headers,
                timeout=180.0,
            ),
            "http://ttsgrab.internal",
        )

    base = os.environ.get("TTSGRAB_MCP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    return httpx.AsyncClient(base_url=base, headers=headers, timeout=180.0), base


@MCP.tool(name="list_voices")
async def list_voices(
    language: str | None = None,
    provider: str | None = None,
    free_only: bool = False,
    limit: int = 40,
) -> str:
    """List available voices.

    Args:
        language: Filter by language tag prefix, e.g. "zh-TW", "en", "ja".
        provider: Filter to one provider name (see list_providers).
        free_only: Only voices that cost nothing to use.
        limit: Maximum number of voices to return.
    """
    params: dict[str, Any] = {"limit": limit}
    if language:
        params["language"] = language
    if provider:
        params["provider"] = provider
    if free_only:
        params["free_only"] = "true"

    client, _ = _client()
    async with client:
        resp = await client.get("/v1/voices", params=params)
        resp.raise_for_status()
        data = resp.json()

    lines = [f"{data.get('total', 0)} voices match; showing up to {limit}:"]
    for v in data.get("voices", []):
        flags = []
        if v.get("cost_class") == "free":
            flags.append("free")
        if v.get("cloning"):
            flags.append("cloneable")
        if v.get("streaming"):
            flags.append("stream")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"- {v['voice_id']} ({v['provider']}) — {v['name']}, "
            f"{'/'.join(v.get('languages', []))}{suffix}"
        )
    return "\n".join(lines)


@MCP.tool(name="list_providers")
async def list_providers() -> str:
    """List configured providers with availability, cost class and stability risk.

    Providers marked official=false are unofficial interfaces: they may break without
    notice and their terms may not permit automated use. Prefer official ones for anything
    that matters.
    """
    client, _ = _client()
    async with client:
        resp = await client.get("/v1/providers")
        resp.raise_for_status()
        data = resp.json()

    lines = []
    for p in data.get("providers", []):
        reason = p.get("reason") or "not configured"
        state = "enabled" if p.get("enabled") else f"disabled — {reason}"
        tag = "official" if p.get("official") else "UNOFFICIAL"
        health = "up" if p.get("up") else ("down" if p.get("up") is False else "unknown")
        lines.append(
            f"- {p['name']} [{tag}, {p.get('cost_class')}, risk={p.get('stability')}]: "
            f"{state}, {p.get('voices', 0)} voices, health={health}"
        )
    return "\n".join(lines) or "no providers configured"


@MCP.tool(name="synthesize_speech")
async def synthesize_speech(
    text: str,
    voice: str,
    provider: str | None = None,
    fmt: str = "mp3",
    speed: float = 1.0,
    policy: str | None = None,
    strict: bool = False,
) -> str:
    """Synthesise speech and return the audio as base64 plus metadata.

    Args:
        text: The text to speak. Up to a few thousand characters; longer text is split
            into sentences and synthesised in parallel.
        voice: A voice_id from list_voices.
        provider: Force a specific provider instead of letting the router choose.
        fmt: Output container: mp3 (default), wav, opus or flac.
        speed: 0.5–2.0. Values outside a provider's support are reported, not silently
            clamped.
        policy: Routing policy: cheapest, fastest, best_quality, local_only, free_only.
        strict: When true, refuse rather than dropping an unsupported parameter.
    """
    payload: dict[str, Any] = {
        "text": text,
        "voice": voice,
        "format": fmt,
        "speed": speed,
        "strict": strict,
        "response": "json",
    }
    if provider:
        payload["provider"] = provider
    if policy:
        payload["policy"] = policy

    client, _ = _client()
    async with client:
        resp = await client.post("/v1/tts", json=payload)
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                return f"error {resp.status_code}: {resp.text[:400]}"
            return json.dumps(err, ensure_ascii=False, indent=2)
        data = resp.json()

    audio_b64 = data.get("audio_base64") or ""
    size = len(base64.b64decode(audio_b64)) if audio_b64 else 0
    meta = {
        "provider": data.get("provider"),
        "voice_id": data.get("voice_id"),
        "format": data.get("format"),
        "bytes": size,
        "duration_ms": data.get("duration_ms"),
        "characters": data.get("characters"),
        "cost_usd": data.get("cost_usd"),
        "cached": data.get("cached"),
        "unsupported": data.get("unsupported", []),
    }
    return json.dumps(meta, ensure_ascii=False, indent=2) + "\n\naudio_base64:\n" + audio_b64


@MCP.tool(name="get_usage")
async def get_usage() -> str:
    """Report this month's character usage and cost, per provider, plus free-tier headroom."""
    client, _ = _client()
    async with client:
        resp = await client.get("/v1/usage")
        resp.raise_for_status()
        data = resp.json()

    t = data.get("totals", {})
    lines = [
        f"Period {data['period']['start'][:10]} → {data['period']['end'][:10]}",
        f"Characters: {t.get('characters', 0):,} · requests: {t.get('requests', 0):,} "
        f"· cost: ${t.get('cost_micro_usd', 0) / 1_000_000:.4f} "
        f"· cache hits: {t.get('cached_hits', 0):,}",
        "",
        "Per provider:",
    ]
    for name, row in (data.get("by_provider") or {}).items():
        lines.append(
            f"- {name}: {row['characters']:,} chars, ${row['cost_micro_usd'] / 1_000_000:.4f}"
        )
    free = data.get("free_tier") or {}
    if free:
        lines.append("")
        lines.append("Free tier remaining:")
        for provider, tiers in free.items():
            for tier in tiers:
                lines.append(
                    f"- {provider}/{tier['tier']}: {tier['remaining']:,} of "
                    f"{tier['limit']:,} left ({tier['used_pct']}% used)"
                )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="ttsgrab MCP server")
    parser.add_argument("--url", default=os.environ.get("TTSGRAB_MCP_BASE_URL"))
    parser.add_argument("--token", default=os.environ.get("TTSGRAB_MCP_TOKEN"))
    parser.add_argument(
        "--transport", default="streamable-http", choices=["stdio", "sse", "streamable-http"]
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.url:
        os.environ["TTSGRAB_MCP_BASE_URL"] = args.url
    if args.token:
        os.environ["TTSGRAB_MCP_TOKEN"] = args.token

    MCP.run(transport=args.transport, host=args.host, port=args.port)


def mount(app, path: str = "/mcp"):
    """Attach the MCP streamable-HTTP app to the gateway at ``path``.

    Returns the inner app so the caller can run its lifespan. Starlette does **not** run
    the lifespan of a mounted sub-application, and the MCP streamable-HTTP transport keeps
    its session task group there — without entering it, every request fails with
    "Task group is not initialized". So the gateway enters it explicitly.

    The SDK's inner app routes on ``/mcp`` by default, which would become ``/mcp/mcp``
    after mounting, so its own path is set to ``/`` and the mount point is public.
    """
    global _MOUNTED_APP, _ADMIN_TOKEN

    from mcp.server.transport_security import TransportSecuritySettings

    # Serve tool calls from the app we are mounted in, not from a guessed URL.
    _MOUNTED_APP = app
    security = getattr(getattr(app, "state", None), "settings", None)
    if security is not None:
        _ADMIN_TOKEN = getattr(security.security, "admin_token", None)

    inner = MCP.streamable_http_app(
        streamable_http_path="/",
        # The gateway is commonly reached by a LAN hostname or container name; the SDK's
        # DNS-rebinding protection allows only localhost by default.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.mount(path, inner)
    return inner


if __name__ == "__main__":
    main()
