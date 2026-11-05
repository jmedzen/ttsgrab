"""Structured logging and Prometheus metrics."""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

SECRET_KEY_HINTS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "subscription_key",
    "x-goog-api-key",
)


def _scrub(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Last line of defence: no secret ever reaches a log sink."""
    for key in list(event_dict):
        lowered = key.lower()
        if any(hint in lowered for hint in SECRET_KEY_HINTS):
            event_dict[key] = "***"
        else:
            value = event_dict[key]
            if isinstance(value, str) and len(value) >= 16 and _looks_like_key(value):
                event_dict[key] = "***"
    return event_dict


def _looks_like_key(value: str) -> bool:
    prefixes = ("sk-", "AIza", "ghp_", "xoxb-", "ttsg_", "eyJ")
    return value.startswith(prefixes)


def add_request_id(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict.setdefault("request_id", request_id_var.get())
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            add_request_id,  # type: ignore[list-item]
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _scrub,  # type: ignore[list-item]
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), stream=sys.stderr)


def new_request_id() -> str:
    rid = uuid.uuid4().hex[:16]
    request_id_var.set(rid)
    return rid


# -- metrics ----------------------------------------------------------------------------
SYNTH_REQUESTS = Counter(
    "ttsgrab_synthesis_requests_total", "Synthesis requests", ["provider", "status", "cached"]
)
SYNTH_LATENCY = Histogram(
    "ttsgrab_synthesis_latency_seconds",
    "End-to-end synthesis latency",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
SYNTH_CHARS = Counter("ttsgrab_characters_total", "Characters synthesised", ["provider", "tier"])
SYNTH_COST = Counter("ttsgrab_cost_microusd_total", "Estimated cost in micro-USD", ["provider"])
CACHE_OPS = Counter("ttsgrab_cache_ops_total", "Cache operations", ["result"])
PROVIDER_UP = Gauge("ttsgrab_provider_up", "Provider enabled and healthy", ["provider"])
WORKER_UP = Gauge("ttsgrab_worker_up", "Worker health", ["worker"])
HTTP_REQUESTS = Counter(
    "ttsgrab_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram("ttsgrab_http_latency_seconds", "HTTP latency", ["method", "path"])


class Timer:
    def __init__(self) -> None:
        self.start = time.perf_counter()

    @property
    def seconds(self) -> float:
        return time.perf_counter() - self.start

    @property
    def ms(self) -> int:
        return int(self.seconds * 1000)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
