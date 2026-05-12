# movate — Feature Backlog

A ranked, checkable list of features for movate. Each item is sized to "thing a user could notice or test." This is the working backlog — the high-level phasing lives in the [implementation roadmap](../../.claude/plans/want-to-take-inspiration-stateful-swan.md).

## How to read this

```
- [ ] **Feature name** `[LEVERAGE] [PHASE] [STATUS] [EFFORT]` — one-line description.
```

| Tag | Meaning |
|---|---|
| `[HIGH]` | Big unlock per unit effort. Ship these first. |
| `[MED]` | Worth doing but not urgent. |
| `[LOW]` | Nice-to-have or stop-energy. Defer or drop. |
| `[v0.1]…[v1.1+]` | Target release phase. |
| `[done]` | Already in repo. |
| `[next]` | Top-of-stack — pick this up next. |
| `[blocked:X]` | Waiting on X. |
| `[idea]` | Captured but no commitment. |
| `≤2h / ≤1d / 2-3d / 1w / 2w+` | Effort estimate. |

**Leverage** = (value to a movate user) ÷ (engineering effort + ongoing maintenance burden). When in doubt, prefer items with strong leverage even if their phase is later — you'll re-evaluate when those phases land.

---

## ⚡ What's next (forward-looking)

Replaces the historical "Top 10" — that lived as accumulated session blurbs and stopped being a roadmap. This is the **actual next 10 things to ship**, ranked by leverage. Recent shipped highlights are preserved in the next section.

### Tier B polish (each ≤ 1 day) — pick up first

1. [ ] **More agent templates** (extractor, RAG, function-caller) `[HIGH] [post-v1.0] [≤1d each]` — most customer-visible. Answers *"what can I build with this"* with concrete `movate init -t <kind>` starts. Template registry already exists.
2. [ ] **`movate logs <run-id> --tail`** `[MED] [v0.4] [≤1d]` — Rich timeline of stored events. Pairs with Telegram alerts (alert lands → check what happened).
3. [ ] **`movate diff <agent-a> <agent-b>`** `[MED] [v0.2] [≤1d]` — show prompt-hash, model, schema deltas. PR-review ergonomics; high leverage at low effort.
4. [ ] **Privacy: redact prompt/output spans by config** `[MED] [v0.4] [≤1d]` — `tracer.redact_io: true` for tenants with PII. **Gates real customer onboarding.**
5. [ ] **Rubric library** (3-5 standard rubrics) `[MED] [v0.2] [≤1d]` — relevance, correctness, faithfulness, safety, tone. Imported by name from `evals/judge.yaml`. Productizes the eval flow.
6. [ ] **`--dry-run` on `movate run`** `[MED] [v0.2] [≤2h]` — render prompt, show what *would* be sent, exit 0. Catches prompt-template bugs before paying for the call.

### CI hardening (small but blocking)

7. [ ] **GH Actions `validate.yml`** `[HIGH] [v1.0] [≤1d]` — schema + topology validation on every PR. Catches broken `agent.yaml` / `workflow.yaml` before review.
8. [ ] **GH Actions `security.yml`** `[MED] [v1.0] [≤1d]` — dependency + secret scan. Standard hygiene.

### Real features (1-2 days each)

9. [ ] **`--parallel` flag for `movate bench`** `[MED] [v0.3] [≤1d]` — currently sequential; parallel respects per-provider rate limits. Cuts bench time ~3x for 4+ models.
10. [ ] **Idempotency on `POST /run` by `request_id`** `[HIGH] [v0.5] [≤1d]` — retry-safe; returns existing job. Needed when customers integrate via CI/CD where retries are common.

### Bigger asks (3-5 days; pick when a customer asks)

* **HTTP streaming for `POST /run?wait=true`** — SSE for interactive UIs.
* **LangGraph TOOL / FUNCTION / SUB_WORKFLOW nodes** — each flips one `can_compile` rejection branch.
* **Provider routing rules** (cost / latency / region) — declarative model selection enforced at executor.

### Operator-blocked (not code)

* **SMS-4: A2P 10DLC brand registration** — 2-3 weeks ops, only matters if customer-facing SMS is a real product surface. Telegram already covers internal/personal alerts.

---

## 📜 Recent session highlights

Reverse chronological. Preserves the historical record of what shipped each session — useful for stakeholder reviews and to spot patterns in our build cadence.

**BenchSummary persistence + `movate bench --baseline` shipped this session.** 30 new tests (721 unit + 3 smoke = 724 total). New `BenchRecord` + `BenchModelRow` Pydantic models in `core/models.py` (mirror `EvalRecord` shape; flat aggregates + nested per-model rows). New `bench_records` table on all 3 backends (sqlite TEXT-JSON for the models column, postgres JSONB, in-memory list) with `(tenant_id, agent, created_at DESC)` index for trend dashboards. New `save_bench` / `get_bench` / `list_benches` methods on the `StorageProvider` Protocol — all tenant-scoped with the same "cross-tenant returns None" semantics as `get_eval`. `BenchSummary.to_record(tenant_id=, judge_method=)` collapses live `ModelBenchResult` into persistable `BenchModelRow`s + computes a stable 16-char `input_hash` (sha256 of canonical-JSON input) for baseline drift detection without storing PII. CLI `movate bench` now persists by default (matches `eval`'s save-by-default behavior) and prints `bench_id` in the Rich summary footer + JSON output for downstream `--baseline` use. New `core/bench_baseline.py` with `BenchBaselineDiff` + per-model `BenchModelDelta`: matches models by provider string, lists added/removed providers, computes score/cost/latency deltas, flags regressions past `--regression-tolerance`, surfaces `input_changed` when baseline + current ran against different inputs. CLI gained `--baseline <bench_id>` + `--regression-tolerance` flags with Rich-rendered diff table + non-zero exit on regression.

**ACA role-assignment deadlock proper fix shipped this session.** Bicep refactored to use user-assigned managed identities (UAIs) created at the `main.bicep` top level, so role assignments (AcrPull on ACR, "Key Vault Secrets User" on KV) can land BEFORE the Container Apps exist. The prior system-assigned-MI design deadlocked on a cold tenant: app creation waited for revision provisioning; revision provisioning needed the roles to pull the image / read KV; roles waited for the app's principalId which only existed after revision came up. Hit live during the Tier 1 #3 walk; documented as a manual workaround in the runbook (`az role assignment create` out-of-band + `az containerapp update --revision-suffix`), then properly fixed via UAI conversion. Future operators on a fresh tenant: deploy works end-to-end without the workaround.

**Telegram alerts shipped this session — operator-wide personal notifications.** 11 new tests. New `core/notify_telegram.py` with `ConsoleTelegramBackend` + `TelegramBackend` implementing the same `NotificationDispatcher` Protocol as email + SMS, composed by `MultiDispatcher`. Async-native via `httpx.AsyncClient` (no SDK dep — Bot API is just HTTP). **Operator-wide trigger** (unlike per-job email/SMS): pings on every terminal job when `MOVATE_TELEGRAM_BOT_TOKEN` + `MOVATE_TELEGRAM_CHAT_ID` env are set. Worker's notify path widened to invoke the dispatcher on every terminal job and let each backend decide internally. Free, zero regulatory tax, cross-platform — the right shape for personal dev-loop alerts where ACS SMS would be overkill (2-3 week A2P 10DLC registration for a one-person notification channel makes no sense).

**SMS notifications via Azure Communication Services shipped this session.** 38 new tests (714 unit + 3 smoke = 717 total). Three items of Group C closed: SMS-1 (vendor decision = ACS), SMS-2 (code path: new `core/notify_sms.py` + `core/phone.py` + `MultiDispatcher` composer, `notify_sms` column on `jobs` across all 3 backends, `movate submit --notify-sms +1...`), SMS-3 (infra: new `infra/azure/modules/communication.bicep` + `enableSms`/`acsFromNumber` params). Toll-free number purchase is intentionally out-of-band; operator runbook in [docs/azure-bootstrap.md](docs/azure-bootstrap.md). **Remaining Group C items 14-15 are operator-side ops (A2P 10DLC brand registration, ~2-3 weeks).**

**`movate watch` hot-reload shipped this session.** 8 new tests. New `cli/watch.py` polls the agent's files (agent.yaml, prompt, both schemas, dataset, judge.yaml) every 0.5s via stdlib mtime checks. On change, re-runs `_validate_agent` (with lint + cost forecast). 200ms debounce for editor write-then-rename. **TDD-style feedback loop: save the prompt, see results in <1s.**

**Cost forecast shipped this session.** 10 new tests. New `core/cost_forecast.py` with `estimate_eval_cost(bundle, *, pricing) -> CostForecast | None`. Renders each case's prompt with Jinja, estimates tokens via chars/4, multiplies by the agent's model's pricing. Prints `eval cost: ~$0.045 (30 cases x ~120 in + ~1024 out tokens)` on every `movate validate` when both a dataset + pricing entry exist. **Catches "$4 surprise" bills BEFORE running the eval.**

**Prompt linter shipped this session.** 19 new tests. New `core/prompt_linter.py` with four rules: `UNDECLARED_INPUT_REF` (error — Jinja2 AST analysis catches `{{ input.X }}` refs not in the input schema), `EMPTY_PROMPT` (error), `MISSING_JSON_INSTRUCTION` (warning), `NO_OUTPUT_SCHEMA_REFERENCE` (warning), `TINY_PROMPT` (warning). Wired into `movate validate`: errors exit 2 always; warnings print but don't fail by default; `--strict` promotes warnings to errors (CI gate).

**Per-tenant monthly cost ceiling shipped this session.** 24 new tests. New `TenantBudget` Pydantic model + `tenant_budgets` table on all 3 backends. `Executor._check_tenant_budget` runs FIRST at execute() entry — zero provider cost incurred on a budget-blocked run. New `TenantBudgetExceededError` + `FailureType.TENANT_BUDGET_EXCEEDED` (no retry, no fallback — the cap is the cap). New `movate tenants set-budget | clear-budget | show | list` CLI. **Closes the runaway-cost gap that v1.0 stages 1-4 left open.**

**KEDA queue-depth worker autoscaling shipped this session.** Bicep-only change in `containerapp-worker.bicep` — replaced the CPU-utilization scale rule with a KEDA `postgresql` scaler that counts claimable jobs. Queue depth is a leading indicator (load visible before any pod's CPU rises); CPU was lagging. `queueDepthPerReplica` param tunes scale-up aggression.

**Per-API-key rate limiting shipped this session.** 16 new tests. Token-bucket (better burst tolerance than leaky-bucket) keyed on `api_key_id`. `core/rate_limit.py` with `RateLimiter` Protocol + `InProcessRateLimiter` + `NoOpRateLimiter`. Middleware integration: rate-limit AFTER successful auth (anonymous floods get 401 cheaply); `/healthz` + `/ready` bypass. Every authenticated response carries `X-RateLimit-{Limit,Remaining,Reset}` headers.

**`/ready` endpoint with deep checks shipped this session.** 3 new tests. New `GET /ready` runs storage ping; 503 + per-check failure info if anything's broken. `/healthz` stays unconditional 200 (liveness). Bicep `containerapp-api.bicep` readinessProbe flipped to `/ready` so ACA pulls broken pods out of rotation without restarting them.

**Job retry policy shipped this session — exponential backoff + dead-letter.** 23 new tests. New `JobStatus.DEAD_LETTER` for retry-exhausted jobs (distinct from `ERROR`). `core/job_retry.py` policy module + `JobRetryPolicy` dataclass (default: 3 attempts, 5s base, 3x factor, 5min cap, ±25% jitter). New `StorageProvider.requeue_job(...)` method; `claim_next_job` is retry-aware. Notifications skipped on retry path so flaky jobs don't spam ops inboxes.

**Azure onboarding tooling shipped this session — `scripts/azure-bootstrap.sh` + `movate doctor --target`.** 10 new tests. Bash script idempotently creates RG + service principal + federated OIDC credential + role assignments per env. `movate doctor --target <name>` walks `az` install → login → subscription match → RG → ACR → both Container Apps → `/healthz` with operator pointers on every red. New `docs/azure-bootstrap.md` is the 8-step end-to-end runbook.

**v1.0 stage 4 shipped this session — tenant isolation audit.** 30 new test invocations. Audit found 9 gaps in storage methods that read or mutated per-tenant rows without filtering by `tenant_id` — every single one now enforces tenant boundary in the SQL WHERE clause. New `tests/test_tenant_isolation.py` parametrized over memory + sqlite + postgres mints two tenants, populates parallel rows in every table, then sweeps every cross-tenant read path. **v1.0 is now feature-complete.**

**v1.0 stage 3 shipped this session — model policy enforcement.** 21 new tests. New `policy:` block on `movate.yaml` with three optional fields (`allowed_providers`, `deny_models`, `max_cost_per_run_usd`). Enforced at TWO layers: `movate validate` (static) and `Executor.execute()` entry (runtime — bundles loaded via `movate serve` can't bypass). Denied models short-circuit BEFORE any provider call so zero cost is incurred.

**v1.0 stage 2 shipped this session — `movate deploy` + GH Actions deploy.yml.** 23 new tests. `cli/deploy.py` wraps `az acr build` + `az containerapp update` + `/healthz` poll. Image-tag default = `movate:<version>-<git-sha-short>`; `--image-tag` override for rollbacks. `.github/workflows/deploy.yml` uses Azure federated OIDC + per-env GitHub Environments for scoped secrets + approval gates.

**Server-side email notifications shipped this session.** 14 new tests. `notify_email` column on `jobs`; `core/notify.py` with `NotificationDispatcher` Protocol + `ConsoleBackend` + `SmtpEmailBackend` (vendor-agnostic — ACS Email / SendGrid / Mailgun / SES all speak SMTP). Worker fires-and-awaits after terminal `update_job`; failure logs but never re-queues.

**Remote-runtime CLI ergonomics shipped this session.** 29 new tests. `core/user_config.py` for `~/.movate/config.yaml` (deployment targets + active pointer; bearer tokens stay in env vars). `core/client.py` with `MovateClient` httpx wrapper. Three new CLI surfaces: `movate config add-target | list-targets | use | show | remove-target`, `movate submit <agent>`, `movate jobs show | wait | list-agents`.

**v1.0 stage 1 shipped this session — Azure Bicep IaC.** Seven modular `.bicep` files orchestrated by `infra/azure/main.bicep`. Per-env SKU defaults (`dev`/`staging`/`prod`) drive Postgres tier, ACA replicas, retention. CI gained a `bicep` job running `bicep build` + `bicep lint` on every PR — no Azure subscription needed.

**Progress UI shipped this session.** 7 new tests. `cli/_progress.py` with `progress_bar()`, `spinner()`, `print_event()` helpers — all stderr-only, auto-degrade on non-TTY. `EvalEngine` / `BenchEngine` / `Worker` gained optional progress callbacks. Suppressed for `-o json` / `-o markdown` / `--mock` so automation paths stay clean.

**v0.5.0 tagged + released this session.** [GitHub Release](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.5.0) with wheel + sdist attached. README capability matrix flipped from staged → shipped for HTTP runtime / worker / Postgres. CI gained a `postgres` job using GHA's `services:` block — the parametrized storage conformance suite now runs against PG on every PR.

---

## v1.0 release status

### Group A — Close the v1.0 deploy loop ✅ all done

1. [x] **v1.0 stage 1: Bicep IaC for Azure** `[HIGH] [v1.0] [done]` — modular `infra/azure/modules/*.bicep`; CI runs `bicep build` + `bicep lint` on every PR. Operator walkthrough at [infra/azure/README.md](infra/azure/README.md).
2. [x] **v1.0 stage 2: `movate deploy` CLI + GH-Actions deploy.yml** `[HIGH] [v1.0] [done]` — `movate deploy --target <name>` wraps `az acr build` + `az containerapp update` (both API + worker) + `/healthz` poll. 23 tests in [tests/test_deploy.py](tests/test_deploy.py).
3. [x] **First Azure deployment validation** `[HIGH] [v1.0] [done]` — validated end-to-end during the last session against a real Pay-As-You-Go subscription. All 9 `movate doctor --target dev` checks green; runtime serving v0.5.0 on the deployed FQDN. Found + fixed three real Bicep bugs along the way (global-name collisions, Microsoft.App provider registration timing, ACA role-assignment deadlock — all three now have permanent fixes in `main.bicep` + the operator runbook).
4. [x] **v1.0 stage 3: Model policy enforcement** `[HIGH] [v1.0] [done]` — `policy:` block on `movate.yaml`; enforced at `movate validate` (static) + `Executor.execute()` entry (runtime). 21 tests in [tests/test_policy.py](tests/test_policy.py).
5. [x] **v1.0 stage 4: Tenant isolation audit** `[HIGH] [v1.0] [done]` — every storage method that touches per-tenant rows now requires + filters by `tenant_id` at the SQL layer (9 audit gaps closed). 30 cross-tenant fuzz tests in [tests/test_tenant_isolation.py](tests/test_tenant_isolation.py).

### Group B — Scale the worker ✅ all done

6. [x] **KEDA Postgres scaler for worker autoscaling** `[HIGH] [post-v1.0] [done]` — `queueDepthPerReplica` param (prod 10, dev 3) on KEDA `postgresql` scaler in `containerapp-worker.bicep`.
7. [x] **Job retry policy with exponential backoff + dead-letter** `[HIGH] [post-v1.0] [done]` — `core/job_retry.py` + new `JobStatus.DEAD_LETTER` + `requeue_job` storage method. 23 tests in [tests/test_job_retry.py](tests/test_job_retry.py).
8. [x] **Rate limiting per API key** `[MED] [post-v1.0] [done]` — token-bucket; default 60 req/min/key. 16 tests in [tests/test_rate_limit.py](tests/test_rate_limit.py).
9. [x] **`/ready` endpoint with deep checks** `[MED] [post-v1.0] [done]` — storage `ping()` on all 3 backends. Bicep readinessProbe flipped to `/ready` so ACA pulls broken pods out of rotation without restarting them.

### Group C — SMS notifications (~½ done; rest ops-blocked)

10. [x] **Server-side email notifications** `[MED] [post-v1.0] [done]` — `notify_email` column + `NotificationDispatcher` Protocol + Console/SMTP backends + worker hook + CLI `--notify-email`. Vendor-agnostic via SMTP.
11. [x] **SMS-1: vendor decision = Azure Communication Services** `[MED] [post-v1.0] [done]` — locked in [docs/v1.0-azure-design.md §10](docs/v1.0-azure-design.md). ACS over Twilio on Azure-native secret + RBAC integration.
12. [x] **SMS-2: code path** `[MED] [post-v1.0] [done]` — `core/notify_sms.py` with `ConsoleSmsBackend` + `AcsSmsBackend`; `core/phone.py` for E.164 validation; `MultiDispatcher` composer; `--notify-sms` CLI flag. 38 tests.
13. [x] **SMS-3: infra (Bicep ACS resource + KV secret + worker env wiring)** `[MED] [post-v1.0] [done]` — `infra/azure/modules/communication.bicep` + `enableSms`/`acsFromNumber` params. Toll-free number bought out-of-band.
14. [ ] **SMS-4: business setup (A2P 10DLC + sender ID approval)** `[BLOCKING] [post-v1.0] [2-3 WEEKS ops, not code]` — register Movate's brand + use case with The Campaign Registry (US A2P 10DLC). Cost: ~$50 brand reg + ~$10/campaign vetting. Only matters if customer-facing SMS is a real product surface; **Telegram covers internal/personal alerts already.**
15. [ ] **SMS-5: real-SMS smoke test** `[LOW] [post-v1.0] [≤0.5d, gated on SMS-4]` — submit a job with `--notify-sms <ops-phone>`, watch the SMS land.

### Group D — Personal-alert channel (new) ✅ done

16. [x] **Telegram bot alerts (operator-wide)** `[HIGH] [post-v1.0] [done]` — `core/notify_telegram.py` with `TelegramBackend` (real, via httpx) + `ConsoleTelegramBackend` (fallback). Different from email/SMS: operator-wide trigger pings on EVERY terminal job. Bicep wires the bot token via KV reference; chat_id non-secret. 5-min setup runbook. 11 tests. **Solves the personal dev-loop alert use case without 2-3 weeks of A2P 10DLC.**

### Group E — Polish (Tier B, the "what's next" backlog)

17. [ ] **More agent templates (extractor, RAG, function-caller)** `[MED] [post-v1.0] [≤1d each]` — `movate init -t <kind>` for common shapes. Template registry already exists.
18. [ ] **`movate logs <run-id> --tail`** `[MED] [v0.4] [≤1d]` — Rich timeline of stored events.
19. [ ] **Privacy: redact prompt/output spans** `[MED] [v0.4] [≤1d]` — `tracer.redact_io: true` for PII-sensitive tenants. Gates real customer onboarding.
20. [ ] **Workflow replay** `[LOW] [post-v1.0] [2-3d]` — `movate run --replay <workflow-run-id>`. Single-agent replay already covers 80% of debug cases; defer until a customer asks.
21. [ ] **HTTP streaming for `POST /run?wait=true`** `[LOW] [post-v1.0] [3-5d]` — server-sent events for interactive UIs.

---

## 1. Foundation — single agent (Phase 1 / v0.1) ✅ all done

- [x] **Repo skeleton + `pyproject.toml` + CI** `[HIGH] [v0.1] [done]` — `uv sync`, ruff, mypy strict, pytest, GH Actions.
- [x] **CLI panel structure (Typer + Rich)** `[HIGH] [v0.1] [done]` — Develop / Run & evaluate / Diagnose / Deploy & operate / Manage.
- [x] **`agent.yaml` schema (`movate/v1`)** `[HIGH] [v0.1] [done]` — Pydantic-validated; rejects floating tags, bad semver, wrong api_version.
- [x] **Loader → `AgentBundle`** `[HIGH] [v0.1] [done]` — YAML + prompt template + JSON schemas + sha256 prompt hash.
- [x] **Failure taxonomy + retry policy** `[HIGH] [v0.1] [done]` — typed errors with default rules per type.
- [x] **`BaseLLMProvider` Protocol** `[HIGH] [v0.1] [done]` — single seam; LiteLLM is implementation detail.
- [x] **`LiteLLMProvider`** `[HIGH] [v0.1] [done]` — `num_retries=0` (movate owns retries); typed exception mapping.
- [x] **`MockProvider`** `[HIGH] [v0.1] [done]` — deterministic, network-free.
- [x] **Pricing table (packaged YAML)** `[MED] [v0.1] [done]` — versioned, auditable.
- [x] **Cost-drift detection (LiteLLM vs table > 5%)** `[MED] [v0.1] [done]`.
- [x] **Budget enforcement per run** `[HIGH] [v0.1] [done]` — `max_cost_usd_per_run` aborts with `BudgetExceededError`.
- [x] **Linear executor with fallback chain** `[HIGH] [v0.1] [done]`.
- [x] **SQLite storage (runs + failures)** `[HIGH] [v0.1] [done]`.
- [x] **Stdout tracer (stderr stream)** `[HIGH] [v0.1] [done]`.
- [x] **Agent template (`movate init`-able)** `[HIGH] [v0.1] [done]`.
- [x] **`movate init` / `validate` / `show` / `run` / `doctor`** `[HIGH] [v0.1] [done]`.

---

## 2. Evals & comparison (Phase 2 / v0.2)

### Shipped

- [x] **Eval engine — exact-match + LLM-as-judge with cross-family enforcement** `[HIGH] [v0.2] [done]` — Azure↔OpenAI treated as same family.
- [x] **`movate eval` with `--gate 0.7` exit-code semantics** `[HIGH] [v0.2] [done]`.
- [x] **N runs per case + aggregation modes** `[HIGH] [v0.2] [done]` — `--runs N --gate-mode mean|min|p10`.
- [x] **Eval result persistence (sqlite `evals` table)** `[MED] [v0.2] [done]`.
- [x] **Dataset hashing + `dataset_hash` on EvalRecord** `[MED] [v0.2] [done]`.
- [x] **Judge config validation at parse time** `[MED] [v0.2] [done]`.
- [x] **judge.yaml.example in template** `[MED] [v0.2] [done]`.
- [x] **`movate bench` (multi-model compare)** `[HIGH] [v0.2] [done]` — `BenchEngine` in [src/movate/core/bench.py](src/movate/core/bench.py).
- [x] **`MockProvider` is judge-aware** `[MED] [v0.2] [done]`.
- [x] **Markdown reporter for CI annotation** `[MED→HIGH] [v0.2] [done]` — `--output markdown` on both `movate eval` and `movate bench`.
- [x] **`movate pricing` (print table)** `[LOW→MED] [v0.2] [done]`.
- [x] **Persist `BenchSummary` to sqlite** `[MED] [v0.4] [done]` — `BenchRecord` + `BenchModelRow` + `bench_records` table on all 3 backends; `save_bench` / `get_bench` / `list_benches` on the `StorageProvider` Protocol. `movate bench` persists by default.
- [x] **Bench baseline (`movate bench --baseline <bench-id>`)** `[HIGH] [v0.4] [done]` — `core/bench_baseline.py` with `BenchBaselineDiff` + per-model `BenchModelDelta`. Matches models by provider; flags regressions past `--regression-tolerance`. Same shape as `movate eval --baseline`.

### Open

- [ ] **Rubric library (3-5 standard rubrics)** `[MED] [v0.2] [≤1d]` — relevance, correctness, faithfulness, safety, tone. Imported by name from `evals/judge.yaml`. **[Tier B — what's next]**
- [ ] **`--parallel` flag for bench** `[MED] [v0.3] [≤1d]` — currently sequential; parallel respects per-provider rate limits.
- [ ] **DeepEval integration** `[LOW] [v0.5+] [1w]` — defer until RAG-grounding metrics are actually needed.
- [ ] **Ragas integration** `[LOW] [v0.5+] [1w]` — same.
- [ ] **TruLens integration** `[LOW] [v0.7+] [1w]` — same.

---

## 3. Sequential workflows (Phase 3 / v0.3)

- [x] **`workflow.yaml` Pydantic spec** `[HIGH] [v0.3] [done]` — [src/movate/core/workflow/spec.py](src/movate/core/workflow/spec.py).
- [x] **`WorkflowGraph` IR (internal)** `[HIGH] [v0.3] [done]` — [src/movate/core/workflow/ir.py](src/movate/core/workflow/ir.py): future-aware enums let v1.1's LangGraph compiler reuse the same IR without a schema break.
- [x] **Sequential compiler with strict validation** `[HIGH] [v0.3] [done]` — `validate_linear` rejects branches, joins, conditional edges, non-agent node types. 27 tests in [tests/test_workflow.py](tests/test_workflow.py).
- [x] **Workflow runner — typed `WorkflowState` plumbing** `[HIGH] [v0.3] [done]`. 6 tests in [tests/test_workflow_runner.py](tests/test_workflow_runner.py).
- [x] **Per-node `RunRecord` linked by `workflow_run_id`** `[HIGH] [v0.3] [done]`.
- [x] **Partial-failure preservation** `[HIGH] [v0.3] [done]` — runner stops at the failing node, returns the pre-merge state, marks workflow `ERROR`.
- [x] **`movate run <workflow>` extension** `[HIGH] [v0.3] [done]` — `is_workflow_path()` auto-detect.
- [x] **`movate show workflow` topology render (ASCII / Mermaid)** `[MED] [v0.3] [done]` — Mermaid `flowchart LR` block ready for PR descriptions.
- [x] **`--node-trace` flag** `[MED] [v0.3] [done]` — surfaces per-node state-delta on stdout after the run. Reconstructs from `result.runs` rather than mutating the runner.
- [x] **`workflow.yaml: runtime` field** `[MED] [v0.3] [done]` — supports `homegrown | langgraph`; compiler dispatches on it.
- [x] **Throwaway IR→LangGraph prototype** `[HIGH] [v0.3] [done]` — built, validated four constructs (linear / conditional / parallel / HITL), captured findings in [docs/langgraph-seam.md](docs/langgraph-seam.md), then promoted the linear case to production code at [src/movate/core/workflow/compilers/langgraph.py](src/movate/core/workflow/compilers/langgraph.py) and **deleted the spike**.

Conditional / parallel / HITL / loops were OUT of v0.3 by design; tracked under §7 LangGraph and largely shipped there. See [§7](#7-langgraph-swap-in--advanced-phase-7--v11).

---

## 4. Observability (Phase 4 / v0.4)

- [x] **Langfuse tracer** `[HIGH] [v0.4] [done]` — `LangfuseTracer` in [src/movate/tracing/langfuse.py](src/movate/tracing/langfuse.py); auto-selects via env. 12 tests.
- [x] **OTel tracer (OTLP exporter)** `[HIGH] [v0.4] [done]` — `OtelTracer` in [src/movate/tracing/otel.py](src/movate/tracing/otel.py); OTLP-HTTP via `BatchSpanProcessor`.
- [x] **Tracer auto-select via `MOVATE_TRACER`** `[MED] [v0.4] [done]` — `stdout | langfuse | otel | composite`.
- [x] **Composite tracer (multi-fanout)** `[MED] [v0.4] [done]` — `CompositeTracer` in [src/movate/tracing/composite.py](src/movate/tracing/composite.py). 26 tests.
- [x] **`movate trace replay <run-id>`** `[HIGH] [v0.4] [done]` — `core/replay.py` (engine) + `cli/trace.py` (rendering). 19 tests.
- [x] **Drift baseline (`movate eval --baseline <eval-id>`)** `[HIGH] [v0.4] [done]` — `core/baseline.py` + `cli/eval.py` with `--baseline`/`--regression-tolerance`. 21 tests.
- [x] **Span attributes — token-level cost breakdown** `[MED] [v0.4] [done]` — `cost_usd`, `pricing_version`, `chosen_provider`, `tokens.input/output/cached_input` mirrored onto the `agent.execute` span. Langfuse + OTel consumers filter by `pricing_version` drift without joining back to `RunRecord`.
- [ ] **`movate logs <run-id> --tail`** `[MED] [v0.4] [≤1d]` — Rich timeline of stored events. **[Tier B — what's next]**
- [ ] **Privacy: redact prompt/output spans by config** `[MED] [v0.4] [≤1d]` — `tracer.redact_io: true` for tenants with PII. **[Tier B — what's next]**
- [ ] **Cost dashboards (Langfuse-side)** `[LOW] [v0.4] [—]` — delegated to Langfuse; just confirm dashboard exists.
- [ ] **Real-time event bus** `[LOW] [post-v1.0] [—]` — defer; tracing covers v0.4 needs.

---

## 5. Server + queue (Phase 5 / v0.5)

### Shipped (per top-of-file blurbs)

- [x] **PostgresProvider** `[HIGH] [v0.5] [done]` — asyncpg pool, `FOR UPDATE SKIP LOCKED`, JSONB. Parametrized storage conformance tests run against PG on every PR via the `postgres` CI job.
- [x] **`migrations/0001_init.sql` runs on startup** `[HIGH] [v0.5] [done]` — sqlite `_MIGRATIONS` list + postgres `INIT_SQL` block. Both idempotent via `IF NOT EXISTS`.
- [x] **`movate.runtime.app` (FastAPI)** `[HIGH] [v0.5] [done]` — `/run`, `/jobs/{id}`, `/agents`, `/healthz`, `/ready`. Bearer-token auth middleware + per-API-key rate-limiter middleware.
- [x] **`movate.runtime.worker`** `[HIGH] [v0.5] [done]` — claim-next-job loop; concurrency-safe via `FOR UPDATE SKIP LOCKED`; retry-aware (skips `next_retry_at > now`).
- [x] **API key issuance + bcrypt hash (`mvt_<env>_<tenant>_<keyid>_<secret>`)** `[HIGH] [v0.5] [done]` — `core/auth.py` + `core/api_keys.py`.
- [x] **`movate auth create-key | list-keys | revoke-key`** `[HIGH] [v0.5] [done]` — `cli/auth.py`. Tenant-scoped; `--quiet` mode prints only the key for shell capture.
- [x] **Tenant isolation audit (every query filtered by `tenant_id`)** `[HIGH] [v1.0] [done]` — 9 audit gaps closed. 30 cross-tenant fuzz test invocations in [tests/test_tenant_isolation.py](tests/test_tenant_isolation.py).
- [x] **Server-side email notifications (SMTP)** `[MED] [v0.5] [done]` — `notify_email` column + `NotificationDispatcher` Protocol + Console/SMTP backends + worker hook + CLI `--notify-email`.
- [x] **`MultiDispatcher` composer** `[MED] [v0.5] [done]` — fans out terminal jobs across email + SMS + Telegram backends; each channel decides whether to fire on the given job.
- [x] **Per-tenant cost ceiling** `[HIGH] [v1.0] [done]` — `TenantBudget` + `tenant_budgets` table + `Executor._check_tenant_budget` at execute() entry. 24 tests.
- [x] **KEDA Postgres scaler** `[HIGH] [post-v1.0] [done]` — worker scales on claimable-job count, not CPU.
- [x] **Job retry policy + dead-letter** `[HIGH] [post-v1.0] [done]` — `core/job_retry.py` + `JobStatus.DEAD_LETTER`. 23 tests.
- [x] **Per-API-key rate limiting** `[MED] [post-v1.0] [done]` — token-bucket; default 60 req/min/key. 16 tests.
- [x] **`/ready` endpoint with deep checks** `[MED] [post-v1.0] [done]` — `StorageProvider.ping()` on all 3 backends. ACA readinessProbe uses it.
- [x] **Remote-runtime CLI ergonomics** `[HIGH] [v0.5] [done]` — `movate config add-target | list-targets | use | show | remove-target`, `movate submit <agent> [--target] [--wait] [--notify] [--notify-email] [--notify-sms]`, `movate jobs show | wait | list | list-agents`. 29 tests.

### Open

- [ ] **Idempotency on `/run` by `request_id`** `[HIGH] [v0.5] [≤1d]` — retry-safe; returns existing job. **[Tier B — what's next]**
- [ ] **`workflow_runs` table linking child runs** `[MED] [v0.5] [≤1d]` — partial (workflow_runs table exists); explicit parent→child run lineage still TODO.
- [ ] **Per-tenant rate limit** `[MED] [v0.5] [≤1d]` — per-API-key already done; per-tenant (across all keys) is a separate gate.
- [ ] **Prom metrics endpoint** `[MED] [v0.5] [≤1d]` — `/metrics` for jobs, runs, latency, cost.
- [ ] **Redis** `[LOW] [post-v0.5] [—]` — defer; Postgres is enough through v1.0.

---

## 6. Deploy + CI gating (Phase 6 / v1.0)

### Shipped

- [x] **Bicep: ACA + Postgres Flex + Key Vault + ACR + Log Analytics** `[HIGH] [v1.0] [done]` — modular `infra/azure/modules/*.bicep` + `main.bicep` orchestrator.
- [x] **Bicep `nameSuffix` param for globally-unique names** `[HIGH] [v1.0] [done]` — appends to KV / ACR / Postgres / ACS names to avoid global-tenant collisions. Surfaced during the Tier 1 #3 walk against a real tenant.
- [x] **Bicep UAI conversion (ACA role-assignment deadlock fix)** `[HIGH] [v1.0] [done]` — switched system-assigned MIs to user-assigned, pre-created at `main.bicep` top level. Role assignments land BEFORE the apps exist; cold-deploy chicken-and-egg eliminated.
- [x] **`scripts/azure-bootstrap.sh <env>`** `[HIGH] [v1.0] [done]` — idempotent: RG + service principal + federated OIDC credential + role assignments. Prints the GH Environment secrets to paste.
- [x] **`movate doctor --target <env>`** `[HIGH] [v1.0] [done]` — walks `az` install → login → subscription match → RG → ACR → both Container Apps → `/healthz` with operator pointers on every red.
- [x] **`movate deploy <env>`** `[HIGH] [v1.0] [done]` — wraps `az acr build` + `az containerapp update` (both apps) + `/healthz` poll. Rollback via `--skip-build --image-tag <prev>`. 23 tests in [tests/test_deploy.py](tests/test_deploy.py).
- [x] **GH Actions `eval-gate.example.yml` (block on regression)** `[HIGH] [v1.0] [done]` — `cli/eval.py` gained `--baseline-file <path>` + `--output-baseline <path>` flags. Example workflow + docs at [docs/ci-eval-gate.md](docs/ci-eval-gate.md). 6 tests.
- [x] **GH Actions `deploy.yml` (release branch → ACA)** `[HIGH] [v1.0] [done]` — federated OIDC + per-env GitHub Environments + approval rules.
- [x] **Model policy enforcement** `[HIGH] [v1.0] [done]` — `policy:` block on `movate.yaml`; enforced at `movate validate` + `Executor.execute()` entry. 21 tests.
- [x] **Per-tenant cost ceiling enforcement** `[HIGH] [v1.0] [done]` — `TenantBudget` + `tenant_budgets` table. 24 tests.
- [x] **First Azure deployment validation** `[HIGH] [v1.0] [done]` — see Group A #3 above. End-to-end validated against a real Azure subscription.

### Open

- [ ] **GH Actions `validate.yml`** `[HIGH] [v1.0] [≤1d]` — schema + topology validation on every PR. **[Tier B — what's next]**
- [ ] **GH Actions `security.yml`** `[MED] [v1.0] [≤1d]` — dependency + secret scan. **[Tier B — what's next]**
- [ ] **Promotion semantics dev → staging → prod** `[MED] [v1.0] [≤1d]` — env profiles + revision tags + promote-this-revision flow.
- [ ] **Deployment health check + rollback** `[MED] [v1.0] [≤1d]` — partial (`/healthz` poll exists in `movate deploy`); add automatic ACA revision pinning on failure.
- [ ] **Multi-region** `[—] [post-v1.0] [—]` — out.
- [ ] **Blue/green** `[LOW] [post-v1.0] [—]` — ACA revisions cover most of this.

---

## 7. LangGraph swap-in + advanced (Phase 7 / v1.1+)

### Shipped (the v1.1 determinism bundle landed earlier than the original plan)

- [x] **`workflow/compilers/langgraph.py`** `[HIGH] [v1.1] [done]` — alternative compiler from `WorkflowGraph` IR; gated by `runtime: langgraph` on workflow.yaml. Linear AGENT case shipped in v1.0; conditional / parallel / HITL extensions all landed on top.
- [x] **Conditional edges** `[HIGH] [v1.1] [done]` — `edges: [{from: A, to: B, kind: conditional, when: "$.score > 0.7"}]`. Hand-rolled JSONPath-like DSL at [src/movate/core/workflow/condition_dsl.py](src/movate/core/workflow/condition_dsl.py). 47 tests in [tests/test_workflow_conditional.py](tests/test_workflow_conditional.py).
- [x] **Parallel fan-out + state-schema reducer annotations** `[HIGH] [v1.1] [done]` — `kind: parallel_fan_out / parallel_fan_in` edges; state-schema gains `x-movate-reducer` extension with six named reducers (append/union/max/min/last/merge). 28 tests in [tests/test_workflow_parallel.py](tests/test_workflow_parallel.py).
- [x] **HITL nodes (`type: human`)** `[HIGH] [v1.1] [done]` — pause graph via LangGraph's `interrupt_before`; resume via API. `NodeSpec.resume_payload_schema` required on HUMAN nodes at YAML parse time. 7 end-to-end tests in [tests/test_workflow_hitl.py](tests/test_workflow_hitl.py).
- [x] **Checkpointing (LangGraph-native; tenant-namespaced)** `[HIGH] [v1.1] [done]` — `TenantNamespacedCheckpointer` wraps `BaseCheckpointSaver`; prefixes every `thread_id` with `tenant_id::`. Memory + sqlite + postgres backends. 15 tests in [tests/test_workflow_checkpointer.py](tests/test_workflow_checkpointer.py).
- [x] **Resume API + body** `[HIGH] [v1.1] [done]` — `resume_workflow(run_id, *, payload, graph, executor, storage, tenant_id) -> WorkflowResult`. `aupdate_state(payload, as_node=record.pause_at)` advances past the interrupt; `ainvoke(None, config)` continues from the merged checkpoint.
- [x] **Tool registry (`movate.tools`)** `[HIGH] [v1.1] [done]` — `@tool` decorator → JSON schema → injected into prompt + tool-calling loop. Required `side_effects: true|false` flag on each tool.
- [x] **Branch-level failure invalidation** `[HIGH] [v1.1] [done]` — when one branch of a conditional / parallel topology fails, sibling branches that completed stay valid in the checkpoint.

### Open

- [ ] **Built-in tools — `kb_search`, `http_get`, `sql_query`** `[MED] [v1.1] [3-5d]` — high reuse across customer engagements. Each is ~1d.
- [ ] **Skill packs (composable rule + prompt bundles)** `[MED] [v1.2] [1w]` — `grounding`, `citation_enforcement`, `pii_redaction`.
- [ ] **Provider routing rules (cost / latency / region)** `[HIGH] [v1.1] [3-5d]` — `models/routing.yaml`; declarative, enforced at executor.
- [ ] **Memory provider** `[MED] [v1.2] [1w]` — short-term + long-term; sqlite + Postgres backends.
- [ ] **Retrieval provider (pgvector)** `[HIGH] [v1.2] [1w]` — embed + ANN; canonical "grounding" implementation.
- [ ] **RBAC** `[MED] [v1.2] [1w]` — role-keyed scopes on `mvt_*` keys.
- [ ] **Azure AD SSO** `[MED] [v1.3] [1w]`.
- [ ] **LangGraph TOOL / FUNCTION / SUB_WORKFLOW node compilation** `[MED] [v1.1] [≤1d each]` — `can_compile` currently rejects these with operator-facing error pointers. Each follow-up flips one rejection branch.
- [ ] **Cached-LLM-response replay mode (`--replay-cached`)** `[MED] [v1.1] [2-3d]` — content-addressed cache on `(prompt_hash, model.provider, model.params)` → `response.data`. Useful for debugging downstream nodes deterministically + cost-free CI evals.

---

## 8. Cross-cutting / developer experience (HIGH leverage globally)

### Shipped

- [x] **Shell tab-completion (`movate --install-completion`)** `[HIGH] [v0.1] [done]` — wired by Typer.
- [x] **`.env` auto-load** `[HIGH] [v0.1] [done]` — wired via python-dotenv.
- [x] **`movate.testing` fixtures package** `[HIGH] [v0.2] [done]` — public surface in [src/movate/testing/](src/movate/testing/). 14 conformance tests.
- [x] **`movate watch <agent>` (hot-reload on YAML change)** `[MED] [v0.2] [done]` — stdlib polling, 200ms debounce. 8 tests.
- [x] **Templates beyond `agent_init` — `faq`, `summarizer`, `classifier`** `[HIGH] [v0.2] [done]` — registry at [src/movate/templates/__init__.py](src/movate/templates/__init__.py). 21 tests.
- [x] **Live-API smoke tests (env-gated)** `[HIGH] [v0.2] [done]` — `pytest -m smoke` against real providers. 3 tests.
- [x] **`movate run --replay <run-id>`** `[HIGH] [v0.4] [done]` — single-agent replay. 14 tests in [tests/test_run_replay.py](tests/test_run_replay.py).
- [x] **Prompt linter** `[MED] [v0.2] [done]` — 4 rules. 19 tests in [tests/test_prompt_linter.py](tests/test_prompt_linter.py).
- [x] **Cost forecast on `validate`** `[MED] [v0.2] [done]` — 10 tests in [tests/test_cost_forecast.py](tests/test_cost_forecast.py).

### Open

- [ ] **More agent templates (extractor, RAG, function-caller)** `[MED] [post-v1.0] [≤1d each]` — see §1 / "What's next". Customer-visible.
- [ ] **Workflow templates — `returns-processing`, `triage-then-respond`** `[MED] [v0.3] [≤1d]` — workflow equivalent of agent templates.
- [ ] **VS Code launch configs (debug a single agent run)** `[MED] [v0.2] [≤2h]`.
- [ ] **`movate diff <agent-a> <agent-b>`** `[MED] [v0.2] [≤1d]` — show prompt-hash, model, schema deltas. **[Tier B — what's next]**
- [ ] **`--dry-run` on `run`** `[MED] [v0.2] [≤2h]` — render prompt, show what *would* be sent, exit 0. **[Tier B — what's next]**
- [ ] **Structured logging (structlog) everywhere** `[MED] [v0.4] [≤1d]` — already a dep; standardize on it.
- [ ] **Docs site (mkdocs) — internal** `[LOW] [v0.6] [1w]` — defer; per-user decision is internal-only, README + `--help` is enough through v0.5.

---

## 🚫 Permanently out of scope

These are explicitly NOT going to ship. Listed here so they don't keep getting re-proposed.

| Item | Why not |
|---|---|
| **Visual workflow editor** | Out per PRD §2. Code-as-config is the design choice. |
| **Marketplace / registry UI** | Out per PRD §2. Internal-only platform; no marketplace need. |
| **Autonomous self-modifying agents** | Out per PRD §2. Out of scope for v1 / v2. |

---

## How to use this file

1. Pick the highest item from `## ⚡ What's next` that isn't blocked.
2. Move it to `[ip]` (in-progress) while you work.
3. On merge, flip to `[x]` with the actual completion date in the commit message — the file itself stays clean.
4. Re-rank the "What's next" list every two weeks. Leverage shifts as context changes.
5. After each session, append a one-paragraph blurb to `## 📜 Recent session highlights` summarizing what shipped — keeps the historical record without bloating the working backlog.
