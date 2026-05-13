# ADR 007 — GitHub integration for agent version control

**Status:** Proposed
**Date:** 2026-05-13
**Deciders:** Engineering + Mova iO product (Angular front-end consumer)
**Context window:** v0.7 (Friday 2026-05-15 deliverable + follow-up)
**Supersedes:** N/A
**Related:** [ADR 003 — Teams integration](003-teams-integration.md) for the
"agents are bundles" precedent; BACKLOG Group G items 76-81

---

## Decision

When the Mova iO Angular front end creates or edits an agent through
MDK's HTTP API, the resulting **canonical bundle** is version-
controlled in GitHub. We commit + push on **explicit user action**
(a "Publish" button in the UI) rather than auto-committing every save,
write to a **single per-tenant repo** (`mova-io-agents-<tenant>`) with
one directory per agent, and authenticate using a **GitHub App**
installed once per tenant org. Push lands on `main` directly; branch
protection rules guard the cases that need review.

In one sentence: **"every agent the Mova iO UI persists is a Git
commit in a single per-tenant repo, written by the MDK runtime via a
GitHub App on an explicit publish action."**

This ADR exists because the Mova iO v1 deliverable bundles "agent
creation" with "version control" — meaning agents created via the
Angular UI must show up in Git history from the first save. Without
this decision codified, we'd end up retrofitting auth + repo
strategy + commit cadence after endpoints ship, which is the most
expensive kind of rework.

## Context

By Friday 2026-05-15, the Mova iO Angular app needs to:

1. Let a user create an agent (via `POST /api/v1/agents`, item 76)
2. Persist it to the canonical folder layout MDK already uses
3. **Push that bundle to GitHub so it's version-controlled**
4. Show "View on GitHub" + commit history in the agent profile UI
5. Let the user roll back to an earlier version

Today's state: MDK writes agents to the local filesystem (`./agents/<name>/`).
The CLI workflow assumes the operator runs `git add . && git commit && git push`
themselves — which is fine for engineers but is the exact friction
the Mova iO UI is trying to eliminate.

The decisions below resolve the four design forks the team had been
hand-waving over:

* **Repo strategy** — one repo per agent? per tenant? per environment?
* **Auth** — Personal Access Token, GitHub App, OAuth-on-behalf-of-user?
* **Commit cadence** — every save? explicit publish? hybrid?
* **Push semantics** — direct push to `main`? PR-based review?

## Decision drivers

| Driver | Weight |
|---|---|
| **Friday demo viability** — the design needs to be implementable in ~6h of code on Friday | HIGH |
| **Multi-tenant cleanliness** — one tenant's agents must never leak into another's repo or visible to another's installation | HIGH |
| **Engineer-friendliness** — when an engineer clones the repo to debug, the layout should match `mdk init`'s output exactly | HIGH |
| **No-surprises commit history** — clicking "save" should not produce 50 "wip" commits; commits should be auditable | MED |
| **Recoverable mistakes** — rolling back to yesterday's version should not lose downstream work | MED |
| **Self-service onboarding** — a new Mova iO tenant should be able to provision their repo in under 5 minutes | MED |

## Architecture

```
┌─────────────────────────┐
│  Mova iO Angular UI     │  user clicks "Publish"
└───────────┬─────────────┘
            │ POST /api/v1/agents/{name}/publish
            │ {commit_message, author}
            ▼
┌─────────────────────────┐
│   MDK runtime           │
│   (BFF-authenticated)   │
└───────────┬─────────────┘
            │ 1. Read canonical bundle from local fs
            │ 2. Load GitHub App installation token (cached, 1h TTL)
            │ 3. PyGithub or raw REST: clone shallow, write bundle, commit, push
            ▼
┌─────────────────────────┐
│  github.com             │
│  mova-io-agents-<tenant>│
│    └─ <agent-name>/     │
│         ├ agent.yaml    │
│         ├ prompt.md     │
│         ├ schema/...    │
│         └ evals/...     │
└─────────────────────────┘
```

### Decision 1: One repo per tenant, one directory per agent

`mova-io-agents-<tenant_slug>` with each agent as a sibling directory:

```
mova-io-agents-acme/
├── README.md
├── faq-bot/
│   ├── agent.yaml
│   ├── prompt.md
│   ├── schema/
│   │   ├── input.json
│   │   └── output.json
│   └── evals/dataset.jsonl
├── support-triage/
│   ├── agent.yaml
│   ├── prompt.md
│   └── ...
└── ...
```

**Why not one repo per agent:** GitHub orgs hit a soft repo-count
ceiling around 1000 and the UI gets unwieldy past a few dozen. A tenant
with 200 agents = 200 repos is operator hell. Per-agent also breaks
cross-agent refactors (extracting a shared prompt fragment becomes a
multi-PR dance).

**Why not one repo across tenants:** Multi-tenant isolation. A leaked
read token on `mova-io-agents-shared` exposes every tenant's prompts.
Per-tenant scopes the blast radius.

**Why not per-environment (dev/staging/prod):** MDK's existing `policy:`
block in `movate.yaml` + Bicep's per-env deploys already handle
promotion. A separate `mova-io-agents-acme-prod` repo would duplicate
content and force operators to learn two branching models. Use git
branches (`main` = staging-ready, `prod` = released) inside the single
repo when prod gating becomes a real need.

### Decision 2: GitHub App auth (not PAT, not OAuth-on-behalf-of-user)

A single MDK-published GitHub App that each tenant org installs once.
Installation grants the App scoped access to one repo
(`mova-io-agents-<tenant>`). The App holds:

* A private key (JWT-signed installation tokens, valid 1h)
* `contents: write`, `metadata: read` permissions — nothing else
* Per-installation scope (App can ONLY see repos the tenant explicitly
  installed it on)

**Why not Personal Access Token:** PATs are user-scoped, not org-scoped.
The MDK runtime would impersonate whichever engineer's PAT was stored,
which obliterates audit trails ("`jeremy.yu` published 4000 commits at
3am" — uh, no he didn't). Rotation is also manual and pain-prone.

**Why not OAuth-on-behalf-of-user:** Requires browser redirect through
the Mova iO Angular flow. Hour 1 of debugging an OAuth refresh-token
issue eats every hour we saved by not picking GitHub App. Also,
on-behalf-of-user means MDK can't push when no user is logged in —
breaks scheduled re-publishes, eval-triggered auto-commits.

**Why not GitHub Actions / workflow tokens:** Those only exist inside
the GHA runtime context. MDK is a long-running service; no GHA runner
is involved.

### Decision 3: Explicit publish action, not auto-commit on save

The Angular UI's "Save" button updates the canonical bundle in the MDK
filesystem (item 76). The Angular UI's separate "Publish" button calls
`POST /api/v1/agents/{name}/publish` (item 78) to commit + push.

**Why not auto-commit-on-save:** Two reasons.
1. Every prompt edit becomes a commit. A 10-minute prompt-tuning
   session produces 50 noisy commits — git log becomes useless for
   "what real changes shipped today?"
2. Half-saved agents (failing validation, mid-edit) would commit.
   Publish gates on `mdk validate` passing — broken agents never land
   in main.

**Why not hybrid (commit on save, push on publish):** Adds a second
mental model. Either it's in git history or it isn't; the "kinda in git
but not visible to teammates" state is confusing.

### Decision 4: Direct push to `main`, branch protection for the agents that need it

Default: `POST /publish` pushes a new commit directly to `main`. Fast
iteration, no PR ceremony.

Where review matters (e.g. an agent operates on PII or makes external
API calls), the tenant adds branch protection to specific paths:

```yaml
# Per-tenant config (item 81 sets this up)
github:
  repo: mova-io-agents-acme
  branch_protection:
    main:
      require_pr:
        - "payments-*/*"      # any agent with this name pattern
        - "*/schema/*.json"   # any I/O schema change
      required_reviewers: 1
```

When `POST /publish` hits a protected path, the runtime creates a
branch (`publish/<agent>/<timestamp>`), commits there, opens a PR, and
returns `{pr_url, status: pending_review}` to the UI instead of a
direct commit SHA.

**Why not always PR-based:** 90% of agent edits are low-stakes prompt
tweaks. Forcing a PR for every change is the wrong default — drives
people back to the CLI (which doesn't go through PRs). PR-on-protected-
paths gives the safety where it matters without taxing the common case.

**Why not require PR for all changes from the Angular UI but allow
direct from CLI:** Inconsistent — the agent's git history then depends
on which surface published it. Confusing for engineers debugging
production.

## What goes where

| Concern | Location |
|---|---|
| **Bundle persistence** | MDK local filesystem under the runtime's `--agents-path` (default `./agents/`); items 55, 76 handle this |
| **GitHub credentials** | Tenant-scoped config in `~/.mdk/config.yaml` under `github:`; item 81 (`mdk github bootstrap`) writes it |
| **Push logic** | New module `src/movate/integrations/github.py` — pure functions (clone, write tree, commit, push); injects an HTTP client for tests |
| **Publish endpoint** | `POST /api/v1/agents/{name}/publish` (item 78) |
| **History endpoint** | `GET /api/v1/agents/{name}/history` (item 79) — reads via GitHub API, NOT git CLI |
| **Revert endpoint** | `POST /api/v1/agents/{name}/revert?to_sha=<sha>` (item 80) — reads bundle at sha, writes to local fs, leaves "next publish" to the user |
| **Bootstrap CLI** | `mdk github bootstrap` (item 81) — creates the per-tenant repo + writes config |

## Auth model

* **GitHub App** registered at the MDK product level (one App for all
  tenants).
* **Per-tenant installation** of the App grants scoped access to
  `mova-io-agents-<tenant>`.
* MDK runtime caches a 1-hour installation token per tenant; refreshes
  via the App's private key on expiry.
* Mova iO BFF forwards the user's tenant identity (header `X-Mdk-Tenant`
  in a future v0.8; for v0.7 the fleet key's own tenant scope applies).

**Secrets stored in MDK runtime's Key Vault:**

| Secret name | Purpose |
|---|---|
| `github-app-private-key` | RSA private key for the App (PEM) |
| `github-app-id` | The App's numeric ID |
| `github-app-installation-{tenant}` | Per-tenant installation ID (set by item 81) |

`mdk doctor` gains a row checking GitHub App credentials are loadable.

## Phasing

**Friday 2026-05-15 (v1 minimum):**

* ADR 007 written, reviewed, status flipped to Accepted (this doc).
* Item 78 (`POST /publish`) shipped, validated against ONE tenant's
  repo (the demo tenant).
* Item 79 (`GET /history`) shipped.
* Item 80 (revert) — stretch, ship if time.
* Item 81 (`mdk github bootstrap`) — stretch; for v1 the demo tenant's
  repo + App installation are provisioned manually.

**Next sprint (v0.7 polish):**

* Item 81 — automated bootstrap CLI
* Branch-protection-path enforcement (decision 4 advanced mode)
* Multi-tenant token caching with a TTL store (replace in-memory cache
  for multi-replica deploys)
* `mdk doctor` row for GitHub App credentials

**v0.8 (per-user attribution):**

* `X-Mdk-Tenant` header support on MDK runtime
* PR-based publish with the SSO user as the PR author (today the
  GitHub App is the author for every commit; the user's name lives in
  the commit message)

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Token theft → cross-tenant access** | App installations are per-tenant; a leaked installation token only opens that tenant's repo. Rotate App private key on incident (24h max blast radius). |
| **Rate limiting** (5000 req/h per installation) | Cache reads aggressively; batch writes (one commit per publish, not one per file); the typical Friday demo will do <50 publishes/h. |
| **Repo grows unboundedly with eval dataset commits** | `evals/dataset.jsonl` is included in publish. Caveat in v0.7. For v0.8: gitignore datasets > 5MB, store them in blob storage + reference by URL. |
| **Dev/staging/prod confusion in one repo** | Use branches (`main` / `prod`) inside the per-tenant repo when prod gating becomes real. Don't proliferate repos. |
| **An engineer pushes directly to the repo, bypassing MDK** | Allowed by design — the repo IS the source of truth. Next MDK pull-on-read reconciles. Pull semantics: item 82 (post-v1) `POST /api/v1/agents/{name}/pull` to surface upstream changes in the runtime fs. |
| **GitHub outage** | `POST /publish` returns 503 with `{retry_after_seconds}`; the canonical bundle is already saved locally so no data loss. UI surfaces "Publish queued; retrying in 30s." |

## Open questions

1. **Should we support self-hosted GitHub Enterprise Server?** Some
   enterprise customers won't accept github.com. For v0.7 we say
   "github.com only; GHES support gated on a real customer asking."
2. **Eval dataset versioning** — datasets often outweigh code. Should
   they live in Git LFS, blob storage, or a separate registry? Defer
   to v0.8 ADR after the publish endpoint ships and we see real sizes.
3. **Skills / contexts / prompts cross-agent reuse** — these can be
   shared across agents in the same tenant repo. Do they get their own
   top-level directory (`mova-io-agents-acme/skills/`,
   `mova-io-agents-acme/contexts/`) or stay per-agent? **Recommend
   per-tenant top-level directories**, mirroring how `mdk init`
   structures a project. Defer formalization to v0.8.
4. **Commit-message format** — `feat(faq-bot): tighten the system
   prompt` (conventional commits) vs free-form. Pick a default the
   Angular UI prefills, let users override. v0.7: prefill with
   `Update <agent-name>`; users can customize.

## Why we're confident this is the right shape

Three independent reasons:

1. **GitHub Apps are the documented, hardened pattern** for service-
   to-service authentication. Every major dev-tools company (Vercel,
   Netlify, Linear) uses this exact shape. Not novel; not risky.
2. **Per-tenant repos match how Mova iO product team already thinks
   about tenancy** — every other piece of tenant data (KV secrets,
   Postgres rows, ACR images) is per-tenant; agents-in-Git should be
   too.
3. **Explicit publish matches engineer mental models** — every dev on
   the team uses Git daily and intuitively expects "save ≠ push." The
   Angular UI's button label "Publish" maps cleanly to "this is going
   in the audit trail now."

## Appendix A — GitHub App manifest sketch

```yaml
# Submitted via https://github.com/settings/apps/new (one-time, MDK product side)
name: Mova iO MDK
description: |
  Movate Development Kit (MDK) GitHub integration. Lets the Mova iO
  Angular front end persist agent definitions to your org's
  mova-io-agents-<tenant> repo on publish.
homepage_url: https://movate.com/mova-io
callback_url: https://api.mova-io.movate.com/oauth/github/callback
webhook:
  url: https://api.mova-io.movate.com/webhooks/github
  events: [push, pull_request]   # for v0.8 pull-from-git
permissions:
  contents: write
  metadata: read
  pull_requests: write   # for protected-path PR flow (decision 4)
```

## Appendix B — Config schema

`~/.mdk/config.yaml` additions:

```yaml
github:
  app_id: 123456                                 # the MDK GitHub App's ID
  installation_id: 78901234                      # this tenant's installation
  private_key_kv_secret: github-app-private-key  # KV ref, never inline
  repo: mova-io-agents-acme                      # per-tenant repo name
  default_branch: main
  commit_author:
    name: Mova iO
    email: noreply@mova-io.movate.com            # appears as commit author
  branch_protection:                             # v0.8 advanced mode
    main:
      require_pr:
        - "payments-*/*"
        - "*/schema/*.json"
      required_reviewers: 1
```

`mdk github bootstrap` (item 81) writes this block interactively.
