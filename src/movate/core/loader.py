"""Agent loader: parse an agent directory into a validated AgentBundle.

Resolves relative paths, validates JSON schemas, and computes a stable hash
of the prompt template body for run-record traceability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, select_autoescape
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from movate.core.models import AgentLifecycle, AgentSpec


class AgentLoadError(Exception):
    """Raised when an agent directory fails to load or validate."""


@dataclass
class AgentBundle:
    """Fully-resolved agent: spec, prompt template, validated schemas, hash."""

    spec: AgentSpec
    agent_dir: Path
    prompt_template: str
    prompt_hash: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator

    def render_prompt(self, input_data: dict[str, Any]) -> str:
        """Render the prompt template with the ``input.*`` namespace.

        No filesystem, network, or other globals are exposed to templates.

        Back-compat path: returns the FULL rendered prompt as a single
        string. For new code (executor / chat memory), prefer
        :meth:`render_messages` which respects the optional system/user
        block split."""
        env = Environment(
            autoescape=select_autoescape(disabled_extensions=("md",)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        template = env.from_string(self.prompt_template)
        return template.render(input=input_data)

    def render_messages(self, input_data: dict[str, Any]) -> list[dict[str, str]]:
        """Render the prompt as a list of role-tagged messages.

        If the template defines a Jinja ``{% block system %}...{% endblock %}``
        (and optionally a ``{% block user %}...{% endblock %}``), return
        ``[{role: system, content: ...}, {role: user, content: ...}]``.
        Otherwise — every existing template — fall through to a single
        ``[{role: user, content: <full rendered prompt>}]`` so behavior
        is unchanged.

        Why this matters: ``movate chat`` with conversation memory
        sends the prior user/assistant exchanges plus the current turn.
        Without a system/user split, the system instructions get
        re-rendered (and re-tokenized) inside every user message.
        With the split, the system instructions are sent ONCE at the
        head of the conversation and the per-turn user content stays
        small.

        Returns plain dicts rather than ``Message`` objects so the
        loader stays free of ``movate.providers.*`` imports — the
        executor maps them to provider Messages."""
        env = Environment(
            autoescape=select_autoescape(disabled_extensions=("md",)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        template = env.from_string(self.prompt_template)

        if "system" in template.blocks:
            ctx = template.new_context({"input": input_data})
            system_text = "".join(template.blocks["system"](ctx)).strip()
            if "user" in template.blocks:
                # Need a fresh context — Jinja blocks consume their context
                # generator and can't be re-iterated.
                ctx_user = template.new_context({"input": input_data})
                user_text = "".join(template.blocks["user"](ctx_user)).strip()
            else:
                # System block present but no user block: render the whole
                # template as the user content (system block included).
                # Operators who go halfway should still get a usable
                # rendering rather than an empty user message.
                user_text = template.render(input=input_data).strip()
            return [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ]

        # No blocks → back-compat single-message render.
        return [{"role": "user", "content": template.render(input=input_data)}]


def load_agent(path: str | Path) -> AgentBundle:
    """Load an agent directory. Raises AgentLoadError on any validation failure."""
    agent_dir = Path(path).resolve()
    if not agent_dir.is_dir():
        raise AgentLoadError(f"agent path is not a directory: {agent_dir}")

    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.exists():
        raise AgentLoadError(f"agent.yaml not found in {agent_dir}")

    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except yaml.YAMLError as exc:
        raise AgentLoadError(f"invalid YAML in {yaml_path}: {exc}") from exc

    try:
        spec = AgentSpec.model_validate(raw)
    except ValidationError as exc:
        raise AgentLoadError(f"agent.yaml validation failed:\n{exc}") from exc

    # Lifecycle gate. Archived agents are non-functional by definition —
    # surface as a load error so callers can't silently run them. Draft /
    # experimental / deprecated all load fine; `movate validate` surfaces
    # the soft warnings for those.
    if spec.lifecycle is AgentLifecycle.ARCHIVED:
        raise AgentLoadError(
            f"agent {spec.name!r} is lifecycle: archived — refusing to load. "
            f"Restore by editing agent.yaml (e.g. ``lifecycle: deprecated``) "
            f"if you need to read its config without running it."
        )

    prompt_path = (agent_dir / spec.prompt).resolve()
    if not prompt_path.exists():
        raise AgentLoadError(f"prompt file not found: {prompt_path}")
    prompt_text = prompt_path.read_text()
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

    input_schema = _load_json(agent_dir / spec.schemas.input)
    output_schema = _load_json(agent_dir / spec.schemas.output)

    try:
        Draft202012Validator.check_schema(input_schema)
        Draft202012Validator.check_schema(output_schema)
    except Exception as exc:
        raise AgentLoadError(f"invalid JSON schema: {exc}") from exc

    return AgentBundle(
        spec=spec,
        agent_dir=agent_dir,
        prompt_template=prompt_text,
        prompt_hash=prompt_hash,
        input_schema=input_schema,
        output_schema=output_schema,
        input_validator=Draft202012Validator(input_schema),
        output_validator=Draft202012Validator(output_schema),
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentLoadError(f"schema file not found: {path}")
    try:
        data: Any = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AgentLoadError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentLoadError(f"schema {path} must be a JSON object, got {type(data).__name__}")
    return data
