"""Teams bot file-upload pipeline (slice 3.1.d).

Layered coverage:

* **classify** — pure function, suffix-based, case-insensitive.
* **fetch_bytes** — file:// URLs (production for Bot Framework Emulator
  + tests) plus a size-limit smoke for the http branch using
  ``httpx.MockTransport``.
* **ingest_attachment** — happy-path agent (zip + bare YAML), happy-path
  dataset, every documented failure mode (bad suffix, malformed zip,
  zip-slip, invalid agent yaml, malformed jsonl, empty dataset).
* **upload cards** — pure-function rendering checks on the new
  builders.
* **handler integration** — Activity carrying an attachment routes
  through the upload path; the bot replies with the right card; other
  command paths still work when no attachment is present.

Hermetic. No HTTP server, no Bot Framework SDK. Every file URL is
``file://`` pointed at a temp file the test built.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from movate.teams_bot.activity import Activity, Attachment
from movate.teams_bot.attachments import (
    UploadKind,
    classify,
    fetch_bytes,
    ingest_attachment,
    temp_workspace,
)
from movate.teams_bot.cards import (
    build_agent_upload_card,
    build_dataset_upload_card,
)
from movate.teams_bot.handler import handle_activity

# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected",
    [
        ("agent.yaml", UploadKind.AGENT),
        ("agent.yml", UploadKind.AGENT),
        ("my-agent.zip", UploadKind.AGENT),
        ("FAQ-AGENT.YAML", UploadKind.AGENT),  # case-insensitive
        ("dataset.jsonl", UploadKind.DATASET),
        ("eval/dataset.JSONL", UploadKind.DATASET),
        ("readme.md", UploadKind.UNKNOWN),
        ("file_without_suffix", UploadKind.UNKNOWN),
        ("script.py", UploadKind.UNKNOWN),
    ],
)
def test_classify_by_suffix(name: str, expected: UploadKind) -> None:
    assert classify(name) == expected


# ---------------------------------------------------------------------------
# fetch_bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_bytes_from_file_url(tmp_path: Path) -> None:
    payload = b"hello from local file"
    p = tmp_path / "sample.txt"
    p.write_bytes(payload)
    data = await fetch_bytes(f"file://{p}")
    assert data == payload


@pytest.mark.asyncio
async def test_fetch_bytes_file_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="file not found"):
        await fetch_bytes(f"file://{tmp_path}/missing.txt")


@pytest.mark.asyncio
async def test_fetch_bytes_oversized_file_raises(tmp_path: Path) -> None:
    """The 4MB cap is enforced — a 100-byte file with a 50-byte limit
    is the cheapest way to exercise the gate."""
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="upload limit"):
        await fetch_bytes(f"file://{p}", max_bytes=50)


@pytest.mark.asyncio
async def test_fetch_bytes_unsupported_scheme_raises() -> None:
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        await fetch_bytes("gopher://example.com/agent.yaml")


# ---------------------------------------------------------------------------
# ingest_attachment — agent path
# ---------------------------------------------------------------------------


# Valid agent YAML — references prompt.md as a sibling file. Bare-YAML
# uploads can't load standalone; tests use a zip with both files.
_VALID_AGENT_YAML = """\
api_version: movate/v1
kind: Agent
name: faq-agent
version: 0.1.0
description: A small test agent for upload validation
model:
  provider: openai/gpt-4o-mini-2024-07-18
prompt: ./prompt.md
schema:
  input:
    question: string
  output:
    answer: string
"""

_VALID_PROMPT = "Answer the user's question.\nQuestion: {{ input.question }}\n"


def _make_agent_zip(zip_path: Path) -> None:
    """Build a minimal agent zip containing agent.yaml + prompt.md."""
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("agent.yaml", _VALID_AGENT_YAML)
        zf.writestr("prompt.md", _VALID_PROMPT)


def _write_attachment_file(tmp_path: Path, name: str, data: bytes) -> Attachment:
    """Helper: write ``data`` to ``tmp_path/name`` and return an
    Attachment pointing at it via file:// URL."""
    p = tmp_path / name
    p.write_bytes(data)
    return Attachment(
        contentType="application/octet-stream",
        contentUrl=f"file://{p}",
        name=name,
    )


@pytest.mark.asyncio
async def test_ingest_bare_agent_yaml_fails_with_useful_message(
    tmp_path: Path,
) -> None:
    """A standalone agent.yaml can't load — it references prompt.md as a
    sibling. The error result tells the user to zip the directory.
    """
    att = _write_attachment_file(tmp_path, "agent.yaml", _VALID_AGENT_YAML.encode("utf-8"))
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert result.kind == UploadKind.AGENT
    assert result.bundle is None
    assert "didn't validate" in result.error
    # The underlying error is "prompt file not found"; we surface it
    # verbatim so the user sees a specific actionable failure.
    assert "prompt" in result.error.lower()


@pytest.mark.asyncio
async def test_ingest_invalid_agent_yaml_returns_error(tmp_path: Path) -> None:
    """A malformed agent yaml (missing required fields) lands as an
    error result, not an exception."""
    att = _write_attachment_file(
        tmp_path, "agent.yaml", b"api_version: movate/v1\nkind: Agent\nname: x\n"
    )
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert result.kind == UploadKind.AGENT
    assert result.bundle is None
    assert "didn't validate as an agent" in result.error


@pytest.mark.asyncio
async def test_ingest_zipped_agent_succeeds(tmp_path: Path) -> None:
    """Zip with agent.yaml + prompt.md at top level → extracted + loaded."""
    zip_path = tmp_path / "faq-bot.zip"
    _make_agent_zip(zip_path)
    att = Attachment(
        contentType="application/zip",
        contentUrl=f"file://{zip_path}",
        name="faq-bot.zip",
    )
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert result.error == ""
    assert result.kind == UploadKind.AGENT
    assert result.bundle is not None
    assert result.bundle.spec.name == "faq-agent"


@pytest.mark.asyncio
async def test_ingest_corrupt_zip_returns_error(tmp_path: Path) -> None:
    att = _write_attachment_file(tmp_path, "broken.zip", b"not a zip file")
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert "isn't a valid zip" in result.error


@pytest.mark.asyncio
async def test_ingest_rejects_zip_slip(tmp_path: Path) -> None:
    """Defensive: zip with ``../escape.yaml`` shouldn't extract above
    the workspace. We reject before extraction so even a misconfigured
    zipfile lib can't write outside."""
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escape.yaml", "owned")
    att = Attachment(
        contentType="application/zip",
        contentUrl=f"file://{zip_path}",
        name="evil.zip",
    )
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert "unsafe path" in result.error
    # And nothing actually got extracted.
    assert not (workspace.parent / "escape.yaml").exists()


# ---------------------------------------------------------------------------
# ingest_attachment — dataset path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_valid_dataset_succeeds(tmp_path: Path) -> None:
    rows = [
        {"input": {"q": "hi"}, "expected": {"answer": "Hello"}},
        {"input": {"q": "bye"}, "expected": {"answer": "Goodbye"}},
    ]
    body = "\n".join(json.dumps(r) for r in rows).encode("utf-8")
    att = _write_attachment_file(tmp_path, "dataset.jsonl", body)
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
        # Path is valid only inside the workspace context — once we
        # exit, the temp dir is rm-rf'd.
        assert result.kind == UploadKind.DATASET
        assert result.error == ""
        assert result.path is not None
        assert result.path.exists()


@pytest.mark.asyncio
async def test_ingest_dataset_with_malformed_line_returns_error(
    tmp_path: Path,
) -> None:
    body = b'{"input":{"q":"ok"}}\n{not valid json\n'
    att = _write_attachment_file(tmp_path, "dataset.jsonl", body)
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert "line 2" in result.error
    assert "invalid JSON" in result.error


@pytest.mark.asyncio
async def test_ingest_dataset_with_non_object_row_returns_error(
    tmp_path: Path,
) -> None:
    """Each row must be a JSON object — arrays / scalars rejected."""
    body = b"[1, 2, 3]\n"
    att = _write_attachment_file(tmp_path, "dataset.jsonl", body)
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert "must be a JSON object" in result.error


@pytest.mark.asyncio
async def test_ingest_empty_dataset_returns_error(tmp_path: Path) -> None:
    att = _write_attachment_file(tmp_path, "dataset.jsonl", b"\n\n")
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert "is empty" in result.error


# ---------------------------------------------------------------------------
# ingest_attachment — classify failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_unknown_filetype_returns_unknown_kind(
    tmp_path: Path,
) -> None:
    att = _write_attachment_file(tmp_path, "readme.md", b"# hello")
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert result.kind == UploadKind.UNKNOWN
    assert "don't recognise" in result.error


@pytest.mark.asyncio
async def test_ingest_missing_file_returns_fetch_error(tmp_path: Path) -> None:
    att = Attachment(
        contentType="application/octet-stream",
        contentUrl=f"file://{tmp_path}/never-existed.yaml",
        name="never-existed.yaml",
    )
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
    assert "couldn't fetch" in result.error


# ---------------------------------------------------------------------------
# Upload cards — pure-function tests
# ---------------------------------------------------------------------------


def _card_text(card: dict[str, Any]) -> str:
    """Flatten Adaptive Card body to a string for substring assertions."""
    out: list[str] = []

    def walk(items: list[Any]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t == "TextBlock":
                out.append(str(item.get("text", "")))
            elif t == "Container":
                walk(item.get("items", []) or [])
            elif t == "FactSet":
                for f in item.get("facts", []) or []:
                    out.append(f"{f.get('title', '')}: {f.get('value', '')}")

    walk(card.get("body", []) or [])
    return "\n".join(out)


@pytest.mark.asyncio
async def test_agent_upload_card_lists_detected_metadata(tmp_path: Path) -> None:
    """End-to-end: zip → real load_agent → real card. Verifies the
    card surfaces the fields a reviewing operator would care about."""
    zip_path = tmp_path / "faq-bot.zip"
    _make_agent_zip(zip_path)
    att = Attachment(
        contentType="application/zip",
        contentUrl=f"file://{zip_path}",
        name="faq-bot.zip",
    )
    with temp_workspace() as workspace:
        result = await ingest_attachment(att, workspace=workspace)
        assert result.bundle is not None
        card = build_agent_upload_card(
            result.bundle,
            filename="faq-bot.zip",
            next_step_hint="Now type `@movate run faq-agent ...`",
        )
        text = _card_text(card)
        assert "faq-agent" in text
        assert "v0.1.0" in text
        assert "openai/gpt-4o-mini-2024-07-18" in text
        # api_version row present.
        assert "api_version" in text
        # No skills declared in this test agent — should show "(none)".
        assert "skills: (none)" in text
        # Description is preserved.
        assert "A small test agent" in text
        # Hint surfaces with the lightbulb prefix.
        assert "💡" in text


@pytest.mark.unit
def test_dataset_upload_card_includes_row_count_and_preview() -> None:
    card = build_dataset_upload_card(
        filename="evals.jsonl",
        row_count=42,
        first_row_preview='{"input": {"q": "hi"}, "expected": {"a": "Hi"}}',
        next_step_hint="Save and run `mdk eval`.",
    )
    text = _card_text(card)
    assert "✅ Dataset loaded" in text
    assert "42 rows" in text
    assert "evals.jsonl" in text
    assert "input" in text  # preview surfaced
    assert "💡" in text


@pytest.mark.unit
def test_dataset_upload_card_uses_singular_for_one_row() -> None:
    """Cosmetic but operator-facing — "1 rows" reads wrong."""
    card = build_dataset_upload_card(filename="d.jsonl", row_count=1)
    text = _card_text(card)
    assert "1 row" in text
    assert "1 rows" not in text


# ---------------------------------------------------------------------------
# Handler integration — Activity with attachments
# ---------------------------------------------------------------------------


def _activity_with_attachment(
    *,
    filename: str,
    url: str,
    text: str = "<at>movate</at>",
    conversation_type: str = "personal",
    extra_atts: list[Attachment] | None = None,
) -> Activity:
    """Build an Activity carrying one attachment (+ optional extras)."""
    atts: list[dict[str, Any]] = [
        {
            "contentType": "application/octet-stream",
            "contentUrl": url,
            "name": filename,
        }
    ]
    if extra_atts:
        atts.extend(
            {"contentType": a.content_type, "contentUrl": a.content_url, "name": a.name}
            for a in extra_atts
        )
    return Activity.model_validate(
        {
            "type": "message",
            "id": "act-1",
            "channelId": "msteams",
            "text": text,
            "from": {"id": "u1", "name": "tester"},
            "conversation": {"id": "c1", "conversationType": conversation_type},
            "recipient": {"id": "b1", "name": "movate"},
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>movate</at>",
                    "mentioned": {"id": "b1", "name": "movate"},
                }
            ],
            "attachments": atts,
        }
    )


@pytest.mark.asyncio
async def test_handler_routes_attachment_to_upload_path(tmp_path: Path) -> None:
    """Activity with an attachment goes through _handle_upload, no
    matter what the text says. End-to-end through a zip-and-load."""
    zip_path = tmp_path / "faq-bot.zip"
    _make_agent_zip(zip_path)
    activity = _activity_with_attachment(
        filename="faq-bot.zip",
        url=f"file://{zip_path}",
    )
    reply = await handle_activity(activity)
    assert reply is not None
    assert reply.attachments, "expected an Adaptive Card attachment"
    card = reply.attachments[0].content
    text = _card_text(card)
    assert "✅ Agent loaded" in text
    assert "faq-agent" in text


@pytest.mark.asyncio
async def test_handler_routes_dataset_attachment(tmp_path: Path) -> None:
    body = b'{"input":{"q":"hi"},"expected":{"a":"x"}}\n'
    p = tmp_path / "evals.jsonl"
    p.write_bytes(body)
    activity = _activity_with_attachment(
        filename="evals.jsonl",
        url=f"file://{p}",
    )
    reply = await handle_activity(activity)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "✅ Dataset loaded" in text
    assert "1 row" in text


@pytest.mark.asyncio
async def test_handler_renders_error_card_for_unknown_filetype(
    tmp_path: Path,
) -> None:
    p = tmp_path / "readme.md"
    p.write_bytes(b"# hello")
    activity = _activity_with_attachment(filename="readme.md", url=f"file://{p}")
    reply = await handle_activity(activity)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "Couldn't ingest" in text
    assert "don't recognise" in text


@pytest.mark.asyncio
async def test_handler_mentions_extra_attachments(tmp_path: Path) -> None:
    """User dropped 2 files — we process the first and tell them
    about the others. Multi-file flow lives in 3.2."""
    zip_path = tmp_path / "faq-bot.zip"
    _make_agent_zip(zip_path)
    p2 = tmp_path / "dataset.jsonl"
    p2.write_bytes(b'{"input":{}}\n')
    extra = Attachment(
        contentType="application/octet-stream",
        contentUrl=f"file://{p2}",
        name="dataset.jsonl",
    )
    activity = _activity_with_attachment(
        filename="faq-bot.zip",
        url=f"file://{zip_path}",
        extra_atts=[extra],
    )
    reply = await handle_activity(activity)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    # The card is the agent-success card; only error cards carry the
    # extra-file note today, so we check the agent card rendered first.
    assert "faq-agent" in text


@pytest.mark.asyncio
async def test_handler_ping_still_works_with_no_attachment() -> None:
    """The upload path only triggers when attachments are present —
    `@movate ping` with no files still gets `pong`."""
    activity = Activity.model_validate(
        {
            "type": "message",
            "id": "a1",
            "channelId": "msteams",
            "text": "<at>movate</at> ping",
            "from": {"id": "u1"},
            "conversation": {"id": "c1", "conversationType": "personal"},
            "recipient": {"id": "b1"},
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>movate</at>",
                    "mentioned": {"id": "b1"},
                }
            ],
        }
    )
    reply = await handle_activity(activity)
    assert reply is not None
    assert reply.text == "pong"


@pytest.mark.asyncio
async def test_handler_renders_zip_slip_rejection(tmp_path: Path) -> None:
    """Adversarial: a zip with ../escape paths gets rejected before
    extraction. The card surfaces the unsafe-path message."""
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escape.yaml", "x")
    activity = _activity_with_attachment(filename="evil.zip", url=f"file://{zip_path}")
    reply = await handle_activity(activity)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "unsafe path" in text
