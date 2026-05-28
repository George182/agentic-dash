"""Per-process throttle for Gemini calls.

Vertex AI's default per-minute quota on a fresh project is tight (often
10–60 RPM for `gemini-2.5-flash`). One analytics prompt can fire 3+ model
calls in close succession (root dispatch → sub-agent code emission → root
narration), which is enough to trip a `429 RESOURCE_EXHAUSTED`.

`throttle_before_model` is an ADK `before_model_callback`. It runs before
every model call and `asyncio.sleep`s enough to keep at least
`GEMINI_MIN_INTERVAL_S` seconds between calls. The last-call timestamp is
a module-level global so the same throttle applies across the root agent
and any sub-agents wired to it (analytics, future ones).

Concurrency caveat: the app is pinned to `--max-instances=1` (in-memory
session services + warm Agent Engine sandbox affinity), so the single
global is the right scope. Multiple simultaneous requests on one instance
would share the throttle — worst case is unnecessary smoothing, never
under-throttling.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

# Minimum seconds between Gemini calls. Default 1.2s ≈ 50 calls/minute,
# safely under the typical 60-RPM Vertex per-project quota for
# `gemini-2.5-flash` while leaving room for the model to actually do work.
MIN_INTERVAL_S: float = float(os.environ.get("GEMINI_MIN_INTERVAL_S", "1.2"))

_last_call_ts: float = 0.0


async def throttle_before_model(
    callback_context: Any, llm_request: Any
) -> None:
    """Sleep so at least MIN_INTERVAL_S has elapsed since the previous call."""
    global _last_call_ts
    elapsed = time.monotonic() - _last_call_ts
    if elapsed < MIN_INTERVAL_S:
        await asyncio.sleep(MIN_INTERVAL_S - elapsed)
    _last_call_ts = time.monotonic()
    return None
