"""Read-only BQML monitoring helpers for the Models tab.

This is the Workstream E surface — a **monitoring element**, not an LLM-driven
tool. The dashboard's `render_models` callback calls into here on Models-tab
activation; nothing in `agent/` references this module. Lazy + cached so the
credential-free smoke test stays green (no BigQuery client at import time) and
repeated tab clicks don't re-fetch. Fail-soft: any BQ error is logged and the
helper returns `[]` so the tab degrades to its empty-state copy instead of
crashing the dashboard.
"""

from __future__ import annotations

import datetime
import decimal
import logging
import os
from typing import Any

from google.cloud import bigquery

logger = logging.getLogger(__name__)

_COMPUTE_PROJECT = os.environ.get("BQ_COMPUTE_PROJECT_ID") or os.environ.get(
    "GOOGLE_CLOUD_PROJECT"
)
_DATA_PROJECT = os.environ.get("BQ_DATA_PROJECT_ID", "bigquery-public-data")
_DATASET = os.environ.get("BQ_DATASET_ID", "thelook_ecommerce")

_client: bigquery.Client | None = None
_models_cache: list[dict[str, Any]] | None = None
_eval_cache: dict[str, list[dict[str, Any]]] = {}


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


def _rows(sql: str) -> list[dict[str, Any]]:
    return [
        {k: _jsonable(v) for k, v in dict(r).items()}
        for r in _bq().query(sql).result()
    ]


def get_models(refresh: bool = False) -> list[dict[str, Any]]:
    """List BQML models in the configured dataset (cached after first call).

    Uses `bigquery.Client.list_models()` (direct API) instead of
    `INFORMATION_SCHEMA.MODELS` — the SCHEMATA path is finicky to construct
    correctly across multi-regions and BQ's parser, and the API call returns
    the same metadata without any of the SQL escape gymnastics.
    """
    global _models_cache
    if _models_cache is not None and not refresh:
        return _models_cache
    try:
        models = list(_bq().list_models(f"{_DATA_PROJECT}.{_DATASET}"))
        _models_cache = [
            {
                "model_id": m.model_id,
                "model_type": m.model_type,
                "created": _jsonable(m.created),
                "modified": _jsonable(m.modified),
            }
            for m in models
        ]
    except Exception as exc:
        # Log only the exception class so resource paths embedded in the BQ
        # error message don't leak into Cloud Logging.
        logger.warning("model_monitor.get_models failed (%s)", type(exc).__name__)
        _models_cache = []
    return _models_cache


def get_model_eval(
    model: str = "arima_sales", refresh: bool = False
) -> list[dict[str, Any]]:
    """Per-series accuracy from `ML.EVALUATE(MODEL …)` for the named model.

    `model` accepts a short name (e.g. `arima_sales`), `<dataset>.<model>`, or
    a fully qualified `<project>.<dataset>.<model>`. Cached per resolved name.
    """
    parts = model.strip("` ").split(".")
    if len(parts) == 1:
        full = f"{_DATA_PROJECT}.{_DATASET}.{parts[0]}"
    elif len(parts) == 2:
        full = f"{_DATA_PROJECT}.{parts[0]}.{parts[1]}"
    else:
        full = ".".join(parts)
    if not refresh and full in _eval_cache:
        return _eval_cache[full]
    sql = f"SELECT * FROM ML.EVALUATE(MODEL `{full}`)"
    try:
        _eval_cache[full] = _rows(sql)
    except Exception as exc:
        # The constructed `full` path embeds the project + dataset (which we
        # consider sensitive) and the BQ exception message echoes the SQL
        # verbatim. Log only the model short name + exception class.
        logger.warning(
            "model_monitor.get_model_eval(%s) failed (%s)",
            parts[-1], type(exc).__name__,
        )
        _eval_cache[full] = []
    return _eval_cache[full]
