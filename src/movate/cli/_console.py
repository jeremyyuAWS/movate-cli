"""Shared stderr console + --quiet-aware ``hint`` helper.

Background: every CLI command used to do
``err = Console(stderr=True); err.print("[dim]...status hint...[/dim]")``.
That works for errors and warnings (operators always want to see those),
but the dim "FYI" prints — "queued j-1 on dev", "no jobs found",
"watching N files" — were leaking into stderr regardless of ``--quiet``,
which breaks the pipe-friendly contract:

  $ movate submit faq-agent '{}' | jq .  # stdout: clean JSON
  $ movate submit faq-agent '{}' -q | jq .  # used to spew hints anyway

The fix is small: a single module-state bool that ``--quiet`` flips,
and a :func:`hint` helper that no-ops while quiet is on. The shared
stderr :data:`stderr` console is exposed so error/warning calls
(which must NEVER be silenced) can keep using it directly without
having to know about the quiet machinery.

Module state instead of an env var because:

* Tests can flip it via :func:`set_quiet` cleanly per-test
  (``monkeypatch`` resets module attrs on teardown).
* The CLI is one process — no subprocess fanout to worry about.
* Env var would also work but bloats the env namespace.
"""

from __future__ import annotations

import typer
from rich.console import Console

stderr = Console(stderr=True)
"""Shared stderr console. Use for error / warning prints that must
NEVER be silenced (--quiet doesn't suppress these on purpose)."""

_quiet: bool = False
_global_target: str | None = None


def set_quiet(value: bool) -> None:
    """Toggle the module-wide quiet flag. Called from the top-level
    Typer callback when ``--quiet`` is passed."""
    global _quiet
    _quiet = value


def is_quiet() -> bool:
    """Read the current quiet flag. Exposed for commands that need
    branching behaviour beyond a simple suppress (e.g. drop a
    spinner when quiet)."""
    return _quiet


def set_global_target(value: str | None) -> None:
    """Set the process-wide default deployment target. Called from the
    top-level Typer callback when ``movate -t <name>`` (or the
    ``MOVATE_TARGET`` env var) is set. Per-command ``--target`` flags
    still win — this is the fallback when none is given."""
    global _global_target
    _global_target = value


def get_global_target() -> str | None:
    """Read the process-wide default deployment target, or ``None``.

    The intended call site is in remote commands' resolve-target
    helper:

      effective = per_command_target or get_global_target()
      target_name, cfg = resolve_target(effective)

    ``resolve_target(None)`` falls back to the config's active
    target, so an unset global means "use the active target" — same
    behaviour as before this option existed."""
    return _global_target


def hint(message: str) -> None:
    """Print a status hint to stderr unless ``--quiet`` is set.

    Use for FYI lines — "queued j-1 on dev", "no jobs found",
    "watching N files" — that an operator wants in interactive mode
    but should NOT appear when stderr is being captured or piped.

    Hard rule: NEVER use this for error or warning messages. Those
    go through :func:`error` / :func:`warn` instead, which always
    survive ``--quiet``."""
    if _quiet:
        return
    stderr.print(message)


def error(message: str, *, context: str | None = None) -> None:
    """Print a red ``✗``-prefixed error to stderr. Always rendered,
    even under ``--quiet`` — operators must see failure.

    With ``context`` we get ``✗ <context>:`` as the prefix, which is
    the right shape for "operation X failed because: <reason>":

      error("connection refused", context="submit")
      # → ✗ submit failed: connection refused

      error("env must be 'live' or 'test'; got 'foo'")
      # → ✗ env must be 'live' or 'test'; got 'foo'

    Doesn't raise ``typer.Exit`` — leaves the caller in control of
    exit code semantics (different commands map errors to different
    codes; the exit-code policy lives at each call site)."""
    if context:
        stderr.print(f"[red]✗ {context} failed:[/red] {message}")
    else:
        stderr.print(f"[red]✗[/red] {message}")


def warn(message: str, *, icon: str = "⚠") -> None:
    """Print a yellow warning to stderr. Always rendered, even under
    ``--quiet`` — warnings are usually "thing degraded but proceeded"
    information the operator wants to know.

    ``icon`` defaults to ``⚠`` (general warning); pass ``⏱`` for
    timeouts so they're scannable in a log. Other icons stay free
    for new shapes (e.g. ``⊘`` for safety-blocked) without growing
    the function surface."""
    stderr.print(f"[yellow]{icon}[/yellow] {message}")


def confirm_destructive(prompt: str, *, yes: bool) -> None:
    """Gate a destructive operation behind an interactive confirm.

    Pattern: every destructive command (``auth revoke-key``,
    ``config remove-target``, ``tenants clear-budget``) takes a
    ``--yes/-y`` flag and calls this helper first. In a TTY the
    operator gets a yes/no prompt; in a script they pass ``-y`` to
    bypass it. When stdin isn't a TTY and ``-y`` wasn't passed,
    Typer / Click raise ``Abort`` (exit 1) rather than block — so
    CI pipelines fail loud if they forgot ``-y``.

    Centralized here so every destructive command uses identical
    wording shape ("Y/N?") and the same exit semantics."""
    if yes:
        return
    if not typer.confirm(prompt):
        raise typer.Abort()


def success(message: str) -> None:
    """Print a green ``✓``-prefixed success line to stderr.

    Distinct from :func:`hint`: success lines are confirmation that
    a destructive / state-changing op completed, so the operator
    must see them regardless of ``--quiet``. Examples:
    ``✓ revoked <key_id>``, ``✓ active target → 'prod'``."""
    stderr.print(f"[green]✓[/green] {message}")


__all__ = [
    "confirm_destructive",
    "error",
    "get_global_target",
    "hint",
    "is_quiet",
    "set_global_target",
    "set_quiet",
    "stderr",
    "success",
    "warn",
]
