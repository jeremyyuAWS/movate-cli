"""GitHub App integration — push agent bundles to a per-tenant repo.

Implements the publish path from ADR 007:

1. Sign a JWT with the App's private key (RS256, 10-min validity).
2. Exchange the JWT for an installation access token
   (POST /app/installations/{id}/access_tokens). Cached in-process
   with a 5-min safety buffer below the 1h GitHub TTL.
3. Push a bundle as ONE commit via the Git Data API:
   blobs → tree (with base_tree) → commit (with parent) → ref update.
   One bundle = one commit. Pristine git history; no "wip" noise.

The module imports ``cryptography`` lazily so the base install stays
clean — install the ``github`` extra to enable it:

    uv pip install '.[github]'

Test seam: every HTTP call goes through an ``httpx.AsyncClient`` you
pass into ``GitHubClient``. In tests, hand in one wired with
``httpx.MockTransport`` (see ``tests/test_integrations_github.py``).

Threading the JWT/installation-token cache through an instance keeps
the typical lifetime (per-process, ~hours) right — the runtime builds
one client at app construction (when ``MDK_GITHUB_ENABLED=1``) and
reuses it across requests.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

logger = logging.getLogger(__name__)

# GitHub returns 201 Created for successful installation-token exchanges.
# Named here so the comparison reads as intent, not magic.
_HTTP_CREATED = 201


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GitHubError(RuntimeError):
    """Raised on any GitHub-integration failure.

    Carries an optional ``status_code`` so the FastAPI layer can map it
    onto the right HTTP response — 503 for "not configured", 502 for
    upstream GitHub failures, 422 for malformed config, etc.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_status = upstream_status


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubConfig:
    """Per-tenant GitHub App configuration.

    For v0.7 we read this from process env (one tenant per runtime) —
    multi-tenant lookup against a config store comes with ADR 007's
    follow-up work (item 81).

    All fields are required EXCEPT the optional ones with defaults.
    """

    app_id: int
    """The MDK GitHub App's numeric ID (set at App creation time)."""

    installation_id: int
    """The tenant's per-org installation ID."""

    private_key_pem: str
    """RSA private key in PEM format (full text, including the BEGIN/END
    lines). In production this comes from Key Vault via env injection;
    in tests it's generated on the fly."""

    repo: str
    """``owner/repo`` — e.g. ``movate/mova-io-agents-acme``."""

    default_branch: str = "main"
    """Branch the publish commits land on. ADR 007 decision 4 keeps this
    as ``main`` for the common case; branch-protection-paths handle the
    PR-required cases (v0.8+, not in scope for item 78)."""

    commit_author_name: str = "Mova iO"
    commit_author_email: str = "noreply@mova-io.movate.com"

    api_base: str = "https://api.github.com"
    """Overridable for self-hosted GHES (open question 1 in ADR 007)."""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> GitHubConfig:
        """Build a config from process env. Raises on missing required vars."""
        e = env if env is not None else os.environ
        try:
            app_id = int(e["MDK_GITHUB_APP_ID"])
            installation_id = int(e["MDK_GITHUB_INSTALLATION_ID"])
            private_key_pem = e["MDK_GITHUB_PRIVATE_KEY"]
            repo = e["MDK_GITHUB_REPO"]
        except KeyError as exc:
            missing = exc.args[0]
            raise GitHubError(
                f"github integration enabled but {missing} is unset; "
                "set MDK_GITHUB_APP_ID, MDK_GITHUB_INSTALLATION_ID, "
                "MDK_GITHUB_PRIVATE_KEY, MDK_GITHUB_REPO (see ADR 007 "
                "Appendix B for the schema)",
                status_code=422,
            ) from None
        except ValueError as exc:
            raise GitHubError(
                f"github integration config has a non-integer ID: {exc}",
                status_code=422,
            ) from None

        return cls(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=private_key_pem,
            repo=repo,
            default_branch=e.get("MDK_GITHUB_DEFAULT_BRANCH", "main"),
            commit_author_name=e.get("MDK_GITHUB_COMMIT_AUTHOR_NAME", "Mova iO"),
            commit_author_email=e.get(
                "MDK_GITHUB_COMMIT_AUTHOR_EMAIL", "noreply@mova-io.movate.com"
            ),
            api_base=e.get("MDK_GITHUB_API_BASE", "https://api.github.com"),
        )


def is_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the GitHub integration is turned on.

    Operators flip it via ``MDK_GITHUB_ENABLED=1`` (any of ``1``,
    ``true``, ``yes`` accepted). Default off — the runtime ships
    publish endpoints that return 503 until ops registers the App
    and sets the env flag. See ADR 007 phasing for the rollout plan.
    """
    e = env if env is not None else os.environ
    raw = e.get("MDK_GITHUB_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    """What the publish path returns on success.

    ``files_changed`` is the per-publish set of relative paths the
    runtime wrote — handy for the Angular UI's "files in this commit"
    panel without a second GitHub call."""

    commit_sha: str
    commit_url: str
    branch: str
    files_changed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Thin wrapper around the parts of GitHub's REST API we need.

    One client per runtime process — internal caches (installation
    token + its expiry) are tied to the instance lifetime. The
    ``http`` arg is injected so tests can pass an ``AsyncClient`` with
    a ``MockTransport`` — production callers should let the default
    constructor build one bound to ``config.api_base``.
    """

    def __init__(
        self,
        config: GitHubConfig,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        # ``clock`` is injectable for deterministic JWT-expiry tests.
        self._clock = clock or time.time
        # Default-built clients have no base_url so tests with
        # MockTransport can mount at "/" without juggling absolute
        # URLs. We always pass the full URL into request() below.
        self._http = http or httpx.AsyncClient(timeout=30.0)
        # Installation-token cache. Refreshed when missing OR within
        # 5 minutes of expiry (token TTL from GitHub is ~1h, leaving
        # plenty of margin).
        self._installation_token: str | None = None
        self._installation_token_expires_at: float = 0.0

    # -- JWT signing -----------------------------------------------------

    def _sign_app_jwt(self) -> str:
        """Build + sign a 10-min App JWT (RS256) per GitHub's spec.

        Spec: https://docs.github.com/en/apps/creating-github-apps/
        authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app

        We hand-build the JWT (header.payload.signature, base64url) so
        we don't pull in PyJWT just for this one call site. Keeps the
        ``github`` extra at a single cryptography wheel.
        """
        # Lazy import — the github extra is what brings cryptography
        # in; if it's missing we'd rather error at FIRST USE than at
        # import time of an unrelated module.
        try:
            from cryptography.hazmat.primitives import (  # noqa: PLC0415
                hashes,
                serialization,
            )
            from cryptography.hazmat.primitives.asymmetric import (  # noqa: PLC0415
                padding,
            )
        except ImportError as exc:  # pragma: no cover - dep-missing path
            raise GitHubError(
                "github integration requires the `github` extra "
                "(install with `uv pip install '.[github]'`)",
                status_code=503,
            ) from exc

        now = int(self._clock())
        header = {"alg": "RS256", "typ": "JWT"}
        # ``iat`` is set 60s in the past to tolerate clock skew between
        # us + GitHub. ``exp`` is the GitHub maximum (10min) so the
        # window where a leaked JWT is usable stays narrow.
        payload = {
            "iat": now - 60,
            "exp": now + (10 * 60),
            "iss": str(self.config.app_id),
        }

        def b64u(data: bytes) -> bytes:
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        encoded_header = b64u(json.dumps(header, separators=(",", ":")).encode())
        encoded_payload = b64u(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = encoded_header + b"." + encoded_payload

        try:
            private_key = serialization.load_pem_private_key(
                self.config.private_key_pem.encode(), password=None
            )
        except Exception as exc:  # pragma: no cover - bad key
            raise GitHubError(
                f"could not parse MDK_GITHUB_PRIVATE_KEY as a PEM RSA key: {exc}",
                status_code=422,
            ) from exc

        # cryptography's load_pem_private_key returns a Union over
        # every supported key type. GitHub Apps require RSA — narrow
        # via isinstance so the .sign() call type-checks against the
        # RSA-specific signature (PKCS1v15 padding + SHA256 hash).
        from cryptography.hazmat.primitives.asymmetric.rsa import (  # noqa: PLC0415
            RSAPrivateKey,
        )

        if not isinstance(private_key, RSAPrivateKey):  # pragma: no cover
            raise GitHubError(
                "MDK_GITHUB_PRIVATE_KEY is not an RSA private key "
                "(GitHub Apps require RSA)",
                status_code=422,
            )

        rsa_key: RSAPrivateKey = private_key
        signature = rsa_key.sign(
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        encoded_signature = b64u(signature)
        return (signing_input + b"." + encoded_signature).decode()

    # -- Installation token ----------------------------------------------

    async def _get_installation_token(self) -> str:
        """Return a valid installation token, refreshing if near expiry."""
        now = self._clock()
        cached = self._installation_token
        # 5-min safety buffer below the actual expiry so we never use
        # a token that times out mid-request. GitHub's TTL is ~1h.
        if cached and now < (self._installation_token_expires_at - 300):
            return cached

        jwt = self._sign_app_jwt()
        url = (
            f"{self.config.api_base}/app/installations/{self.config.installation_id}/access_tokens"
        )
        resp = await self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        # GitHub returns 201 Created on a successful access-token mint.
        # Anything else (401 bad credentials, 404 wrong installation,
        # 5xx upstream) → surface as a 502 with the upstream status
        # preserved so the operator can see what GitHub said.
        if resp.status_code != _HTTP_CREATED:
            raise GitHubError(
                f"github installation token exchange failed "
                f"({resp.status_code}): {resp.text[:200]}",
                status_code=502,
                upstream_status=resp.status_code,
            )
        body = resp.json()
        token_raw = body.get("token")
        if not isinstance(token_raw, str):
            raise GitHubError(
                "github installation-token response missing 'token' field",
                status_code=502,
                upstream_status=resp.status_code,
            )
        token: str = token_raw
        # GitHub returns ``expires_at`` as ISO-8601. We don't need to
        # parse precisely — just refresh well before then. Cache the
        # raw UTC epoch derived from the response's `expires_at` or,
        # if absent, default to "now + 50 minutes" (safe under the 1h
        # TTL).
        expires_at_iso = body.get("expires_at")
        if expires_at_iso:
            try:
                from datetime import datetime  # noqa: PLC0415

                expires_at = datetime.fromisoformat(
                    expires_at_iso.replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                expires_at = now + (50 * 60)
        else:
            expires_at = now + (50 * 60)

        self._installation_token = token
        self._installation_token_expires_at = expires_at
        return token

    # -- Authenticated REST helper ---------------------------------------

    async def _api(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expected_status: tuple[int, ...] = (200, 201),
    ) -> dict[str, Any]:
        """Issue an authenticated API call against the configured repo.

        ``path`` is the resource path relative to ``/repos/{repo}/``
        (e.g. ``"git/blobs"``, ``"git/refs/heads/main"``). Returns the
        parsed JSON body; raises :class:`GitHubError` on a status not
        in ``expected_status``.
        """
        token = await self._get_installation_token()
        url = f"{self.config.api_base}/repos/{self.config.repo}/{path}"
        resp = await self._http.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=json_body,
        )
        if resp.status_code not in expected_status:
            raise GitHubError(
                f"github {method} {path} failed ({resp.status_code}): {resp.text[:200]}",
                status_code=502,
                upstream_status=resp.status_code,
            )
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            raise GitHubError(
                f"github {method} {path} returned non-object ({type(data).__name__})",
                status_code=502,
            )
        return data

    # -- Public API ------------------------------------------------------

    async def publish_bundle(
        self,
        bundle_dir: Path,
        *,
        target_dir: str,
        message: str,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> PublishResult:
        """Push every file under ``bundle_dir`` to ``<repo>/<target_dir>/``
        as a single commit on the configured default branch.

        Steps (all via the Git Data API so the whole publish is ONE
        commit, not one-per-file):

        1. Read every file under ``bundle_dir`` recursively.
        2. POST /git/blobs for each (base64-encoded content).
        3. GET /git/ref/heads/{branch} → head commit SHA.
        4. GET /git/commits/{sha} → base tree SHA.
        5. POST /git/trees with base_tree + the new blobs.
        6. POST /git/commits with the new tree + parent + author/message.
        7. PATCH /git/refs/heads/{branch} → fast-forward to the new commit.

        Raises :class:`GitHubError` on any upstream failure. The
        caller is expected to translate to the right HTTP status.
        """
        if not bundle_dir.exists() or not bundle_dir.is_dir():
            raise GitHubError(
                f"bundle directory not found: {bundle_dir}",
                status_code=404,
            )

        files = _collect_bundle_files(bundle_dir)
        if not files:
            raise GitHubError(
                f"bundle directory is empty: {bundle_dir}",
                status_code=422,
            )

        # Step 1: blobs.
        tree_entries: list[dict[str, Any]] = []
        for rel_path, content in files:
            blob = await self._api(
                "POST",
                "git/blobs",
                json_body={
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                },
                expected_status=(201,),
            )
            tree_entries.append(
                {
                    "path": f"{target_dir.rstrip('/')}/{rel_path}",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        # Step 2: head ref → commit → tree.
        branch = self.config.default_branch
        head_ref = await self._api(
            "GET",
            f"git/ref/heads/{branch}",
            expected_status=(200,),
        )
        head_commit_sha = head_ref["object"]["sha"]
        head_commit = await self._api(
            "GET",
            f"git/commits/{head_commit_sha}",
            expected_status=(200,),
        )
        base_tree_sha = head_commit["tree"]["sha"]

        # Step 3: new tree off the base.
        new_tree = await self._api(
            "POST",
            "git/trees",
            json_body={
                "base_tree": base_tree_sha,
                "tree": tree_entries,
            },
            expected_status=(201,),
        )

        # Step 4: new commit pointing at the new tree.
        new_commit = await self._api(
            "POST",
            "git/commits",
            json_body={
                "message": message,
                "tree": new_tree["sha"],
                "parents": [head_commit_sha],
                "author": {
                    "name": author_name or self.config.commit_author_name,
                    "email": author_email or self.config.commit_author_email,
                    # Use the server's clock for `date` if needed — GitHub
                    # backfills if omitted, which is what we want.
                },
            },
            expected_status=(201,),
        )

        # Step 5: fast-forward the ref.
        await self._api(
            "PATCH",
            f"git/refs/heads/{branch}",
            json_body={"sha": new_commit["sha"], "force": False},
            expected_status=(200,),
        )

        commit_sha = new_commit["sha"]
        commit_url = f"https://github.com/{self.config.repo}/commit/{commit_sha}"
        return PublishResult(
            commit_sha=commit_sha,
            commit_url=commit_url,
            branch=branch,
            files_changed=[entry["path"] for entry in tree_entries],
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Call when shutting down."""
        await self._http.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_bundle_files(
    bundle_dir: Path,
) -> list[tuple[str, bytes]]:
    """Walk ``bundle_dir`` and return ``[(relative_path, content), ...]``.

    Relative paths use forward slashes (Git tree paths are always
    POSIX, even on Windows hosts). Dot-prefixed entries are skipped —
    they're never part of a valid bundle (``.staging-*``, ``.deleted-*``,
    ``.git``, ``.DS_Store``).
    """
    out: list[tuple[str, bytes]] = []
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle_dir)
        # Skip dot-prefixed segments anywhere in the path. Mirrors the
        # registry's scan logic for consistency.
        if any(part.startswith(".") for part in rel.parts):
            continue
        out.append((rel.as_posix(), path.read_bytes()))
    return out


__all__ = [
    "GitHubClient",
    "GitHubConfig",
    "GitHubError",
    "PublishResult",
    "is_enabled",
]
