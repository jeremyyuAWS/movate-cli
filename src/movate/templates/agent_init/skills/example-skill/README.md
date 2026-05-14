# Example skill — the MDK skill pattern

This folder demonstrates what a skill looks like in MDK. It's a
reference; out-of-the-box it's **not** wired into the agent — the
skill loader discovers skills from your project's `skills/` directory
(sibling of `agents/`), not from inside an individual agent dir.

## Anatomy of a skill

Every skill lives in its own subdirectory under `<project>/skills/<name>/`
with at minimum a `skill.yaml`:

```
<project>/
├── agents/
│   └── my-agent/
│       ├── agent.yaml
│       ├── prompt.md
│       └── ...
└── skills/
    └── example-skill/
        └── skill.yaml      ← this file
```

The `skill.yaml` declares **what the skill does** (name, description,
I/O schemas) and **how it executes** (Python function reference, HTTP
endpoint, or MCP tool). See `skill.yaml` in this folder for a fully
annotated example.

## Three backends

| Kind | What it is | When to use |
|---|---|---|
| `python` | A Python function resolved via importlib (`pkg.mod:func`). Receives the validated input dict + a `SkillExecutionContext`; returns a dict matching the output schema. | Pure logic, calculations, in-process lookups. Fastest. |
| `http` | A URL hit with the input as a JSON body. Response parsed + validated against the output schema. Auth via `bearer-from-env:VAR_NAME`. | Wrapping an external API. |
| `mcp` | A Model Context Protocol server (subprocess) + tool name. The MDK runtime spawns the server and routes calls. | Pulling in someone else's tool ecosystem (GitHub MCP, Slack MCP, etc.). Lands in a follow-up MDK release. |

The example here uses `python`. Switch to `http` or `mcp` by changing
`implementation.kind` and the matching fields — the rest of the skill
contract (schemas, cost, side effects) stays identical.

## Wiring this example into an agent

1. **Move the folder to your project's skills directory.** From the
   scaffolded agent dir:
   ```bash
   mkdir -p ../skills
   mv skills/example-skill ../skills/example-skill
   rmdir skills
   ```
   You now have `<project>/skills/example-skill/skill.yaml` at the
   architecturally-correct location.

2. **Implement the Python function** the skill's `implementation.entry`
   points at. Create `myproject/skills/time.py` (or wherever, then update
   the `entry` to match):
   ```python
   from datetime import datetime, timezone

   def get_current_time(input_payload: dict, ctx) -> dict:
       now = datetime.now(timezone.utc)
       return {"iso": now.isoformat(), "epoch_seconds": int(now.timestamp())}
   ```
   Make sure the module is on the Python path the MDK runtime sees.

3. **Reference the skill in your agent.yaml** so the loader registers
   it and the prompt template can use it:
   ```yaml
   skills:
     - example-skill
   ```

4. **Validate + run.** `mdk validate` checks every declared skill
   resolves; `mdk run` invokes the agent and gives the model access
   to call your skill.

## Cost + side effects — why they exist

`cost.per_call_usd` is added to the run's `metrics.cost_usd` every
time the skill fires. The per-tenant budget and the agent's
`max_cost_usd_per_run` ceiling enforce it without extra plumbing.

`side_effects` is a documentary annotation rendered in `mdk show
<skill>`. Future project-policy gates will let operators declare
"agents in this project may not use `mutates-state` skills" — declare
honestly so the policy layer works when it lands.

## Where to look next

- `src/movate/core/models.py` → `SkillSpec` — the full schema for
  every field this template uses.
- `src/movate/core/skill_loader.py` → discovery + parsing logic.
- `docs/skills-and-tools.md` (when present) → end-to-end skill
  authoring guide.
