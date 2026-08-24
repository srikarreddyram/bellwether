# Bellwether — Product Requirements

**Version** 3.0 · **Status** Implemented · **Supersedes** [v2](prd-v2-historical.md)

---

## 1. Summary

Bellwether is a progressive delivery platform for teams who do not own a
Kubernetes cluster. It takes a repository URL, runs two copies of that
repository side by side as *stable* and *canary*, routes real traffic between
them through a measuring proxy, and promotes or rolls back on the evidence.

The target repository is never modified.

## 2. Problem

Canary deployment is a solved problem *if* you have containerised, adopted a
service mesh, and have someone who knows both. Argo Rollouts, Spinnaker and
CodeDeploy all assume that. Below that line — a small team shipping a Flask or
Express service onto a VM — the practical options are "deploy and watch Grafana"
or "do not deploy on Fridays".

The gap is not the traffic splitting. It is the *decision loop*: shift a slice,
measure it against a threshold, and act automatically on the answer.

## 3. Users

| User | Needs |
|---|---|
| Backend developer | Test a build under real traffic without owning infrastructure |
| SRE / on-call | Automatic rollback that does not depend on someone being awake |
| Tech lead | An audit trail showing why each release was promoted or aborted |
| Platform team | One deployment path across services in different languages |

## 4. Goals

- **G1** Deploy any repository with no modification to it.
- **G2** Split traffic for real, and measure the split from the request path.
- **G3** Decide promotion from observed telemetry, never from a guess.
- **G4** Guarantee rollback on every failure path.
- **G5** Complete a full rollout in under ten minutes for interpreted runtimes.
- **G6** Be safe to expose to a network, or refuse to start.

### Non-goals

Multi-node scheduling; service-mesh integration; production-grade proxy
throughput; business-metric analysis; replacing a real CI system.

---

## 5. Functional requirements

### 5.1 Repository ingestion

| ID | Requirement | Status |
|---|---|---|
| FR-01 | Accept any repository on an allowlisted host over HTTPS or SSH | ✅ |
| FR-02 | Clone shallowly by default, depth configurable | ✅ |
| FR-03 | Reject URLs by scheme, host, path shape, embedded credentials, `..`, leading `-`, and non-routable hosts | ✅ |
| FR-04 | Never pass a URL through a shell | ✅ |
| FR-05 | Fail with an actionable message naming the allowed hosts | ✅ |

### 5.2 Runtime detection

| ID | Requirement | Status |
|---|---|---|
| FR-10 | Detect Docker, Procfile, Flask, FastAPI, Django, Node, Go, Maven, Gradle, Ruby, Rust, PHP and static sites | ✅ |
| FR-11 | Prefer a Dockerfile over every heuristic when Docker is available | ✅ |
| FR-12 | Accept a repository-side `.bellwether.yml` launch spec | ✅ |
| FR-13 | Accept a platform-side launch override that outranks it | ✅ |
| FR-14 | Isolate each instance — a per-instance virtualenv or a container | ✅ |
| FR-15 | Fall back to a health stub, warning that the risk gate is not exercising the app | ✅ |
| FR-16 | Wait for the app to answer HTTP before declaring it healthy; any status counts | ✅ |

> **On FR-14.** v2 installed each target repository's dependencies into the
> platform's own interpreter, so a deployed app could overwrite the platform's
> Flask — and stable and canary, which exist precisely to run two versions of
> one application, shared one set of packages.

### 5.3 Traffic proxy

| ID | Requirement | Status |
|---|---|---|
| FR-20 | Single entrypoint; read the weight on every request | ✅ |
| FR-21 | Support GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS | ✅ |
| FR-22 | Forward headers and bodies; strip hop-by-hop headers; recompute Content-Length | ✅ |
| FR-23 | Return no body on HEAD | ✅ |
| FR-24 | Add `X-Bellwether-Target` and `X-Bellwether-Latency` | ✅ |
| FR-25 | Record latency and status per request, attributed to the selected cohort | ✅ |
| FR-26 | Persist telemetry atomically, coalescing writes | ✅ |
| FR-27 | Serve `/__bellwether/health` and `/__bellwether/metrics`, excluded from telemetry | ✅ |
| FR-28 | Offer sticky cohort sessions, repinning when a cohort is withdrawn | ✅ |
| FR-29 | Return 502 on an unreachable upstream, counted against that cohort | ✅ |

### 5.4 Risk evaluation

| ID | Requirement | Status |
|---|---|---|
| FR-30 | Compute canary P95 latency and 5xx rate from proxy telemetry | ✅ |
| FR-31 | Compute P95 by interpolation between closest ranks | ✅ |
| FR-32 | Exclude 4xx from the error rate | ✅ |
| FR-33 | Require freshness and a minimum sample count before trusting telemetry | ✅ |
| FR-34 | Default to ABORT on insufficient data; label any simulated verdict | ✅ |
| FR-35 | Log every decision, its metrics and its reasons to MLflow | ✅ |
| FR-36 | Continue deploying when MLflow is unavailable | ✅ |
| FR-37 | Exit 0 on PROMOTE, 1 on ABORT | ✅ |

> **On FR-34.** In v2 the proxy was never started, so telemetry never existed,
> so every evaluation took the fallback path and compared `random.uniform(50, 150)`
> against the threshold. Failing closed is the correction.

### 5.5 Pipeline

| ID | Requirement | Status |
|---|---|---|
| FR-40 | Nine stages: checkout, verify, baseline, proxy, canary, 10%, risk, 50%, 100% | ✅ |
| FR-41 | Start stable before the proxy, and the proxy before the canary | ✅ |
| FR-42 | Shift no traffic until both instances are healthy | ✅ |
| FR-43 | Re-score at 50% before promoting to 100% | ✅ |
| FR-44 | Smoke-test the canary before retiring stable | ✅ |
| FR-45 | Roll back on every non-success path — failure, abort, exception | ✅ |
| FR-46 | Honour an abort within one poll interval, not one stage | ✅ |
| FR-47 | Allow exactly one rollout at a time, atomically | ✅ |
| FR-48 | Serve the stage catalogue to the dashboard | ✅ |

### 5.6 API and dashboard

| ID | Requirement | Status |
|---|---|---|
| FR-50 | REST plus WebSocket; full snapshot on connect | ✅ |
| FR-51 | Persist history in SQLite with schema versioning | ✅ |
| FR-52 | Reconcile runs interrupted by a restart | ✅ |
| FR-53 | Rollback works whether or not a rollout is running | ✅ |
| FR-54 | Confirm before rollback in the UI | ✅ |
| FR-55 | Structured errors: `{error: {code, message}}` on every failure | ✅ |
| FR-56 | Fault injection against the canary only, off unless enabled | ✅ |

---

## 6. Non-functional requirements

| ID | Requirement | Target | Status |
|---|---|---|---|
| NF-01 | Full rollout, interpreted runtime | < 10 min | ✅ ~25 s for a small app |
| NF-02 | Proxy overhead per request | < 5 ms | ✅ writes coalesced off the hot path |
| NF-03 | Rollback to 0% | < 30 s | ✅ |
| NF-04 | Upstream timeout rather than hanging | 10 s | ✅ |
| NF-05 | Every decision traceable with metrics and reason | 100% | ✅ when MLflow is installed |
| NF-06 | No credentials in source or in `bellwether config` output | none | ✅ enforced in CI |
| NF-07 | Type coverage | mypy strict | ✅ |
| NF-08 | Concurrency correctness under multithreaded load | no lost samples | ✅ under test |
| NF-09 | Supported Python | 3.10 – 3.13 | ✅ CI matrix |

---

## 7. Security requirements

| ID | Requirement | Status |
|---|---|---|
| SEC-01 | No shell invocation anywhere in the codebase | ✅ |
| SEC-02 | Repository URLs allowlisted and validated | ✅ |
| SEC-03 | Webhooks require HMAC-SHA256; fail closed when unconfigured | ✅ |
| SEC-04 | CORS is an explicit allowlist; `*` refused at startup | ✅ |
| SEC-05 | Loopback bind by default; non-loopback requires a token or startup fails | ✅ |
| SEC-06 | Secrets redacted in all diagnostic output | ✅ |
| SEC-07 | CI fails on committed credential patterns | ✅ |

---

## 8. What v2 got wrong

Recorded because the failures were structural, not incidental.

| Defect | Consequence |
|---|---|
| The proxy was never started by the orchestrator | No telemetry was ever produced |
| The risk engine fell back to `random.uniform()` | Every risk decision was a random number |
| The canary launched before the stable baseline | Most traffic hit a closed port during the canary phase |
| Abort returned without resetting the weight | Rollback left the canary serving traffic |
| `repo_url` reached `shell=True` from an open-CORS endpoint | Unauthenticated remote code execution |
| The webhook had no signature check | The same, for anyone who could reach the port |
| Non-atomic writes to shared state files | Silent 0% weight; risk engine forced into fallback |
| Cross-thread mutation of unsynchronised lists | Lost telemetry samples |
| `int(len(v) * 0.95)` used as P95 | The maximum, not the 95th percentile |
| `time.time()` as a primary key | Collisions, and history that reordered itself |
| `'FAILED'` backend vs `'FAILURE'` frontend | Failed runs never rendered as failed |
| No tests | None of the above was detectable |

---

## 9. Roadmap

**v3.1** — Session-affinity by header as well as cookie; per-repository threshold
profiles; Slack and webhook notification on ABORT.

**v3.2** — Multi-instance canaries (N replicas per cohort); latency comparison
*against the stable baseline* rather than an absolute threshold alone.

**v4.0** — Optional Kubernetes backend reusing the same risk gate; an
`nginx`/`envoy` data plane for real production throughput.
