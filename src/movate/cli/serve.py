"""``movate serve`` — run the FastAPI runtime.

Builds the app via :func:`build_app`, scanning ``agents_path`` for
agent definitions at startup, and binds uvicorn to the requested
host/port. Storage init runs on the same loop uvicorn drives so
``aiosqlite`` connections aren't bound to a dead loop.

Workers (stage 4) consume the queue this populates. Without a
worker running, jobs land in the ``QUEUED`` state and stay there —
``GET /jobs/{id}`` returns the queued state, ``/run`` still works.
For end-to-end execution, run ``movate worker`` in a sibling
process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import uvicorn
from rich.console import Console

from movate.runtime.app import build_app
from movate.runtime.registry import scan_agents
from movate.storage import build_storage

err = Console(stderr=True)


def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
    port: int = typer.Option(8000, "--port", help="Bind port."),
    agents_path: Path = typer.Option(
        Path("./agents"),
        "--agents-path",
        envvar="MOVATE_AGENTS_PATH",
        help="Directory to scan for agent.yaml files. Falls back to empty catalog if missing.",
    ),
    log_level: str = typer.Option(
        "info",
        "--log-level",
        help="uvicorn log level (debug | info | warning | error).",
    ),
) -> None:
    """Start the movate FastAPI runtime.

    [bold]Examples:[/bold]

      [dim]# Default — binds 127.0.0.1:8000, scans ./agents/[/dim]
      $ movate serve

      [dim]# Custom port + remote-accessible[/dim]
      $ movate serve --host 0.0.0.0 --port 8080

      [dim]# Specific agents path[/dim]
      $ movate serve --agents-path /opt/movate/agents
    """
    storage = build_storage()
    asyncio.run(storage.init())

    agents = scan_agents(agents_path)
    if not agents:
        err.print(
            f"[yellow]⚠[/yellow] no agents loaded from {agents_path} "
            f"(GET /agents will return empty)"
        )
    else:
        err.print(f"[green]✓[/green] loaded {len(agents)} agent(s) from {agents_path}")
        for b in agents:
            err.print(f"  - {b.spec.name} v{b.spec.version}")

    app = build_app(storage, agents=agents)
    err.print(f"[bold]movate[/bold] serving on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level=log_level.lower())
