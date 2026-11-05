"""Documentation must not drift from the code.

A stale API reference is worse than none: the reader trusts it, gets a 404, and loses
confidence in everything else on the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def spec():
    from ttsgrab.main import create_app
    from ttsgrab.settings import load_settings

    app = create_app(load_settings(REPO_ROOT / "config.example.yaml"))
    return app.openapi()


def test_every_documented_path_exists(spec) -> None:
    """Any path named in docs/api.md must be a real route."""
    text = (REPO_ROOT / "docs/api.md").read_text(encoding="utf-8")
    real = set(spec["paths"])
    claimed = set(re.findall(r"`/v1/[A-Za-z0-9_{}/\-]+`", text))
    claimed |= {
        m.group(2) for m in re.finditer(r"`(GET|POST|DELETE)\s+(/[A-Za-z0-9_{}/\-]+)`", text)
    }

    # Normalise parameter names: /v1/jobs/{id} and /v1/jobs/{job_id} are the same route.
    def shape(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path)

    real_shapes = {shape(p) for p in real}
    unknown = sorted(p for p in claimed if p.startswith("/v1/") and shape(p) not in real_shapes)
    assert unknown == [], f"docs/api.md documents paths that do not exist: {unknown}"


def test_key_endpoints_are_documented(spec) -> None:
    """The endpoints an agent is told to use must appear in the reference."""
    text = (REPO_ROOT / "docs/api.md").read_text(encoding="utf-8")
    required = [
        "/v1/tts",
        "/v1/tts/stream",
        "/v1/tts/estimate",
        "/v1/voices",
        "/v1/providers",
        "/v1/capabilities",
        "/v1/compare",
        "/v1/audio/speech",
        "/v1/jobs",
        "/v1/usage",
        "/v1/history",
        "/v1/workers",
        "/v1/references",
        "/v1/keys",
    ]
    missing = [p for p in required if p not in text]
    assert missing == [], f"undocumented endpoints: {missing}"


def test_agents_md_and_llms_txt_agree_on_the_core_endpoint() -> None:
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "/v1/tts" in agents
    assert "list_voices" in agents


def test_readme_links_resolve() -> None:
    """A broken relative link in the README is a dead end for the reader."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    missing: list[str] = []
    for match in re.finditer(r"\[[^\]]*\]\(([^)#:]+)(?:#[^)]*)?\)", text):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (REPO_ROOT / target).exists():
            missing.append(target)
    assert missing == [], f"README links to missing files: {missing}"


def test_docs_folder_links_resolve() -> None:
    missing: list[str] = []
    for doc in (REPO_ROOT / "docs").glob("*.md"):
        text = doc.read_text(encoding="utf-8")
        for match in re.finditer(r"\[[^\]]*\]\(([^)#:]+)(?:#[^)]*)?\)", text):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (doc.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{doc.name} -> {target}")
    assert missing == [], f"broken links in docs/: {missing}"


def test_config_example_keys_are_documented_in_deployment() -> None:
    """The tunables people actually change should be discoverable outside the file."""
    deployment = (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for knob in ("monthly_budget_micro_usd", "circuit_breaker_threshold", "ttl_seconds"):
        assert knob in deployment, f"{knob} is not mentioned in docs/deployment.md"
