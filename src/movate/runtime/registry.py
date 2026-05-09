"""Agent registry — scan a filesystem path for ``agent.yaml`` files.

Used by ``GET /agents`` to advertise what's available on this runtime.
The scan happens **once** at app build time (in ``movate serve``) so
each request is a constant-time list lookup, not a fresh disk walk.

Robustness invariant: a single broken ``agent.yaml`` MUST NOT prevent
the runtime from starting. We log a warning and skip — the operator
sees the warning at startup, the rest of the catalog still loads, and
``GET /agents`` reflects only the agents that actually loaded.
"""

from __future__ import annotations

import logging
from pathlib import Path

from movate.core.loader import AgentBundle, AgentLoadError, load_agent

logger = logging.getLogger(__name__)


def scan_agents(root: Path) -> list[AgentBundle]:
    """Walk ``root`` for directories containing an ``agent.yaml``.

    Returns the list of successfully-loaded :class:`AgentBundle`s,
    sorted by spec name for stable ordering. Missing or non-directory
    ``root`` returns an empty list (operator running ``movate serve``
    without any agents on disk shouldn't crash — they just have an
    empty catalog).

    Walks **only one level deep** by design: agent layouts are flat
    (``agents/<name>/agent.yaml``). Recursing arbitrarily would pick
    up nested test fixtures and dev scratch dirs.
    """
    if not root.exists() or not root.is_dir():
        logger.info("agents_root_missing path=%s", root)
        return []

    bundles: list[AgentBundle] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "agent.yaml").exists():
            # Not every subdirectory is an agent — skip silently.
            # Could be a `.git`, an `evals/` shared dataset, etc.
            continue
        try:
            bundle = load_agent(entry)
        except AgentLoadError as exc:
            # One bad agent.yaml shouldn't blackhole the catalog.
            logger.warning("agent_load_skipped path=%s reason=%s", entry, exc)
            continue
        bundles.append(bundle)

    bundles.sort(key=lambda b: b.spec.name)
    return bundles


__all__ = ["scan_agents"]
