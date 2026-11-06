def test_rate_limited_sends_the_retry_after_header() -> None:
    """AGENTS.md tells clients to honour Retry-After on a 429.

    For a long time the delay existed only inside the JSON body, so a client that trusted
    the documentation waited for a header that was never sent.
    """
    from ttsgrab.core.errors import RateLimited

    exc = RateLimited("slow down", details={"retry_after_seconds": 42})
    headers = exc.http_headers("req-123")
    assert headers["Retry-After"] == "42"
    assert headers["X-Request-Id"] == "req-123"


def test_retry_after_is_absent_when_the_delay_is_unknown() -> None:
    """Sending "Retry-After: 0" would invite an immediate retry that must fail again."""
    from ttsgrab.core.errors import RateLimited

    assert "Retry-After" not in RateLimited("slow down").http_headers()


def test_non_rate_limit_errors_do_not_carry_retry_after() -> None:
    from ttsgrab.core.errors import BudgetExceeded

    headers = BudgetExceeded("over budget").http_headers("req-1")
    assert "Retry-After" not in headers
    assert headers["X-Request-Id"] == "req-1"
