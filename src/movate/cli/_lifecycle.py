"""Shared rendering for :class:`movate.core.models.AgentLifecycle`.

Both ``movate validate`` and ``movate show`` print a ``lifecycle`` row
in their metadata tables; the same colour mapping is used in either
context. The function lives here (not on the enum itself) so the colour
choice stays in the CLI layer — :mod:`movate.core` shouldn't know about
Rich markup.
"""

from __future__ import annotations

from movate.core.models import AgentLifecycle


def lifecycle_cell(lc: AgentLifecycle) -> str:
    """Return the lifecycle value with Rich markup matching its maturity tier.

    Colour intent:

    * ``draft``        — dim yellow ("not for serious use yet")
    * ``experimental`` — yellow ("known limitations")
    * ``validated``    — green ("vetted")
    * ``certified``    — bold green ("production-grade")
    * ``deprecated``   — yellow ("migrate away")
    * ``archived``     — red (defensive; loader normally refuses)
    """
    if lc is AgentLifecycle.DRAFT:
        return f"[dim yellow]{lc.value}[/dim yellow]"
    if lc is AgentLifecycle.EXPERIMENTAL:
        return f"[yellow]{lc.value}[/yellow]"
    if lc is AgentLifecycle.VALIDATED:
        return f"[green]{lc.value}[/green]"
    if lc is AgentLifecycle.CERTIFIED:
        return f"[bold green]{lc.value}[/bold green]"
    if lc is AgentLifecycle.DEPRECATED:
        return f"[yellow]{lc.value}[/yellow]"
    if lc is AgentLifecycle.ARCHIVED:
        return f"[red]{lc.value}[/red]"
    return lc.value
