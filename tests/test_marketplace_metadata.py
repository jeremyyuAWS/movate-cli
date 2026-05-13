"""Tests for the Mova iO marketplace metadata on ``AgentSpec``.

Group F / item 29 from BACKLOG.md. The Agent Marketplace UI (separate
product) reads ``persona`` / ``role`` / ``capabilities`` as the source
of truth for catalog, profiles, and search facets. All three fields
are optional and backward-compatible — pre-v0.8 ``agent.yaml`` files
load unchanged and the new ``mdk show`` rows only render when populated.

Two coverage layers:

* **Schema layer** — ``AgentSpec`` parses, validates capabilities as
  URL-safe slugs, caps persona + role lengths.
  (test_models.py already owns the unit-level tests for this; this file
  focuses on the CLI rendering and the loader → show end-to-end path.)
* **CLI layer** — ``mdk show <agent>`` renders the new rows when
  populated, omits them otherwise. Keeps the table compact for the
  pre-v0.8 common case.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


def _write_agent(
    parent: Path,
    *,
    name: str = "demo",
    role: str = "",
    persona: str = "",
    capabilities: list[str] | None = None,
) -> Path:
    """Build a minimal agent dir with optional marketplace metadata.

    Inline-shorthand schema fields (`schema: { ... }`) keep this test
    independent of any actual schema files on disk.
    """
    agent_dir = parent / f"{name}-agent"
    agent_dir.mkdir(parents=True)
    extras = ""
    if role:
        extras += f"role: {role}\n"
    if persona:
        # YAML-quote in case persona contains punctuation.
        extras += f"persona: {persona!r}\n"
    if capabilities:
        extras += "capabilities:\n" + "".join(f"  - {c}\n" for c in capabilities)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: { who: string }\n"
        "  output: { greeting: string }\n"
        f"{extras}"
    )
    (agent_dir / "prompt.md").write_text("Hello, {{ input.who }}!")
    return agent_dir


def _strip_ansi(text: str) -> str:
    """Drop terminal escape codes so substring assertions don't depend
    on Rich's colorization choices (which differ between local + CI)."""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


# ---------------------------------------------------------------------------
# `mdk show` rendering — opt-in rows
# ---------------------------------------------------------------------------


def test_show_omits_marketplace_rows_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare agent.yaml — no marketplace fields — must NOT clutter
    the show table with empty marketplace rows. The pre-v0.8 common
    case is "I don't know what those fields are"; the table should
    look exactly like it did before."""
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent(tmp_path)
    result = runner.invoke(app, ["show", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cleaned = _strip_ansi(result.stdout)
    # No marketplace metadata rows when nothing's populated.
    assert "role" not in cleaned.split("model.provider")[0] or "role" not in cleaned
    assert "persona" not in cleaned
    assert "capabilities" not in cleaned


def test_show_renders_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent(tmp_path, role="support-triage")
    result = runner.invoke(app, ["show", str(agent_dir)])
    assert result.exit_code == 0
    cleaned = _strip_ansi(result.stdout)
    assert "role" in cleaned
    assert "support-triage" in cleaned


def test_show_renders_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent(
        tmp_path,
        persona="Concise and technical; 1-2 line answers.",
    )
    result = runner.invoke(app, ["show", str(agent_dir)])
    assert result.exit_code == 0
    cleaned = _strip_ansi(result.stdout)
    assert "persona" in cleaned
    assert "Concise" in cleaned


def test_show_renders_capabilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent(
        tmp_path,
        capabilities=["faq-lookup", "ticket-routing"],
    )
    result = runner.invoke(app, ["show", str(agent_dir)])
    assert result.exit_code == 0
    cleaned = _strip_ansi(result.stdout)
    assert "capabilities" in cleaned
    assert "faq-lookup" in cleaned
    assert "ticket-routing" in cleaned


def test_show_renders_all_three_when_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Demo path — every field populated; all three rows render."""
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent(
        tmp_path,
        role="data-analyst",
        persona="Numeric, citation-heavy.",
        capabilities=["sql-gen", "chart-pick"],
    )
    result = runner.invoke(app, ["show", str(agent_dir)])
    assert result.exit_code == 0
    cleaned = _strip_ansi(result.stdout)
    assert "data-analyst" in cleaned
    assert "Numeric" in cleaned
    assert "sql-gen" in cleaned
    assert "chart-pick" in cleaned


# ---------------------------------------------------------------------------
# Validation — `mdk validate` rejects malformed capability slugs
# ---------------------------------------------------------------------------


def test_validate_rejects_capability_with_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capability with a space is not URL-safe — marketplace search
    would break. ``mdk validate`` must catch it before the agent ever
    lands in the catalog."""
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent(tmp_path, capabilities=["faq lookup"])
    result = runner.invoke(app, ["validate", str(agent_dir)])
    # Non-zero exit, and the failure message points at the slug rule.
    assert result.exit_code != 0
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "lowercase alphanumeric" in combined or "faq lookup" in combined


def test_validate_accepts_well_formed_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slug-style capabilities pass validate cleanly."""
    monkeypatch.chdir(tmp_path)
    agent_dir = _write_agent(
        tmp_path,
        capabilities=["faq-lookup", "v2-routing", "summarize"],
    )
    result = runner.invoke(app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
