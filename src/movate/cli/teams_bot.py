"""``mdk teams-bot serve`` — boot the Teams Bot Framework webhook.

Mirrors the shape of ``mdk serve`` (the runtime HTTP server) — same
``--host`` / ``--port`` / ``--log-level`` flags, uvicorn under the
hood. Defaults to port 3978 because that's what the Bot Framework
Emulator looks for out of the box; ``mdk serve`` keeps 8000.

Slice 3.1.b: the bot now actually calls the runtime via
:class:`MovateClient` and renders Adaptive Cards. ``--runtime-url``
is forwarded; ``--fleet-api-key`` (or the ``MOVATE_TEAMS_FLEET_API_KEY``
env) is required when the runtime expects auth.
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
            "Base URL of the Movate runtime the bot forwards "
            "`run` / `eval` commands to. Required when the bot is "
            "expected to actually run anything (it is, in 3.1.b+)."
        ),
    ),
    fleet_api_key: str = typer.Option(
        None,
        "--fleet-api-key",
        envvar="MOVATE_TEAMS_FLEET_API_KEY",
        help=(
            "The bot's API key for the runtime. Required when "
            "--runtime-url is set. Falls back to the "
            "MOVATE_TEAMS_FLEET_API_KEY env var; warned (not failed) "
            "when absent so the bot still boots for `ping` / `help` "
            "smoke tests."
        ),
    ),
    langfuse_public_host: str = typer.Option(
        None,
        "--langfuse-public-host",
        envvar="MOVATE_TEAMS_LANGFUSE_PUBLIC_HOST",
        help=(
            "Public Langfuse base URL. When set, successful run cards "
            "include a 'View trace' button. Off by default — only "
            "enable when the URL is routable for the audience (don't "
            "show prospects an internal-only URL)."
        ),
    ),
) -> None:
    """Boot the Teams bot webhook on ``host:port``.

    [bold]Quickstart for local dev:[/bold]

    Terminal 1 — run the Movate runtime:

        $ mdk serve --agents-path ./agents --port 8000

    Terminal 2 — issue an API key for the bot:

        $ mdk auth create-key --tenant local --name teams-bot

    Terminal 3 — run the Teams bot pointed at the runtime:

        $ MOVATE_TEAMS_FLEET_API_KEY=mvt_... \\
            mdk teams-bot serve --runtime-url http://127.0.0.1:8000

    Terminal 4 — point the Bot Framework Emulator at
    ``http://localhost:3978/api/messages``. ``@movate ping`` confirms
    the wire works; ``@movate run faq-agent {"question": "hi"}``
    drives a real round-trip through the runtime and renders the
    result as an Adaptive Card.
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

    # Soft-warn (not exit) when a runtime URL is set without an API
    # key — the bot is still useful for ping/help smoke tests, and
    # the ``run`` command surfaces a config-error card to the user
    # rather than crashing the bot on boot. Hard failure here would
    # break the "smoke test the bot manifest before secrets are
    # plumbed" workflow.
    if runtime_url and not fleet_api_key:
        err.print(
            "[yellow]![/yellow] --runtime-url is set but no "
            "--fleet-api-key / MOVATE_TEAMS_FLEET_API_KEY — "
            "`run` will return a config-error card until you set one."
        )

    app = build_app(
        runtime_url=runtime_url,
        fleet_api_key=fleet_api_key,
        langfuse_public_host=langfuse_public_host,
    )

    err.print(
        f"[green]✓[/green] movate teams-bot listening on "
        f"[bold]http://{host}:{port}[/bold]\n"
        f"  webhook:    POST /api/messages\n"
        f"  health:     GET  /health\n"
        f"  runtime:    {runtime_url or '(not configured)'}\n"
        f"  langfuse:   {langfuse_public_host or '(off)'}\n"
        f"  [dim]auth:       NONE on inbound (JWT validation lands later)[/dim]"
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level)
