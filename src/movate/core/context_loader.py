"""Context loader: discover ``contexts/<name>.md`` files and resolve agent references.

Contexts are shared markdown fragments prepended to an agent's prompt at
render time. The headline use-case is the "company style guide /
glossary / safety disclaimer" pattern — you write it once, every agent
that lists it in ``agent.yaml: contexts:`` gets it injected.

Three deliberate constraints on contexts (ADR 002):

* **Pure markdown.** No Jinja, no Python, no template syntax to learn.
  The body is concatenated verbatim. A future v2 may add an
  interpolation form (``{{ context.style }}``) as an escape hatch;
  v1 keeps the surface trivial.
* **Flat layout.** ``contexts/<name>.md`` only — no nested folders,
  no dotfile siblings. Operators can drop in a markdown file and it
  Just Works without learning a directory convention.
* **Declaration-order prepending.** When an agent lists multiple
  contexts, they're prepended in the order written, joined with a
  ``\\n\\n---\\n\\n`` separator. Deterministic; the operator can
  reason about which guide "wins" by reading the list.

This module is the counterpart to :mod:`movate.core.skill_loader` for
the contexts half of ADR 002.
"""

from __future__ import annotations

from pathlib import Path

# Separator between adjacent contexts when concatenated into the prompt
# prefix. Markdown's `---` is a horizontal-rule break — visually
# distinct in renderers and not something the model is likely to
# accidentally emit. Wrapped in blank lines so the prepended block
# remains a well-formed markdown chunk no matter what the prompt
# template body starts with.
_CONTEXT_SEPARATOR = "\n\n---\n\n"


class ContextLoadError(Exception):
    """Raised when a context file is missing or unparseable."""


def load_context_registry(project_root: str | Path) -> dict[str, str]:
    """Discover every context under ``<project_root>/contexts/<name>.md``.

    Returns a ``name → body`` map keyed by the file's basename (with
    the ``.md`` suffix stripped). Nested subdirectories are NOT
    scanned — keep contexts flat so operators don't have to learn a
    directory convention. Dotfiles and non-markdown files are
    silently skipped so a stray ``.DS_Store`` or ``README.md``-style
    sibling doesn't crash the loader.

    Empty registry (no ``contexts/`` folder, or it's empty) is the
    permissive default — agents whose ``contexts:`` list is empty
    don't care; agents that reference a missing context fail later
    at name resolution.
    """
    project_dir = Path(project_root).resolve()
    contexts_root = project_dir / "contexts"
    if not contexts_root.is_dir():
        return {}

    registry: dict[str, str] = {}
    for entry in sorted(contexts_root.iterdir()):
        # Skip dotfiles + subdirectories + non-markdown.
        if entry.name.startswith("."):
            continue
        if not entry.is_file():
            continue
        if entry.suffix.lower() != ".md":
            continue
        name = entry.stem  # filename without `.md`
        if not name:
            # An entry like ``.md`` would yield an empty name — skip
            # rather than register an unreferencable context.
            continue
        try:
            body = entry.read_text()
        except OSError as exc:
            raise ContextLoadError(f"failed to read context {entry}: {exc}") from exc
        registry[name] = body
    return registry


def resolve_agent_contexts(
    context_names: list[str],
    registry: dict[str, str],
) -> list[tuple[str, str]]:
    """Resolve an agent's ``contexts: [...]`` list against the registry.

    Returns ``(name, body)`` pairs in declaration order. Unknown
    names raise :class:`ContextLoadError` with the available names
    listed so operators can spot a typo immediately — same pattern
    as :func:`movate.core.skill_loader.resolve_agent_skills`.
    """
    resolved: list[tuple[str, str]] = []
    for name in context_names:
        if name not in registry:
            available = sorted(registry.keys())
            hint = str(available) if available else "(empty registry; add contexts/<name>.md)"
            raise ContextLoadError(
                f"agent references context {name!r} but no such context is "
                f"registered. Available: {hint}"
            )
        resolved.append((name, registry[name]))
    return resolved


def build_context_prefix(contexts: list[tuple[str, str]]) -> str:
    """Concatenate resolved contexts into the prompt-prefix string.

    Empty input returns the empty string — single-shot prompts get
    nothing prepended, which matches v0.5 behavior bit-for-bit. The
    returned prefix ends with the standard separator so the caller
    can simply ``prefix + rendered_prompt`` without an extra join.

    Why not interpolate or template? See module docstring + ADR 002.
    """
    if not contexts:
        return ""
    bodies = [body.rstrip("\n") for _, body in contexts]
    return _CONTEXT_SEPARATOR.join(bodies) + _CONTEXT_SEPARATOR
