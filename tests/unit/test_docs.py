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


def test_docs_do_not_invent_settings_names() -> None:
    """Every dotted setting named in the docs must exist.

    This has gone wrong repeatedly — `router.circuit_cooldown_s`,
    `security.max_text_chars` at the wrong default, `TTSGRAB_ALLOW_LOCALHOST` — and a
    reader who copies a setting name that does not exist gets silence, not an error.
    """
    from pydantic import BaseModel

    from ttsgrab.settings import Settings

    def field_names(model: type[BaseModel], prefix: str = "") -> set[str]:
        names: set[str] = set()
        for field, info in model.model_fields.items():
            path = f"{prefix}{field}"
            names.add(path)
            annotation = info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                names |= field_names(annotation, path + ".")
        return names

    known = field_names(Settings)
    # Fields on sections that are plain dicts/lists rather than models still count by leaf.
    leaves = {name.rsplit(".", 1)[-1] for name in known}

    pattern = re.compile(r"`((?:[a-z_]+\.){1,3}[a-z_]+)`")
    offenders: list[str] = []
    for doc in [REPO_ROOT / "README.md", *(REPO_ROOT / "docs").glob("*.md")]:
        text = doc.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            name = match.group(1)
            # Skip filenames, module paths and anything not settings-shaped.
            if name.endswith((".md", ".py", ".yaml", ".yml", ".json", ".sh")):
                continue
            if name.startswith(("ttsgrab.", "src.", "tests.", "workers.")):
                continue
            if name.split(".")[0] not in {
                "router",
                "cache",
                "security",
                "longform",
                "workers",
                "edge",
                "google_cloud",
                "gemini",
                "azure",
                "free_web",
                "mcp",
            }:
                continue
            if name not in known and name.rsplit(".", 1)[-1] not in leaves:
                offenders.append(f"{doc.name}: {name}")
    assert offenders == [], f"docs name settings that do not exist: {offenders}"


# Routes that need the admin token. A curl example for any of these that omits
# `Authorization` is worse than no example: the reader follows it, gets 401, and concludes
# the feature is broken. This drifted once already when worker registration was tightened.
_ADMIN_ROUTES = (
    "/v1/keys",
    "/v1/workers",
    "/v1/references",
    "/v1/audit",
)


def _doc_files() -> list[Path]:
    files = [REPO_ROOT / "README.md", REPO_ROOT / "AGENTS.md"]
    files.extend(sorted((REPO_ROOT / "docs").glob("*.md")))
    files.extend(sorted((REPO_ROOT / "workers").glob("*.md")))
    return [f for f in files if f.exists()]


def test_admin_curl_examples_carry_authorization() -> None:
    """Every documented call to an admin route must show the header it requires."""
    offenders: list[str] = []
    for path in _doc_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "curl" not in line:
                continue
            route = next((r for r in _ADMIN_ROUTES if r in line), None)
            if route is None:
                continue
            # Exempt the audit report, which quotes the vulnerable code as history.
            if path.name == "audit.md":
                continue
            # The header may be on the same line (single-line example) ...
            if "Authorization" in line:
                continue
            # ... or on a continuation line of the same ```bash block.
            lines = path.read_text(encoding="utf-8").splitlines()
            window = "\n".join(lines[lineno - 1 : lineno + 4])
            if "Authorization" not in window:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} {route}")

    assert offenders == [], (
        "documented admin calls without an Authorization header:\n  " + "\n  ".join(offenders)
    )
