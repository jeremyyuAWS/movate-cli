"""Agent bundle persistence for ``POST /api/v1/agents`` (Group G item 76).

The endpoint accepts a multipart form containing either:

* **Individual canonical files** — ``agent_yaml``, ``prompt``,
  ``input_schema``, ``output_schema``, optional ``dataset``.
* **A zipped bundle** — single ``bundle`` field containing the
  canonical folder layout.

Both modes converge on the same disk layout:

::

    <agents_path>/<name>/
        agent.yaml
        prompt.md
        schema/
            input.json
            output.json
        evals/
            dataset.jsonl      # optional

This module:

1. Stages the bundle into a temp dir
2. Validates the layout (allowed paths only — no escape, no extras)
3. Runs ``load_agent()`` to confirm the bundle is a real, parseable
   ``AgentSpec`` — same code path the CLI uses
4. Atomically renames the temp dir to its final location
5. Returns the canonical layout for the response

Errors raise :class:`AgentCreationError` with a typed ``status_code``
the route handler maps to an HTTP code (409 for conflict, 422 for
malformed bundles, 500 for unexpected disk failures).

Why a separate module: the route handler stays focused on
multipart-form parsing + auth + HTTP shape; the persistence logic is
testable without spinning up FastAPI.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from movate.core.loader import AgentBundle, AgentLoadError, load_agent

if TYPE_CHECKING:
    pass


# Files that are allowed at known canonical paths inside a bundle.
# This is the v0.7 minimum set; skills/, contexts/, prompts/, and
# knowledge.yaml are deferred to follow-up items (see BACKLOG Group G
# items 69, 71, and the wider Group F memory work). Accepting them
# silently would mean writing files we don't know how to render in
# ``mdk show`` yet; rejecting them with a clear "deferred to v0.8"
# error keeps the contract honest.
_REQUIRED_FILES: frozenset[str] = frozenset(
    {
        "agent.yaml",
        "prompt.md",
        "schema/input.json",
        "schema/output.json",
    }
)
_OPTIONAL_FILES: frozenset[str] = frozenset({"evals/dataset.jsonl"})
_ALLOWED_FILES: frozenset[str] = _REQUIRED_FILES | _OPTIONAL_FILES


class AgentCreationError(Exception):
    """Raised on any failure that should surface as a non-2xx HTTP
    response. The ``status_code`` attribute maps to the HTTP code
    the route handler should return.

    ``status_code`` choices:

    * **409** — agent with this name already exists
    * **422** — malformed bundle (invalid YAML, layout violation,
      validation failure)
    * **500** — unexpected I/O / filesystem failure
    """

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PersistResult:
    """What :func:`persist_bundle` returns on success.

    ``bundle`` is the freshly-loaded :class:`AgentBundle` so callers
    can pluck spec fields for the response (name, version, etc.)
    without a second load. ``files_persisted`` is the sorted list of
    canonical paths that landed under ``agent_dir``.
    """

    bundle: AgentBundle
    agent_dir: Path
    files_persisted: list[str]


def persist_bundle(
    files: dict[str, bytes],
    *,
    agents_path: Path,
    on_conflict: str = "reject",
) -> PersistResult:
    """Validate + persist an agent bundle to the canonical layout.

    ``files`` is a mapping of canonical path (e.g. ``"agent.yaml"``,
    ``"schema/input.json"``) → file bytes. The keys MUST be in the
    :data:`_ALLOWED_FILES` set; the route handler is responsible for
    extracting individual multipart fields OR unzipping a ``bundle``
    field into this shape.

    ``agents_path`` is where ``<name>/`` lands. Created if missing.

    ``on_conflict`` is one of:

    * ``"reject"`` (default) — raise :class:`AgentCreationError` with
      ``status_code=409`` if the target dir already exists. Used by
      ``POST /api/v1/agents``.
    * ``"replace"`` — atomically replace an existing dir. Used by
      ``PUT /api/v1/agents/{name}`` (item 57, deferred from v1).

    Raises :class:`AgentCreationError` on any failure; never leaves
    partial state on disk (temp dir is cleaned up on every error path).
    """
    _validate_layout(files)

    # Pull the agent name from agent.yaml to determine the target dir.
    # Parsing this early — before staging — lets us 409 on conflict
    # without writing a single byte if the agent already exists.
    name = _extract_agent_name(files["agent.yaml"])
    target_dir = agents_path / name

    if target_dir.exists() and on_conflict == "reject":
        raise AgentCreationError(
            f"agent {name!r} already exists at {target_dir}; "
            f"use PUT /api/v1/agents/{name} to update",
            status_code=409,
        )

    # Stage to a sibling temp dir, validate, then atomic-rename. The
    # tempdir lives under agents_path so the final rename is on the
    # same filesystem (rename across mountpoints would fail).
    agents_path.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{name}-", dir=agents_path))
    try:
        _write_files(staging, files)
        # load_agent() runs the same validation the CLI does — Pydantic
        # parse + prompt linter + schema sanity. If this raises, we
        # never publish anything to the live agents_path. Return value
        # is intentionally discarded — we re-load from the FINAL path
        # below so the bundle's internal paths reference the canonical
        # location, not the staging tmpdir.
        try:
            load_agent(staging)
        except AgentLoadError as exc:
            raise AgentCreationError(
                f"bundle failed validation: {exc}",
                status_code=422,
            ) from exc

        # Re-key the bundle's agent_dir to the FINAL location, not the
        # staging tmpdir. Callers serializing the bundle want the
        # canonical path.
        files_persisted = sorted(files.keys())

        # Atomic publish. If on_conflict=replace, an existing target
        # gets swapped into a .stale-<timestamp> sibling for the
        # operator to clean up out-of-band — safer than rmtree-then-
        # rename (which has a window where target_dir doesn't exist).
        if target_dir.exists():
            stale = target_dir.with_name(f".stale-{name}-{staging.name[-8:]}")
            target_dir.rename(stale)
            try:
                staging.rename(target_dir)
            except Exception:
                # Rollback: put the stale dir back, surface the failure.
                stale.rename(target_dir)
                raise
            # Best-effort cleanup of the stale dir. If this fails it's
            # cosmetic — the new bundle is live and the stale one is
            # safe to delete manually.
            shutil.rmtree(stale, ignore_errors=True)
        else:
            staging.rename(target_dir)

        # Reload from the final path so the bundle's internal paths
        # reference the canonical location, not the staging tmpdir.
        final_bundle = load_agent(target_dir)
        return PersistResult(
            bundle=final_bundle,
            agent_dir=target_dir,
            files_persisted=files_persisted,
        )
    except AgentCreationError:
        # Already-typed error; clean up staging + re-raise.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        # Unexpected I/O / OS failure. Surface as 500 so the operator
        # knows it's NOT a bad bundle (which would be a 422).
        shutil.rmtree(staging, ignore_errors=True)
        raise AgentCreationError(
            f"persist failed: {exc}",
            status_code=500,
        ) from exc


# ---------------------------------------------------------------------------
# Bundle-unzip helpers (zipped-bundle path)
# ---------------------------------------------------------------------------


def unzip_bundle(zip_bytes: bytes) -> dict[str, bytes]:
    """Unpack a zipped bundle into the ``{canonical_path: bytes}``
    dict that :func:`persist_bundle` accepts.

    Reads zip member names exactly — no automatic stripping of a
    leading top-level directory. Operators commonly zip an agent dir
    as ``zip -r faq-bot.zip faq-bot/`` which produces ``faq-bot/...``
    entries; we DO handle that case by stripping any single common
    top-level prefix shared by every entry.

    Raises :class:`AgentCreationError` (status 422) on:

    * Not a valid zip
    * Any entry escaping the bundle via ``..``
    * Any entry outside :data:`_ALLOWED_FILES`
    """
    try:
        with zipfile.ZipFile(_bytesio(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            stripped_names = _strip_common_prefix(names)
            files: dict[str, bytes] = {}
            for original, canonical in zip(names, stripped_names, strict=True):
                # Zip slip defense: reject any path that escapes via ..
                # or is absolute. _strip_common_prefix preserves the rest
                # of the path, so we check after the strip.
                if ".." in Path(canonical).parts or Path(canonical).is_absolute():
                    raise AgentCreationError(
                        f"bundle entry {original!r} has an unsafe path (must be relative, no '..')",
                        status_code=422,
                    )
                if canonical not in _ALLOWED_FILES:
                    raise AgentCreationError(
                        f"bundle entry {canonical!r} is not part of the "
                        f"canonical layout. Allowed: "
                        f"{sorted(_ALLOWED_FILES)}",
                        status_code=422,
                    )
                files[canonical] = zf.read(original)
            return files
    except zipfile.BadZipFile as exc:
        raise AgentCreationError(
            f"bundle is not a valid zip: {exc}",
            status_code=422,
        ) from exc


def _bytesio(data: bytes):  # type: ignore[no-untyped-def]
    """Tiny indirection so the zipfile call site stays one line."""
    from io import BytesIO  # noqa: PLC0415

    return BytesIO(data)


def _strip_common_prefix(names: list[str]) -> list[str]:
    """If every entry begins with the same first path segment, drop it.

    Handles ``zip -r faq-bot.zip faq-bot/`` producing entries like
    ``faq-bot/agent.yaml``. We strip the ``faq-bot/`` so the canonical
    layout matches what :data:`_ALLOWED_FILES` expects.

    Returns a NEW list; never mutates ``names``.
    """
    if not names:
        return []
    first_parts = {n.split("/", 1)[0] for n in names if "/" in n}
    # Only strip if EVERY entry has the same first segment AND no
    # entry is at the top level (which would mean the prefix isn't
    # really common).
    if len(first_parts) == 1 and all("/" in n for n in names):
        prefix = next(iter(first_parts)) + "/"
        return [n[len(prefix) :] for n in names]
    return list(names)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_layout(files: dict[str, bytes]) -> None:
    """Reject early if the bundle's keys aren't the canonical set."""
    keys = set(files.keys())
    missing = _REQUIRED_FILES - keys
    if missing:
        raise AgentCreationError(
            f"bundle is missing required files: {sorted(missing)}. "
            f"Required: {sorted(_REQUIRED_FILES)}",
            status_code=422,
        )
    extras = keys - _ALLOWED_FILES
    if extras:
        raise AgentCreationError(
            f"bundle contains files outside the canonical layout: "
            f"{sorted(extras)}. Allowed: {sorted(_ALLOWED_FILES)}",
            status_code=422,
        )


def _extract_agent_name(agent_yaml_bytes: bytes) -> str:
    """Parse the agent's ``name`` field from raw YAML bytes.

    Keeps the parse minimal — full validation happens later via
    :func:`load_agent` after staging. We only need the name to
    determine the target dir and check for conflicts.
    """
    import yaml  # noqa: PLC0415

    try:
        spec = yaml.safe_load(agent_yaml_bytes.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AgentCreationError(
            f"agent.yaml is not valid YAML: {exc}",
            status_code=422,
        ) from exc
    name = spec.get("name") if isinstance(spec, dict) else None
    if not isinstance(name, str) or not name:
        raise AgentCreationError(
            "agent.yaml is missing the required 'name' field",
            status_code=422,
        )
    return name


def _write_files(staging: Path, files: dict[str, bytes]) -> None:
    """Write each canonical-path → bytes entry into the staging dir,
    creating parent directories as needed.
    """
    for canonical_path, content in files.items():
        dest = staging / canonical_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
