"""Download + classify file attachments from Teams Activities.

Slice 3.1.d adds the path for users to drag an ``agent.yaml`` (or a
zipped agent dir) or a ``dataset.jsonl`` into a Teams channel and have
the bot validate / ingest it before running.

What this module owns
---------------------

* :class:`UploadKind` — discriminated tag for what we resolved the
  attachment to (agent / dataset / unknown).
* :class:`UploadResult` — outcome of one attachment: the kind, the
  on-disk path, the agent or dataset payload (when applicable), and an
  error message when validation failed. Pure value type — no I/O.
* :func:`ingest_attachment` — async function that fetches the
  attachment's bytes (file:// or http(s)://), classifies by filename
  suffix, validates by trying to load it, and returns an
  :class:`UploadResult`.
* :func:`temp_workspace` — context-manager that yields a fresh temp dir
  for an upload + cleans up on exit.

What's deferred
---------------

* **Microsoft Graph auth.** Teams native attachments arrive with a
  Graph URL that needs the bot's AAD token. For alpha (Bot Framework
  Emulator), we read ``file://`` URLs directly and HTTP(S) URLs without
  auth. Production Graph integration is a hardening follow-up — the
  download helper is structured to swap the fetcher.
* **Zip support.** Agents can ship as a directory (multiple files); we
  unpack ``.zip`` uploads on the fly so they validate through the
  existing :func:`load_agent` path. PR ships .zip support; .tar.gz and
  others can be added later if there's demand.
* **Multi-attachment runs.** This PR processes the FIRST file
  attachment on the Activity. Datasets-alongside-agents flow lives in
  slice 3.2 (eval-with-upload).
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from movate.core.loader import AgentBundle
    from movate.teams_bot.activity import Attachment


# Suffixes we recognise. The fetcher classifies by filename, not
# content-type, because Teams + the Emulator both default to
# ``application/octet-stream`` for arbitrary uploads.
_AGENT_SUFFIXES = {".yaml", ".yml", ".zip"}
_DATASET_SUFFIXES = {".jsonl"}

# Cap the bytes we'll accept per upload. Teams' own attachment ceiling
# is 4MB; we set our soft cap there too so the failure message can
# explain it. Pathological uploads (a 4MB jsonl with one giant line)
# get rejected at parse time downstream — that's correct.
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024


class UploadKind(StrEnum):
    """What we classified the attachment as.

    StrEnum values match what the handler renders in cards — keep
    them user-facing-friendly.
    """

    AGENT = "agent"
    DATASET = "dataset"
    UNKNOWN = "unknown"


@dataclass
class UploadResult:
    """Outcome of a single attachment ingest.

    Discriminated on ``kind`` + ``error``:

    * ``kind != UNKNOWN, error == ""`` — successful ingest. ``path``
      points at the on-disk artifact (for agents: the agent dir; for
      datasets: the .jsonl file). ``bundle`` is set when the agent
      loaded cleanly; ``None`` otherwise. ``filename`` is the original
      uploaded name.
    * ``error != ""`` — validation failed. The handler renders this
      via the error card. ``path`` may be ``None`` (download failed)
      or set (download succeeded but validation didn't).
    * ``kind == UNKNOWN`` — we couldn't tell what the file was supposed
      to be (bad suffix, empty filename). ``error`` carries the
      explanation.
    """

    kind: UploadKind
    filename: str = ""
    path: Path | None = None
    bundle: AgentBundle | None = None
    """Set only for successful AGENT uploads. Tests + the run handler
    consume this directly without re-loading."""

    error: str = ""
    """User-facing error message. Empty on success. Composed for card
    rendering — friendly wording, no stack traces."""


@contextlib.contextmanager
def temp_workspace(prefix: str = "movate-teams-upload-") -> Iterator[Path]:
    """Yield a fresh temp directory; clean it up on exit.

    The caller fetches + unpacks files into this dir, then either
    moves them somewhere persistent (rare) or lets them die when the
    context exits. We use ``shutil.rmtree(..., ignore_errors=True)``
    because the bot is single-process — there's no concurrent reader
    that could keep a file handle open.
    """
    work = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def fetch_bytes(url: str, *, max_bytes: int = _MAX_UPLOAD_BYTES) -> bytes:
    """Fetch the attachment's bytes from a Bot Framework attachment URL.

    Two paths:

    * ``file://`` URL — Bot Framework Emulator's local-dev path. Read
      from the filesystem directly. Used by tests too — much faster
      than spinning up an HTTP server.
    * ``http(s)://`` URL — Teams native sends a Microsoft Graph URL.
      For 3.1.d we hit it with httpx and no special auth (Emulator
      proxies / public test endpoints work). **Production Graph
      auth is a hardening follow-up** — see issue tracker.

    Raises :class:`ValueError` on unsupported schemes or oversized
    payloads. The handler turns these into error cards.
    """
    if url.startswith("file://"):
        path = Path(url[len("file://") :])
        if not path.is_file():
            raise ValueError(f"file not found: {path}")
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise ValueError(
                f"file is {len(data):,} bytes; the bot's upload limit is "
                f"{max_bytes:,} bytes (~{max_bytes / 1024 / 1024:.0f}MB). "
                "For larger datasets, paste a SharePoint link instead."
            )
        return data

    if url.startswith(("http://", "https://")):
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
            if len(data) > max_bytes:
                raise ValueError(f"file is {len(data):,} bytes; limit is {max_bytes:,}.")
            return data

    raise ValueError(
        f"unsupported URL scheme: {url!r}. Expected file:// (Emulator) "
        "or http(s):// (Teams native)."
    )


def classify(name: str) -> UploadKind:
    """Classify an upload by filename suffix.

    Suffix-first because Teams + the Emulator default to
    ``application/octet-stream`` for arbitrary uploads — the MIME type
    is unreliable. Names are case-insensitive (operators paste from
    all sorts of OSes).
    """
    suffix = Path(name).suffix.lower()
    if suffix in _AGENT_SUFFIXES:
        return UploadKind.AGENT
    if suffix in _DATASET_SUFFIXES:
        return UploadKind.DATASET
    return UploadKind.UNKNOWN


async def ingest_attachment(
    attachment: Attachment,
    *,
    workspace: Path,
) -> UploadResult:
    """Download + classify + validate one attachment.

    Steps (each returns an :class:`UploadResult` on failure rather
    than raising):

    1. **Classify** by filename suffix. Empty name or unknown suffix
       → ``UploadResult(kind=UNKNOWN, error=...)``.
    2. **Fetch** bytes from the URL. Network / size failures →
       ``UploadResult(error=...)``.
    3. **Materialise** the bytes on disk under ``workspace``. For
       ``.zip`` files, extract first. For .jsonl, drop as-is.
    4. **Validate** by loading. Agent → :func:`movate.core.loader.load_agent`;
       dataset → :func:`movate.core.eval.load_dataset`. Both can raise
       — translate into ``error`` on the result.

    The caller (the handler) renders the result via the upload card
    builders.
    """
    name = attachment.name or "<unnamed>"
    kind = classify(name)
    if kind == UploadKind.UNKNOWN:
        return UploadResult(
            kind=kind,
            filename=name,
            error=(
                f"`{name}` — I don't recognise this file type. "
                "Upload an `agent.yaml`, a zipped agent directory "
                "(`.zip`), or a dataset (`.jsonl`)."
            ),
        )

    try:
        data = await fetch_bytes(attachment.content_url)
    except Exception as exc:
        return UploadResult(
            kind=kind,
            filename=name,
            error=f"couldn't fetch `{name}`: {exc}",
        )

    if kind == UploadKind.AGENT:
        return _ingest_agent(name, data, workspace)
    return _ingest_dataset(name, data, workspace)


def _ingest_agent(name: str, data: bytes, workspace: Path) -> UploadResult:
    """Materialise + validate an agent upload.

    Three input shapes converge on a single ``agent_dir`` that
    :func:`load_agent` consumes:

    * Bare ``agent.yaml`` — write to ``workspace/agent.yaml``, then
      load. The agent must declare its prompt + schema as inline
      shorthand because there are no sibling files.
    * Bare ``.yml`` — same as above.
    * Zip — unpack everything under ``workspace/agent/``. The zip's
      top-level entries become the agent's files (``agent.yaml``,
      ``prompt.md``, ``schema/*.json``, etc.).
    """
    from movate.core.loader import AgentLoadError, load_agent  # noqa: PLC0415

    agent_dir = workspace / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(name).suffix.lower()
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(_bytes_to_tempfile(data, workspace, name)) as zf:
                # Defensive: reject zips with absolute paths or `..`
                # — classic zip-slip protection. The bot is single-
                # process so a sneaky zip can't escalate, but operators
                # would still notice files appearing outside the
                # workspace.
                for member in zf.namelist():
                    p = Path(member)
                    if p.is_absolute() or ".." in p.parts:
                        return UploadResult(
                            kind=UploadKind.AGENT,
                            filename=name,
                            error=(
                                f"zip contains unsafe path `{member}`. "
                                "Re-zip without absolute paths or `..` entries."
                            ),
                        )
                zf.extractall(agent_dir)
        except zipfile.BadZipFile as exc:
            return UploadResult(
                kind=UploadKind.AGENT,
                filename=name,
                error=f"`{name}` isn't a valid zip file: {exc}",
            )
    else:
        # Bare yaml — write as agent.yaml inside agent_dir so load_agent
        # finds it under the conventional name regardless of what the
        # user actually called the file.
        (agent_dir / "agent.yaml").write_bytes(data)

    try:
        bundle = load_agent(agent_dir)
    except AgentLoadError as exc:
        return UploadResult(
            kind=UploadKind.AGENT,
            filename=name,
            path=agent_dir,
            error=f"`{name}` didn't validate as an agent: {exc}",
        )
    return UploadResult(
        kind=UploadKind.AGENT,
        filename=name,
        path=agent_dir,
        bundle=bundle,
    )


def _ingest_dataset(name: str, data: bytes, workspace: Path) -> UploadResult:
    """Materialise + validate a dataset upload.

    Datasets are .jsonl — one JSON object per line. We DON'T use
    :func:`movate.core.eval.load_dataset` directly because that wants
    an :class:`AgentBundle` to resolve paths against; instead we do
    the line-by-line JSON parse here so an upload-only validate (no
    accompanying agent) still works.

    Returns ``error`` populated when any line fails to parse — the
    handler card surfaces the line number so the user can fix.
    """
    import json  # noqa: PLC0415

    path = workspace / "dataset.jsonl"
    path.write_bytes(data)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return UploadResult(
            kind=UploadKind.DATASET,
            filename=name,
            path=path,
            error=f"`{name}` isn't valid UTF-8: {exc}",
        )

    row_count = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
        except json.JSONDecodeError as exc:
            return UploadResult(
                kind=UploadKind.DATASET,
                filename=name,
                path=path,
                error=(f"`{name}` line {line_no}: invalid JSON ({exc.msg} at col {exc.colno})"),
            )
        if not isinstance(row, dict):
            return UploadResult(
                kind=UploadKind.DATASET,
                filename=name,
                path=path,
                error=(
                    f"`{name}` line {line_no}: each row must be a JSON object, "
                    f"got {type(row).__name__}"
                ),
            )
        row_count += 1

    if row_count == 0:
        return UploadResult(
            kind=UploadKind.DATASET,
            filename=name,
            path=path,
            error=f"`{name}` is empty — no dataset rows found.",
        )

    return UploadResult(
        kind=UploadKind.DATASET,
        filename=name,
        path=path,
    )


def _bytes_to_tempfile(data: bytes, workspace: Path, suggested_name: str) -> Path:
    """Write bytes to a temp file inside the workspace.

    Used by the zip path before extraction — :class:`zipfile.ZipFile`
    accepts a path or a file-like, but a path keeps the call simple
    and lets us reference the file in error messages.
    """
    safe_name = Path(suggested_name).name or "upload.bin"
    out = workspace / safe_name
    out.write_bytes(data)
    return out


__all__ = [
    "UploadKind",
    "UploadResult",
    "classify",
    "fetch_bytes",
    "ingest_attachment",
    "temp_workspace",
]
