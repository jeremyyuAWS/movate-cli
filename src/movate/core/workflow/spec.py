"""WorkflowSpec — Pydantic contract for ``workflow.yaml``.

Spec is the *parsed YAML*. The IR (:class:`WorkflowGraph`) is what the
runner/compiler walks. Keeping them separate means we can evolve the
internal IR (e.g. add metadata for LangGraph routing) without breaking
the user-facing schema.

v0.3 surface intentionally narrow:

* one ``entrypoint`` node
* node types limited to ``"agent"``
* edges have ``from`` and ``to`` only — no ``when:``, no parallel fan-out

Later phases relax these via separate validator passes.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class WorkflowRuntime(StrEnum):
    """Which compiler the workflow runner uses to execute this graph.

    * ``homegrown`` (default) — movate's own topology walker
      (:class:`movate.core.workflow.runner.WorkflowRunner`). Covers linear
      DAGs end-to-end with our retry / fallback / cost / tracing /
      tenant-isolation guarantees. The v0.3 default; no extra dep.
    * ``langgraph`` — compile the graph onto a LangGraph ``StateGraph``
      and run via ``CompiledStateGraph.invoke()``. Required-extra:
      ``uv pip install 'movate-cli[langgraph]'``. Unlocks conditional
      edges, parallel fan-out, HITL pause/resume, and the LangGraph
      checkpointer ecosystem when those features ship in v1.1.x.

    Linear AGENT workflows run equivalently under either runtime —
    same RunRecord shape, same cost, same WorkflowRunRecord. The
    ``runtime`` field is the seam: operators flip it per-workflow when
    they need a v1.1 feature, without breaking the v0.3 path.
    """

    HOMEGROWN = "homegrown"
    LANGGRAPH = "langgraph"


class WorkflowSpecLoadError(Exception):
    """Raised when ``workflow.yaml`` cannot be parsed or fails Pydantic validation."""


class NodeSpec(BaseModel):
    """One workflow node as written in YAML.

    Two node types ship:

    * ``agent`` (default) — invokes a registered agent via the executor.
      ``ref`` is the path to the agent directory.
    * ``human`` — HITL pause point. The workflow halts at this node and
      waits for an external system to call ``POST /workflows/{id}/resume``
      with a JSON payload matching the node's ``resume_payload_schema``.
      Requires ``runtime: langgraph`` + a ``checkpointer:`` (compiler
      enforces both at workflow load).

    Other node types (``tool`` / ``function`` / ``sub_workflow``) are
    declared in the IR but not yet accepted on the YAML surface — adding
    them is additive when their compiler integrations land.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128)
    type: Literal["agent", "human"] = "agent"
    ref: str = Field(
        default="",
        description=(
            "Path to agent dir (relative to workflow.yaml) for `type: agent`. "
            "Unused for `type: human` — the resume API supplies the payload at "
            "runtime."
        ),
    )

    resume_payload_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "JSON Schema for the payload an external system must supply via "
            "`POST /workflows/{id}/resume`. Required for `type: human`; "
            "ignored otherwise. Validated at compile time; the resume API "
            "validates incoming payloads against it before merging into state."
        ),
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$", v):
            raise ValueError(
                f"node id {v!r} must be lowercase alphanumeric with hyphens/underscores"
            )
        return v

    @model_validator(mode="after")
    def _validate_human_node_shape(self) -> NodeSpec:
        """Cross-field rules tied to ``type``:

        * ``type: human`` requires ``resume_payload_schema`` — the resume
          API needs it to validate incoming payloads. Surfaced at YAML
          parse so a missing schema doesn't slip past validate.
        * ``type: human`` MUST NOT carry a ``ref`` — there's no agent to
          point at. Rejected at parse so operators don't accidentally
          declare a HUMAN node with a dangling agent path.
        * ``type: agent`` requires ``ref`` (the agent dir) — already
          implicit through the existing loader checks but worth
          double-locking here.
        """
        if self.type == "human":
            if self.resume_payload_schema is None:
                raise ValueError(
                    f"node {self.id!r}: type=human requires resume_payload_schema "
                    f"(the JSON Schema the resume API will validate the payload against)"
                )
            if self.ref:
                raise ValueError(
                    f"node {self.id!r}: type=human must NOT have ref — there's no "
                    f"agent to invoke. Remove the ref field."
                )
        elif self.type == "agent" and not self.ref:
            raise ValueError(f"node {self.id!r}: type=agent requires ref (path to the agent dir)")
        return self


class EdgeKindYaml(StrEnum):
    """Mirrors :class:`movate.core.workflow.ir.EdgeKind` at the YAML
    surface. All four kinds shipped:

    * ``sequential`` — default; unconditional A→B transition.
    * ``conditional`` — fires only when ``when:`` evaluates truthy.
    * ``parallel_fan_out`` — multiple edges from one source run
      concurrently. State keys written by parallel branches need
      ``x-movate-reducer`` annotations in ``state_schema``.
    * ``parallel_fan_in`` — multiple edges into one target. LangGraph
      waits on all upstream branches before firing the target.
    """

    SEQUENTIAL = "sequential"
    CONDITIONAL = "conditional"
    PARALLEL_FAN_OUT = "parallel_fan_out"
    PARALLEL_FAN_IN = "parallel_fan_in"


class EdgeSpec(BaseModel):
    """One workflow edge as written in YAML.

    Two shapes are supported in v1.1:

    * ``kind: sequential`` (default) — unconditional A→B transition.
    * ``kind: conditional`` — fires only when ``when:`` is truthy. The
      LAST conditional edge from a given source must have ``when: null``
      to act as the default (compiler enforces). See
      :mod:`movate.core.workflow.condition_dsl` for the expression syntax.

    Parallel fan-out / fan-in are explicitly out of scope until a later
    PR (see BACKLOG.md "Tier 2 follow-up: determinism implementation" §6).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_id: str = Field(..., alias="from")
    to_id: str = Field(..., alias="to")
    kind: EdgeKindYaml = Field(
        default=EdgeKindYaml.SEQUENTIAL,
        description=(
            "Edge kind. `sequential` (default) is an unconditional "
            "transition. `conditional` fires only when `when:` evaluates "
            "truthy at runtime."
        ),
    )
    when: str | None = Field(
        default=None,
        description=(
            "Expression in the condition DSL "
            '(`$.field < 0.7`, `$.a in ["x", "y"]`, etc.). Required '
            "when `kind: conditional` except on the explicit-default edge "
            "(the LAST conditional edge from a source must have `when: null`). "
            "Ignored for `kind: sequential`."
        ),
    )

    @model_validator(mode="after")
    def _validate_kind_and_when(self) -> EdgeSpec:
        """Cross-field: ``when:`` is only meaningful on conditional edges.
        Sequential and parallel kinds reject it at YAML parse time so a
        typo doesn't get silently ignored at runtime."""
        if self.when is not None and self.kind is not EdgeKindYaml.CONDITIONAL:
            raise ValueError(
                f"edge {self.from_id}→{self.to_id} has kind: {self.kind.value} but "
                f"declares `when:`; only `kind: conditional` accepts a "
                f"`when:` clause."
            )
        return self


class WorkflowSpec(BaseModel):
    """Top-level workflow.yaml contract."""

    model_config = ConfigDict(extra="forbid")

    api_version: Literal["movate/v1"]
    kind: Literal["Workflow"] = "Workflow"

    name: str = Field(..., min_length=1, max_length=128)
    version: str
    description: str = ""
    owner: str = ""

    runtime: WorkflowRuntime = Field(
        default=WorkflowRuntime.HOMEGROWN,
        description=(
            "Which compiler the runner uses. Defaults to `homegrown` "
            "(movate's own topology walker). Set to `langgraph` to "
            "compile onto a LangGraph StateGraph instead — required for "
            "conditional edges, parallel fan-out, HITL, and checkpointer "
            "features that land in v1.1.x. Linear AGENT workflows behave "
            "equivalently under either runtime."
        ),
    )

    checkpointer: str | None = Field(
        default=None,
        description=(
            "Persistence backend for LangGraph checkpoints. One of "
            "`memory` (in-process; fast; lost on restart), `sqlite` "
            "(single-file persistence; deferred), `postgres` (multi-node "
            "shared; deferred), or null to disable. Required for HITL "
            "workflows once HUMAN nodes ship; optional for linear v1.0 "
            "workflows where checkpoints are diagnostic only. Field has "
            "no effect when `runtime: homegrown` — the homegrown runner "
            "doesn't checkpoint between nodes."
        ),
    )

    state_schema: str = Field(
        ..., description="Path to a JSON Schema file, relative to workflow.yaml"
    )
    entrypoint: str = Field(..., description="ID of the starting node")

    nodes: list[NodeSpec] = Field(..., min_length=1)
    edges: list[EdgeSpec] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", v):
            raise ValueError(f"workflow name {v!r} must be lowercase alphanumeric with hyphens")
        return v

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError(f"workflow version {v!r} must be semver (MAJOR.MINOR.PATCH)")
        return v


def load_workflow_spec(path: str | Path) -> tuple[WorkflowSpec, Path]:
    """Load and validate a ``workflow.yaml`` file.

    Returns the spec plus the directory that contains it (so callers can
    resolve relative ``ref``s and ``state_schema`` paths).
    """
    p = Path(path).resolve()
    if p.is_dir():
        p = p / "workflow.yaml"
    if not p.is_file():
        raise WorkflowSpecLoadError(f"workflow.yaml not found at {p}")

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise WorkflowSpecLoadError(f"invalid YAML in {p}: {exc}") from exc

    try:
        spec = WorkflowSpec.model_validate(raw)
    except ValidationError as exc:
        raise WorkflowSpecLoadError(f"workflow.yaml validation failed:\n{exc}") from exc

    return spec, p.parent
