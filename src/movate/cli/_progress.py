"""Reusable progress UI helpers built on Rich.

All progress writes go to **stderr** so the stdout JSON pipe stays
clean — running ``movate eval ./agent -o json | jq .eval_id`` works
whether progress is showing or not. Same for ``movate run ... | tee
result.json`` and friends.

Auto-degrades on non-TTY: Rich's ``Console.is_terminal`` is False for
pipes, redirected streams, and CI environments, so we render a no-op
in those cases. Tests via Typer's ``CliRunner`` see clean stderr.

Three primitives:

* :func:`progress_bar` — known-length loop with a moving bar and
  elapsed time. Use for eval cases, bench models — anything where the
  total is known up front.
* :func:`spinner` — indeterminate-duration single operation. Use for
  one-shot provider calls, agent loads, etc.
* :func:`print_event` — one-line event print to stderr. Use for
  worker job feeds, serve startup banners, anywhere a streaming log
  feel beats a progress bar.

None of these are async-context-managers because Rich's progress
machinery is synchronous-friendly and works fine inside ``async``
functions. They're plain ``with`` blocks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

# Single shared stderr console so output ordering stays consistent
# across helpers. Callers that already have their own Console can
# pass it via ``console=`` overrides.
_stderr = Console(stderr=True)


@contextmanager
def progress_bar(
    *,
    description: str,
    total: int | None = None,
    transient: bool = True,
    console: Console | None = None,
) -> Iterator[Callable[..., None]]:
    """Context manager yielding an ``advance`` callable.

    Usage::

        with progress_bar(description="cases", total=len(cases)) as advance:
            for case in cases:
                ...
                advance()  # advance by 1
                advance(suffix=" (mean=0.83)")  # add a side-suffix

    ``total`` may be ``None`` for indeterminate-then-known progress —
    the first ``advance(total=N)`` call sets it. Useful when the total
    is known by the engine but not by the CLI until the first callback
    fires.

    ``transient=True`` clears the bar on exit (default; clean output
    after completion). Pass ``transient=False`` to leave it visible —
    handy for long failure post-mortems.
    """
    target = console or _stderr
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=target,
        transient=transient,
        # Rich already disables animation on non-TTY, but being
        # explicit helps in CI logs that capture some control codes.
        disable=not target.is_terminal,
    )
    with progress:
        task_id = progress.add_task(description, total=total)

        def advance(amount: int = 1, *, total: int | None = None, suffix: str = "") -> None:
            if total is not None:
                progress.update(task_id, total=total)
            if suffix:
                progress.update(task_id, description=f"{description}{suffix}")
            progress.advance(task_id, amount)

        yield advance


@contextmanager
def spinner(message: str, *, console: Console | None = None) -> Iterator[None]:
    """Indeterminate-duration spinner for one-shot operations.

    No-op when stderr isn't a TTY — Rich's status uses ANSI escapes
    that can confuse log capture in CI; cleaner to skip entirely.

    Usage::

        with spinner("calling provider..."):
            response = await executor.execute(...)
    """
    target = console or _stderr
    if not target.is_terminal:
        yield
        return
    with target.status(message, spinner="dots"):
        yield


def print_event(message: str, *, style: str = "", console: Console | None = None) -> None:
    """One-line event print to stderr.

    Style strings are Rich markup (e.g. ``"green"``, ``"bold red"``).
    Empty string = default style. Auto-rendered as plain text when
    stderr isn't a TTY.
    """
    target = console or _stderr
    if style:
        target.print(message, style=style)
    else:
        target.print(message)


__all__ = ["print_event", "progress_bar", "spinner"]
