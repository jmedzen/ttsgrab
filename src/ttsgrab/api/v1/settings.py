"""Read and edit the gateway's configuration from the UI.

One place for every knob that used to be split across config.yaml, .env, environment
variables and the database. The product is a single-tenant box, so "where do I change this"
should have one answer.

Precedence is unchanged and is reported honestly: environment variables still beat
``config.yaml``. When a value was written here but is being overridden by an env var, the
response says so instead of showing a setting that has no effect.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ttsgrab.api.deps import AdminDep, Ctx, PrincipalDep, write_audit
from ttsgrab.core import configfile
from ttsgrab.core.errors import InvalidRequest

router = APIRouter(tags=["settings"])

# Editable through the API. Deliberately an allowlist: `extra="ignore"` means an unknown
# key would vanish silently, and this endpoint writes to a file the operator also edits by
# hand. A typo must be refused, not stored.
EDITABLE: dict[str, str] = {
    # Providers
    "edge.enabled": "bool",
    "free_web.enabled": "bool",
    "free_web.gtts_enabled": "bool",
    "free_web.translate_tts_enabled": "bool",
    "google_cloud.enabled": "bool",
    "google_cloud.api_key": "secret",
    "google_cloud.credentials_file": "str",
    "google_cloud.monthly_budget_micro_usd": "int",
    "google_ai_studio.enabled": "bool",
    "google_ai_studio.free_api_keys": "secret_list",
    "google_ai_studio.paid_api_keys": "secret_list",
    "azure.enabled": "bool",
    "azure.api_key": "secret",
    "azure.region": "str",
    # Routing and cost
    "router.default_policy": "str",
    "router.fallback_chain": "list",
    "router.monthly_budget_micro_usd": "int",
    "router.quality_scores": "dict",
    # Cache
    "cache.enabled": "bool",
    "cache.dir": "str",
    "cache.max_bytes": "int",
    "cache.ttl_seconds": "int",
    # Long-form
    "longform.chunk_threshold": "int",
    "longform.max_concurrency": "int",
    # Security
    "security.admin_token": "secret",
    "security.allow_localhost_without_token": "bool",
    "security.rate_limit_per_minute": "int",
    # Favourites
    "favourites.seed_enabled": "bool",
    "favourites.seed": "list",
    # Misc
    "env": "str",
    "write_metadata": "bool",
}


class SettingsPatch(BaseModel):
    values: dict[str, Any]


def _cfg_path(settings: object) -> Path:
    return configfile.config_path(getattr(settings, "_config_path", None))


def _env_override(dotted: str) -> str | None:
    """The TTSGRAB_ env var that would win over this config key, if it is set."""
    env_name = "TTSGRAB_" + dotted.replace(".", "__").upper()
    return env_name if env_name in os.environ else None


@router.get("/v1/settings", summary="Effective configuration, with secret values masked")
async def get_settings(ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    settings = ctx.settings
    path = _cfg_path(settings)
    on_disk = configfile.read_raw(path)

    # Only the editable keys, so the response stays readable and does not leak internals.
    values: dict[str, Any] = {}
    overridden: list[dict[str, str]] = []
    for dotted in EDITABLE:
        value = _current(settings, dotted)
        values[dotted] = value
        env_var = _env_override(dotted)
        if env_var:
            overridden.append({"key": dotted, "env_var": env_var})

    return {
        "path": str(path),
        "exists": path.is_file(),
        "values": configfile.mask_tree(values),
        "on_disk": configfile.mask_tree(on_disk),
        "env_overrides": overridden,
        "editable": EDITABLE,
    }


def _current(settings: object, dotted: str) -> Any:
    node: Any = settings
    for part in dotted.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return node


@router.put("/v1/settings", summary="Write configuration to config.yaml")
async def put_settings(body: SettingsPatch, ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    if not body.values:
        raise InvalidRequest("no values supplied")

    unknown = [k for k in body.values if k not in EDITABLE]
    if unknown:
        raise InvalidRequest(
            "unknown setting keys; refuse rather than store something that does nothing",
            details={"unknown": unknown, "editable": sorted(EDITABLE)},
        )

    cleaned: dict[str, Any] = {}
    for dotted, value in body.values.items():
        cleaned[dotted] = _coerce(dotted, value)

    path = _cfg_path(ctx.settings)
    configfile.set_values(path, cleaned)

    await write_audit(
        ctx,
        principal=principal,
        action="settings.updated",
        target=str(path),
        detail={"keys": sorted(cleaned)},
    )

    # The running process still holds the old values. Saying so is better than pretending
    # the change is live: some settings (port, database URL) can only apply on restart.
    return {
        "written": sorted(cleaned),
        "path": str(path),
        "restart_required": True,
        "note": "Written to config.yaml. Restart the gateway for these to take effect.",
    }


def _coerce(dotted: str, value: Any) -> Any:
    """Type-check against the declared kind, so a bad value fails here and not at boot."""
    kind = EDITABLE[dotted]
    if value is None:
        return None
    if kind == "bool":
        if not isinstance(value, bool):
            raise InvalidRequest(f"{dotted} must be true or false")
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidRequest(f"{dotted} must be an integer")
        if value < 0:
            raise InvalidRequest(f"{dotted} must not be negative")
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise InvalidRequest(f"{dotted} must be a string")
        return value
    if kind == "secret":
        if not isinstance(value, str):
            raise InvalidRequest(f"{dotted} must be a string")
        return value
    if kind in ("list", "secret_list"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvalidRequest(f"{dotted} must be a list of strings")
        return value
    if kind == "dict":
        if not isinstance(value, dict):
            raise InvalidRequest(f"{dotted} must be an object")
        return value
    raise InvalidRequest(f"{dotted} has an unknown type {kind}")
