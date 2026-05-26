# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-container app: a Plotly Dash dashboard whose results are produced by a Google
ADK agent (Gemini + BigQuery), streamed to the browser over the **AG-UI protocol** via
Server-Sent Events. The user asks a natural-language question; the agent writes one
BigQuery Standard SQL query, runs it, and streams chat tokens, tool trajectory, and a
result snapshot back as AG-UI events.

`README.md` has the full prose architecture diagram, prerequisites, the env-var config
table, and Cloud Run deploy notes — read it for those. This file captures the things that
only become clear after reading several source files together.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                              # installs the `agent` + `app` packages
cp .env.example .env                          # set GOOGLE_API_KEY and BQ_COMPUTE_PROJECT_ID
gcloud auth application-default login         # ADC for BigQuery query execution
uvicorn app.server:app --reload --port 8080   # serves Dash at / and AG-UI at /agui
```

There is **no pytest suite, linter, or formatter** configured — don't invent commands for them.
But there is a **credential-free smoke test** (dummy env vars are enough — the BigQuery client is
lazy) that checks the load-bearing route order and that Dash boots through the mount:

```bash
GOOGLE_API_KEY=dummy BQ_COMPUTE_PROJECT_ID=dummy python - <<'PY'
from starlette.routing import Mount, Route
from starlette.testclient import TestClient
from app.server import app
routes = app.routes
i_route = next(i for i,r in enumerate(routes) if isinstance(r,Route) and r.path=="/agui" and "POST" in r.methods)
i_mount = next(i for i,r in enumerate(routes) if isinstance(r,Mount))
assert i_route < i_mount, "/agui must be registered before the Dash mount"
c = TestClient(app)
assert c.get("/").status_code == 200 and c.get("/assets/agui.js").status_code == 200
print("OK")
PY
```

Full verification is manual: run the server, open http://localhost:8080, ask e.g. *"top 10 product
categories by number of orders"*, and watch chat / trajectory / table+chart populate.

## Architecture: the three things that aren't obvious

**1. The UI is driven from the browser by `set_props`, not by callback return values.**
`app/assets/agui.js` is the real engine. It POSTs a `RunAgentInput` to `/agui`, reads the
SSE stream itself (there is no Python AG-UI browser client), and pushes live updates into
Dash components *by id* with `window.dash_clientside.set_props` — `#chat`, `#trajectory`,
and the `kpis` store. The `clientside_callback` in `app/dash_app.py` declares an Output
(`run-status`) only for the final status string; the streaming content does **not** flow
through Dash's normal callback graph. To add a new streamed surface you must edit *both*
files: add a component (and any rendering callback) in `dash_app.py`, and handle the
corresponding AG-UI event in `agui.js`.

**2. Data reaches the chart through agent state, not a return value.**
`bigquery_query` in `agent/data_science_agent.py` writes `last_query` / `row_count` / `rows`
into `tool_context.state`. `ag_ui_adk` turns state changes into AG-UI `STATE_SNAPSHOT` /
`STATE_DELTA` events. `agui.js` applies them (it has a tiny hand-rolled RFC 6902 JSON Patch
applier for deltas) into a local `state` object and writes it to the `kpis` dcc.Store. Only
then does a normal *server-side* callback (`render_results`) build the DataTable + bar chart
from that store. So the path is: tool → agent state → SSE STATE event → JS patch → kpis store
→ server callback → table/chart.

**3. Route registration order is load-bearing.** In `app/server.py`, `/agui` is registered
*before* the Dash WSGI app is mounted at `/`. The mount at `/` is a catch-all; registering
it first would shadow `/agui`. Keep the AG-UI endpoint above the `app.mount("/", ...)` line.

## Other things worth knowing

- **The AG-UI wire JSON is camelCase; Python constructor kwargs are snake_case.** `ag_ui` event
  classes are PascalCase `*Event` and take snake_case kwargs (`thread_id`, `message_id`), but they
  serialize to camelCase on the wire — which is why `agui.js` reads `ev.toolCallName`, `ev.snapshot`,
  `ev.delta` and the request body uses `threadId` / `runId`. When you touch one side, expect the
  other's casing. (Verified against `ag-ui-protocol==0.1.18`, `ag-ui-adk==0.6.4`, `google-adk==2.1.0`.)

- **Three different row caps** in `data_science_agent.py`, intentionally distinct:
  `_MAX_ROWS` (env `BQ_MAX_ROWS`, default 1000) bounds the BigQuery fetch; `_UI_ROW_CAP`
  (200) bounds what's pushed into dashboard state; a literal `50` bounds what's returned to
  the model. Adjust the right one for the symptom you're fixing.
- **Two BigQuery projects are separate by design:** `BQ_COMPUTE_PROJECT_ID` is billed for
  running queries; `BQ_DATA_PROJECT_ID`/`BQ_DATASET_ID` point at where the data lives
  (defaults to public `bigquery-public-data.thelook_ecommerce`, so no data loading needed).
- **The agent is deliberately minimal** — one `LlmAgent`, one `bigquery_query` tool, no RAG
  / Code Interpreter / AlloyDB. BQML statements (`CREATE MODEL`, `ML.PREDICT`) are just SQL
  through the same tool. Keep it small unless asked otherwise.
- **State services are in-memory** (`use_in_memory_services=True`), so a deployment must pin
  `--max-instances=1` or a thread's state won't survive across instances.
- `_jsonable()` exists because BigQuery returns dates/`Decimal`/`bytes` that aren't
  JSON-serializable for the SSE stream — extend it if a query returns a new unsupported type.
