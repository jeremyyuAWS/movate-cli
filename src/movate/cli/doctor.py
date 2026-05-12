"""``movate doctor`` — environment + configuration sanity check.

Default mode reports on the local environment (Python, deps, provider
keys, tracer, storage). Pass ``--target <name>`` to add an Azure-side
preflight that walks the deploy path (``az`` login → subscription
→ resource group → ACR → Container Apps → ``/healthz``) — the
first thing to run when ``movate deploy`` is acting up.

Output layout: one Rich panel per category, color-coded by worst
status in the section, with copyable fix hints on issues.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from movate import __version__
from movate.cli._theme import (
    ARROW,
    DIM,
    error_panel,
    fail_badge,
    kv_table,
    ok_badge,
    success_panel,
    warn_badge,
    warn_panel,
)
from movate.providers.pricing import load_pricing
from movate.tracing import build_tracer

console = Console()

# Required runtime deps. Missing = the package didn't install properly.
_REQUIRED_DEPS = ("typer", "rich", "pydantic", "yaml", "jinja2", "litellm", "aiosqlite")

# Optional deps + which extras provide them. Missing = some optional path
# of the CLI (serve, postgres, langfuse) won't work; everything else is fine.
_OPTIONAL_DEPS_TO_EXTRA = {
    "langfuse": "observability",
    "opentelemetry": "observability",
    "asyncpg": "postgres",
    "fastapi": "serve",
}
_OPTIONAL_DEPS = tuple(_OPTIONAL_DEPS_TO_EXTRA.keys())

_PROVIDER_KEYS = (
    ("OPENAI_API_KEY", "OpenAI"),
    ("ANTHROPIC_API_KEY", "Anthropic"),
    ("AZURE_OPENAI_API_KEY", "Azure OpenAI"),
    ("GEMINI_API_KEY", "Gemini"),
)
_TRACING_KEYS = (
    ("MOVATE_TRACER", "explicit override"),
    ("LANGFUSE_SECRET_KEY", "Langfuse secret"),
    ("LANGFUSE_PUBLIC_KEY", "Langfuse public"),
    ("LANGFUSE_HOST", "Langfuse host"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTel endpoint"),
    ("OTEL_SERVICE_NAME", "OTel service.name"),
)


@dataclass
class Check:
    """One row in a doctor panel.

    ``status`` drives the badge color; ``detail`` is the short label
    next to the badge; ``fix`` is a copyable command shown below the
    row when the status isn't ``ok``.
    """

    label: str
    status: Literal["ok", "warn", "fail"]
    detail: str = ""
    fix: str = ""


@dataclass
class Section:
    """A panel's worth of checks. Title shows in the panel header."""

    title: str
    checks: list[Check] = field(default_factory=list)

    def add(self, *args, **kwargs) -> None:
        self.checks.append(Check(*args, **kwargs))

    @property
    def worst(self) -> Literal["ok", "warn", "fail"]:
        statuses = {c.status for c in self.checks}
        if "fail" in statuses:
            return "fail"
        if "warn" in statuses:
            return "warn"
        return "ok"

    def summary(self) -> str:
        """Short kind-line for the panel title: ``3 ok``, ``2 ok · 1 warning``."""
        n_ok = sum(1 for c in self.checks if c.status == "ok")
        n_warn = sum(1 for c in self.checks if c.status == "warn")
        n_fail = sum(1 for c in self.checks if c.status == "fail")
        parts: list[str] = []
        if n_ok:
            parts.append(f"{n_ok} ok")
        if n_warn:
            parts.append(f"{n_warn} warning{'s' if n_warn > 1 else ''}")
        if n_fail:
            parts.append(f"{n_fail} failed")
        return " · ".join(parts) or "no checks"


def doctor(
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Also run the Azure preflight for a registered target "
            "(az login → subscription → RG → ACR → Container Apps → /healthz). "
            "Use this when `movate deploy` is failing."
        ),
    ),
) -> None:
    """Report on the local environment, deps, API keys, and movate state.

    With ``--target <name>``, adds a second section walking the Azure
    deploy path so you see the earliest broken link, not a stack trace
    from ``movate deploy``.
    """
    sections: list[Section] = [
        _runtime_section(),
        _required_deps_section(),
        _optional_deps_section(),
        _provider_keys_section(),
        _tracing_section(),
        _storage_and_project_section(),
    ]

    for section in sections:
        _render_section(section)
        console.print()

    _render_summary(sections)

    if target is not None:
        _render_azure_preflight(target)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _runtime_section() -> Section:
    s = Section("runtime")
    s.add("python", "ok", sys.version.split()[0])
    s.add("movate", "ok", __version__)
    return s


def _required_deps_section() -> Section:
    s = Section("required deps")
    for mod in _REQUIRED_DEPS:
        if importlib.util.find_spec(mod):
            s.add(mod, "ok")
        else:
            s.add(
                mod,
                "fail",
                "install failed",
                fix=f"uv tool install --editable . --force",
            )
    return s


def _optional_deps_section() -> Section:
    s = Section("optional deps")
    for mod, extra in _OPTIONAL_DEPS_TO_EXTRA.items():
        if importlib.util.find_spec(mod):
            s.add(mod, "ok")
        else:
            s.add(
                mod,
                "warn",
                "not installed",
                fix=f"uv tool install --editable '.[{extra}]' --force",
            )
    return s


def _provider_keys_section() -> Section:
    s = Section("provider keys")
    any_key = False
    for env_var, label in _PROVIDER_KEYS:
        present = bool(os.environ.get(env_var, "").strip())
        any_key = any_key or present
        if present:
            s.add(env_var, "ok", label)
        else:
            s.add(env_var, "warn", f"{label} missing", fix=f"export {env_var}=…")
    if not any_key:
        # Demote the warnings to a single hint — no keys at all is a
        # consistent state (mock-mode dev), not a per-provider issue.
        s.add(
            "hint",
            "warn",
            "no provider keys set",
            fix="movate run … --mock  # offline path, no API keys needed",
        )
    return s


def _tracing_section() -> Section:
    s = Section("tracing")
    for env_var, label in _TRACING_KEYS:
        present = bool(os.environ.get(env_var, "").strip())
        if present:
            s.add(env_var, "ok", label)
        else:
            s.add(env_var, "warn", f"{label} unset")
    # Resolved tracer — what `movate run` would actually use.
    try:
        tracer = build_tracer()
        s.add("resolved", "ok", tracer.name)
    except Exception as exc:  # pragma: no cover - diagnostic only
        s.add("resolved", "fail", str(exc))
    return s


def _storage_and_project_section() -> Section:
    s = Section("storage + project")

    sqlite_path = Path("~/.movate/local.db").expanduser()
    s.add(
        "sqlite",
        "ok",
        f"{sqlite_path} ({'exists' if sqlite_path.exists() else 'will be created on first run'})",
    )

    try:
        pricing = load_pricing()
        s.add(
            "pricing",
            "ok",
            f"v{pricing.version} ({len(pricing.models)} models, "
            f"last_verified {pricing.last_verified})",
        )
    except Exception as exc:
        s.add("pricing", "fail", f"load failed: {exc}")

    project_yaml = Path("movate.yaml")
    if project_yaml.exists():
        s.add("movate.yaml", "ok", str(project_yaml.resolve()))
    else:
        s.add(
            "movate.yaml",
            "warn",
            "not in cwd; defaults will be used",
            fix="movate init <name>   # to scaffold a project",
        )
    return s


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_section(section: Section) -> None:
    """Render one section as a Rich panel.

    Panel border color reflects ``section.worst``. Each row is the
    check label + status badge + detail. Fix hints (when present and
    the row isn't ``ok``) render dim below the row, prefixed by ``→``.
    """
    table = kv_table()
    badge_for = {
        "ok": ok_badge,
        "warn": warn_badge,
        "fail": fail_badge,
    }

    for check in section.checks:
        badge_fn = badge_for[check.status]
        rendered = badge_fn(check.detail) if check.detail else badge_fn("")
        table.add_row(check.label, rendered)
        if check.fix and check.status != "ok":
            table.add_row("", f"[{DIM}]{ARROW} fix: {check.fix}[/{DIM}]")

    panel_fn = {
        "ok": success_panel,
        "warn": warn_panel,
        "fail": error_panel,
    }[section.worst]
    console.print(panel_fn(table, name=section.title, kind=section.summary()))


def _render_summary(sections: list[Section]) -> None:
    """One-line aggregate verdict at the bottom of the local checks."""
    n_ok = sum(sum(1 for c in s.checks if c.status == "ok") for s in sections)
    n_warn = sum(sum(1 for c in s.checks if c.status == "warn") for s in sections)
    n_fail = sum(sum(1 for c in s.checks if c.status == "fail") for s in sections)

    if n_fail:
        prefix = "[red]✗ doctor[/red]"
    elif n_warn:
        prefix = "[yellow]! doctor[/yellow]"
    else:
        prefix = "[green]✓ doctor[/green]"

    parts: list[str] = [f"{n_ok} ok"]
    if n_warn:
        parts.append(f"{n_warn} warning{'s' if n_warn > 1 else ''}")
    if n_fail:
        parts.append(f"{n_fail} failed")
    console.print(f"{prefix}  [dim]{' · '.join(parts)}[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# Azure preflight (--target)
# ---------------------------------------------------------------------------


def _render_azure_preflight(target_name: str) -> None:
    """Render the Azure-side checks as one (or two) panels.

    Resolves the target first; a missing target is itself a finding
    (operator pointer in the error tells them to run
    ``movate config add-target``).
    """
    # Local imports — keep the doctor command's hot-path tight; these
    # are only needed when --target is set.
    from movate.cli._azure_doctor import run_azure_preflight  # noqa: PLC0415
    from movate.core.user_config import UserConfigError, resolve_target  # noqa: PLC0415

    try:
        target_name_resolved, target_cfg = resolve_target(target_name)
    except UserConfigError as exc:
        # Substring "azure preflight skipped" preserved for test fidelity.
        console.print(f"[red]✗ azure preflight skipped:[/red] {exc}")
        return

    section = Section(f"azure preflight {ARROW} {target_name_resolved}")
    for check in run_azure_preflight(target_name_resolved, target_cfg):
        if check.status == "ok":
            section.add(check.name, "ok", check.detail)
        elif check.status == "missing":
            section.add(check.name, "warn", check.detail or "missing")
        else:
            section.add(check.name, "fail", check.detail or "error")

    _render_section(section)
