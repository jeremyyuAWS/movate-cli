"""Tests for ``--quiet`` propagation to dim-style status hints.

Contract:

* By default, ``movate <cmd>`` emits dim FYI lines to stderr — "queued
  j-1 on dev. Poll with: ...", "no jobs found", etc.
* When ``--quiet`` / ``-q`` is passed, those lines are suppressed but
  error / warning prints still appear (operators must always see
  failure).
* Stdout is unaffected: ``--quiet`` only gates stderr hints, never
  the actual command output.

Implementation lives in :mod:`movate.cli._console`. The top-level
Typer callback in ``main.py`` flips a module-state flag when
``--quiet`` is set; :func:`hint` no-ops when that flag is on.
"""

from __future__ import annotations

import pytest

from movate.cli._console import (
    error,
    get_global_target,
    is_quiet,
    set_global_target,
    set_quiet,
    success,
    warn,
)


@pytest.fixture(autouse=True)
def _reset_cli_state() -> None:
    """Each test starts with quiet + global-target both cleared.
    Done as a fixture so a test that fails between set/clear can't
    poison the next test."""
    set_quiet(False)
    set_global_target(None)
    yield
    set_quiet(False)
    set_global_target(None)


@pytest.mark.unit
def test_is_quiet_reflects_set_quiet() -> None:
    """The flag is readable so commands can branch on it (e.g. to
    skip a progress spinner entirely under --quiet)."""
    assert is_quiet() is False
    set_quiet(True)
    assert is_quiet() is True
    set_quiet(False)
    assert is_quiet() is False


@pytest.mark.unit
def test_error_helper_prefix_with_context(capsys: pytest.CaptureFixture[str]) -> None:
    """``error(msg, context="submit")`` renders ``✗ submit failed: <msg>``."""
    # Use a fresh Rich Console wired at stderr capture so we can read it
    # back without fighting the module-global one.
    from rich.console import Console  # noqa: PLC0415

    from movate.cli import _console  # noqa: PLC0415

    buf_console = Console(file=__import__("io").StringIO(), force_terminal=False, no_color=True)
    original = _console.stderr
    _console.stderr = buf_console  # type: ignore[misc]
    try:
        error("connection refused", context="submit")
    finally:
        _console.stderr = original  # type: ignore[misc]

    out = buf_console.file.getvalue()  # type: ignore[union-attr]
    assert "submit failed" in out
    assert "connection refused" in out
    assert "✗" in out


@pytest.mark.unit
def test_error_helper_no_context_just_marker() -> None:
    """``error(msg)`` without context renders just ``✗ <msg>``."""
    from rich.console import Console  # noqa: PLC0415

    from movate.cli import _console  # noqa: PLC0415

    buf_console = Console(file=__import__("io").StringIO(), force_terminal=False, no_color=True)
    original = _console.stderr
    _console.stderr = buf_console  # type: ignore[misc]
    try:
        error("env must be 'live' or 'test'")
    finally:
        _console.stderr = original  # type: ignore[misc]

    out = buf_console.file.getvalue()  # type: ignore[union-attr]
    assert "env must be" in out
    assert "failed" not in out  # no "failed" verb when no context


@pytest.mark.unit
def test_warn_and_success_render_correct_marker() -> None:
    """``warn`` uses yellow + ⚠ by default (or custom icon like ⏱);
    ``success`` uses green + ✓."""
    from rich.console import Console  # noqa: PLC0415

    from movate.cli import _console  # noqa: PLC0415

    buf_console = Console(file=__import__("io").StringIO(), force_terminal=False, no_color=True)
    original = _console.stderr
    _console.stderr = buf_console  # type: ignore[misc]
    try:
        warn("disk almost full")
        warn("timed out after 30s", icon="⏱")
        success("revoked key abc")
    finally:
        _console.stderr = original  # type: ignore[misc]

    out = buf_console.file.getvalue()  # type: ignore[union-attr]
    assert "⚠" in out
    assert "disk almost full" in out
    assert "⏱" in out
    assert "timed out" in out
    assert "✓" in out
    assert "revoked key abc" in out


@pytest.mark.unit
def test_error_helper_not_suppressed_by_quiet() -> None:
    """``--quiet`` only gates ``hint()``. ``error`` / ``warn`` /
    ``success`` always render — operators must see failure."""
    from rich.console import Console  # noqa: PLC0415

    from movate.cli import _console  # noqa: PLC0415

    buf_console = Console(file=__import__("io").StringIO(), force_terminal=False, no_color=True)
    original = _console.stderr
    _console.stderr = buf_console  # type: ignore[misc]
    set_quiet(True)
    try:
        error("the world is on fire")
    finally:
        _console.stderr = original  # type: ignore[misc]

    out = buf_console.file.getvalue()  # type: ignore[union-attr]
    assert "world is on fire" in out


@pytest.mark.unit
def test_global_target_get_set_round_trip() -> None:
    """Direct setter/getter contract for the process-wide default
    deployment target."""
    assert get_global_target() is None
    set_global_target("prod")
    assert get_global_target() == "prod"
    set_global_target(None)
    assert get_global_target() is None


@pytest.mark.unit
def test_cli_top_level_target_propagates_to_global_state(monkeypatch, tmp_path) -> None:
    """``movate -t prod jobs show j-1`` should stash ``prod`` on the
    process-wide global. We exercise this indirectly: invoke the CLI
    with a top-level ``-t prod`` to a command that exits before doing
    any real I/O (config show), then read the module state.

    Direct assertion on module state is fine here — the alternative
    (asserting the full HTTP resolve_target() chain) re-tests
    user_config + storage, which has its own coverage."""
    from typer.testing import CliRunner  # noqa: PLC0415

    from movate.cli.main import app as cli_app  # noqa: PLC0415

    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    monkeypatch.delenv("MOVATE_TARGET", raising=False)
    runner = CliRunner(mix_stderr=False)

    # Sanity: with no -t, global state stays None.
    runner.invoke(cli_app, ["config", "list-targets"])
    assert get_global_target() is None

    # With -t prod at the top level, the global is set during the
    # invoke (and our autouse fixture would clear it after — but it
    # is observable mid-invoke via the global getter).
    set_global_target(None)  # reset
    runner.invoke(cli_app, ["-t", "prod", "config", "list-targets"])
    assert get_global_target() == "prod"


@pytest.mark.unit
def test_cli_movate_target_env_var_propagates(monkeypatch, tmp_path) -> None:
    """``MOVATE_TARGET=prod movate config list-targets`` should
    populate the global the same way ``-t prod`` does. (Typer's
    ``envvar=`` makes this automatic; we just guard the contract.)"""
    from typer.testing import CliRunner  # noqa: PLC0415

    from movate.cli.main import app as cli_app  # noqa: PLC0415

    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("MOVATE_TARGET", "staging")
    runner = CliRunner(mix_stderr=False)

    set_global_target(None)
    runner.invoke(cli_app, ["config", "list-targets"])
    assert get_global_target() == "staging"


@pytest.mark.unit
def test_cli_quiet_flag_suppresses_config_list_empty_hint(monkeypatch, tmp_path) -> None:
    """End-to-end through the CLI: ``movate -q config list-targets``
    with no config drops the "no targets registered..." hint that
    normally goes to stderr.

    Uses ``config list-targets`` (no auth, no network) rather than
    ``submit`` so the test stays hermetic and focused on quiet
    propagation, not the entire HTTP path."""
    from typer.testing import CliRunner  # noqa: PLC0415

    from movate.cli.main import app as cli_app  # noqa: PLC0415

    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    runner = CliRunner(mix_stderr=False)

    # Without --quiet: the "no targets registered" hint appears on stderr.
    result_loud = runner.invoke(cli_app, ["config", "list-targets"])
    assert result_loud.exit_code == 0, result_loud.stdout + result_loud.stderr
    assert "no targets" in result_loud.stderr

    # With --quiet: hint suppressed.
    result_quiet = runner.invoke(cli_app, ["--quiet", "config", "list-targets"])
    assert result_quiet.exit_code == 0, result_quiet.stdout + result_quiet.stderr
    assert "no targets" not in result_quiet.stderr
