"""AgentLifecycle — schema + loader + validate/show CLI integration.

Contracts:

* Default lifecycle is ``draft`` (so legacy agents without the field load
  but get gated out of strict CI).
* ``archived`` is rejected by the loader — running an archived agent is a
  programming error, not a soft warning.
* ``movate validate`` surfaces the lifecycle row in its output table.
* ``movate validate --strict`` exits 2 on ``draft`` AND on ``deprecated``;
  bare ``movate validate`` exits 0 on the same input (only deprecated still
  warns visibly).
* ``movate show`` surfaces the lifecycle row.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.loader import AgentLoadError, load_agent
from movate.core.models import AgentLifecycle
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _set_lifecycle(agent_dir: Path, value: str | None) -> None:
    """Inject or remove the ``lifecycle:`` line on an existing scaffold."""
    yaml = agent_dir / "agent.yaml"
    text = yaml.read_text()
    text = re.sub(r"^lifecycle:.*\n", "", text, flags=re.MULTILINE)
    if value is not None:
        text = text.replace(
            'owner: ""\n',
            f'owner: ""\nlifecycle: {value}\n',
            1,
        )
    yaml.write_text(text)


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lifecycle_enum_exposes_six_values() -> None:
    """Adding a value is a breaking-ish change (CLI rendering needs an
    update); pin the membership so the test surfaces the moment someone
    adds one without thinking about the rendering side."""
    assert {x.value for x in AgentLifecycle} == {
        "draft",
        "experimental",
        "validated",
        "certified",
        "deprecated",
        "archived",
    }


@pytest.mark.unit
def test_agent_spec_defaults_lifecycle_to_draft(tmp_path: Path) -> None:
    """Agents that omit the field load — they get the safest default
    (draft → fails strict CI, requires explicit promotion)."""
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, None)  # remove the scaffold's explicit `lifecycle: draft`
    bundle = load_agent(a)
    assert bundle.spec.lifecycle is AgentLifecycle.DRAFT


@pytest.mark.unit
def test_agent_spec_accepts_each_lifecycle_value(tmp_path: Path) -> None:
    for value in [
        "draft",
        "experimental",
        "validated",
        "certified",
        "deprecated",
    ]:
        a = scaffold_agent(tmp_path / value, name=f"agent-{value}")
        _set_lifecycle(a, value)
        bundle = load_agent(a)
        assert bundle.spec.lifecycle.value == value


@pytest.mark.unit
def test_agent_spec_rejects_unknown_lifecycle(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "preview")  # not a real state
    with pytest.raises(AgentLoadError, match="validation failed"):
        load_agent(a)


# ---------------------------------------------------------------------------
# Loader: archived is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_loader_refuses_archived(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "archived")
    with pytest.raises(AgentLoadError, match="archived"):
        load_agent(a)


# ---------------------------------------------------------------------------
# movate validate — lifecycle surfacing + strict gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_renders_lifecycle_row(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "validated")
    r = runner.invoke(cli_app, ["validate", str(a)])
    assert r.exit_code == 0
    plain = _strip_ansi(r.stdout)
    assert "lifecycle:" in plain
    assert "validated" in plain


@pytest.mark.unit
def test_validate_strict_fails_on_draft(tmp_path: Path) -> None:
    """A scaffold straight out of `movate init` is `draft` — non-strict
    validate passes (you're allowed to iterate locally); strict fails so
    a draft agent can't sneak into a CI merge."""
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "draft")

    r_loose = runner.invoke(cli_app, ["validate", str(a)])
    assert r_loose.exit_code == 0  # bare validate passes draft

    r_strict = runner.invoke(cli_app, ["validate", str(a), "--strict"])
    assert r_strict.exit_code == 2
    plain = _strip_ansi(r_strict.stdout)
    assert "draft" in plain
    assert "promote" in plain.lower()


@pytest.mark.unit
def test_validate_warns_on_deprecated_even_without_strict(tmp_path: Path) -> None:
    """Deprecated is a downstream-visible signal: anyone using this agent
    needs to know to migrate. Surface the warning at every validate, not
    just under --strict."""
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "deprecated")

    r = runner.invoke(cli_app, ["validate", str(a)])
    plain = _strip_ansi(r.stdout)
    assert "deprecated" in plain
    assert "discouraged" in plain.lower()
    # Bare validate still exits 0 on deprecated — the agent works; the
    # warning is informational. Strict promotes it to exit 2.
    assert r.exit_code == 0

    r_strict = runner.invoke(cli_app, ["validate", str(a), "--strict"])
    assert r_strict.exit_code == 2


@pytest.mark.unit
def test_validate_passes_strict_on_validated(tmp_path: Path) -> None:
    """validated + certified are the clean states — pass even under strict."""
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "validated")
    r = runner.invoke(cli_app, ["validate", str(a), "--strict"])
    assert r.exit_code == 0


@pytest.mark.unit
def test_validate_passes_strict_on_certified(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "certified")
    r = runner.invoke(cli_app, ["validate", str(a), "--strict"])
    assert r.exit_code == 0


@pytest.mark.unit
def test_validate_rejects_archived_with_load_error(tmp_path: Path) -> None:
    """Archived agents are caught by the loader, so validate surfaces
    them as exit-2 load failures rather than reaching the lifecycle gate."""
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "archived")
    r = runner.invoke(cli_app, ["validate", str(a)])
    assert r.exit_code == 2
    plain = _strip_ansi(r.stdout)
    assert "archived" in plain


# ---------------------------------------------------------------------------
# movate show — lifecycle row in table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_renders_lifecycle_row(tmp_path: Path) -> None:
    a = scaffold_agent(tmp_path / "a", name="demo")
    _set_lifecycle(a, "experimental")
    r = runner.invoke(cli_app, ["show", str(a)])
    assert r.exit_code == 0
    plain = _strip_ansi(r.stdout)
    assert "lifecycle" in plain
    assert "experimental" in plain


# ---------------------------------------------------------------------------
# Scaffold template — `movate init` lands in `draft`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scaffold_template_defaults_to_draft(tmp_path: Path) -> None:
    """The packaged template should bake in `lifecycle: draft` so newly-
    scaffolded agents are explicit about their state. This catches
    accidental removal of the field from the template."""
    a = scaffold_agent(tmp_path / "a", name="demo")
    text = (a / "agent.yaml").read_text()
    assert "lifecycle: draft" in text

    bundle = load_agent(a)
    assert bundle.spec.lifecycle is AgentLifecycle.DRAFT


# ---------------------------------------------------------------------------
# In-repo example agents — sanity check that the bundled exemplars all
# load under --strict so the agents.yml gate stays green.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("agent", ["faq-agent", "case-reasoner"])
def test_bundled_agents_pass_strict(agent: str) -> None:
    """Both committed example agents should hold a lifecycle that
    survives `movate validate --strict` — otherwise the gate workflow
    (agents.yml) immediately fails on the upgrade."""
    repo_root = Path(__file__).resolve().parent.parent
    agent_dir = repo_root / "agents" / agent
    r = runner.invoke(cli_app, ["validate", str(agent_dir), "--strict"])
    assert r.exit_code == 0, r.stdout
    bundle = load_agent(agent_dir)
    assert bundle.spec.lifecycle in (
        AgentLifecycle.VALIDATED,
        AgentLifecycle.CERTIFIED,
    ), f"{agent} should be at least validated to pass agents.yml"
