> **HISTORICAL — superseded by [`prd.md`](prd.md).**
>
> This is the v2 specification, retained for provenance. It describes a
> Jenkins-driven system with a `dashboard.html` static page and an in-memory
> audit log. None of that exists any more: Jenkins is gone, the dashboard is a
> React application, and history is persisted in SQLite. Several requirements
> here were never met by the implementation they describe — most notably the
> risk engine, which never read live telemetry because the proxy was never
> started. Read it as a record of intent, not as documentation.

---

BELLWETHER
ADAPTIVE CANARY PLATFORM

Product Requirements Document  &  Architecture Specification


Version	2.0 — Production Architecture
Status	Ready for Development Handoff
Owner	Tejsrikar Reddy
Repo	github.com/srikarreddyram/bellwether-canary-platform
Platform	Antigravity
Date	April 2026
 
1. Executive Summary
The Bellwether Adaptive Canary Platform is a cloud-native, language-agnostic continuous deployment system that accepts any GitHub repository URL and orchestrates a progressive, risk-gated traffic rollout — without requiring any modification to the target repository.

The platform acts as a deployment harness that wraps external repositories. It:
•	Clones the target repository twice to create isolated stable and canary instances
•	Auto-detects the runtime (Python/Flask, FastAPI, Django, Node.js, or static HTML)
•	Launches stable on port 8001 and canary on port 8002 as independent OS processes
•	Routes live traffic through a Python-based proxy on port 9000 with configurable weights: 0% → 10% → 50% → 100%
•	Evaluates deployment risk via an MLflow-backed scoring engine reading real latency and error-rate telemetry
•	Automatically rolls back to the stable baseline if any risk threshold is breached
•	Exposes all pipeline state, MLflow metrics, console output, and rollback controls via a real-time web dashboard

The target repo requires zero modification. The platform is the infrastructure.

2. Problem Statement
2.1  Current State
Most CI/CD pipelines treat deployment as binary: code either goes to production or it does not. This creates two failure modes: big-bang deployments that expose all users to untested code simultaneously, and conservative freeze policies that slow release velocity to avoid risk.

Existing canary tooling (Kubernetes Argo Rollouts, AWS CodeDeploy, Spinnaker) requires infrastructure ownership, containerisation, and significant DevOps expertise. It is not accessible to small teams deploying standard Python or Node.js applications on bare metal or local infrastructure.

2.2  Target Users
User	Context	Pain Point
Backend Developer	Shipping Flask/Node APIs	No safe way to test new builds under real traffic
DevOps Engineer	Managing Orchestrator pipelines	Manual rollback is slow and error-prone
Tech Lead	Overseeing deployment governance	No immutable audit trail of deployment decisions
Platform Team	Supporting multiple services	Each service has its own ad-hoc deployment process

2.3  Goals
•	Accept any GitHub URL and deploy it without repo modification
•	Provide real, measurable traffic splitting — not simulated
•	Make automated rollback decisions based on live telemetry, not guesswork
•	Give operators a single dashboard for the entire deployment lifecycle
•	Complete a full canary cycle (0% → 10% → 50% → 100%) in under 10 minutes

3. Scope
3.1  In Scope — v2.0
•	Auto-detection and launch of Python (Flask, FastAPI, Django) and Node.js applications
•	Two-process canary deployment with isolated working directories
•	Python stdlib HTTP traffic proxy with configurable weight file
•	MLflow risk scoring engine reading live proxy telemetry (latency P95, error rate)
•	Native Python Orchestrator with 10 named stages
•	Flask REST API bridging Orchestrator and the dashboard
•	Real-time SPA dashboard: pipeline rail, traffic gauge, MLflow metrics, deployment history, console, rollback
•	Automatic rollback on risk threshold breach or pipeline failure
•	In-memory deployment audit log per API session

3.2  Out of Scope — v2.0
•	Java / Maven / Gradle build support (build step takes > 5 minutes — scheduled for v2.1)
•	Docker / containerisation (bare-process model only)
•	Multi-node or cloud deployment (single macOS host only)
•	Persistent audit log beyond API session (requires database — v3.0)
•	WebSocket real-time push from API to dashboard (3-second polling is sufficient)
•	Authentication on the dashboard or API (internal tool only)

3.3  Roadmap
Version	Theme	Key Features
v2.1	Java Support	Maven/Gradle build step in launch_app.sh, JVM warmup timeout handling
v2.2	Persistent Storage	SQLite audit log, MLflow run retention policy, session-sticky canary routing
v3.0	Cloud Native	Docker containerisation, Kubernetes ingress traffic splitting, multi-region
v3.1	Smart Governance	ML-based anomaly detection, Slack/email alerts, approval gates before promotion
 

4. Functional Requirements
4.1  Repository Ingestion
ID	Requirement	Priority	Source
FR-01	System SHALL accept any public GitHub HTTPS URL as the REPO_URL pipeline parameter	MUST	Goal
FR-02	System SHALL clone with --depth 1 to minimise clone time	MUST	Performance
FR-03	System SHALL create two independent clone directories: /tmp/bellwether_stable_app and /tmp/bellwether_canary_app	MUST	Architecture
FR-04	System SHALL fail fast with a clear error if the repo is not clonable	MUST	QA
FR-05	System SHOULD support private repos via Orchestrator credential store HTTPS credentials	SHOULD	Roadmap

4.2  Runtime Detection
ID	Requirement	Priority	Source
FR-10	launch_app.sh SHALL detect Flask by 'flask' in requirements.txt or 'from flask' in any .py file	MUST	FR-01
FR-11	launch_app.sh SHALL detect FastAPI by 'fastapi' or 'uvicorn' in requirements.txt	MUST	FR-01
FR-12	launch_app.sh SHALL detect Django by 'django' in requirements.txt	MUST	FR-01
FR-13	launch_app.sh SHALL detect Node.js by package.json with a 'start' script	MUST	FR-01
FR-14	launch_app.sh SHALL detect static HTML by presence of index.html at repo root	MUST	FR-01
FR-15	For unrecognised repo types, SHALL fall back to a minimal Flask health-check wrapper on the specified port	MUST	Resiliency
FR-16	launch_app.sh SHALL wait up to 15 seconds for the app to respond before reporting status	MUST	Resiliency

4.3  Traffic Proxy
ID	Requirement	Priority	Source
FR-20	traffic_proxy.py SHALL run on port 9000 as the single public-facing entry point	MUST	Architecture
FR-21	Proxy SHALL read canary weight (0-100) from /tmp/bellwether_traffic_weight on every request	MUST	Architecture
FR-22	Proxy SHALL support GET, POST, PUT, DELETE, PATCH, and HEAD methods	MUST	Compatibility
FR-23	Proxy SHALL forward all request headers (excluding host and connection) and the full body	MUST	Compatibility
FR-24	Proxy SHALL add X-Bellwether-Target and X-Bellwether-Latency response headers	SHOULD	Observability
FR-25	Proxy SHALL record per-request latency and error status for both stable and canary cohorts	MUST	Risk Engine
FR-26	Proxy SHALL persist rolling metrics snapshot to /tmp/bellwether_proxy_metrics.json after every request	MUST	Risk Engine
FR-27	Proxy SHALL expose /__bellwether/health and /__bellwether/metrics internal endpoints	MUST	Operations

4.4  Risk Evaluation
ID	Requirement	Priority	Source
FR-30	evaluate_risk.py SHALL read canary latency P95 and error rate from proxy metrics when >= 5 canary requests have been observed	MUST	Architecture
FR-31	SHALL fall back to simulated metrics if proxy data is unavailable or stale (> 120 seconds old)	MUST	Resiliency
FR-32	Default thresholds: latency P95 > 500ms OR error rate > 5% SHALL trigger ABORT	MUST	Risk
FR-33	All metrics, decisions, and abort reasons SHALL be logged to MLflow at ./mlruns	MUST	Governance
FR-34	evaluate_risk.py SHALL exit with code 1 on ABORT and code 0 on PROMOTE	MUST	Architecture

4.5  Pipeline Stages
The Native Python Orchestrator defines 10 stages executed in the following sequence:
#	Stage	Action	Failure Behaviour
0	Checkout	deleteDir(), git clone --depth 1 from REPO_URL, chmod +x scripts	Pipeline aborts — no deployment starts
1	Rollback Gate	Fires only if FORCE_ROLLBACK=true. Runs deploy_script.sh 0, then error() to mark FAILED	N/A — this IS the failure path
2	Install Dependencies	pip3 install platform deps. Validates with python3 -c import check	Abort — cannot continue without deps
3	Verify DB Structure	Runs verify_db_structure.py. Replace with real schema check in production	Abort with clear error message
4	Launch Stable	Clones repo to /tmp/bellwether_stable_app, runs launch_app.sh on port 8001	Abort — cannot split without baseline
5	Launch Proxy	Kills any existing port 9000 process, starts traffic_proxy.py, verifies health endpoint	Abort — proxy is the traffic gate
6	Launch Canary	Clones repo to /tmp/bellwether_canary_app, runs launch_app.sh on port 8002	Abort — no canary to route to
7	Canary 10%	Writes '10' to weight file via deploy_script.sh, sends 10 warm-up requests via proxy	post{failure} fires rollback
8	Evaluate Risk	Runs evaluate_risk.py. Reads real proxy metrics. Exits 1 on ABORT	post{failure} fires rollback
9	Promote 50%	Writes '50' to weight file, sends 20 test requests through proxy	post{failure} fires rollback
10	Promote 100%	Writes '100', terminates stable process, runs smoke test on canary	post{failure} fires rollback
 

5. Architecture
See [architecture.md](architecture.md) for detailed architecture and runtime process models.

6. Dashboard Specification
dashboard.html is a single-page application served as a static file. It connects to the Flask API at http://localhost:5001, polls every 3 seconds, and requires no build step. Open directly in any browser.

6.1  Views
View	Nav Label	Contents
Deploy	🚀 Deploy	Repo URL input, Deploy button, 7-bubble pipeline rail, live traffic % card, build status card
Monitor	📡 Monitor	Pipeline rail and traffic/build cards in a dedicated view for demo walkthrough
MLflow Metrics	📊 MLflow Metrics	Latency P95 gauge, error rate gauge, risk decision badge, run history table (last 8 runs)
History	🗂 History	Deployment history table: status dot, repo URL, triggered timestamp
Console	🖥 Console	Monospace scrollable Orchestrator console output, last 200 lines
Rollback	⚠️ Rollback	Warning banner, Rollback Now button, explanation of what rollback does and its audit trail impact

6.2  Stage Map
The pipeline rail renders 7 bubbles mapped to Orchestrator stage names via the STAGE_MAP constant. Stage key values must match Orchestrator stage names exactly, including case and spacing.
STAGE_MAP = [
  { name: 'Checkout',      icon: '📦',  key: 'Checkout'              },
  { name: 'Install Deps',  icon: '🔧',  key: 'Install Dependencies'  },
  { name: 'Verify DB',     icon: '🗄',  key: 'Verify DB Structure'   },
  { name: 'Canary 10%',    icon: '🐤',  key: 'Canary 10%'            },
  { name: 'Risk Score',    icon: '🤖',  key: 'Evaluate Risk'         },
  { name: 'Promote 50%',   icon: '🚦',  key: 'Promote 50%'           },
  { name: 'Promote 100%',  icon: '🚀',  key: 'Promote 100%'          },
]

TRAFFIC_BY_STAGE = {
  'Checkout': 0,  'Install Dependencies': 0,  'Verify DB Structure': 0,
  'Canary 10%': 10,  'Evaluate Risk': 10,
  'Promote 50%': 50,  'Promote 100%': 100,
}

7. Key Data Flows
See [architecture.md](architecture.md) for detailed deployment and rollback data flows.

8. Non-Functional Requirements
ID	Category	Requirement	Target
NF-01	Performance	Full pipeline (checkout → 100%) SHALL complete within	< 10 min
NF-02	Performance	Traffic proxy SHALL add no more than per-request overhead of	< 5 ms
NF-03	Performance	Dashboard poll cycle (all 4 API calls) SHALL complete within	< 1 s
NF-04	Reliability	Rollback SHALL complete and proxy weight SHALL reach 0 within	< 30 s
NF-05	Reliability	Proxy SHALL return 502 on target app downtime rather than hanging (timeout)	10 s
NF-06	Observability	Every PROMOTE/ABORT decision SHALL be traceable in MLflow with timestamp, metrics, and reason	100%
NF-07	Compatibility	launch_app.sh SHALL work on macOS 12+ with bash 3.2+	bash 3.2+
NF-08	Security	GitHub Personal Access Token SHALL NOT be committed to VCS — injected via env or Orchestrator credential store	No secrets
NF-09	Maintainability	Risk thresholds SHALL be configurable via two constants at the top of evaluate_risk.py	2 constants

9. Known Limitations & Technical Debt
ID	Limitation	Impact	Priority
KL-01	_history list in Api.py is lost when the process restarts	No persistent audit log across sessions	P1
KL-02	MLflow runs accumulate in ./mlruns indefinitely — no retention policy	Performance degrades after many builds	P2
KL-03	Traffic splitting is probabilistic per-request, not session-sticky	Same user may see different versions within one session	P2
KL-04	Flask FLASK_RUN_PORT env var may be ignored by apps with hardcoded port config	App launches but listens on wrong port — proxy gets connection refused	P1
KL-05	GitHub Personal Access Token stored in plaintext in Api.py	Token exposure if repo is public or token is reused	P1
KL-06	Java and compiled language repos are not supported — no build step before launch	Pipeline falls back to health wrapper; target app does not run	P3
KL-07	http.server.ThreadingHTTPServer can block on slow clients at high concurrency	Latency spikes above 50 req/s — replace with gunicorn or aiohttp for production	P3
 

10. Development Handoff — Priority Tasks
Tasks ordered by priority for the Antigravity development team. P1 items must be completed before new feature work begins.

P1 — Critical
ID	Description	File(s)	Acceptance Criteria
T-01	Move Orchestrator credentials to environment variables. Set JENKINS_USER and JENKINS_TOKEN via macOS env or Orchestrator global env vars. Remove hardcoded values from Api.py.	Api.py	No credentials in source. API starts using env vars only.
T-02	Fix Flask port binding in launch_app.sh. Use gunicorn as launcher for reliable port control: gunicorn -b 0.0.0.0:$PORT <module>:app. Auto-detect the app module name from the Flask entry point file.	launch_app.sh	Flask app reliably responds on specified port with any repo.
T-03	Replace in-memory _history with SQLite persistence. Table: deployments(id, repo_url, triggered_at, status, build_number). Write on trigger, update on status poll.	Api.py	Deployment history survives Api.py restart.

P2 — High Value
ID	Description	File(s)	Acceptance Criteria
T-04	Pass PORT env var to Node.js apps. Node reads process.env.PORT — pass PORT=8001 or PORT=8002 to npm start command in launch_app.sh.	launch_app.sh	Node.js app responds on the correct port.
T-05	Add MLflow run retention: keep only the last 50 runs in ./mlruns. Run cleanup at the start of each Evaluate Risk stage.	evaluate_risk.py	./mlruns stays under 50 runs after 100 builds.
T-06	Make risk thresholds configurable via API parameters. Add LATENCY_THRESHOLD_MS and ERROR_RATE_THRESHOLD pipeline params that evaluate_risk.py reads from environment.	Orchestratorfile, evaluate_risk.py	Thresholds settable per-build with no code changes.
T-07	Implement session-sticky canary routing option. Store IP → target mapping in proxy dict with 10-minute TTL. Enable via a separate sticky weight file.	traffic_proxy.py	Same IP consistently routes to same target within TTL window.

P3 — Future Sprint
•	Java/Maven: add mvn package step in launch_app.sh before java -jar target/*.jar
•	WebSocket push from Api.py to dashboard, replacing 3-second polling
•	Slack/email alert when ABORT decision is written to MLflow
•	Multi-repo A/B test mode: run two different repos as stable vs canary
•	Docker isolation: wrap each launched process in a container
•	Browser-based log streaming: tail /tmp/bellwether_canary.log in real time via dashboard

11. Local Development Setup
11.1  Prerequisites
•	macOS 12 (Monterey) or later
•	Python 3.9+ available as python3 in PATH
•	Node.js 18+ and npm 9+ (for Node app support)
•	Orchestrator LTS on port 8080, pipeline job named Adaptive-Canary-Core
•	GitHub Personal Access Token for user tejsr (rotates every 30 days)
•	pip3 available globally, not only inside venv

11.2  Cloud Setup
•	Type: Pipeline
•	Pipeline definition: Pipeline script from SCM
•	SCM: Git — https://github.com/srikarreddyram/bellwether-canary-platform.git
•	Branch: */main — Script Path: Orchestratorfile
•	Build with Parameters: auto-detected from parameters{} block in Orchestratorfile

11.3  Start Sequence
# 1. Enter repo and activate venv
cd ~/bellwether-canary-platform
source venv/bin/activate

# 2. Start API (new terminal tab)
python3 Api.py
# Expect: Bellwether Canary API running on http://localhost:5001

# 3. Open dashboard
open dashboard.html
# Top-right pill should show: API ONLINE (green)

# 4. Optional — trigger build from terminal
curl -X POST http://localhost:8080/job/Adaptive-Canary-Core/buildWithParameters \
     -u tejsr:<API_TOKEN> \
     --data "REPO_URL=https://github.com/srikarreddyram/bellwether-canary-platform.git"

11.4  Health Checks
# API
curl http://localhost:5001/api/health
# Expected: {"status": "ok"}

# Proxy (after Launch Proxy stage has run)
curl http://localhost:9000/__bellwether/health
# Expected: {"status": "ok", "canary_weight": 10}

# Stable app
curl http://localhost:8001/

# Canary app
curl http://localhost:8002/

# Traffic split check
curl -I http://localhost:9000/
# Look for X-Bellwether-Target: stable  OR  X-Bellwether-Target: canary

12. Glossary
Term	Definition
Canary Deployment	A release strategy where a new version receives a small percentage of live traffic before full promotion, allowing risk assessment with real users before committing.
Traffic Weight	The percentage of incoming requests routed to the canary instance. Stored as an integer (0-100) in /tmp/bellwether_traffic_weight and read by the proxy on every request.
Stable Instance	The currently deployed, known-good version of the application. Runs on port 8001. Receives (100 - weight)% of traffic.
Canary Instance	The new version under test. Runs on port 8002. Receives weight% of traffic. Killed on rollback or after successful 100% promotion.
Traffic Proxy	A lightweight Python HTTP server (traffic_proxy.py) on port 9000 that probabilistically forwards requests to stable or canary based on the weight file.
Risk Score	The output of evaluate_risk.py. Either PROMOTE (exit 0) or ABORT (exit 1), based on canary latency P95 and error rate versus configured thresholds.
MLflow	An open-source platform for tracking ML and operational metrics. Used to log deployment decisions, latency, error rate, and abort reasons with immutable timestamps.
FORCE_ROLLBACK	A Orchestrator boolean parameter that, when true, causes the pipeline to skip all stages except Rollback Gate and immediately route 0% traffic to the canary.
launch_app.sh	The universal application launcher. Auto-detects the runtime type of a cloned repository and starts the application on a specified port.
Latency P95	The 95th percentile response time — the latency value below which 95% of requests complete. A better production health indicator than mean latency.
wfapi	Orchestrator Pipeline Status — the REST endpoint (/wfapi/describe) that returns stage-level status for Declarative Pipelines. Used by Api.py to populate the dashboard pipeline rail.
PID File	A file containing the process ID of a running background process. Used by deploy_script.sh and launch_app.sh to kill specific processes cleanly on rollback.


13. Pitfalls and Limitations (V2)
- **No Session Stickiness:** Traffic splitting is probabilistic per-request, meaning a user might flip between stable and canary versions.
- **Lack of Environment Isolation:** Applications run as bare OS background processes, risking dependency conflicts and dirty states.
- **Proxy Bottleneck:** Python's ThreadedHTTPServer isn't ideal for high concurrency.


14. V3 Improvements
- **Docker Isolation:** Apps are built and run in dynamic Docker containers for perfect isolation.
- **Session Stickiness:** The proxy uses an `Bellwether-Cohort` cookie to ensure consistent routing.
- **Expanded Language Support:** Added Maven build steps for Java and `npm run build` for Node.js.
- **WebSockets:** Real-time Dashboard updates powered by Flask-SocketIO instead of HTTP polling.
- **Smart Alerting:** Webhook POST payloads fired upon `ABORT` decisions.


15. V4 Upgrades
- **Premium Dashboard UI:** Redesigned with glassmorphism, animated stages, and live Chart.js metrics tracking.
- **Traffic & Chaos Generator:** Simulated background traffic and a 'Chaos Mode' toggle to artificially inject latency and errors for live rollback demonstrations.
- **GitHub Webhook Automation:** The platform now automatically triggers pipelines upon receiving GitHub push webhooks at `/api/webhook/github`.


16. React & Tailwind Migration
- **Frontend Arch:** The dashboard is now a discrete Single Page Application built with React and Vite.
- **Styling:** Vanilla CSS was replaced with TailwindCSS utility classes.


17. Cloud CI/CD Migration
- **Orchestrator Deprecation:** Orchestrator was fully removed in favor of a hybrid cloud model.
- **GitHub Actions:** Cloud CI handles the 'Build & Test' phase virtually, while a native Python orchestrator handles the physical Canary routing locally.
