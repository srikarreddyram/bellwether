# Bellwether

**Risk-gated progressive delivery for repositories that need no modification.**

Bellwether clones a target repository twice, runs the two copies side by side as
*stable* and *canary*, routes real traffic between them through a measuring
proxy, and promotes or rolls back based on what that traffic actually did.

A bellwether is the animal that walks ahead of the flock wearing the bell — the
leading indicator. Same idea as a canary, one paddock over.

```
                        ┌──────────────┐
  live traffic ────────▶│ traffic proxy│──── 90% ───▶ stable   :8001
                        │    :9000     │──── 10% ───▶ canary   :8002
                        └──────┬───────┘
                               │ per-request latency + status
                               ▼
                        ┌──────────────┐      promote ─▶ 50% ─▶ 100%
                        │  risk gate   │──────┤
                        └──────────────┘      abort ───▶ 0% + canary stopped
```

---

## Why it exists

Progressive delivery normally means owning Kubernetes. Argo Rollouts, Spinnaker
and CodeDeploy all assume you have containerised, have a service mesh, and have
someone who knows both. Bellwether gives you the same *decision loop* — shift a
slice of traffic, measure it, promote or roll back — against plain processes on
one host, for a repository you may not even control.

**The target repository is never modified.** Detection is automatic; when it
guesses wrong you describe the build from the outside.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,tracking]"

bellwether api                       # control plane  → http://127.0.0.1:5001
cd frontend && npm install && npm run dev   # dashboard → http://localhost:5173
```

Then deploy something:

```bash
bellwether deploy https://github.com/owner/repository.git
```

Or drive it from the dashboard. Either way the rollout runs the same nine stages.

---

## What it runs

Detection is ordered most-explicit-first, so an escape hatch always wins over a
guess.

| Priority | Trigger | How it runs |
|---|---|---|
| 1 | operator launch override (API/CLI) | exactly what you specify |
| 2 | `.bellwether.yml` in the repository | exactly what it declares |
| 3 | `Dockerfile` (+ docker on PATH) | `docker build` then `docker run` with the port mapped |
| 4 | `Procfile` with a `web:` line | that command, `$PORT` substituted |
| 5 | `manage.py` + django | gunicorn in an isolated virtualenv |
| 6 | `FastAPI()` / `fastapi` dep | uvicorn in an isolated virtualenv |
| 7 | `Flask()` / `flask` dep | gunicorn in an isolated virtualenv |
| 8 | `package.json` | `npm ci`, optional build, `npm start` |
| 9 | `go.mod` | `go build`, run the binary |
| 10 | `pom.xml` / `build.gradle` | maven or gradle package, `java -jar` |
| 11 | `Gemfile` | `bundle exec rackup` or `rails server` |
| 12 | `Cargo.toml` | `cargo build --release` |
| 13 | `composer.json` / `index.php` | PHP built-in server |
| 14 | `index.html` | static file server |
| 15 | anything else | a health stub, **loudly warned about** |

Because a Dockerfile already states exactly how a project builds and runs, in
the one format every language shares, it outranks every heuristic below it.

### When detection is wrong

Two ways to correct it, neither of which touches the target repository:

**From the platform** — send a launch spec with the deploy request:

```jsonc
{
  "repoUrl": "https://github.com/owner/repository.git",
  "launch": {
    "build": ["make deps", "make build"],
    "start": "./bin/server --listen 127.0.0.1:${PORT}",
    "health_path": "/healthz",
    "env": { "APP_ENV": "canary" }
  }
}
```

**From the repository** — commit a `.bellwether.yml` (opt-in; the platform-side
override still wins):

```yaml
runtime: node
build:
  - npm ci
  - npm run build
start: node dist/server.js
health_path: /healthz
```

Commands are executed as argument vectors. **No shell is ever invoked**, so
`;`, `|` and `$(…)` are literal characters, not operators.

---

## The rollout

| # | Stage | Traffic | What it does |
|---|---|---|---|
| 0 | Checkout | 0% | Validate the URL against the host allowlist, clone shallowly |
| 1 | Verify | 0% | Detect the runtime, apply MLflow retention |
| 2 | Baseline | 0% | Start **stable**. A canary is meaningless without it |
| 3 | Proxy | 0% | Ensure the proxy is live, routing everything to stable |
| 4 | Canary | 0% | Start the candidate alongside stable, still receiving nothing |
| 5 | Canary 10% | 10% | Shift traffic, soak under generated load |
| 6 | Risk Gate | 10% | Score measured latency and error rate |
| 7 | Promote 50% | 50% | Halve the split, **re-score** before committing |
| 8 | Promote 100% | 100% | Smoke-test the canary, then retire the old baseline |

**Every exit path rolls back.** Stage failure, operator abort, risk gate, or an
unexpected exception all run through the same `finally`: weight to 0%, canary
stopped, run recorded. Traffic is never left mid-shift.

---

## The risk gate

```
ABORT if canary latency P95 > 500 ms
     or canary error rate   > 5%
PROMOTE otherwise
```

Both thresholds are configurable. Three properties worth stating outright:

- **It scores real traffic.** Latency and status come from the proxy's own
  per-request record, not from a synthetic probe.
- **Absent evidence is not evidence of safety.** With too few canary requests,
  the default policy is `abort`. `simulate` and `promote` exist, and a simulated
  verdict is labelled as simulated everywhere it appears.
- **4xx is not counted against the build.** A 404 is the client's fault;
  counting it would abort rollouts because somebody probed a bad path.

Every decision — metrics, thresholds, reasons, timestamp — is logged to MLflow
when it is installed. MLflow is optional: losing observability must never take
the deployment path down with it.

---

## Security

`/api/deploy` clones and executes code named in the request body. It is treated
accordingly.

- **No shell, anywhere.** Every subprocess is an argument vector.
- **Repository URLs are allowlisted** by scheme, host, and path shape.
  Credentials-in-URL, `..`, leading `-` (git flag smuggling) and non-routable
  hosts (SSRF) are all rejected.
- **CORS is an explicit allowlist.** `*` is refused at config load, because a
  wildcard would let any page you visit trigger a deployment.
- **The webhook requires a valid HMAC signature**, and fails closed when no
  secret is configured.
- **Loopback by default.** Binding elsewhere without `BELLWETHER_API_TOKEN` is a
  startup error, not a warning.

Report anything you find privately rather than through a public issue.

---

## CLI

```bash
bellwether api                     # control plane + websocket
bellwether proxy                   # data plane, standalone
bellwether deploy <repo-url>       # one rollout, synchronously
bellwether risk --json             # score current telemetry; exits 0/1
bellwether weight 50               # read or set the split
bellwether rollback                # 0% and stop the canary
bellwether status                  # full state as JSON
bellwether config                  # effective settings (secrets redacted)
bellwether load --seconds 30       # drive traffic through the proxy
```

The proxy runs as its own process on purpose: restarting the control plane to
ship a fix must not drop live traffic.

---

## Configuration

Every setting is an environment variable, read and validated once at startup, so
a typo fails immediately instead of degrading quietly. `bellwether config` prints
the effective values. See [`.env.example`](.env.example) for the full list; the
ones you are most likely to touch:

| Variable | Default | Meaning |
|---|---|---|
| `BELLWETHER_STATE_DIR` | `$TMPDIR/bellwether` | All runtime state |
| `BELLWETHER_LATENCY_P95_THRESHOLD_MS` | `500` | Abort above this |
| `BELLWETHER_ERROR_RATE_THRESHOLD` | `0.05` | Abort above this |
| `BELLWETHER_INSUFFICIENT_DATA_POLICY` | `abort` | `abort` / `simulate` / `promote` |
| `BELLWETHER_ALLOWED_REPO_HOSTS` | `github.com,gitlab.com,bitbucket.org` | Clone allowlist |
| `BELLWETHER_CORS_ORIGINS` | `http://localhost:5173,…` | Dashboard origins |
| `BELLWETHER_API_TOKEN` | unset | Required for non-loopback binds |
| `BELLWETHER_WEBHOOK_SECRET` | unset | Required for the GitHub webhook |

---

## Development

```bash
make install      # editable install with dev extras
make test         # pytest, including real end-to-end rollouts
make lint         # ruff + mypy (strict) + eslint
make check        # everything CI runs
make dev          # API and dashboard together
```

The suite runs actual deployments: it clones a real repository, launches two
real instances, starts the real proxy, drives real traffic through it, and
requires the risk gate to reach its verdict from telemetry the proxy genuinely
recorded.

---

## Limits

Stated plainly, because a deployment tool that oversells itself is worse than
one that does less.

- **Single host.** No multi-node scheduling, no service mesh.
- **The proxy is `ThreadingHTTPServer`.** Fine for a few hundred requests per
  second; it is not nginx. Front it with something real for genuine production
  load.
- **Bare processes unless you ship a Dockerfile.** Per-instance virtualenvs
  isolate Python dependencies; containers isolate everything. Prefer the latter.
- **The risk gate reads latency and error rate only.** It does not know your
  business metrics.
- **Compiled runtimes are slow to roll out.** A Maven build inside the stage
  budget can exceed ten minutes.

## Licence

MIT.
