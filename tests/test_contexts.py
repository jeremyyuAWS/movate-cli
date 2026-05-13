"""Tests for shared contexts — the second half of ADR 002 / PR 4.

Three layers of coverage:

* **context_loader** pure functions — registry discovery, name
  resolution, prefix construction. Hermetic, no Jinja, no I/O beyond
  the tmp_path fixture.
* **AgentBundle.render_prompt** integration — proves a context is
  prepended in declaration order with the standard separator, and
  that an agent without contexts renders bit-for-bit identical to
  the v0.5 path.
* **CLI** integration via `mdk show <agent>` — context list + per-
  context byte size appear in the rendered table.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.context_loader import (
    ContextLoadError,
    build_context_prefix,
    load_context_registry,
    resolve_agent_contexts,
)
from movate.core.loader import AgentLoadError, load_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_context(parent: Path, name: str, body: str) -> Path:
    """Drop a markdown context file. ``name`` is the base name (no
    extension); the file lands at ``parent/contexts/<name>.md``."""
    contexts_dir = parent / "contexts"
    contexts_dir.mkdir(parents=True, exist_ok=True)
    path = contexts_dir / f"{name}.md"
    path.write_text(body)
    return path


def _write_agent(
    parent: Path,
    *,
    name: str = "demo",
    contexts: list[str] | None = None,
    skills: list[str] | None = None,
    prompt_body: str = "Hello, {{ input.who }}!",
) -> Path:
    """Build a minimal agent dir with optional contexts/skills lists."""
    agent_dir = parent / f"{name}-agent"
    agent_dir.mkdir(parents=True)
    extras = ""
    if contexts:
        extras += "contexts:\n" + "".join(f"  - {c}\n" for c in contexts)
    if skills:
        extras += "skills:\n" + "".join(f"  - {s}\n" for s in skills)
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
    (agent_dir / "prompt.md").write_text(prompt_body)
    return agent_dir


# ---------------------------------------------------------------------------
# context_loader pure functions
# ---------------------------------------------------------------------------


def test_registry_discovers_markdown_files(tmp_path: Path) -> None:
    _write_context(tmp_path, "style-guide", "Be concise.")
    _write_context(tmp_path, "glossary", "FAQ = frequently asked.")
    reg = load_context_registry(tmp_path)
    assert set(reg.keys()) == {"style-guide", "glossary"}
    assert reg["style-guide"] == "Be concise."


def test_registry_skips_non_markdown_files(tmp_path: Path) -> None:
    """A stray ``.DS_Store`` or ``README.txt`` under contexts/ must
    NOT register as a context — the operator's mental model is
    "drop a markdown file in." Anything else is noise."""
    (tmp_path / "contexts").mkdir()
    (tmp_path / "contexts" / "README.txt").write_text("not a context")
    (tmp_path / "contexts" / ".DS_Store").write_text("metadata")
    _write_context(tmp_path, "style-guide", "Be concise.")
    reg = load_context_registry(tmp_path)
    assert set(reg.keys()) == {"style-guide"}


def test_registry_skips_subdirectories(tmp_path: Path) -> None:
    """``contexts/<name>.md`` only — no nested folders. Keeps the
    layout flat so operators don't have to learn a convention."""
    (tmp_path / "contexts").mkdir()
    (tmp_path / "contexts" / "nested").mkdir()
    (tmp_path / "contexts" / "nested" / "deep.md").write_text("ignored")
    _write_context(tmp_path, "top", "kept")
    reg = load_context_registry(tmp_path)
    assert set(reg.keys()) == {"top"}


def test_registry_empty_when_no_contexts_folder(tmp_path: Path) -> None:
    """Permissive default — projects without a ``contexts/`` folder
    get an empty registry. Agents with ``contexts: []`` work fine;
    agents that reference a missing name fail at resolution."""
    assert load_context_registry(tmp_path) == {}


def test_resolve_returns_pairs_in_declaration_order(tmp_path: Path) -> None:
    """Order matters — operators reason about which context "wins" by
    reading the list top to bottom. The resolver must preserve that."""
    _write_context(tmp_path, "alpha", "A")
    _write_context(tmp_path, "beta", "B")
    reg = load_context_registry(tmp_path)
    pairs = resolve_agent_contexts(["beta", "alpha"], reg)
    assert [name for name, _ in pairs] == ["beta", "alpha"]
    assert [body for _, body in pairs] == ["B", "A"]


def test_resolve_unknown_name_errors_with_available_list(tmp_path: Path) -> None:
    _write_context(tmp_path, "style-guide", "x")
    reg = load_context_registry(tmp_path)
    with pytest.raises(ContextLoadError, match="no such context is registered"):
        resolve_agent_contexts(["style-guide", "missing"], reg)


def test_resolve_against_empty_registry_lists_helpful_hint(tmp_path: Path) -> None:
    """The error message points the operator at the action they need
    to take ("add contexts/<name>.md") rather than a bare "not found"."""
    with pytest.raises(ContextLoadError, match=r"contexts/<name>\.md"):
        resolve_agent_contexts(["anything"], {})


# ---------------------------------------------------------------------------
# build_context_prefix
# ---------------------------------------------------------------------------


def test_prefix_empty_returns_empty_string() -> None:
    """Single-shot agents get nothing prepended — bit-for-bit
    backward compatible with v0.5 prompt rendering."""
    assert build_context_prefix([]) == ""


def test_prefix_single_context_ends_with_separator() -> None:
    """One context: body + separator. Caller can ``prefix + rendered``
    and the result is well-formed markdown."""
    prefix = build_context_prefix([("style", "Be concise.")])
    assert prefix == "Be concise.\n\n---\n\n"


def test_prefix_multiple_contexts_joined_by_separator() -> None:
    prefix = build_context_prefix(
        [("style", "Be concise."), ("glossary", "FAQ = frequently asked.")]
    )
    assert prefix == "Be concise.\n\n---\n\nFAQ = frequently asked.\n\n---\n\n"


def test_prefix_strips_trailing_newlines_per_context() -> None:
    """A context body authored with a trailing blank line shouldn't
    introduce extra spacing in the concatenated prefix — we want
    the separator to be the only delimiter, not body-trailing-newlines
    + separator."""
    prefix = build_context_prefix([("a", "first\n\n\n"), ("b", "second")])
    # Trailing \n's stripped from 'first'; 'second' kept as-is.
    assert prefix == "first\n\n---\n\nsecond\n\n---\n\n"


# ---------------------------------------------------------------------------
# AgentBundle.render_prompt integration
# ---------------------------------------------------------------------------


def test_render_prompt_no_contexts_is_unchanged(tmp_path: Path) -> None:
    """The contexts feature is purely additive — an agent without
    ``contexts: [...]`` renders exactly its prompt template."""
    agent_dir = _write_agent(tmp_path, prompt_body="Hi {{ input.who }}!")
    bundle = load_agent(agent_dir)
    assert bundle.render_prompt({"who": "world"}) == "Hi world!"


def test_render_prompt_prepends_single_context(tmp_path: Path) -> None:
    _write_context(tmp_path, "style", "Be concise.")
    agent_dir = _write_agent(tmp_path, contexts=["style"], prompt_body="Hi {{ input.who }}!")
    bundle = load_agent(agent_dir)
    rendered = bundle.render_prompt({"who": "world"})
    assert rendered == "Be concise.\n\n---\n\nHi world!"


def test_render_prompt_prepends_multiple_contexts_in_order(tmp_path: Path) -> None:
    _write_context(tmp_path, "style", "Be concise.")
    _write_context(tmp_path, "glossary", "FAQ = frequently asked.")
    agent_dir = _write_agent(
        tmp_path,
        contexts=["style", "glossary"],
        prompt_body="Q: {{ input.who }}",
    )
    bundle = load_agent(agent_dir)
    rendered = bundle.render_prompt({"who": "x"})
    # Order matches the agent.yaml list, not registry order.
    assert rendered.startswith("Be concise.")
    assert "FAQ = frequently asked." in rendered
    # Style guide comes before glossary in the rendered output.
    assert rendered.index("Be concise.") < rendered.index("FAQ")


def test_loader_rejects_unknown_context_with_field_path(tmp_path: Path) -> None:
    """Agent references a context that doesn't exist on disk → load
    fails with a message naming the field and the available list."""
    _write_context(tmp_path, "style", "x")
    agent_dir = _write_agent(tmp_path, contexts=["style", "missing"])
    with pytest.raises(AgentLoadError, match=r"contexts resolution failed"):
        load_agent(agent_dir)


def test_contexts_compose_with_skills(tmp_path: Path) -> None:
    """The two features are independent and must coexist — an agent
    can declare both ``contexts:`` and ``skills:`` and load successfully.
    Contexts apply at render time, skills at execution time."""
    # Make a no-op skill so spec.skills resolves
    from tests.test_skills import _write_skill_dir  # noqa: PLC0415

    _write_context(tmp_path, "style", "Be concise.")
    _write_skill_dir(
        tmp_path / "skills",
        "noop",
        entry="tests.test_skills:_dummy_skill",
    )
    agent_dir = _write_agent(
        tmp_path,
        contexts=["style"],
        skills=["noop"],
        prompt_body="Hi {{ input.who }}",
    )
    bundle = load_agent(agent_dir)
    assert [name for name, _ in bundle.contexts] == ["style"]
    assert [s.spec.name for s in bundle.skills] == ["noop"]
    assert bundle.render_prompt({"who": "world"}).startswith("Be concise.")


# ---------------------------------------------------------------------------
# CLI surface — `mdk show <agent>`
# ---------------------------------------------------------------------------


def test_mdk_show_displays_contexts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdk show <agent>` renders the agent's contexts list with byte
    sizes so operators can spot a runaway file."""
    monkeypatch.chdir(tmp_path)
    _write_context(tmp_path, "style-guide", "Be concise.")
    agent_dir = _write_agent(tmp_path, contexts=["style-guide"])
    result = runner.invoke(app, ["show", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    # The contexts row + the per-context byte-size line both appear.
    # Strip ANSI escapes for tolerant substring matching across
    # different terminal widths.
    import re  # noqa: PLC0415

    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "contexts" in cleaned
    assert "style-guide" in cleaned
    assert "bytes" in cleaned
