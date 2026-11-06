"""Configuration: env vars + ``config.yaml``.

Precedence (highest first): environment variable / ``.env`` → ``config.yaml`` → defaults.

Validation philosophy: a provider that is missing credentials is *disabled*, not fatal.
The service must always boot with zero secrets configured.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class SecuritySettings(BaseModel):
    # Bearer tokens accepted by /v1. Empty list == auth disabled (dev only).
    api_keys: list[str] = Field(default_factory=list)
    # Token the web UI uses. Distinct from API keys so UI access can be audited.
    admin_token: str | None = None
    # When true, the UI can be opened without a token from localhost only.
    allow_localhost_without_token: bool = True
    rate_limit_per_minute: int = 120
    # X-Forwarded-For is only believed when you say how many proxies sit in front. Left at
    # 0 the header is ignored, because a client can set it to anything — including
    # 127.0.0.1, which would otherwise grant localhost privileges to anyone.
    trusted_proxy_hops: int = 0
    # Webhook delivery is an outbound request to a caller-supplied URL. Private targets
    # (a bot on the LAN) are common, so they are opt-in rather than assumed.
    allow_private_webhooks: bool = False
    max_text_chars: int = 20000
    max_request_body_bytes: int = 2 * 1024 * 1024
    allowed_upload_mimes: list[str] = Field(
        default_factory=lambda: [
            "audio/wav",
            "audio/x-wav",
            "audio/mpeg",
            "audio/flac",
            "audio/ogg",
            "audio/webm",
            "audio/mp4",
        ]
    )
    max_upload_bytes: int = 25 * 1024 * 1024
    # SSRF guard for worker base_url registration.
    allow_private_worker_urls: bool = True  # LAN workers are the whole point
    allowed_worker_hosts: list[str] = Field(default_factory=list)


class GoogleCloudSettings(BaseModel):
    enabled: bool = False
    # Path to a service-account JSON file. Never inline the JSON itself.
    credentials_file: str | None = None
    # Alternative: a plain API key (works for the REST API on some endpoints).
    api_key: str | None = None
    project_id: str | None = None
    location: str = "global"
    # Monthly character budget across ALL Google voice types. 0 == unlimited.
    monthly_budget_chars: int = 1_000_000
    # Hard USD cap in micro-USD. 0 == unlimited.
    monthly_budget_micro_usd: int = 0
    # Never exceed the documented free allowance automatically.
    respect_free_tier: bool = True


class GoogleAIStudioSettings(BaseModel):
    """Google AI Studio (Gemini API) TTS — separate credentials from Cloud TTS.

    Free and paid keys are configured separately because they have different allowances.
    A free AI Studio key is limited to a handful of *requests per day* (Gemini TTS was cut
    to 10 RPD in November 2026), which is far below what a single long article needs; a key
    with billing attached has a much larger allowance. Keeping the two lists apart is what
    lets the pool spend the free allowance first and only then bill.
    """

    enabled: bool = False
    model: str = "gemini-3.8-flash-tts"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    # Keys with no billing attached: few requests per day.
    free_api_keys: list[str] = Field(default_factory=list)
    # Keys on a billed project: no daily request cap, billed per use.
    paid_api_keys: list[str] = Field(default_factory=list)
    # Daily request allowance per free key. Configurable because Google has moved it
    # more than once and an operator may be on a different tier than the default.
    free_rpd: int = 10
    strategy: Literal["free_first", "round_robin", "least_used"] = "free_first"
    monthly_budget_chars: int = 0
    monthly_budget_micro_usd: int = 0


class AzureSettings(BaseModel):
    enabled: bool = False
    region: str | None = None
    api_key: str | None = None
    tier: Literal["F0", "S0"] = "F0"
    monthly_budget_chars: int = 500_000


class EdgeSettings(BaseModel):
    """Unofficial Microsoft Edge read-aloud endpoint.

    Default-on per project decision, but it is the *only* unofficial provider that is.
    It is fragile (Microsoft rotates DRM tokens) and has no commercial-redistribution
    grant. See docs/providers.md.
    """

    enabled: bool = True
    allow_public: bool = True
    proxy: str | None = None


class FreeWebSettings(BaseModel):
    """gTTS / translate_tts / StreamElements. All unofficial, all off by default."""

    enabled: bool = False
    gtts_enabled: bool = False
    gtts_tld: str = "com"
    translate_tts_enabled: bool = False
    streamelements_enabled: bool = False


class McpSettings(BaseModel):
    """MCP server exposure. Requires the optional ``mcp`` extra."""

    enabled: bool = True
    path: str = "/mcp"
    # Bearer token MCP clients must present. Falls back to the admin token when unset, but
    # the mount is refused entirely if neither is configured — its tools synthesise speech
    # and spend money, so an unauthenticated transport is not acceptable.
    token: str | None = None


class CacheSettings(BaseModel):
    enabled: bool = True
    dir: str = "./data/cache"
    max_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB
    ttl_seconds: int = 30 * 24 * 3600


class FavouritesSettings(BaseModel):
    """Initial favourite voices, declared in config.yaml.

    Favourites are normally clicked in the UI and stored in the database. This section
    exists so a deployment can ship a sensible shortlist — the four voices your team
    actually uses — instead of everyone re-starring them after a fresh install.

    Seeding is additive and idempotent: entries already present are left alone, never
    removed. Deleting a seeded favourite in the UI must stick, so this is applied only
    when the table is empty.
    """

    seed: list[str] = Field(default_factory=list)
    # Off by default: an existing deployment that never mentioned favourites should not
    # have rows appear because of an upgrade.
    seed_enabled: bool = False


class RouterSettings(BaseModel):
    default_policy: Literal[
        "cheapest", "fastest", "best_quality", "local_only", "free_only", "explicit"
    ] = "cheapest"
    # Ordered fallback applied after the primary choice.
    fallback_chain: list[str] = Field(
        # Every shippable provider, so a fallback is never skipped just because nobody
        # remembered to list it. Unconfigured ones are simply not enabled and get skipped.
        default_factory=lambda: [
            "edge",
            "google_cloud",
            "azure",
            "google_ai_studio",
            "kokoro",
            "cosyvoice",
            "gpt_sovits",
        ]
    )
    max_fallbacks: int = 2
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_s: int = 60
    request_timeout_s: float = 60.0
    # quality rank per provider/voice-tier, used by best_quality
    quality_scores: dict[str, float] = Field(default_factory=dict)


class WorkerRegistrySettings(BaseModel):
    health_interval_s: int = 30
    health_timeout_s: int = 5
    # A worker that fails N consecutive health checks is evicted from routing.
    unhealthy_threshold: int = 3
    recovery_threshold: int = 2
    default_timeout_s: float = 120.0


class LongFormSettings(BaseModel):
    enabled: bool = True
    # Split above this many characters into sentences.
    chunk_threshold: int = 400
    max_sentence_chars: int = 300
    max_concurrency: int = 4
    pause_ms_between_sentences: int = 120


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TTSGRAB_",
        env_file=".env",
        env_file_encoding="utf-8",
        # Without this, TTSGRAB_GOOGLE_CLOUD__API_KEY is silently ignored and the loader
        # falls back to defaults — i.e. every nested knob in .env.example would be a lie.
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Which config file was read, for writing settings back to the same place. Not part of
    # the schema (excluded from env and from dumps) — set by load_settings after parsing.
    _config_path: Any = PrivateAttr(default=None)

    app_name: str = "ttsgrab"
    env: Literal["dev", "prod", "test"] = "dev"
    host: str = "127.0.0.1"
    port: int = 8300
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    database_url: str = "sqlite+aiosqlite:///./data/ttsgrab.db"
    data_dir: str = "./data"

    # Write provenance tags into the audio itself (mp3/flac). On by default: a file that
    # outlives the gateway is the normal case, and the cost is a container header rewrite.
    write_metadata: bool = True

    public_base_url: str = "http://127.0.0.1:8300"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    security: SecuritySettings = Field(default_factory=SecuritySettings)
    edge: EdgeSettings = Field(default_factory=EdgeSettings)
    free_web: FreeWebSettings = Field(default_factory=FreeWebSettings)
    google_cloud: GoogleCloudSettings = Field(default_factory=GoogleCloudSettings)
    google_ai_studio: GoogleAIStudioSettings = Field(default_factory=GoogleAIStudioSettings)
    azure: AzureSettings = Field(default_factory=AzureSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    favourites: FavouritesSettings = Field(default_factory=FavouritesSettings)
    workers: WorkerRegistrySettings = Field(default_factory=WorkerRegistrySettings)
    longform: LongFormSettings = Field(default_factory=LongFormSettings)

    @field_validator("database_url")
    @classmethod
    def _sqlite_needs_async_driver(cls, v: str) -> str:
        if v.startswith("sqlite://") and not v.startswith("sqlite+aiosqlite://"):
            return v.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    def ensure_dirs(self) -> None:
        for p in (
            self.data_path,
            Path(self.cache.dir),
            self.data_path / "audio",
            self.data_path / "reference",
        ):
            p.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------------------
# config.yaml merge
# --------------------------------------------------------------------------------------

# Env var names for secrets that may also come from the environment directly (Docker
# secrets mount as files, which we read here too).
_ENV_SECRETS = {
    "GOOGLE_APPLICATION_CREDENTIALS": ("google_cloud", "credentials_file"),
    "GOOGLE_CLOUD_TTS_API_KEY": ("google_cloud", "api_key"),
    "GEMINI_API_KEY": ("google_ai_studio", "free_api_keys"),
    "AZURE_SPEECH_KEY": ("azure", "api_key"),
    "TTSGRAB_ADMIN_TOKEN": ("security", "admin_token"),
}

_SECRET_FILE_VARS = {
    # Docker secret style: VAR_FILE points at a file whose content is the secret.
    "GOOGLE_APPLICATION_CREDENTIALS_FILE": ("google_cloud", "credentials_file"),
    "GEMINI_API_KEY_FILE": ("google_ai_studio", "free_api_keys"),
    "AZURE_SPEECH_KEY_FILE": ("azure", "api_key"),
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_secret_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _ensure_async_driver(url: str) -> str:
    """Force an async driver onto a plain SQLAlchemy URL.

    People write ``postgresql://`` because that is what every tutorial uses; SQLAlchemy
    would then load a synchronous driver and the async engine would refuse it.
    """
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def _validate_config_keys(raw: dict[str, Any], path: Path) -> None:
    """Reject unknown keys in config.yaml.

    ``extra="ignore"`` is right for environment variables — a shell is full of unrelated
    names — but wrong for a file a human edits. A typo in
    ``google_cloud.monthly_budget_micro_usd`` would otherwise be dropped in silence while
    the operator believed they had set a spending cap. Fail loudly instead.
    """
    from pydantic import BaseModel

    def check(model: type[BaseModel], data: dict[str, Any], prefix: str) -> None:
        unknown: list[str] = []
        for key, value in data.items():
            field = model.model_fields.get(key)
            if field is None:
                unknown.append(f"{prefix}{key}")
                continue
            annotation = field.annotation
            if (
                isinstance(value, dict)
                and isinstance(annotation, type)
                and issubclass(annotation, BaseModel)
            ):
                check(annotation, value, f"{prefix}{key}.")
        if unknown:
            raise ValueError(
                f"{path}: unknown configuration key(s): {', '.join(sorted(unknown))}. "
                "See config.example.yaml for the supported keys."
            )

    check(Settings, raw, "")


class _FileSettingsSource(PydanticBaseSettingsSource):
    """config.yaml as a *source*, not as init kwargs.

    Passing the file as ``Settings(**data)`` makes it the highest-priority input, so an
    environment variable can never override it — the opposite of what the README promises
    and the opposite of what an operator expects when they set a value in `.env`.
    Registering it as a source puts it below the environment, where it belongs.
    """

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field, field_name):
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Build settings from defaults → config.yaml → env/`.env`."""
    raw: dict[str, Any] = {}

    path = Path(config_path or os.environ.get("TTSGRAB_CONFIG") or "config.yaml")
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        raw = loaded

    # Docker-secret style indirection before handing to pydantic-settings.
    for env_var, (section, field) in _SECRET_FILE_VARS.items():
        secret_path = os.environ.get(env_var)
        if not secret_path:
            continue
        value = _read_secret_file(secret_path)
        sect = raw.setdefault(section, {})
        if field == "api_keys":
            existing = sect.get("api_keys") or []
            sect["api_keys"] = [*existing, *[k.strip() for k in value.splitlines() if k.strip()]]
        else:
            sect[field] = value

    _validate_config_keys(raw, path)

    # Priority, highest first: init kwargs (none here) → environment → .env → config.yaml
    # → defaults. The file is deliberately below the environment.
    # `.env` is a developer convenience, and it is the one input a test suite must not
    # inherit: running `make test` after `make .env` would otherwise apply the operator's
    # real port, budget and router settings to every test that builds Settings.
    env_file = None if os.environ.get("TTSGRAB_IGNORE_ENV_FILE") else ".env"

    class _ConfigAwareSettings(Settings):
        model_config = SettingsConfigDict(
            env_prefix="TTSGRAB_",
            env_file=env_file,
            env_file_encoding="utf-8",
            env_nested_delimiter="__",
            extra="ignore",
        )

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return (
                init_settings,
                env_settings,
                dotenv_settings,
                _FileSettingsSource(settings_cls, raw),
                file_secret_settings,
            )

    settings = _ConfigAwareSettings()

    # Plain env vars that pydantic-settings does not reach (nested, differently named).
    for env_var, (section, field) in _ENV_SECRETS.items():
        secret = os.environ.get(env_var)
        if not secret:
            continue
        target: Any = getattr(settings, section)
        if field == "api_keys":
            if secret not in target.api_keys:
                target.api_keys = [*target.api_keys, secret]
        else:
            setattr(target, field, secret)

    if os.environ.get("DATABASE_URL"):
        settings.database_url = _ensure_async_driver(os.environ["DATABASE_URL"])
    settings.database_url = settings.database_url or "sqlite+aiosqlite:///./data/ttsgrab.db"

    # Remember which file was actually read, so a settings write goes back to the same
    # place. Without this an operator running with TTSGRAB_CONFIG=/etc/ttsgrab.yaml would
    # have the UI write ./config.yaml and silently lose the change.
    settings._config_path = path

    settings.ensure_dirs()
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
