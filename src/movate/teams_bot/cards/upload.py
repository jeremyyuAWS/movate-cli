"""Adaptive Card builders for upload validation outcomes (slice 3.1.d).

Two paths funnel into two card shapes:

* :func:`build_agent_upload_card` — agent uploaded + loaded cleanly.
  Lists the detected metadata so the operator can confirm "this is the
  right thing" before running it: name, version, runtime, model
  provider, declared skills + objectives + contexts. Used when the
  user uploads an agent file (with or without an accompanying ``run``
  command).
* :func:`build_dataset_upload_card` — dataset uploaded + parsed
  cleanly. Lists the row count + a 1-line preview of the first row.
  Used when the user uploads a `.jsonl` for evaluation.

Failures (bad zip, didn't validate as agent, malformed JSON line, etc.)
reuse the existing ``build_error_card`` from :mod:`.error` rather than
adding a third template — the message is already shaped for cards by
:class:`UploadResult.error`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from movate.teams_bot.cards._common import (
    container,
    empty_card,
    fact,
    fact_set,
    text_block,
)

if TYPE_CHECKING:
    from movate.core.loader import AgentBundle


def build_agent_upload_card(
    bundle: AgentBundle,
    *,
    filename: str,
    next_step_hint: str | None = None,
) -> dict[str, Any]:
    """Render an agent upload's metadata as an Adaptive Card.

    The card answers "what did the bot just receive?" — operators
    scanning a channel can verify the upload matches what they
    intended without inspecting raw YAML.

    Args:
        bundle: loaded :class:`AgentBundle` — fields read: name,
            version, runtime, model.provider, skills, objectives,
            contexts.
        filename: original upload name (``agent.yaml``,
            ``faq-bot.zip``, etc.) — surfaces as a subtle prefix line.
        next_step_hint: optional one-line suggestion ("Now type
            ``@movate run <input-json>`` to execute it"). Rendered
            with the lightbulb prefix.
    """
    spec = bundle.spec
    card = empty_card()

    # Header — green check for "loaded successfully".
    card["body"].append(
        text_block(
            f"✅ Agent loaded — `{spec.name}` v{spec.version}",
            weight="Bolder",
            size="Large",
            color="Good",
        )
    )

    # Subtle filename echo so the user knows WHICH upload this card
    # corresponds to (helps when multiple are in-flight).
    card["body"].append(
        text_block(f"from: `{filename}`", is_subtle=True),
    )

    # Metadata FactSet. Skills / objectives / contexts get their own
    # rows because they're the bits the user genuinely cares to verify
    # ("did the bot see my calc skill?").
    facts = [
        fact("runtime", spec.runtime.value),
        fact("model", spec.model.provider),
    ]
    if spec.model.fallback:
        # First fallback is enough for the card — the full list is in
        # the agent.yaml the user just dragged in.
        facts.append(fact("fallback", spec.model.fallback[0].provider))
    facts.append(fact("api_version", spec.api_version))
    if bundle.skills:
        facts.append(fact("skills", ", ".join(s.spec.name for s in bundle.skills)))
    else:
        facts.append(fact("skills", "(none)"))
    if spec.objectives:
        facts.append(
            fact(
                "objectives",
                ", ".join(o.id for o in spec.objectives),
            )
        )
    if bundle.contexts:
        # ``contexts`` is a list of (name, body) tuples in declaration
        # order — surface just the names in the card.
        facts.append(fact("contexts", ", ".join(name for name, _ in bundle.contexts)))
    card["body"].append(fact_set(facts))

    # Description — if the agent declared one — gets its own block so
    # it's prominent (it's the human-readable purpose statement).
    if spec.description:
        card["body"].append(
            container(
                [
                    text_block("Description", weight="Bolder", is_subtle=True),
                    text_block(spec.description),
                ],
                style="emphasis",
            )
        )

    # Optional hint — same lightbulb pattern the error cards use.
    if next_step_hint:
        card["body"].append(text_block(f"💡 {next_step_hint}", is_subtle=True))

    # Drop the empty actions array (same pattern as run_result).
    if not card["actions"]:
        del card["actions"]
    return card


def build_dataset_upload_card(
    *,
    filename: str,
    row_count: int,
    first_row_preview: str = "",
    next_step_hint: str | None = None,
) -> dict[str, Any]:
    """Render a dataset upload's stats as an Adaptive Card.

    Args:
        filename: original upload name.
        row_count: number of non-empty parsed rows.
        first_row_preview: a one-line JSON preview of the first row,
            truncated if needed — helps the user confirm the dataset
            has the right shape.
        next_step_hint: optional one-line suggestion (e.g. "Eval-with-
            upload lands in slice 3.2; for now use `mdk eval <agent>
            --dataset <path>` locally").
    """
    card = empty_card()

    card["body"].append(
        text_block(
            f"✅ Dataset loaded — {row_count} row{'s' if row_count != 1 else ''}",
            weight="Bolder",
            size="Large",
            color="Good",
        )
    )
    card["body"].append(text_block(f"from: `{filename}`", is_subtle=True))

    facts = [fact("rows", str(row_count))]
    if first_row_preview:
        # Trim brutally for FactSet's narrow second column.
        facts.append(fact("row 1", _truncate(first_row_preview, 60)))
    card["body"].append(fact_set(facts))

    if next_step_hint:
        card["body"].append(text_block(f"💡 {next_step_hint}", is_subtle=True))

    if not card["actions"]:
        del card["actions"]
    return card


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
