"""Settings loading: defaults, the config file, env precedence, and strict key checking."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ttsgrab.settings import load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_defaults_work_with_no_config_file(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "absent.yaml")
    assert settings.app_name == "ttsgrab"
    assert settings.env in {"dev", "prod", "test"}
    # Edge is on by default; everything needing a key is off.
    assert settings.edge.enabled is True
    assert settings.google_cloud.enabled is False
    assert settings.azure.enabled is False
    assert settings.google_ai_studio.enabled is False


def test_example_config_is_valid_and_complete() -> None:
    """config.example.yaml is documentation people copy; it must parse and apply."""
    example = REPO_ROOT / "config.example.yaml"
    assert example.is_file(), "the shipped example must exist"
    settings = load_settings(example)
    assert settings.router.default_policy == "cheapest"
    assert settings.cache.max_bytes > 0
    assert settings.longform.chunk_threshold > 0
    assert settings.mcp.enabled is True


def test_unknown_key_in_config_file_is_rejected(tmp_path: Path) -> None:
    """A typo in a budget field must not be dropped in silence — that is a false sense of
    cost control."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "google_cloud:\n  monthly_bugdet_micro_usd: 5000000\n",  # transposed letters
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="monthly_bugdet_micro_usd"):
        load_settings(path)


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("routery:\n  policy: fastest\n", encoding="utf-8")
    with pytest.raises(ValueError, match="routery"):
        load_settings(path)


def test_config_file_values_apply(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "router:\n"
        "  default_policy: free_only\n"
        "  circuit_breaker_threshold: 5\n"
        "cache:\n"
        "  max_bytes: 1048576\n"
        "  ttl_seconds: 600\n",
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.router.default_policy == "free_only"
    assert settings.router.circuit_breaker_threshold == 5
    assert settings.cache.max_bytes == 1_048_576
    assert settings.cache.ttl_seconds == 600


def test_env_overrides_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("router:\n  default_policy: cheapest\n", encoding="utf-8")
    monkeypatch.setenv("TTSGRAB_ROUTER__DEFAULT_POLICY", "best_quality")
    assert load_settings(path).router.default_policy == "best_quality"


def test_invalid_enum_value_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The conftest sets TTSGRAB_ENV=test for isolation, and env legitimately outranks the
    # file, so clear it to exercise the file's own validation.
    monkeypatch.delenv("TTSGRAB_ENV", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("env: production\n", encoding="utf-8")  # should be prod
    with pytest.raises(Exception, match=r"dev|prod|test"):
        load_settings(path)


def test_secret_file_indirection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker secrets: point at a file, never put the value in the environment."""
    secret = tmp_path / "google.json"
    secret.write_text('{"type": "service_account"}', encoding="utf-8")
    monkeypatch.setenv("TTSGRAB_GOOGLE_CLOUD__CREDENTIALS_FILE", str(secret))
    settings = load_settings(tmp_path / "absent.yaml")
    assert settings.google_cloud.credentials_file == str(secret)


def test_api_keys_from_env_override_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """List fields take a JSON array from the environment; pydantic-settings does not split
    on commas because a key could contain one."""
    path = tmp_path / "config.yaml"
    path.write_text("security:\n  api_keys: [ttsg_from_file]\n", encoding="utf-8")
    monkeypatch.setenv("TTSGRAB_SECURITY__API_KEYS", '["ttsg_from_env"]')
    keys = load_settings(path).security.api_keys
    assert keys == ["ttsg_from_env"], "environment must win over the file"


def test_database_url_gets_an_async_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    settings = load_settings(tmp_path / "absent.yaml")
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_sqlite_url_is_left_alone_apart_from_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./data/x.db")
    settings = load_settings(tmp_path / "absent.yaml")
    assert settings.database_url == "sqlite+aiosqlite:///./data/x.db"


def test_env_var_file_is_not_required(tmp_path: Path) -> None:
    # A missing .env must not raise; most deployments use real env vars.
    assert load_settings(tmp_path / "absent.yaml") is not None


def test_budget_defaults_are_non_zero_somewhere() -> None:
    """Shipping a zero budget everywhere would make the cap decorative."""
    settings = load_settings(REPO_ROOT / "config.example.yaml")
    budgets = [
        settings.google_cloud.monthly_budget_micro_usd,
        settings.google_ai_studio.monthly_budget_micro_usd,
    ]
    assert any(b > 0 for b in budgets)


def test_azure_has_no_microusd_budget_field() -> None:
    """Documented in config.example.yaml; assert it so the comment cannot rot."""
    from ttsgrab.settings import AzureSettings

    assert "monthly_budget_micro_usd" not in AzureSettings.model_fields
    assert "monthly_budget_chars" in AzureSettings.model_fields


def test_env_variables_still_ignore_unknown_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell may contain unrelated TTSGRAB_* names; env must stay permissive even though
    the config file is strict."""
    monkeypatch.setenv("TTSGRAB_SOMETHING_UNRELATED", "1")
    assert load_settings(tmp_path / "absent.yaml") is not None


def test_example_config_keys_all_exist() -> None:
    """Every key in the example must be a real field, checked structurally rather than by
    parsing, so a future rename shows up here."""
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    from pydantic import BaseModel

    from ttsgrab.settings import Settings

    def walk(model: type[BaseModel], data: dict, prefix: str = "") -> list[str]:
        bad: list[str] = []
        for key, value in data.items():
            field = model.model_fields.get(key)
            if field is None:
                bad.append(prefix + key)
            elif (
                isinstance(value, dict)
                and isinstance(field.annotation, type)
                and issubclass(field.annotation, BaseModel)
            ):
                bad.extend(walk(field.annotation, value, f"{prefix}{key}."))
        return bad

    assert walk(Settings, example) == []


def test_env_example_uses_real_variable_names() -> None:
    """Every TTSGRAB_* name in .env.example must map to a real field.

    This is the file people copy. A name that does not exist is worse than no name at all:
    it looks configured and silently does nothing.
    """
    from pydantic import BaseModel

    from ttsgrab.settings import Settings

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    names = [
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip().startswith("TTSGRAB_") and "=" in line
    ]
    assert names, "the example should list variables"

    # Read by the Caddy service in the tls profile, not by the gateway itself.
    consumed_elsewhere = {"TTSGRAB_DOMAIN"}

    bad: list[str] = []
    for name in names:
        if name in consumed_elsewhere:
            continue
        path = name.removeprefix("TTSGRAB_").lower().split("__")
        model: type[BaseModel] = Settings
        for part in path:
            field = model.model_fields.get(part)
            if field is None:
                bad.append(name)
                break
            if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel):
                model = field.annotation
    assert bad == [], f"unknown variables in .env.example: {bad}"


def test_undeclared_variables_are_ignored_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell may contain TTSGRAB_* names from another tool; booting must not depend on a
    clean environment."""
    monkeypatch.setenv("TTSGRAB_NOT_A_REAL_SETTING", "x")
    assert load_settings(tmp_path / "absent.yaml") is not None


def _settings_env_names() -> set[str]:
    from pydantic import BaseModel

    from ttsgrab.settings import Settings

    def walk(model: type[BaseModel], prefix: str = "") -> set[str]:
        names: set[str] = set()
        for field_name, field in model.model_fields.items():
            path = f"{prefix}{field_name}".upper()
            names.add("TTSGRAB_" + path.replace(".", "__"))
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                names |= walk(annotation, path + ".")
        return names

    return walk(Settings)


def test_compose_passes_only_real_variables_to_containers() -> None:
    """The left-hand side of a compose environment mapping goes straight into the container,
    so an invented name there is silently ignored at runtime — a configured-looking setting
    that does nothing."""
    import re

    valid = _settings_env_names()
    # Read by environments other than the gateway: worker images pick up TTSGRAB_DEVICE,
    # and the Caddy service reads TTSGRAB_DOMAIN.
    not_gateway_settings = {"TTSGRAB_DEVICE", "TTSGAB_DEVICE", "TTSGRAB_DOMAIN"}

    problems: list[str] = []
    for name in ("docker-compose.yml", "docker-compose.distributed.yml"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for key in re.findall(r"^\s+(TTSGRAB_[A-Z0-9_]+):", text, flags=re.MULTILINE):
            if key not in valid and key not in not_gateway_settings:
                problems.append(f"{name}: {key}")
    assert problems == [], f"unknown container env vars: {problems}"


def test_every_profile_can_start_the_gateway() -> None:
    """core, gpu and cpu-local must each include the gateway, or `--profile gpu up` would
    start workers with nothing to talk to."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    gateway = compose["services"]["gateway"]
    assert {"core", "gpu", "cpu-local"} <= set(gateway["profiles"])


def test_gpu_workers_require_the_nvidia_runtime() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name in ("kokoro", "cosyvoice"):
        devices = (
            compose["services"][name]
            .get("deploy", {})
            .get("resources", {})
            .get("reservations", {})
            .get("devices", [])
        )
        assert devices, f"{name} is a GPU service but reserves no device"
        assert devices[0]["driver"] == "nvidia"
        assert "gpu" in compose["services"][name]["profiles"]


def test_the_advertised_port_matches_the_configured_one() -> None:
    """Every place that names a port must agree, or `make up` prints one address and the
    gateway listens on another. This drifted the moment the default changed."""
    import re
    from pathlib import Path

    from ttsgrab.settings import Settings

    port = Settings.model_fields["port"].default
    base = f"127.0.0.1:{port}"
    root = Path(__file__).resolve().parents[2]

    checks = {
        "src/ttsgrab/settings.py": f'public_base_url: str = "http://{base}"',
        "web/vite.config.ts": f"'http://{base}'",
        "docker-compose.yml": f"${{TTSGRAB_PORT:-{port}}}:{port}",
        "docker-compose.yml#health": f'"http://{base}/healthz"',
        ".env.example": f"TTSGRAB_PORT={port}",
        "deploy/caddy/Caddyfile": f"gateway:{port}",
    }
    for name, needle in checks.items():
        path = root / name.split("#")[0]
        assert path.exists(), f"{name} is missing"
        assert needle in path.read_text(encoding="utf-8"), (
            f"{name} does not contain {needle!r}; the default port is {port} and something "
            "still points somewhere else"
        )

    # The Makefile prints the URL a user will click, so it must be right too.
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    assert f"http://localhost:{port}" in makefile
    assert re.search(rf"--port {port}\b", makefile), "make dev starts a different port"


def test_no_stray_legacy_port_in_startup_commands() -> None:
    """Guard against a partial edit: a doc telling users to run on the old port."""
    import re
    from pathlib import Path

    from ttsgrab.settings import Settings

    port = Settings.model_fields["port"].default
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []

    for path in list(root.glob("*.md")) + list((root / "docs").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if f"--port {port}" in line:
                continue
            # A localhost/gateway URL on a *different* port than configured.
            for match in re.finditer(r"(?:localhost|127\.0\.0\.1|gateway):(\d{4})", line):
                found = int(match.group(1))
                if found != port and found not in (5173,):
                    offenders.append(f"{path.name}: {line.strip()[:90]}")

    assert offenders == [], (
        "documentation points at a port the gateway does not use:\n  " + "\n  ".join(offenders)
    )


def test_a_developers_dotenv_does_not_leak_into_the_test_suite() -> None:
    """`make .env` is step two of the quickstart; `make test` is step ten.

    Without this guarantee, a developer's real TTSGRAB_ROUTER__DEFAULT_POLICY or
    TTSGRAB_PORT silently overrides what a test set up, producing failures that exist only
    on their machine. The flag is set in conftest so no individual test has to remember it.
    """
    import os

    assert os.environ.get("TTSGRAB_IGNORE_ENV_FILE") == "1"
