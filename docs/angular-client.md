# Angular client for the MDK runtime

How the Mova iO Angular web app talks to the MDK runtime over HTTP.

> **Status:** v0.7 alpha. The runtime exposes a stable OpenAPI 3.1
> spec at `/openapi.json` plus interactive docs at `/docs` (Swagger
> UI) and `/redoc` (ReDoc). The Angular team auto-generates a
> TypeScript client from the spec — **no hand-written DTOs.**

## The contract

* **Spec:** OpenAPI 3.1 emitted at `GET /openapi.json` by FastAPI.
  Currently ~7 paths and ~16 schemas; growing to ~21 paths by end of
  Friday 2026-05-15 per BACKLOG Group G.
* **Versioning:** new resource endpoints land under `/api/v1/<resource>`.
  The existing `/healthz`, `/ready`, `/agents`, `/run`, `/jobs/*`,
  `/runs/*` stay unversioned for back-compat (these were shipped before
  the versioning policy was set). Breaking changes bump to `/api/v2/`;
  additive changes (new endpoints, new optional fields, new enum values
  in non-discriminator positions) don't.
* **Auth:** Bearer token in the `Authorization` header. For v0.7 alpha
  the Angular app uses a **single fleet API key behind a
  backend-for-frontend proxy**; per-user SSO + scoped keys lands later
  (see BACKLOG item 53).
* **CORS:** Configured per environment via `MDK_CORS_ALLOWED_ORIGINS`
  (comma-separated). Dev permissive (`*`); staging + prod locked to
  the Mova iO web app's hostname.

## Generating the TypeScript client

### One-time setup (in the Angular repo, NOT in mdk-cli)

```bash
npm install --save-dev @openapitools/openapi-generator-cli
```

Pick **one** generator — recommendations:

| Generator | Pros | Cons |
|---|---|---|
| `ng-openapi-gen` | Angular-native, builds RxJS-flavored services, supports HttpClient | Less active maintenance |
| `@openapitools/openapi-generator-cli` (typescript-angular) | Larger community, more options, broader update cadence | Generates more boilerplate |
| `openapi-typescript-codegen` | Smallest output, fetch-based | Not Angular-specific; need to wrap in services |

The first two integrate with Angular's `HttpClient` out of the box.
Pick `ng-openapi-gen` for the smaller, Angular-tailored client unless
you've already standardized on `openapi-generator-cli` elsewhere.

### Regenerate the client on every MDK version bump

Add to the Angular repo's `package.json`:

```json
{
  "scripts": {
    "client:gen": "openapi-generator-cli generate \
        -i http://localhost:8000/openapi.json \
        -g typescript-angular \
        -o src/app/api-client \
        --additional-properties=ngVersion=18,supportsES6=true,withInterfaces=true"
  }
}
```

Then `npm run client:gen` against a running MDK runtime. CI in the
Angular repo can call this against a deployed staging URL on each PR
to catch drift between the front-end's expected contract and the
runtime's actual one.

### Hosted OpenAPI spec for CI

Run `mdk serve` (or hit a deployed runtime) and grab the spec:

```bash
curl -sS http://localhost:8000/openapi.json > openapi.json
```

In CI, source the spec from the deployed staging runtime to keep
generation hermetic + reproducible:

```bash
curl -sS https://movate-staging-api.eastus2.azurecontainerapps.io/openapi.json > openapi.json
```

## Calling the API from Angular

After client gen, the auto-built services live under
`src/app/api-client/api/` (one TS file per OpenAPI `tag` in the spec).
Example usage in an Angular service:

```typescript
import { Injectable } from '@angular/core';
import { AgentsService, EvalsService } from '../api-client';

@Injectable({ providedIn: 'root' })
export class MdkClient {
  constructor(
    private agents: AgentsService,
    private evals: EvalsService,
  ) {}

  listAgents(role?: string) {
    return this.agents.getAgents({ role });
  }

  kickoffEval(agentName: string, gate = 0.7) {
    return this.evals.postAgentsAgentNameEvals(agentName, { gate });
  }
}
```

The HTTP interceptor that adds the bearer token is the Angular team's
responsibility — the generated client just takes a `basePath` +
`accessToken` provider at module import time:

```typescript
ApiModule.forRoot(() => new Configuration({
  basePath: environment.mdkBaseUrl,
  accessToken: () => localStorage.getItem('mdk_token') ?? '',
}))
```

## Local dev — point Angular at a local MDK runtime

```bash
# Terminal 1: run MDK
mdk serve --rate-limit-per-minute 0   # disable rate limit for local dev

# Terminal 2: Angular dev server
MDK_CORS_ALLOWED_ORIGINS="http://localhost:4200" mdk serve
ng serve --proxy-config proxy.conf.json
```

`proxy.conf.json`:

```json
{
  "/api": {
    "target": "http://localhost:8000",
    "secure": false,
    "changeOrigin": true
  }
}
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser console: `CORS policy: No 'Access-Control-Allow-Origin' header` | `MDK_CORS_ALLOWED_ORIGINS` doesn't include the Angular host | Add the host (e.g. `http://localhost:4200`) to the env var, restart `mdk serve` |
| `401 Unauthorized` on every call | Bearer token missing / expired | Confirm `Authorization: Bearer mvt_live_...` header is set; mint a fresh key with `mdk auth create-key` |
| `429 Too Many Requests` | Rate limit hit | `X-RateLimit-Remaining: 0` header confirms; raise limit via `--rate-limit-per-minute` or wait the `Retry-After` seconds |
| Generated client missing a new endpoint | OpenAPI spec served from an older runtime build | Re-deploy or `npm run client:gen` against the latest |
| `openapi-generator-cli` complains about `nullable` | OpenAPI 3.1 vs 3.0 quirks | Use `--openapi-normalizer REF_AS_PARENT_IN_ALLOF=true` or pin to a recent generator version (≥7.0) |

## Auth model — fleet key + backend-for-frontend (BFF) proxy

**Decision (locked 2026-05-13 for v0.7 alpha):** the Mova iO Angular
app does NOT hold an MDK API key directly. Instead it talks to its
own BFF (a thin Node.js / .NET / Python proxy in the same domain as
the Angular app) which holds a single elevated fleet key (`mvt_live_…`)
and forwards browser requests to MDK with the bearer header attached
server-side.

**Why this shape:**

* The Angular app cannot safely store an MDK API key in `localStorage`
  — anyone with XSS access on the page lifts the key, and the key can
  authorize cross-tenant operations.
* A BFF is a 50-line proxy that solves the auth-token-handling problem
  cleanly without a multi-week SSO integration.
* MDK's tenant isolation already filters every query at the SQL layer
  by `tenant_id`, so the fleet key + BFF can scope per-request via a
  forwarded user identity (a future `X-Mdk-Tenant` header the BFF
  sets after its own SSO check).

**What the Mova iO BFF does:**

1. Validates the Angular session (the BFF is the SSO consumer, not
   MDK).
2. Looks up which MDK tenant the session belongs to.
3. Forwards `<Angular request>` to MDK with:
   * `Authorization: Bearer mvt_live_<fleet_key>` (fleet identity)
   * `X-Mdk-Tenant: <tenant_id>` (deferred to v0.8 — for now the
     fleet key's own tenant scope applies)
4. Passes the response back to Angular unchanged.

**Follow-up to land before public Teams catalog or external customers:**

* Per-user SSO with scoped per-user API keys minted server-side
* `X-Mdk-Tenant` header support on the MDK side so the fleet key
  caller can act as a specific tenant for the duration of one request
* Audit log entries that attribute every action to the SSO identity,
  not just the fleet key

These are tracked under BACKLOG item 53 (Group G) and item 26
(Teams hardening) — both required before any production rollout.

## Pagination + filter conventions (BACKLOG item 54)

All list endpoints under `/api/v1/*` follow the same envelope:

```jsonc
GET /api/v1/agents?role=support-triage&cursor=eyJpZCI6...&limit=50
→ 200 OK
{
  "items": [ { ... }, { ... } ],
  "next_cursor": "eyJpZCI6IjEyMyJ9",   // null when there's no next page
  "total_estimate": 142                 // approximate; cheaper than COUNT(*)
}
```

**Pagination:** cursor-based, opaque. The cursor is a base64'd
JSON token the server interprets (today: just the last row's id).
Clients should NOT parse it — pass it back verbatim. `limit` defaults
to 50, capped at 100.

**Filtering:** repeated query params. To filter by multiple statuses:
`?status=queued&status=running` — NOT `?status=queued,running`. Angular's
`HttpParams.appendAll({ status: ['queued', 'running'] })` produces the
repeated form natively.

**Sorting:** `?sort=-created_at` (prefix `-` for descending). Default
sort is `-created_at` for every collection endpoint. Multi-field sort
not supported in v0.7; defer until a customer asks.

**Why opaque cursors over offset/limit:** offset pagination produces
duplicate or skipped rows when items are inserted between page fetches
(common during live eval runs), and gets slow on deep paginations.
Cursor pagination is stable.

## Related backlog items

* [Group G in BACKLOG.md](../BACKLOG.md#group-g--backend-api-for-mova-io-angular-front-end-friday-2026-05-15-deliverable) — full endpoint catalog
* Item 50 — this doc + the OpenAPI verification path
* Item 51 — CORS middleware (shipped)
* Item 52 — `/api/v1/` prefix routing scaffold (shipped — empty router mounted, populated as G-MUST items land)
* Item 53 — auth model (fleet key + BFF proxy locked in for v0.7; per-user SSO deferred)
* Item 54 — pagination + filter conventions (this section)
