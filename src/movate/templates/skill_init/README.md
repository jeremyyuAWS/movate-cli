# __SKILL_NAME__

A reusable skill an agent can invoke during a tool-use loop. See
[docs/adr/002-skills-and-contexts.md](../../docs/adr/002-skills-and-contexts.md)
for the broader design.

## Test it in isolation

```bash
mdk skills run __SKILL_NAME__ '{"query": "hello"}'
```

That dispatches the skill directly without spinning up an agent —
useful for iterating on `impl.py` without the LLM cost of a full
tool-use loop.

## Wire it into an agent

```yaml
# agents/your-agent/agent.yaml
skills:
  - __SKILL_NAME__
```

The executor converts the input schema into a tool spec for the
model, dispatches `tool_use` responses here, and feeds the result
back as a `tool_result` until the model emits a final answer.

## Where the code lives

- `skill.yaml` — the contract (name, schema, backend pointer, cost).
- `impl.py` — the Python function. Sync or async; signature is
  `(input: dict, ctx: SkillExecutionContext) -> dict`.

For HTTP-backed skills (call a REST API instead of running Python),
edit `skill.yaml` and replace `implementation.kind: python` with
`implementation.kind: http`. See the project README's "HTTP skills"
section.
