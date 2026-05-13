"""Tests for CORS middleware on the MDK runtime.

BACKLOG Group G item 51. The Mova iO Angular front end calls the
runtime from a different origin (e.g. ``https://mova-io.movate.com``);
without CORS the browser blocks every response.

Coverage:

* CORS middleware mounted only when origins are configured (empty
  default keeps server-to-server use cases unchanged).
* ``MDK_CORS_ALLOWED_ORIGINS`` env var is read.
* ``MOVATE_CORS_ALLOWED_ORIGINS`` is honored as legacy alias.
* Explicit ``cors_allowed_origins=`` kwarg overrides env (for tests).
* Preflight OPTIONS requests get the expected headers.
* Rate-limit headers are exposed (so the Angular client can read them).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movate.runtime import build_app
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


# ---------------------------------------------------------------------------
# CORS disabled by default (no env, no kwarg)
# ---------------------------------------------------------------------------


def test_no_cors_headers_when_unconfigured(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty allow-list means the CORSMiddleware isn't mounted.
    Server-to-server callers (the existing `mdk submit` path, the
    Teams bot, etc.) keep working as before — no behavior change.

    A browser hitting this would get its request blocked by its own
    CORS policy, which is the correct + conservative default.
    """
    monkeypatch.delenv("MDK_CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("MOVATE_CORS_ALLOWED_ORIGINS", raising=False)
    client = TestClient(build_app(storage))
    r = client.get(
        "/healthz",
        headers={"Origin": "http://localhost:4200"},
    )
    assert r.status_code == 200
    # No Access-Control-Allow-Origin header without configured origins.
    assert "access-control-allow-origin" not in {h.lower() for h in r.headers}


# ---------------------------------------------------------------------------
# Explicit kwarg
# ---------------------------------------------------------------------------


def test_cors_kwarg_enables_middleware(storage: InMemoryStorage) -> None:
    """`build_app(cors_allowed_origins=[...])` mounts CORS without
    touching the environment. Used by tests; also lets ops construct
    apps with origins from a config file."""
    client = TestClient(build_app(storage, cors_allowed_origins=["http://localhost:4200"]))
    r = client.get(
        "/healthz",
        headers={"Origin": "http://localhost:4200"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:4200"


def test_cors_kwarg_with_unlisted_origin_does_not_echo_it(
    storage: InMemoryStorage,
) -> None:
    """Browsers won't accept a request from origin X if the server's
    Access-Control-Allow-Origin doesn't include X. Verify the runtime
    is strict — an origin not in the allow-list gets no ACAO header
    back."""
    client = TestClient(build_app(storage, cors_allowed_origins=["https://mova-io.movate.com"]))
    r = client.get(
        "/healthz",
        headers={"Origin": "http://attacker.example.com"},
    )
    assert r.status_code == 200
    # Either no ACAO header at all, or it's pinned to the configured
    # origin (not the attacker's). Both are spec-compliant rejections
    # from the browser's perspective.
    acao = r.headers.get("access-control-allow-origin")
    assert acao != "http://attacker.example.com"


# ---------------------------------------------------------------------------
# Env-var fallback
# ---------------------------------------------------------------------------


def test_cors_reads_mdk_env_var(storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    """`MDK_CORS_ALLOWED_ORIGINS` is the canonical env var. Single
    origin, leading/trailing whitespace tolerated."""
    monkeypatch.setenv(
        "MDK_CORS_ALLOWED_ORIGINS",
        "  http://localhost:4200  ",
    )
    client = TestClient(build_app(storage))
    r = client.get(
        "/healthz",
        headers={"Origin": "http://localhost:4200"},
    )
    assert r.headers["access-control-allow-origin"] == "http://localhost:4200"


def test_cors_reads_multiple_origins_from_env(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comma-separated list — dev, staging, prod all in one var."""
    monkeypatch.setenv(
        "MDK_CORS_ALLOWED_ORIGINS",
        "http://localhost:4200,https://staging.mova-io.movate.com,https://mova-io.movate.com",
    )
    client = TestClient(build_app(storage))
    for origin in [
        "http://localhost:4200",
        "https://staging.mova-io.movate.com",
        "https://mova-io.movate.com",
    ]:
        r = client.get("/healthz", headers={"Origin": origin})
        assert r.headers["access-control-allow-origin"] == origin, (
            f"origin {origin} should be allowed"
        )


def test_cors_falls_back_to_legacy_movate_env(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy `MOVATE_CORS_ALLOWED_ORIGINS` keeps working — same
    transitional pattern as every other MDK_*/MOVATE_* alias."""
    monkeypatch.delenv("MDK_CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("MOVATE_CORS_ALLOWED_ORIGINS", "http://localhost:4200")
    client = TestClient(build_app(storage))
    r = client.get(
        "/healthz",
        headers={"Origin": "http://localhost:4200"},
    )
    assert r.headers["access-control-allow-origin"] == "http://localhost:4200"


def test_cors_mdk_env_wins_over_legacy(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both prefixes are set the canonical `MDK_*` wins. Matches
    the env-aliases module convention."""
    monkeypatch.setenv("MDK_CORS_ALLOWED_ORIGINS", "https://canonical.example.com")
    monkeypatch.setenv("MOVATE_CORS_ALLOWED_ORIGINS", "https://legacy.example.com")
    client = TestClient(build_app(storage))
    r = client.get(
        "/healthz",
        headers={"Origin": "https://canonical.example.com"},
    )
    assert r.headers["access-control-allow-origin"] == "https://canonical.example.com"


# ---------------------------------------------------------------------------
# Explicit kwarg trumps env (for hermetic tests)
# ---------------------------------------------------------------------------


def test_cors_kwarg_wins_over_env(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing the kwarg overrides whatever's in the environment.
    Lets ops pin origins from a YAML config, lets tests stay
    hermetic."""
    monkeypatch.setenv("MDK_CORS_ALLOWED_ORIGINS", "https://env-origin.example.com")
    client = TestClient(
        build_app(storage, cors_allowed_origins=["https://kwarg-origin.example.com"])
    )
    r = client.get(
        "/healthz",
        headers={"Origin": "https://kwarg-origin.example.com"},
    )
    assert r.headers["access-control-allow-origin"] == "https://kwarg-origin.example.com"


# ---------------------------------------------------------------------------
# Preflight (OPTIONS request)
# ---------------------------------------------------------------------------


def test_preflight_options_returns_cors_headers(storage: InMemoryStorage) -> None:
    """Browsers send an OPTIONS preflight before any non-simple request
    (POST with JSON body, custom headers, etc.). Without correct
    preflight headers, the Angular app's actual request never fires."""
    client = TestClient(build_app(storage, cors_allowed_origins=["http://localhost:4200"]))
    r = client.options(
        "/run",
        headers={
            "Origin": "http://localhost:4200",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    # CORS preflight returns 200 (Starlette default).
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:4200"
    # POST must be in the allowed methods list.
    assert "POST" in r.headers.get("access-control-allow-methods", "")


# ---------------------------------------------------------------------------
# Exposed headers — rate-limit must be visible to browser JS
# ---------------------------------------------------------------------------


def test_rate_limit_headers_exposed_to_browser_js(
    storage: InMemoryStorage,
) -> None:
    """X-RateLimit-* and Retry-After need expose_headers so the Angular
    client can read them via Response.headers.get(). Without this, the
    browser strips them from the JS-visible response even though the
    runtime sent them.

    Starlette emits ``access-control-expose-headers`` on the actual
    cross-origin response (NOT on the preflight OPTIONS), so we test
    against an unauthed GET that does cross-origin work.
    """
    client = TestClient(build_app(storage, cors_allowed_origins=["http://localhost:4200"]))
    r = client.get(
        "/healthz",
        headers={"Origin": "http://localhost:4200"},
    )
    exposed = r.headers.get("access-control-expose-headers", "")
    exposed_lc = exposed.lower()
    assert "x-ratelimit-limit" in exposed_lc
    assert "x-ratelimit-remaining" in exposed_lc
    assert "x-ratelimit-reset" in exposed_lc
    assert "retry-after" in exposed_lc
