# Friday demo script — MDK endpoints for Deva

A read-aloud-able script for live-demoing the MDK v1 endpoint
surface to Deva. **15-20 minutes** depending on Q&A.

> **This is the demo script, NOT the pre-meeting verification.** For
> "make sure everything is alive before the meeting" see
> [`friday-demo-smoke.md`](friday-demo-smoke.md). Run that one 30 min
> before this one.

---

## Before the meeting (5 min, do alone)

```bash
cd /Users/css173265/projects/movate-cli/scripts/deva-curl

# Confirm bearer is in .env (already set; just verify)
cat .env | grep MDK_TOKEN

# Confirm runtime is alive
./01-health.sh
# Expect: {"status": "ok", "version": "0.7.0"} + {"status": "ready", ...}

# Confirm OpenAPI shows all 11 v1 routes
curl -s "$(grep MDK_BASE .env | cut -d'"' -f2)/openapi.json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(len([p for p in d['paths'] if '/api/v1' in p]), 'v1 routes')"
# Expect: 11 v1 routes
```

If anything fails, see the troubleshooting section at the bottom.

---

## The demo (15-17 min)

### Opening — set the stage (1 min)

> *"Today I'll walk you through every endpoint your Angular UI can
> call against MDK. All of these are live on Azure right now — your
> wizard can hit them directly from `ng serve`. I'm going to do it
> in bash with curl wrappers so the wire shapes are obvious, but
> you'll wire them through your generated TypeScript client."*

**Mention:**
- Runtime URL: `https://movate-dev-api.victoriouswater-7958662f.eastus2.azurecontainerapps.io`
- OpenAPI spec at `/openapi.json` → use `openapi-generator-cli` for the TS client
- Auth: single fleet bearer (this one), wrap in BFF per `docs/angular-client.md`

---

### Step 1 — Health check (30 sec)

```bash
./01-health.sh
```

**Expected output:**
```json
{"status": "ok", "version": "0.7.0"}
{"status": "ready", "version": "0.7.0", "checks": {"storage": "ok"}}
```

**Talking points:**
- `/healthz` is unauthed liveness — your load balancer hits this
- `/ready` is authed deep-check — surfaces storage issues; Container Apps probes use this
- Version `0.7.0` is what's serving right now

---

### Step 2 — Create an agent from your wizard's JSON shape (2 min)

> *"This is the endpoint your 'Onboard Agent' wizard's submit button
> calls. The JSON body matches your wizard's field set exactly."*

```bash
./02-create-agent.sh "Demo Friday Bot"
```

**Expected (truncated):**
```json
{
    "name": "demo-friday-bot",
    "version": "0.1.0",
    "description": "Demo agent created via deva-curl wrapper",
    "agent_dir": "demo-friday-bot",
    "files_persisted": [
        "agent.yaml",
        "prompt.md",
        "schema/input.json",
        "schema/output.json"
    ]
}
```

**Talking points to call out:**
- *"Notice the name got slugified — `Demo Friday Bot` → `demo-friday-bot`.
  Your UI can pass human-readable names; MDK handles URL-safety."*
- *"It persisted 4 files. The wizard doesn't collect I/O schemas, so
  MDK generates sensible defaults — free-form text in, free-form text
  out. Your wizard's 'Reference Output' becomes an `examples` entry
  in the agent.yaml."*
- *"`files_persisted` is what your UI shows in the 'Files in this
  agent' panel — these are canonical paths, not absolute filesystem
  paths."*

---

### Step 3 — See it in the catalog (1 min)

```bash
./03-list-agents.sh
```

**Expected:** a JSON array of agents including `demo-friday-bot`.

**Talking points:**
- *"This is what populates your Agent Catalog page. It's the unversioned
  endpoint — fine for the catalog grid."*
- *"For richer marketplace facets (filtering by role/capabilities/tags),
  there's a follow-up endpoint coming — item 63 in the backlog."*

---

### Step 4 — Get the full profile (2 min)

```bash
./04-get-agent.sh demo-friday-bot
```

**Talking points — point out specific fields in the response:**
- *"`role`, `persona`, `capabilities`, `tags` — that's the marketplace
  metadata. Your wizard's dropdowns + textareas populate these."*
- *"`model_provider`, `model_params`, `model_fallback` — your UI shows
  this in the 'Model Config' panel."*
- *"`prompt`, `prompt_hash` — full prompt body inline, SHA-256 for
  change detection. When the user edits and re-fetches, compare hashes
  to show a 'prompt changed' badge."*
- *"`input_schema`, `output_schema` — render as collapsible JSON
  blocks in the UI."*
- *"`dataset: null` — this agent has no eval dataset yet. Your UI
  should disable the 'Run Eval' button when null."*

---

### Step 5 — Validate before publish (1 min)

> *"This is the gate your 'Publish' button uses. Returns errors,
> warnings, and a cost forecast for the eval."*

```bash
./05-validate-agent.sh demo-friday-bot
```

**Expected:**
```json
{
    "passed": true,
    "errors": [],
    "warnings": [],
    "cost_forecast": null
}
```

**Talking points:**
- *"`passed: true` → green checkmark in the UI"*
- *"`errors[]` → red chips, block save"*
- *"`warnings[]` → yellow chips, informational"*
- *"`cost_forecast` is null here because no dataset. With a dataset it
  shows projected cost — let users see 'this eval will cost ~$0.45'
  before clicking Run."*

---

### Step 6 — Run the agent (3 min)

> *"Two modes — async or inline. Pick based on your UX."*

**Mode A — async (production-shape):**
```bash
./06-run-agent.sh demo-friday-bot '{"input": "hello async"}'
```

Returns `{"job_id": "...", "status": "queued"}` immediately.

```bash
# Pretend you saved the job_id; poll until terminal:
./07-job-status.sh <paste-job-id>
```

**Talking points (async):**
- *"Client doesn't block. Use this for production where users navigate
  away while their job runs."*
- *"Polling every 1-2 seconds is the v1 pattern; SSE streaming is item
  75 in the backlog if/when polling feels laggy."*

**Mode B — inline (wizard-create-and-run round-trip):**
```bash
./06-run-agent.sh demo-friday-bot '{"input": "hello inline"}' wait mock
```

**Expected:** HTTP 200 with full RunView (run_id, output, metrics, status).

**Talking points (inline):**
- *"`?wait=true` blocks the HTTP request until the agent finishes.
  Returns the full RunView in one response — no polling."*
- *"`mock` flag uses the MockProvider for deterministic dev. Drop it
  to use the real LLM."*
- *"This is the mode you'll want for the wizard demo flow: user clicks
  'Try It', sees output immediately. Behind the scenes it's the same
  Executor stack the worker uses."*
- **Heads up to mention:** *"status will be `error` in this demo
  because MockProvider returns `{"message": ...}` but the wizard's
  default schema expects `{"output"}`. With a real LLM call the
  prompt produces the right shape. The 200 + RunView wire contract
  is the point — your UI gets the same shape either way."*

---

### Step 7 — Run an eval (3 min)

> *"Evals work on agents that have a dataset. Your wizard-created
> agent doesn't yet — dataset upload is item 111 in the backlog,
> ~1h to ship when needed. For now I'll demo against `faq-agent`
> which has a dataset baked into the image."*

```bash
./09-run-eval.sh faq-agent
```

**Expected:**
```json
{
    "eval_id": "...",
    "status": "success",
    "message": ""
}
```

**Save the eval_id, then:**
```bash
./10-eval-scorecard.sh <paste-eval-id>
```

**Talking points to call out in the scorecard:**
- *"`mean_score`, `pass_rate`, `sample_count`, `total_cost_usd` — these
  drive the eval-result UI."*
- *"`judge_method` — `exact` for substring match; `llm_judge` when the
  agent declares one. Switches automatically based on agent config."*
- *"`gate_mode` — `mean` is default; can be `min` or `p10` for
  stricter gating."*

**Then show history:**
```bash
./11-eval-history.sh faq-agent
```

**Talking point:** *"This drives your 'evals over time' chart on the
agent profile page."*

---

### Step 8 — Trace replay (2 min)

> *"Observability — given any run_id, get the full timeline."*

Find a run_id from the earlier inline run, then:

```bash
./12-trace.sh <run-id>
```

**Talking points:**
- *"`kind: agent` for single-agent runs; `kind: workflow` for multi-node
  workflows."*
- *"`run` carries everything for an agent trace: input/output, metrics,
  cost, latency, status, prompt_hash."*
- *"For workflows, `nodes[]` is the chronological child-run list — your
  flow-chart UI walks this to render the executed graph."*
- *"`total_cost_usd` and `total_latency_ms` are pre-computed across
  children for the summary card."*

---

### Step 9 — Cleanup with soft-delete (1 min)

> *"And when the user clicks 'Delete' in the wizard:"*

```bash
./13-delete-agent.sh demo-friday-bot
```

**Expected:**
```json
{
    "name": "demo-friday-bot",
    "deleted_dir": ".deleted-demo-friday-bot-<timestamp>"
}
```

**Talking points:**
- *"Soft-delete — the bundle moves to a sibling `.deleted-<name>-<ts>/`
  directory. Bytes survive for a recovery window."*
- *"Operators can `mv` it back if needed; a future cron sweep removes
  .deleted-\* dirs older than 7 days."*
- *"Your UI sees the agent disappear from `GET /agents` immediately
  (the in-memory registry refreshes on every CRUD)."*

**Confirm gone:**
```bash
./03-list-agents.sh
```

`demo-friday-bot` should no longer appear.

---

### Closing — wrap up (1 min)

> *"That's the whole surface. To recap the verbs you have:"*
> *— Create + Read + Update + Validate + Run + Delete*
> *— Eval kickoff + scorecard + history*
> *— Trace replay + filterable job history*

**Action items to leave Deva with:**

1. **Send me your Mova iO production hostname** — I'll add it to the CORS allow-list (one `az` command, no rebuild).
2. **Generate your TypeScript client** against `/openapi.json`:
   ```bash
   npx @openapitools/openapi-generator-cli generate \
     -i https://movate-dev-api.victoriouswater-7958662f.eastus2.azurecontainerapps.io/openapi.json \
     -g typescript-angular \
     -o src/app/api-client
   ```
3. **Auth pattern: wrap the bearer in a BFF** — see `docs/angular-client.md`. Angular itself shouldn't hold the `mvt_live_...` key (XSS risk). Your BFF holds it and forwards.
4. **Pick your wait-mode story** — inline for the wizard's "Try It" button, async for production runs where users walk away.

---

## During the demo — if something fails

| Failure | Recovery |
|---|---|
| Any wrapper says "Missing .env" | `cp scripts/deva-curl/.env.example scripts/deva-curl/.env`, paste the bearer |
| 401 on every call | Bearer expired or wrong; `cat scripts/deva-curl/.env` to verify |
| 404 on a v1 endpoint | Old image still serving; ping me, I'll redeploy in 5 min |
| 422 with `invalid_bundle` on create | Probably the model string format — must be LiteLLM-style `provider/model-id` |
| Wrapper prints raw JSON without color | Pipe to `jq` for prettier output: `./04-get-agent.sh foo \| jq .` |
| Inline run mode returns `status: "error"` | Expected with `mock` flag — schema mismatch with MockProvider; the wire works |

## After the demo — onboarding bundle to send Deva

Paste this into Slack/email after the meeting:

```
Subject: MDK v0.7 — onboarding for the Friday demo

Hi Deva,

Everything you saw today is live. Your wiring kit:

Runtime:      https://movate-dev-api.victoriouswater-7958662f.eastus2.azurecontainerapps.io
OpenAPI spec: <runtime>/openapi.json
Swagger UI:   <runtime>/docs (point + click exploration)
Bearer token: <pasted from scripts/deva-curl/.env>

Curl wrappers: scripts/deva-curl/ in the mdk-cli repo. Set MDK_TOKEN
in .env once; everything else is one-line invocations.

TypeScript client: see docs/angular-client.md for the openapi-generator-cli
incantation + the BFF auth pattern.

CORS today: http://localhost:4200 only. Send me your prod Mova iO
hostname and I'll add it (one az command, no rebuild).

Let me know what 422s / what shapes you need clarified.
```

## Reference

* All 13 wrappers documented in [`scripts/deva-curl/README.md`](../scripts/deva-curl/README.md)
* End-to-end smoke verification: [`docs/friday-demo-smoke.md`](friday-demo-smoke.md)
* Auth pattern + client gen: [`docs/angular-client.md`](angular-client.md)
* What's NOT yet wired (post-Friday): BACKLOG.md Group I-R
