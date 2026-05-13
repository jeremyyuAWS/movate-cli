# MDK curl wrappers for Deva

Pre-baked shell scripts that hit every v1 endpoint Mova iO needs to
connect to MDK. **You paste the bearer once into `.env`; the scripts
do everything else.** No header copy-pasting, no token-juggling, no
`-H "Authorization: ..."` to remember.

## One-time setup (30 seconds)

```bash
cd scripts/deva-curl
cp .env.example .env
$EDITOR .env                  # paste MDK_TOKEN (you'll get this from Jeremy)
```

That's it. The token lives in `.env` (gitignored). Every script
auto-sources it.

## The flow

```bash
./01-health.sh                                       # confirm runtime is alive
./02-create-agent.sh "Hello Bot"                     # create from wizard JSON
./03-list-agents.sh                                  # see it in the registry
./04-get-agent.sh hello-bot                          # full profile
./05-validate-agent.sh hello-bot                     # shippability gate
./06-run-agent.sh hello-bot '{"input": "ping"}'      # queue a run → get job_id
./07-job-status.sh <job_id>                          # poll until terminal
./08-list-jobs.sh hello-bot                          # recent runs
./09-run-eval.sh hello-bot                           # kick off mock eval
./10-eval-scorecard.sh <eval_id>                     # scorecard
./11-eval-history.sh hello-bot                       # eval list
./12-trace.sh <run_id>                               # observability timeline
```

## Mapping to your wizard's four verbs

| Verb | Script(s) |
|---|---|
| **Create agent** | `02-create-agent.sh` (from-wizard JSON), `04-get-agent.sh`, `05-validate-agent.sh` |
| **Poll / Run** | `06-run-agent.sh` + `07-job-status.sh`, `08-list-jobs.sh` |
| **Eval** | `09-run-eval.sh` + `10-eval-scorecard.sh`, `11-eval-history.sh` |
| **Observability** | `12-trace.sh`, `08-list-jobs.sh` |

## Each script self-describes

Every script has a usage banner at the top — `head -20 09-run-eval.sh`
or just open it in any editor. Defaults work for the demo flow; pass
args to override.

## Output

Each script:

1. Echoes the HTTP method + URL it's about to hit
2. Echoes any arg context (agent name, gate, etc.)
3. Prints the JSON response, indented (via `python3 -m json.tool`)
4. Prints `← HTTP <code> (<latency>s)` on stderr so the status doesn't
   pollute the JSON (pipe to `jq` cleanly: `./04-get-agent.sh | jq .model_provider`)

## When the deployed Mova iO is ready

Send Jeremy the production Mova iO hostname (whatever Angular is
served from in your Azure tenant) and CORS will be updated for it.
**Today's CORS allow-list is `http://localhost:4200` only** — local
ng-serve hits the runtime fine; deployed Mova iO calls will be blocked
by the browser until the host is added. Adding it is a one-line
`az containerapp update` on Jeremy's side, no rebuild.

## Generating the TypeScript client for Angular

```bash
# From the Mova iO Angular repo (not this dir):
npx @openapitools/openapi-generator-cli generate \
    -i https://movate-dev-api.victoriouswater-7958662f.eastus2.azurecontainerapps.io/openapi.json \
    -g typescript-angular \
    -o src/app/api-client
```

Full details: [`docs/angular-client.md`](../../docs/angular-client.md)
in this repo.

## Troubleshooting

| Symptom | Likely fix |
|---|---|
| `✗ Missing .env` | Run the one-time setup above |
| 401 on every call | Confirm `MDK_TOKEN` in `.env` is the value Jeremy sent (no extra whitespace) |
| 404 on `/api/v1/...` | Old image still serving — `az containerapp revision list -g movate-dev-rg -n movate-dev-api` then ping Jeremy if it's stuck |
| 422 with "invalid_bundle" | The wizard payload is malformed — `head -30 02-create-agent.sh` for the expected shape |
| CORS error from browser | Send your Mova iO hostname to Jeremy for the env-var update |

## What's NOT yet wired (next-sprint)

These endpoints exist in the code but aren't in the wrappers:

- `POST /api/v1/agents` (multipart bundle upload — useful if you ever
  need to upload an existing `agent.yaml`+files; the wizard-shape
  endpoint covers 99% of cases)
- GitHub publish/history endpoints (item 78/79; design done — see
  `docs/adr/007-github-agent-version-control.md`)

If you want either, ping Jeremy.
