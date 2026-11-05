"""Uniform error envelope.

Every failure the gateway produces — its own or adapted from an upstream — comes out
as this shape, so an agent can decide to retry or fall back without parsing prose.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(enum.StrEnum):
    # Client
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    UNSUPPORTED_PARAM = "unsupported_param"
    TEXT_TOO_LONG = "text_too_long"
    REQUEST_TOO_LARGE = "request_too_large"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"
    # Provider
    PROVIDER_ERROR = "provider_error"
    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    VOICE_NOT_FOUND = "voice_not_found"
    UPSTREAM_AUTH = "upstream_auth"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    # Gateway
    ALL_PROVIDERS_FAILED = "all_providers_failed"
    INTERNAL = "internal_error"
    TIMEOUT = "timeout"


_RETRYABLE = {
    ErrorCode.PROVIDER_ERROR,
    ErrorCode.PROVIDER_UNAVAILABLE,
    ErrorCode.UPSTREAM_RATE_LIMITED,
    ErrorCode.ALL_PROVIDERS_FAILED,
    ErrorCode.TIMEOUT,
    ErrorCode.INTERNAL,
    ErrorCode.RATE_LIMITED,
}


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    # Providers the caller could plausibly try instead.
    suggested_fallback: list[str] = Field(default_factory=list)
    param: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class GatewayError(Exception):
    """Base class for anything that should terminate a request with our envelope."""

    code: ErrorCode = ErrorCode.INTERNAL
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        suggested_fallback: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        self.details = details or {}
        self.suggested_fallback = suggested_fallback or []
        if retryable is not None:
            self.retryable = retryable

    def to_body(self, request_id: str | None = None) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            message=self.message,
            retryable=self.retryable or self.code in _RETRYABLE,
            suggested_fallback=self.suggested_fallback,
            param=self.param,
            details=self.details,
            request_id=request_id,
        )


class InvalidRequest(GatewayError):
    code = ErrorCode.INVALID_REQUEST
    http_status = 400


class UnsupportedParam(GatewayError):
    code = ErrorCode.UNSUPPORTED_PARAM
    http_status = 422


class RequestTooLarge(GatewayError):
    code = ErrorCode.REQUEST_TOO_LARGE
    http_status = 413


class TextTooLong(GatewayError):
    code = ErrorCode.TEXT_TOO_LONG
    http_status = 413


class NotFound(GatewayError):
    code = ErrorCode.NOT_FOUND
    http_status = 404


class Unauthorized(GatewayError):
    code = ErrorCode.UNAUTHORIZED
    http_status = 401


class Forbidden(GatewayError):
    code = ErrorCode.FORBIDDEN
    http_status = 403


class RateLimited(GatewayError):
    code = ErrorCode.RATE_LIMITED
    http_status = 429
    retryable = True


class BudgetExceeded(GatewayError):
    """Monthly budget cap hit. Deliberately NOT retryable — retrying cannot help."""

    code = ErrorCode.BUDGET_EXCEEDED
    http_status = 402
    retryable = False


class QuotaExceeded(GatewayError):
    code = ErrorCode.QUOTA_EXCEEDED
    http_status = 402
    retryable = False


class ProviderError(GatewayError):
    code = ErrorCode.PROVIDER_ERROR
    http_status = 502
    retryable = True

    def __init__(self, provider: str, message: str, **kw: Any) -> None:
        super().__init__(f"[{provider}] {message}", **kw)
        self.provider = provider
        self.details.setdefault("provider", provider)


class ProviderUnavailable(ProviderError):
    code = ErrorCode.PROVIDER_UNAVAILABLE
    http_status = 503


class UpstreamAuthError(ProviderError):
    code = ErrorCode.UPSTREAM_AUTH
    http_status = 502
    retryable = False


class UpstreamRateLimited(ProviderError):
    code = ErrorCode.UPSTREAM_RATE_LIMITED
    http_status = 503
    retryable = True


class AllProvidersFailed(GatewayError):
    code = ErrorCode.ALL_PROVIDERS_FAILED
    http_status = 503
    retryable = True


class Timeout(GatewayError):
    code = ErrorCode.TIMEOUT
    http_status = 504
    retryable = True
