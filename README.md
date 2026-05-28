# agentic-dash

A Plotly Dash data-science dashboard powered by a Google ADK agent (Gemini +
BigQuery), streaming over the **AG-UI protocol** via Server-Sent Events. You ask
a natural-language question; a minimal custom agent turns it into BigQuery
Standard SQL, runs it, and streams the answer — chat tokens, tool trajectory,
and a result snapshot — back to the browser as AG-UI events. Everything runs in
a single container and is Cloud Run-ready.

## Architecture

```
            ┌─────────────────────── single container ───────────────────────┐
            │                                                                  │
 browser    │   FastAPI shell (app/server.py)                                  │
 ┌────────┐ │   ┌───────────────────────────────┐                             │
 │ Dash   │ │   │ POST /agui  (text/event-stream)│   ADKAgent (ag_ui_adk)      │
 │ UI     │─┼──▶│   RunAgentInput ──────────────▶│──▶ DataScienceAgent (Gemini)│
 │        │ │   └───────────────────────────────┘        │                    │
 │ assets/│ │            ▲   AG-UI SSE events             │ bigquery_query tool│
 │ agui.js│◀┼────────────┘   (TEXT_MESSAGE_CONTENT,       ▼                    │
 │  (SSE  │ │                 TOOL_CALL_*, STATE_*)   ┌──────────┐             │
 │ reader)│ │                                         │ BigQuery │             │
 └────────┘ │   GET  /  ──▶ Dash (WSGI mount)         └──────────┘             │
            │                                                                  │
            └──────────────────────────────────────────────────────────────────┘
```

Request flow:

1. The browser POSTs a `RunAgentInput` to `/agui`. The SSE is read in JS
   (`app/assets/agui.js`) — AG-UI has no Python browser client.
2. `ag_ui_adk` drives the ADK `DataScienceAgent`, which calls one of three
   tools: `bigquery_query` (Standard SQL / BQML), `bqml_forecast` (single-series
   ARIMA forecast), or `call_analytics_agent` (matplotlib chart via a Vertex
   Agent Engine code-execution sandbox).
3. The tool writes a result snapshot into agent state; `ag_ui_adk` emits AG-UI
   `STATE_SNAPSHOT` / `STATE_DELTA` events alongside the chat and tool-call
   events.
4. `agui.js` pushes deltas into Dash components (`#chat`, `#trajectory`, the
   `kpis` store) with `window.dash_clientside.set_props`. A server-side callback
   then renders the results table + bar chart from the `kpis` store.

The `/agui` route is registered **before** the Dash app is mounted at `/`, so
the catch-all WSGI mount doesn't shadow it.

## Prerequisites

- Python 3.12+
- A GCP project with the Vertex AI and BigQuery APIs enabled
- Either a service-account JSON key (Vertex + BigQuery scopes) or local ADC via
  `gcloud auth application-default login`
- Data defaults to the public `bigquery-public-data.thelook_ecommerce` dataset,
  so no data loading is required

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env     # fill in your project, location, and SA key path
uvicorn app.server:app --reload --port 8080
```

Open http://localhost:8080 and ask, e.g., *"total sales per country"* (table +
bar chart), *"forecast Kaggle stickers at Discount Stickers in Canada for 30
days"* (BQML forecast chart), or *"plot the monthly trend for Kaggle in
Canada"* (matplotlib chart in the Analysis panel — see "Code interpreter"
below).

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Purpose | Default |
|----------|---------|---------|
| `GOOGLE_GENAI_USE_VERTEXAI` | Route Gemini calls through Vertex (required) | `1` |
| `GOOGLE_CLOUD_PROJECT` | GCP project hosting Vertex + the data | — |
| `GOOGLE_CLOUD_LOCATION` | Vertex region | `us-central1` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Absolute path to SA key (or use ADC) | — |
| `BQ_COMPUTE_PROJECT_ID` | Project billed for queries | `GOOGLE_CLOUD_PROJECT` |
| `BQ_DATA_PROJECT_ID` | Project the data lives in | `bigquery-public-data` |
| `BQ_DATASET_ID` | Default dataset | `thelook_ecommerce` |
| `BQ_MAX_ROWS` | Max rows fetched per query | `1000` |
| `AGENT_MODEL` | Root agent model | `gemini-2.5-flash` |
| `ANALYTICS_AGENT_MODEL` | Analytics sub-agent model | `AGENT_MODEL` |
| `AGUI_USER_ID` | AG-UI user id | `dashboard` |
| `AGENT_ENGINE_RESOURCE_NAME` | Pinned Agent Engine (skip lazy create) | — |
| `AGENT_ENGINE_SANDBOX_RESOURCE_NAME` | Pinned code-execution sandbox | — |

## Code interpreter (analysis charts)

Chart-style asks ("plot…", "show the monthly trend…", "distribution by…") are
handled by an analytics sub-agent that runs matplotlib code in a **Vertex Agent
Engine sandbox**. The captured PNG is bridged into agent state and rendered as
`<img>` in the dashboard's Analysis panel.

- **First call** lazily creates an Agent Engine + sandbox under your project
  (~10s cold start). The sandbox has a 1-year TTL but auto-evicts after 14 days
  idle; the executor re-creates it transparently.
- **To avoid orphan engines** accumulating across cold starts, pin
  `AGENT_ENGINE_RESOURCE_NAME` and `AGENT_ENGINE_SANDBOX_RESOURCE_NAME` in
  `.env`. Generate the resource names once with:

  ```bash
  python tools/probe_agent_engine.py
  ```

## Deploy to Cloud Run

Use the included deploy script, which is idempotent and reads all sensitive
values from your local (gitignored) `.env`:

```bash
bash tools/deploy.sh
```

The script:

1. Reads `GOOGLE_CLOUD_PROJECT`, `BQ_*`, and `AGENT_ENGINE_*` from `.env`.
2. Creates the Artifact Registry repo (if missing).
3. Grants the Cloud Build SA `roles/run.builder` (if missing).
4. Creates six Secret Manager secrets (or adds a new version if values changed)
   and grants `roles/secretmanager.secretAccessor` to the runtime SA.
5. Builds the slim multi-stage image via Cloud Build → Artifact Registry.
6. Deploys to Cloud Run with `--ingress=internal-and-cloud-load-balancing`,
   `--no-allow-unauthenticated`, `--max-instances=1`, `--timeout=3600`,
   non-sensitive flags via `--set-env-vars`, and all GCP identifiers via
   `--set-secrets`.

Notes:

- **SSE timeout:** `/agui` is a long-lived stream. The script uses
  `--timeout=3600`.
- **In-memory state:** session/state services are in-memory, so the script
  pins `--max-instances=1`. The Agent Engine sandbox also has affinity to a
  session.
- **Service account:** the script targets `agentic-dash-sa@<project>.iam.gserviceaccount.com`
  (override via `CLOUD_RUN_SA` in `.env`). It expects the SA to already have
  the runtime roles: Vertex AI User, BigQuery Job User + Data Viewer, Logging
  Writer. The script handles Secret Accessor itself.

## Secrets posture

This project is designed so secret values never enter a tracked file, a built
image, or the application's stdout/stderr logs.

| Surface | What's enforced |
|---|---|
| **Git** | `.env`, `*service-account*.json`, ADC files are in `.gitignore`. `tools/deploy.sh` reads all identifiers from `.env`; nothing project-specific is hardcoded in the committed script. |
| **Docker image** | `.dockerignore` excludes `.env`, `.git`, `.venv`. The multi-stage build copies only the installed venv into the runtime stage; no source tree, no `.env` ever in the image. |
| **Cloud Run service spec** | Sensitive GCP identifiers (project IDs, dataset, Agent Engine paths) are wired via `--set-secrets` so they appear in the service spec as Secret Manager *references*, never as values. Only non-sensitive flags (model name, tuning knobs) use `--set-env-vars`. |
| **Cloud Logging** | Module loggers in `app/model_monitor.py` and `agent/analytics_agent.py` log only exception *class names* on failure paths, not exception messages — preventing BigQuery / artifact-service errors from echoing fully-qualified resource paths into Cloud Logging. |

If you add a new secret-bearing env var, mirror this split: `--set-secrets` for
identifiers, `--set-env-vars` only for true non-sensitive config.
