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
2. `ag_ui_adk` drives the ADK `DataScienceAgent`, which calls the single
   `bigquery_query` tool to run Standard SQL.
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
- A Gemini API key (Google AI Studio) **or** a Vertex AI setup
- A GCP project with the BigQuery API enabled, plus Application Default
  Credentials (`gcloud auth application-default login`) for query execution
- Data defaults to the public `bigquery-public-data.thelook_ecommerce` dataset,
  so no data loading is required

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env     # set GOOGLE_API_KEY and BQ_COMPUTE_PROJECT_ID
uvicorn app.server:app --reload --port 8080
```

Open http://localhost:8080 and ask, e.g., *"top 10 product categories by number
of orders"*. Watch the chat stream, the trajectory fill, and the results table +
bar chart populate.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Purpose | Default |
|----------|---------|---------|
| `GOOGLE_API_KEY` | Gemini AI Studio key (or use Vertex vars) | — |
| `BQ_COMPUTE_PROJECT_ID` | Project billed for queries | `GOOGLE_CLOUD_PROJECT` |
| `BQ_DATA_PROJECT_ID` | Project the data lives in | `bigquery-public-data` |
| `BQ_DATASET_ID` | Default dataset | `thelook_ecommerce` |
| `BQ_MAX_ROWS` | Max rows fetched per query | `1000` |
| `AGENT_MODEL` | Gemini model | `gemini-2.5-flash` |
| `AGUI_USER_ID` | AG-UI user id | `dashboard` |

## Deploy to Cloud Run

```bash
gcloud run deploy agentic-dash --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --max-instances=1 \
  --set-env-vars BQ_COMPUTE_PROJECT_ID=your-project,GOOGLE_API_KEY=your-key
```

Notes:

- **SSE timeout:** `/agui` is a long-lived stream. Raise Cloud Run's request
  timeout (`--timeout`) if long queries get cut off.
- **In-memory state:** session/state services are in-memory, so pin
  `--max-instances=1` (or wire an external store) to keep a thread's state on a
  single instance.
