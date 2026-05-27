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
_MODEL = f"{_DATA_PROJECT}.{_DATASET}.arima_sales"
_HISTORY_DAYS = 90  # actuals shown before the forecast on the chart

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


def bqml_forecast(
    tool_context: ToolContext,
    country: str,
    store: str,
    product: str,
    horizon: int = 30,
) -> dict[str, Any]:
    """Forecast `num_sold` for ONE (country, store, product) series with the
    BQML ARIMA_PLUS model, and push a history+forecast snapshot to the dashboard.

    Returns recent actuals plus the forecast points (value + 80% interval).
    """
    horizon = max(1, min(int(horizon), 30))  # model was trained with HORIZON=30
    sql = f"""
    WITH actuals AS (
      SELECT date, num_sold AS actual,
             CAST(NULL AS FLOAT64) AS forecast,
             CAST(NULL AS FLOAT64) AS lo, CAST(NULL AS FLOAT64) AS hi
      FROM `{_DATA_PROJECT}.{_DATASET}.train`
      WHERE country=@country AND store=@store AND product=@product
        AND num_sold IS NOT NULL
      ORDER BY date DESC LIMIT @history
    ),
    fc AS (
      SELECT DATE(forecast_timestamp) AS date,
             CAST(NULL AS INT64) AS actual,
             forecast_value AS forecast,
             confidence_interval_lower_bound AS lo,
             confidence_interval_upper_bound AS hi
      FROM ML.FORECAST(MODEL `{_MODEL}`,
                       STRUCT(@horizon AS horizon, 0.8 AS confidence_level))
      WHERE country=@country AND store=@store AND product=@product
    )
    SELECT * FROM actuals
    UNION ALL SELECT * FROM fc
    ORDER BY date
    """
    params = [
        bigquery.ScalarQueryParameter("country", "STRING", country),
        bigquery.ScalarQueryParameter("store", "STRING", store),
        bigquery.ScalarQueryParameter("product", "STRING", product),
        bigquery.ScalarQueryParameter("horizon", "INT64", horizon),
        bigquery.ScalarQueryParameter("history", "INT64", _HISTORY_DAYS),
    ]
    job = _bq().query(
        sql, job_config=bigquery.QueryJobConfig(query_parameters=params)
    )
    rows = [{k: _jsonable(v) for k, v in dict(r).items()} for r in job.result()]

    # Snapshot for the dashboard — rides along in the same agent-state stream.
    tool_context.state["forecast"] = rows
    tool_context.state["forecast_series"] = {
        "country": country, "store": store,
        "product": product, "horizon": horizon,
    }
    forecast_pts = [r for r in rows if r["forecast"] is not None]
    return {
        "status": "success",
        "series": f"{country} / {store} / {product}",
        "horizon": horizon,
        "history_points": len(rows) - len(forecast_pts),
        "forecast": forecast_pts[:50],
    }


def _on_before_agent(callback_context) -> None:
    callback_context.state.setdefault("row_count", 0)
    callback_context.state.setdefault("rows", [])
    return None


_DIMENSIONS = """- country: Canada, Finland, Italy, Kenya, Norway, Singapore
- store: Discount Stickers, Premium Sticker Mart, Stickers for Less
- product: Holographic Goose, Kaggle, Kaggle Tiers, Kerneler, Kerneler Dark Mode"""

_INSTRUCTION = f"""You are a data-science assistant for a daily sticker-sales dataset in BigQuery.

Tables (always use fully-qualified names):
- `{_DATA_PROJECT}.{_DATASET}.train` — daily history 2010-01-01..2016-12-26
- `{_DATA_PROJECT}.{_DATASET}.test`  — 2016 holdout
Columns: id INT64, date DATE, country STRING, store STRING, product STRING, num_sold INT64.
The data is 90 daily time series = every combination of:
{_DIMENSIONS}
A trained BQML ARIMA_PLUS model `{_MODEL}` forecasts num_sold (88 of the 90
series fit; two near-empty "Holographic Goose" series have no forecast).

Choose the right tool:
- For exploration/aggregation (totals, comparisons, distinct values), write ONE
  Standard SQL query and call `bigquery_query`.
- For a forecast of a SINGLE series, call `bqml_forecast` with the exact
  country, store and product (and optional horizon, 1-30 days, default 30).
  Do NOT hand-write ML.FORECAST SQL — the tool runs it and feeds the chart.

After a tool runs, summarize the result in plain language. Prefer compact
aggregations (GROUP BY / LIMIT) over raw row dumps.
"""

root_agent = LlmAgent(
    name="DataScienceAgent",
    model=os.environ.get("AGENT_MODEL", "gemini-2.5-flash"),
    instruction=_INSTRUCTION,
    tools=[bigquery_query, bqml_forecast],
    before_agent_callback=_on_before_agent,
)
