"""End-to-end smoke tests for the NL2SQL path.

Mirrors what `app/assets/agui.js` does: POSTs a `RunAgentInput` to `/agui`,
parses the SSE stream, and verifies that each canonical NL question

  - invoked the `bigquery_query` tool,
  - emitted a sensible `last_query` (looks like a SELECT/WITH),
  - populated `state["row_count"]` in the expected range,
  - finished without a `RUN_ERROR`.

Run while the dashboard server is up:

    uvicorn app.server:app --port 8080 &
    python tools/test_nl2sql.py            # default port 8080
    python tools/test_nl2sql.py --port 8090

Exit code 0 if every case passes; non-zero otherwise. Prints a PASS/FAIL
summary at the end. The runs share the same `threadId` to keep AG-UI
session reuse cheap — each test gets a fresh `runId`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import requests

TESTS = [
    {
        "name": "totals_per_country",
        "prompt": "What is the total num_sold per country across the train period?",
        "min_rows": 5,
        "max_rows": 7,
    },
    {
        "name": "distinct_stores",
        "prompt": "How many distinct stores are in the train table?",
        "min_rows": 1,
        "max_rows": 1,
    },
    {
        "name": "top_3_products",
        "prompt": "Top 3 products by total num_sold across the train period.",
        "min_rows": 1,
        "max_rows": 5,
    },
    {
        "name": "filtered_average",
        "prompt": (
            "What is the average daily num_sold for Kaggle products in Canada in 2015?"
        ),
        "min_rows": 1,
        "max_rows": 1,
    },
]


def _run_one(port: int, prompt: str, timeout: float = 180.0) -> dict:
    """POST `prompt` to /agui, consume the SSE, return aggregated outcomes."""
    body = {
        "threadId": "nl2sql-tests",
        "runId": str(uuid.uuid4()),
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": prompt}],
    }
    r = requests.post(
        f"http://127.0.0.1:{port}/agui",
        json=body,
        headers={"Accept": "text/event-stream"},
        stream=True,
        timeout=timeout,
    )
    r.raise_for_status()

    tool_calls: list[str] = []
    errors: list[str] = []
    last_query: str | None = None
    row_count: int | None = None

    buf = ""
    for chunk in r.iter_content(chunk_size=None, decode_unicode=True):
        if not chunk:
            continue
        buf += chunk
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data: ")),
                None,
            )
            if not data_line:
                continue
            try:
                ev = json.loads(data_line[6:])
            except Exception:
                continue
            et = ev.get("type", "")
            if et == "TOOL_CALL_START":
                tool_calls.append(ev.get("toolCallName") or ev.get("toolCallId") or "?")
            elif et == "STATE_SNAPSHOT":
                snap = ev.get("snapshot") or {}
                if "last_query" in snap:
                    last_query = snap["last_query"]
                if "row_count" in snap:
                    row_count = snap["row_count"]
            elif et == "STATE_DELTA":
                for op in ev.get("delta") or []:
                    path = op.get("path", "")
                    if "/last_query" in path:
                        last_query = op.get("value")
                    elif "/row_count" in path:
                        row_count = op.get("value")
            elif et == "RUN_ERROR":
                errors.append(ev.get("message") or "?")

    return {
        "tool_calls": tool_calls,
        "errors": errors,
        "last_query": last_query,
        "row_count": row_count,
    }


def _verdict(test: dict, out: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if "bigquery_query" not in out["tool_calls"]:
        reasons.append(f"bigquery_query not invoked (saw {out['tool_calls']!r})")
    if out["errors"]:
        reasons.append(f"errors: {out['errors']!r}")
    rc = out["row_count"]
    if rc is None:
        reasons.append("row_count not in stream")
    elif rc < test["min_rows"] or rc > test["max_rows"]:
        reasons.append(
            f"row_count={rc} not in [{test['min_rows']},{test['max_rows']}]"
        )
    lq = (out["last_query"] or "").strip()
    if not lq:
        reasons.append("last_query not in stream")
    elif not lq.upper().lstrip("(").startswith(("SELECT", "WITH")):
        reasons.append(f"last_query doesn't open with SELECT/WITH: {lq[:80]!r}")
    return (not reasons), reasons


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    print(f"NL2SQL e2e tests → http://127.0.0.1:{args.port}/agui\n")

    results = []
    for test in TESTS:
        print(f"[run] {test['name']}: {test['prompt']!r}")
        t0 = time.time()
        try:
            out = _run_one(args.port, test["prompt"])
        except Exception as exc:
            elapsed = time.time() - t0
            results.append(
                {"name": test["name"], "pass": False, "elapsed": elapsed,
                 "row_count": None, "reasons": [f"exception: {exc}"]}
            )
            print(f"  → FAIL ({elapsed:.1f}s) exception: {exc}\n")
            continue
        elapsed = time.time() - t0
        ok, reasons = _verdict(test, out)
        results.append(
            {"name": test["name"], "pass": ok, "elapsed": elapsed,
             "row_count": out["row_count"], "reasons": reasons}
        )
        print(f"  → {'PASS' if ok else 'FAIL'}  "
              f"{elapsed:.1f}s  row_count={out['row_count']}  "
              f"tool_calls={out['tool_calls']}")
        for line in reasons:
            print(f"     - {line}")
        print()

    print("=" * 72)
    passed = sum(1 for r in results if r["pass"])
    print(f"SUMMARY: {passed}/{len(results)} passed")
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        rc = "—" if r["row_count"] is None else str(r["row_count"])
        reason = "ok" if r["pass"] else "; ".join(r["reasons"])
        print(f"  [{mark}] {r['name']:24} {r['elapsed']:5.1f}s  rc={rc:>4}  {reason}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
