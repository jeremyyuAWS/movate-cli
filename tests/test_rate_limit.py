"""Per-API-key rate limiting — token-bucket math + middleware integration.

Two layers:

1. **Pure-Python token-bucket math** — ``InProcessRateLimiter.check``
   tested directly with mocked time via ``time.monotonic`` /
   ``time.time`` monkeypatches. Asserts capacity, refill, burst,
   denied path with ``retry_after``.
2. **Middleware integration** — full FastAPI app with a low-capacity
   limiter; assert the Nth request gets 429 with the right headers,
   ``/healthz`` and ``/ready`` are NOT rate-limited (they're
   unauthed; ACA probes them every 10s).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import mint_api_key
from movate.core.models import ApiKeyEnv
from movate.core.rate_limit import (
    InProcessRateLimiter,
    NoOpRateLimiter,
    RateLimitDecision,
)
from movate.runtime import build_app
from movate.testing import InMemoryStorage

# ---------------------------------------------------------------------------
# 1. Pure-Python token-bucket math
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_clock(monkeypatch) -> Iterator[list[float]]:
    """Pin both ``time.monotonic`` and ``time.time`` to a mutable
    holder so tests can advance the clock deterministically.

    ``holder[0]`` is the current "time" in seconds. Tests mutate it
    to fast-forward (e.g. ``holder[0] += 30``) and observe behavior
    at the new clock position.
    """
    holder = [1000.0]  # monotonic must be a positive float
    monkeypatch.setattr("movate.core.rate_limit.time.monotonic", lambda: holder[0])
    monkeypatch.setattr("movate.core.rate_limit.time.time", lambda: holder[0])
    yield holder


@pytest.mark.unit
async def test_bucket_starts_full(fixed_clock) -> None:
    """First request gets allowed=True with ``remaining = capacity - 1``."""
    limiter = InProcessRateLimiter(limit_per_minute=60)
    decision = await limiter.check("key-A")
    assert decision.allowed is True
    assert decision.limit == 60
    assert decision.remaining == 59  # one consumed
    assert decision.retry_after_seconds is None


@pytest.mark.unit
async def test_bucket_drains_on_repeated_calls(fixed_clock) -> None:
    """N requests in a row drain the bucket to N-capacity. With no
    elapsed time, no refill happens between calls."""
    limiter = InProcessRateLimiter(limit_per_minute=5)
    decisions = [await limiter.check("key-A") for _ in range(5)]

    # All 5 allowed; remaining decrements each time.
    assert all(d.allowed for d in decisions)
    assert [d.remaining for d in decisions] == [4, 3, 2, 1, 0]

    # 6th request → denied.
    denied = await limiter.check("key-A")
    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.retry_after_seconds is not None
    assert denied.retry_after_seconds >= 1


@pytest.mark.unit
async def test_bucket_refills_with_elapsed_time(fixed_clock) -> None:
    """After enough time passes, the bucket refills and the next
    request succeeds. 60 req/min = 1 req/sec refill rate."""
    limiter = InProcessRateLimiter(limit_per_minute=60)
    # Drain the bucket completely.
    for _ in range(60):
        decision = await limiter.check("key-A")
        assert decision.allowed
    # Next one denied.
    assert (await limiter.check("key-A")).allowed is False

    # Advance 5 seconds → 5 tokens refill (1/sec rate).
    fixed_clock[0] += 5
    for _ in range(5):
        decision = await limiter.check("key-A")
        assert decision.allowed, "should allow 5 requests after 5s refill"
    # 6th denied again.
    assert (await limiter.check("key-A")).allowed is False


@pytest.mark.unit
async def test_bucket_capacity_caps_refill(fixed_clock) -> None:
    """After a long idle period, the bucket is FULL (capacity) — never
    over. A 1-hour idle on a 60/min limit doesn't give you 3600
    requests, just 60."""
    limiter = InProcessRateLimiter(limit_per_minute=60)
    # First request consumes 1 → 59 left.
    await limiter.check("key-A")
    # Sleep 1 hour worth of refill (3600 tokens worth).
    fixed_clock[0] += 3600
    decision = await limiter.check("key-A")
    # Should have refilled to capacity (60), then consumed 1.
    assert decision.remaining == 59


@pytest.mark.unit
async def test_per_key_isolation(fixed_clock) -> None:
    """Two keys are independent. Draining A's bucket doesn't affect B."""
    limiter = InProcessRateLimiter(limit_per_minute=3)
    # Drain A.
    for _ in range(3):
        await limiter.check("key-A")
    assert (await limiter.check("key-A")).allowed is False

    # B's bucket is still full — first check consumes 1, leaving 2.
    b_first = await limiter.check("key-B")
    assert b_first.allowed is True
    assert b_first.remaining == 2


@pytest.mark.unit
async def test_retry_after_decreases_as_time_passes(fixed_clock) -> None:
    """``retry_after`` shrinks as elapsed time accumulates — at t=0
    we wait the full 1s, at t=0.5s we wait 0.5s (rounded up to 1)."""
    limiter = InProcessRateLimiter(limit_per_minute=60)
    # Drain.
    for _ in range(60):
        await limiter.check("key-A")
    first = await limiter.check("key-A")
    assert first.retry_after_seconds == 1  # 1s to refill 1 token at 1/s

    # Halfway through the refill — still rounds up to 1.
    fixed_clock[0] += 0.4
    second = await limiter.check("key-A")
    assert second.allowed is False
    assert second.retry_after_seconds == 1  # ceil(0.6) = 1


@pytest.mark.unit
async def test_limit_below_one_raises() -> None:
    """``limit_per_minute < 1`` is operator error — fail loud at
    construction. Use the explicit NoOp limiter to disable."""
    with pytest.raises(ValueError, match="limit_per_minute"):
        InProcessRateLimiter(limit_per_minute=0)
    with pytest.raises(ValueError):
        InProcessRateLimiter(limit_per_minute=-1)


@pytest.mark.unit
async def test_noop_always_allows() -> None:
    """``NoOpRateLimiter`` always allows; sentinel limit=0 is the
    operator signal "rate limiting is disabled."""
    limiter = NoOpRateLimiter()
    for _ in range(1000):
        d: RateLimitDecision = await limiter.check("any-key")
        assert d.allowed is True
        assert d.limit == 0


# ---------------------------------------------------------------------------
# 2. Middleware integration — full HTTP path with low capacity
# ---------------------------------------------------------------------------


@pytest.fixture
async def auth_app() -> tuple[TestClient, str]:
    """Build an app with a tight 3 req/min limit + a registered API key.

    Returns (client, bearer_token). Each test starts with a full
    bucket since the limiter is fresh per app.
    """
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="rl-test")
    await storage.save_api_key(minted.record)
    app = build_app(storage, rate_limit_per_minute=3)
    return TestClient(app), f"Bearer {minted.full_key}"


@pytest.mark.unit
async def test_authenticated_request_carries_rate_limit_headers(auth_app) -> None:
    """Every successful auth'd response includes ``X-RateLimit-*``
    headers so clients can budget proactively."""
    client, token = auth_app
    r = client.get("/agents", headers={"Authorization": token})
    assert r.status_code == 200
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert int(r.headers["X-RateLimit-Remaining"]) >= 0
    assert int(r.headers["X-RateLimit-Reset"]) > 0


@pytest.mark.unit
async def test_burst_exhausts_then_429_with_retry_after(auth_app) -> None:
    """Drain the bucket; next request is 429 with ``Retry-After`` +
    the standard rate-limit headers (so clients can handle it
    programmatically)."""
    client, token = auth_app
    # 3 allowed.
    for _ in range(3):
        r = client.get("/agents", headers={"Authorization": token})
        assert r.status_code == 200

    # 4th denied.
    r = client.get("/agents", headers={"Authorization": token})
    assert r.status_code == 429
    body = r.json()
    assert body["detail"]["error"]["code"] == "rate_limited"
    # Retry-After is the standard RFC 7231 header.
    assert int(r.headers["Retry-After"]) >= 1
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert r.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.unit
async def test_unauthenticated_request_not_rate_limited(auth_app) -> None:
    """Auth fails BEFORE the rate-limit check, so a flood of bad-key
    requests gets 401 (not 429) and doesn't drain anyone's bucket."""
    client, _ = auth_app
    bad = "Bearer mvt_live_deadbeef_00000000_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    for _ in range(20):
        r = client.get("/agents", headers={"Authorization": bad})
        assert r.status_code == 401


@pytest.mark.unit
async def test_healthz_not_rate_limited(auth_app) -> None:
    """``/healthz`` is unauthed → never hits the rate-limit code path.
    Floods of probes from ACA mustn't be capped."""
    client, _ = auth_app
    for _ in range(100):
        r = client.get("/healthz")
        assert r.status_code == 200
    # No rate-limit headers on unauthed endpoints — they don't have an
    # api_key_id to attribute against.
    assert "X-RateLimit-Limit" not in r.headers


@pytest.mark.unit
async def test_ready_not_rate_limited(auth_app) -> None:
    """``/ready`` likewise; ACA hits this every 10s, mustn't burn a budget."""
    client, _ = auth_app
    for _ in range(100):
        r = client.get("/ready")
        assert r.status_code == 200


@pytest.mark.unit
async def test_per_key_isolation_at_http_layer() -> None:
    """Two API keys → two independent buckets. Draining key A doesn't
    affect key B's budget."""
    storage = InMemoryStorage()
    await storage.init()
    a = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="a")
    b = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, label="b")
    await storage.save_api_key(a.record)
    await storage.save_api_key(b.record)
    client = TestClient(build_app(storage, rate_limit_per_minute=2))

    tok_a = f"Bearer {a.full_key}"
    tok_b = f"Bearer {b.full_key}"

    # Drain A.
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 200
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 200
    assert client.get("/agents", headers={"Authorization": tok_a}).status_code == 429

    # B still has a full bucket.
    assert client.get("/agents", headers={"Authorization": tok_b}).status_code == 200


@pytest.mark.unit
async def test_disabled_rate_limit_serves_zero_limit_header() -> None:
    """``rate_limit_per_minute=0`` → NoOpRateLimiter. Every request
    allowed; headers show the sentinel ``Limit: 0`` so operators can
    spot "rate limiting is OFF" without grepping config."""
    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)
    client = TestClient(build_app(storage, rate_limit_per_minute=0))
    token = f"Bearer {minted.full_key}"

    for _ in range(50):
        r = client.get("/agents", headers={"Authorization": token})
        assert r.status_code == 200
    assert r.headers["X-RateLimit-Limit"] == "0"


@pytest.mark.unit
async def test_token_refill_lets_blocked_client_recover(monkeypatch) -> None:
    """End-to-end recovery: get 429, wait the Retry-After window
    (simulated by advancing the limiter's clock), succeed.

    Patches the limiter's clock so we don't actually sleep — same
    pattern as the pure-math tests, applied to the middleware path.
    """
    holder = [1000.0]
    monkeypatch.setattr("movate.core.rate_limit.time.monotonic", lambda: holder[0])
    monkeypatch.setattr("movate.core.rate_limit.time.time", lambda: holder[0])

    storage = InMemoryStorage()
    await storage.init()
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)
    client = TestClient(build_app(storage, rate_limit_per_minute=2))
    token = f"Bearer {minted.full_key}"

    # Drain.
    client.get("/agents", headers={"Authorization": token})
    client.get("/agents", headers={"Authorization": token})
    denied = client.get("/agents", headers={"Authorization": token})
    assert denied.status_code == 429
    retry_after = int(denied.headers["Retry-After"])

    # Fast-forward past the retry window.
    holder[0] += retry_after + 1

    # Now allowed.
    recovered = client.get("/agents", headers={"Authorization": token})
    assert recovered.status_code == 200


# Suppress an unused-import warning for ``time`` (only used by
# fixtures/monkeypatching — pyflakes can't see through the dotted
# string in ``monkeypatch.setattr``).
_ = time
