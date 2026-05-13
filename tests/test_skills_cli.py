"""Tests for ``mdk skills`` — scaffold, list, run.

Three subcommands; each gets coverage on the happy path + the operator-
facing failure modes:

* ``list`` — empty registry hint, populated table with name + backend +
  cost columns, surfaces a SkillLoadError if a project skill.yaml is
  malformed.
* ``scaffold`` — produces the expected file tree with name substitution,
  refuses to clobber an existing dir without --force.
* ``run`` — happy path (Python skill returns dict), invalid JSON input
  rejected before any skill load, unknown skill name surfaces with a
  ``hint:`` suggesting scaffold, SkillError exits non-zero with the
  type tag visible.

Tests run the skill registry against tmp_path; the scaffold tests
construct a real Python skill on disk + add the parent to sys.path so
``mdk skills run`` can import the impl module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_python_skill(
    parent: Path,
    name: str,
    *,
    impl_body: str = "    return {'result': 'echo:' + str(input)}",
) -> Path:
    """Drop a python-backed skill at <parent>/skills/<name>/.

    The impl is wired so ``<name>.impl:run`` resolves via importlib —
    the test harness adds the parent to sys.path before invoking.
    """
    skill_dir = parent / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "__init__.py").write_text("")
    (skill_dir / "skill.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Skill\n"
        f"name: {name}\n"
        "version: 0.1.0\n"
        "schema:\n"
        "  input: {query: string}\n"
        "  output: {result: string}\n"
        "implementation:\n"
        "  kind: python\n"
        f"  entry: {name}.impl:run\n"
    )
    (skill_dir / "impl.py").write_text("def run(input, ctx):\n" + impl_body + "\n")
    return skill_dir


# ---------------------------------------------------------------------------
# `mdk skills list`
# ---------------------------------------------------------------------------


def test_list_empty_registry_shows_hint(tmp_path: Path) -> None:
    """No ``skills/`` folder → friendly message with a `scaffold` hint
    instead of a blank table or a hard error."""
    result = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "no skills registered" in result.stdout
    assert "scaffold" in result.stdout


def test_list_populated_registry_renders_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two python skills, both should appear in the table with name +
    backend + entry columns visible."""
    _write_python_skill(tmp_path, "alpha")
    _write_python_skill(tmp_path, "beta")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    # Strip ANSI for tolerant matching across terminal widths.
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.stdout)
    assert "alpha" in cleaned
    assert "beta" in cleaned
    assert "python" in cleaned


def test_list_malformed_skill_yaml_errors(tmp_path: Path) -> None:
    """One broken skill.yaml in the registry → clean error, not crash."""
    skill_dir = tmp_path / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text("not: valid: yaml: at all:")
    result = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "registry load failed" in combined or "skill" in combined.lower()


# ---------------------------------------------------------------------------
# `mdk skills scaffold`
# ---------------------------------------------------------------------------


def test_scaffold_creates_expected_file_tree(tmp_path: Path) -> None:
    """The scaffold should produce a working skill folder — yaml + impl
    + README — with the name substituted into each file."""
    result = runner.invoke(app, ["skills", "scaffold", "weather", "--project", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    skill_dir = tmp_path / "skills" / "weather"
    assert (skill_dir / "skill.yaml").exists()
    assert (skill_dir / "impl.py").exists()
    assert (skill_dir / "README.md").exists()
    # Name substitution worked in every file.
    yaml_body = (skill_dir / "skill.yaml").read_text()
    assert "name: weather" in yaml_body
    assert "weather.impl:run" in yaml_body
    readme = (skill_dir / "README.md").read_text()
    assert "weather" in readme


def test_scaffold_refuses_overwrite_without_force(tmp_path: Path) -> None:
    result = runner.invoke(app, ["skills", "scaffold", "weather", "--project", str(tmp_path)])
    assert result.exit_code == 0
    # Second invocation without --force must refuse.
    second = runner.invoke(app, ["skills", "scaffold", "weather", "--project", str(tmp_path)])
    assert second.exit_code == 2
    combined = second.stdout + (second.stderr or "")
    assert "already exists" in combined


def test_scaffold_force_overwrites(tmp_path: Path) -> None:
    """First scaffold then mutate the impl; second scaffold with --force
    restores the template body."""
    runner.invoke(app, ["skills", "scaffold", "weather", "--project", str(tmp_path)])
    impl_path = tmp_path / "skills" / "weather" / "impl.py"
    impl_path.write_text("# vandalized\n")
    result = runner.invoke(
        app, ["skills", "scaffold", "weather", "--project", str(tmp_path), "--force"]
    )
    assert result.exit_code == 0
    # Template body restored.
    assert "vandalized" not in impl_path.read_text()
    assert "echo:" in impl_path.read_text()


def test_scaffolded_skill_loads_in_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: scaffold a skill, then list it — proves the
    generated skill.yaml validates cleanly through ``mdk validate``-
    style parsing."""
    runner.invoke(app, ["skills", "scaffold", "demo", "--project", str(tmp_path)])
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    listed = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    assert listed.exit_code == 0
    assert "demo" in listed.stdout


# ---------------------------------------------------------------------------
# `mdk skills run`
# ---------------------------------------------------------------------------


def test_run_happy_path_emits_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a python skill end-to-end via the CLI; stdout has the
    pretty-printed JSON result, stderr has the ✓ banner so a pipe
    captures only the payload."""
    _write_python_skill(tmp_path, "echo", impl_body="    return {'result': input['query']}")
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "echo",
            '{"query": "hello"}',
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    # stdout = pretty JSON payload
    out = json.loads(result.stdout)
    assert out == {"result": "hello"}
    # stderr = success banner (so pipes get clean JSON on stdout)
    assert "echo" in (result.stderr or "")


def test_run_rejects_invalid_json_input(tmp_path: Path) -> None:
    """Bad JSON input is caught BEFORE any skill load — fast feedback."""
    _write_python_skill(tmp_path, "echo")
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "echo",
            "not json {",
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "JSON" in combined or "not valid" in combined.lower()


def test_run_rejects_non_object_input(tmp_path: Path) -> None:
    """JSON list / scalar at top-level is rejected — skill inputs must
    be objects."""
    _write_python_skill(tmp_path, "echo")
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "echo",
            '["not", "an", "object"]',
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2


def test_run_unknown_skill_hints_scaffold(tmp_path: Path) -> None:
    """Operator typo'd the skill name. We point them at `skills list`
    + `skills scaffold` rather than a bare "not found"."""
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "nonexistent",
            "{}",
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "scaffold" in combined or "list" in combined.lower()


def test_run_skill_error_surfaces_type_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill that raises → exit 1 (distinct from exit 2 for the
    CLI's own input errors) + the SkillErrorType visible in stderr."""
    _write_python_skill(
        tmp_path,
        "exploder",
        impl_body="    raise RuntimeError('boom')",
    )
    monkeypatch.syspath_prepend(str(tmp_path / "skills"))
    result = runner.invoke(
        app,
        [
            "skills",
            "run",
            "exploder",
            '{"query": "x"}',
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "backend_error" in combined
    assert "boom" in combined
