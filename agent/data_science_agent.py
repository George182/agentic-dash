"""Minimal custom data-science agent: Gemini + a single BigQuery tool.

Deliberately small — no Vertex RAG, Code Interpreter, or AlloyDB. The one
`bigquery_query` tool runs Standard SQL (including BQML statements) and writes a
result snapshot into agent state, which `ag_ui_adk` surfaces to the dashboard as
AG-UI STATE events.
"""

from __future__ import annotations

import datetime
import decimal
import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext
from google.cloud import bigquery

# Project billed for query execution (must have the BigQuery API enabled).
_COMPUTE_PROJECT = os.environ.get("BQ_COMPUTE_PROJECT_ID") or os.environ.get(
    "GOOGLE_CLOUD_PROJECT"
)
# Where the data lives — defaults to a public dataset so "live" needs no data loading.
_DATA_PROJECT = os.environ.get("BQ_DATA_PROJECT_ID", "bigquery-public-data")
_DATASET = os.environ.get("BQ_DATASET_ID", "thelook_ecommerce")
_MAX_ROWS = int(os.environ.get("BQ_MAX_ROWS", "1000"))
_UI_ROW_CAP = 200  # cap rows pushed to the dashboard state

_client: bigquery.Client | None = None


def _bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=_COMPUTE_PROJECT)
    return _client


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def bigquery_query(tool_context: ToolContext, sql: str) -> dict[str, Any]:
    """Run a BigQuery Standard SQL query and return the rows.

    Use fully-qualified table names, e.g.
    `bigquery-public-data.thelook_ecommerce.orders`. BQML statements
    (CREATE MODEL, ML.PREDICT, ML.EVALUATE) are valid SQL and run through this
    same tool. Prefer small aggregations suitable for a dashboard.
    """
    job = _bq().query(sql)
    rows = [
        {k: _jsonable(v) for k, v in dict(row).items()}
        for row in job.result(max_results=_MAX_ROWS)
    ]
    # Surface a snapshot to the dashboard via agent state -> AG-UI STATE events.
    tool_context.state["last_query"] = sql
    tool_context.state["row_count"] = len(rows)
    tool_context.state["rows"] = rows[:_UI_ROW_CAP]
    return {
        "status": "success",
        "row_count": len(rows),
        "rows": rows[:50],  # a smaller slice back to the model
    }


def _on_before_agent(callback_context) -> None:
    callback_context.state.setdefault("row_count", 0)
    callback_context.state.setdefault("rows", [])
    return None


_INSTRUCTION = f"""You are a data-science assistant for a BigQuery dataset.

Default dataset: `{_DATA_PROJECT}.{_DATASET}` (BigQuery public data).

For each user question:
1. Write ONE BigQuery Standard SQL query against the dataset.
2. Call the `bigquery_query` tool with that SQL.
3. Summarize the result in plain language.

Always use fully-qualified table names. Prefer compact aggregations
(GROUP BY / LIMIT) that make sense on a dashboard rather than raw row dumps.
"""

root_agent = LlmAgent(
    name="DataScienceAgent",
    model=os.environ.get("AGENT_MODEL", "gemini-2.5-flash"),
    instruction=_INSTRUCTION,
    tools=[bigquery_query],
    before_agent_callback=_on_before_agent,
)
