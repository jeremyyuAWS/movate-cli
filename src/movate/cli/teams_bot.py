"""``mdk teams-bot serve`` — boot the Teams Bot Framework webhook.

Mirrors the shape of ``mdk serve`` (the runtime HTTP server) — same
``--host`` / ``--port`` / ``--log-level`` flags, uvicorn under the
hood. Defaults to port 3978 because that's what the Bot Framework
Emulator looks for out of the box; ``mdk serve`` keeps 8000.

For 3.1.a we DON'T forward to the runtime — the bot echoes parsed
commands back. The ``--runtime-url`` flag is plumbed through to
prepare for 3.1.b which adds :class:`MovateClient` integration.
"""

from __future__ import annotations

import typer
from rich.console import Console

err = Console(stderr=True)


teams_bot_app = typer.Typer(
    name="teams-bot",
    help=(
        "Microsoft Teams bot — Movate's self-serve front door for "
        "non-technical users. See ADR 003 for the design."
    ),
    no_args_is_help=True,
)


@teams_bot_app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host."),
    port: int = typer.Option(
        3978,
        "--port",
        help=(
            "Bind port. 3978 is the Bot Framework Emulator default — "
            "leave it unless you have a reason to change."
        ),
    ),
    log_level: str = typer.Option(
        "info",
        "--log-level",
        help="uvicorn log level (debug | info | warning | error).",
    ),
    runtime_url: str = typer.Option(
        "http://127.0.0.1:8000",
        "--runtime-url",
        envvar="MOVATE_RUNTIME_URL",
        help=(
            "Base URL of the Movate runtime to forward `run` / `eval` "
            "commands to. Unused in slice 3.1.a (skeleton echoes "
            "back); wired to MovateClient in 3.1.b."
        ),
    ),
) -> None:
    """Boot the Teams bot webhook on ``host:port``.

    [bold]Quickstart for local dev:[/bold]

    Terminal 1 — run the Movate runtime (any agent serves):

        $ mdk serve --agents-path ./agents --port 8000

    Terminal 2 — run the Teams bot pointed at it:

        $ mdk teams-bot serve --runtime-url http://127.0.0.1:8000

    Terminal 3 — point the Bot Framework Emulator at
    ``http://localhost:3978/api/messages`` (no app id needed for
    local dev). You can now ``@movate ping`` in the emulator.

    Slice 3.1.a only echoes parsed commands; live agent execution
    arrives in 3.1.b once the result-rendering Adaptive Card is
    available.
    """
    try:
        import uvicorn  # noqa: PLC0415

        from movate.teams_bot.app import build_app  # noqa: PLC0415
    except ImportError as exc:
        err.print(
            "[red]✗[/red] missing dependencies for the Teams bot. "
            f"Install with: [bold]uv add 'movate-cli[teams]'[/bold]\n"
            f"  ({exc})"
        )
        raise typer.Exit(code=2) from exc

    # ``runtime_url`` is parked here as a runtime config dict accessible
    # via app.state — slice 3.1.b reads it when building MovateClient.
    # We pass it eagerly so misconfiguration surfaces at boot, not on
    # the first user message.
    app = build_app()
    app.state.runtime_url = runtime_url

    err.print(
        f"[green]✓[/green] movate teams-bot listening on "
        f"[bold]http://{host}:{port}[/bold]\n"
        f"  webhook:    POST /api/messages\n"
        f"  health:     GET  /health\n"
        f"  runtime:    {runtime_url}\n"
        f"  [dim]auth:       NONE (slice 3.1.a; JWT validation lands later)[/dim]"
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level)
