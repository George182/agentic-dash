"""Analytics sub-agent: matplotlib charts via a Vertex Agent Engine sandbox.

Wrapped as an `AgentTool` by the root agent (see `data_science_agent.py`).
Mirrors the wiring in the upstream `google/adk-samples` data-science agent.

================================================================================
DESIRED FLOW (the contract this module is implementing)
================================================================================
1. User chats with the ROOT agent (`DataScienceAgent`).
2. Root agent routes data-fetch asks to the `bigquery_query` tool — NL2SQL,
   rows land in root `state["rows"]`.
3. When the user wants a viz, root agent calls `call_analytics_agent(question)`.
   The fetched rows are passed in the request to THIS sub-agent (state is
   shared into the child session at `AgentTool.run_async` start, per
   `google/adk/tools/agent_tool.py`).
4. Sub-agent emits Python in a code block; the executor runs it in the Vertex
   Agent Engine sandbox; matplotlib writes a PNG.
5. The `_commit_figures` after-agent callback below surfaces those PNGs via:
     (a) the artifact service (forwarded to the parent automatically), and
     (b) `state["figures"]` through the State proxy (so AgentTool's event
         loop forwards the delta back to the parent session).
6. `ag_ui_adk` streams the parent's state delta as a `STATE_DELTA` AG-UI event;
   `agui.js` writes it into the Dash `kpis` store; the `render_analysis`
   callback renders the PNGs as `<img>` in the dashboard's Analysis panel.

================================================================================
ROLLBACK NOTE (read me if this stops working as expected)
================================================================================
The figure-bridge (`_commit_figures` below + `STAGE_KEY` stage in
`code_executor.py`) is the only piece of this module NOT mirrored verbatim
from the upstream sample — the sample doesn't surface figures into a separate
UI, so it has no equivalent. If this combo breaks (e.g. ADK changes how
`after_agent_callback` interacts with `AgentTool.run_async`'s state-delta
forwarding, or the artifact service signature shifts), the fallback that
restores the sample-faithful state is:

  - Delete `_commit_figures` and remove `after_agent_callback=_commit_figures`
    from the `LlmAgent(...)` call below.
  - Revert `agent/code_executor.py` to use the base
    `AgentEngineSandboxCodeExecutor` directly (no override / no staging).
  - Accept that the dashboard's Analysis card will stay empty for plot prompts
    (figures will still surface inline to the LLM for narration, exactly as
    the upstream sample does it).

That's a 5-minute change and leaves the rest of Workstream D intact.
"""

from __future__ import annotations

import os
import sys

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.code_executors.code_execution_utils import CodeExecutionUtils
from google.genai import types

from .code_executor import (
    STAGE_KEY,
    CapturingAgentEngineSandboxExecutor,
)
from .throttle import throttle_before_model

_FIGURE_CAP = 4
_FIGURES_KEY = "figures"


_INSTRUCTION = """You are a data-visualisation agent. The user request includes
both a natural-language question and the data rows as JSON. Your job: produce
ONE clear matplotlib chart that answers the question, save it as a PNG, and
return a one-line description.

Rules:
- Use `matplotlib` (already available); call `matplotlib.use("Agg")` before
  importing `pyplot`. You may also use `pandas` for shaping the rows.
- Pick the chart type that fits the question (bar for categorical totals,
  line for time series, scatter for two numeric columns, etc.). One chart, not
  multiple subplots.
- Title the chart and label both axes. Use the data's own column names.
- Save with `plt.savefig("plot.png", dpi=110, bbox_inches="tight")`. The
  filename can be anything ending in .png/.jpg.
- End by `print(...)`-ing a single short sentence summarising what's plotted.
- Do NOT call `plt.show()`. Do NOT write more than one figure.

# --- additions layered on top of the original prompt ---

⚠ Critical: **to produce a chart you MUST emit a fenced ```python``` code
block. Describing the chart in prose alone does NOT draw it** — the dashboard
only renders what `plt.savefig(...)` writes inside the code block. Narration
around the code (a one-line intro, a one-line takeaway) is welcome.

The root agent already handled NL2SQL via `bigquery_query` and the rows are
in the request — you do NOT do SQL yourself. Your sandbox also has `numpy`
and `scipy` if the chart benefits from a transform, summary stat, or
correlation; reach for them when they help.

If the question is purely analytical (no chart needed), emit code that
computes and `print(...)`s the answer instead of drawing.
"""


def _build_executor() -> CapturingAgentEngineSandboxExecutor:
    sandbox = os.environ.get("AGENT_ENGINE_SANDBOX_RESOURCE_NAME")
    engine = os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
    kwargs = {}
    if sandbox:
        kwargs["sandbox_resource_name"] = sandbox
    if engine:
        kwargs["agent_engine_resource_name"] = engine
    return CapturingAgentEngineSandboxExecutor(**kwargs)


async def _commit_figures(callback_context: CallbackContext) -> None:
    """Drain the executor's staged PNGs and surface them via both ADK channels.

    Runs once after the sub-agent finishes. Reads the raw bytes the (sync)
    executor parked in `state[STAGE_KEY]`, saves each as an artifact (the
    parent receives it transparently via `ForwardingArtifactService`), and
    writes data URIs into `state[_FIGURES_KEY]` through the State proxy so
    `AgentTool.run_async` forwards the delta to the parent session — where
    `ag_ui_adk` streams it as a `STATE_DELTA` event.
    """
    staged = callback_context.state.get(STAGE_KEY)
    if not staged:
        return

    new_uris: list[str] = []
    for item in staged:
        name = item["name"]
        mime = item["mime_type"]
        data = item["content"]

        part = types.Part(inline_data=types.Blob(mime_type=mime, data=data))
        try:
            await callback_context.save_artifact(name, part)
        except Exception as exc:
            # Artifacts are nice-to-have for downstream tooling; the
            # dashboard render only needs the data URIs below. Log only the
            # exception class so any resource paths embedded in the artifact
            # service error don't leak into Cloud Logging.
            print(
                f"[analytics_agent] save_artifact failed ({type(exc).__name__})",
                file=sys.stderr, flush=True,
            )

        b64 = CodeExecutionUtils.get_encoded_file_content(data)
        new_uris.append(f"data:{mime};base64,{b64.decode('ascii')}")

    existing = callback_context.state.get(_FIGURES_KEY) or []
    callback_context.state[_FIGURES_KEY] = (existing + new_uris)[-_FIGURE_CAP:]
    callback_context.state[STAGE_KEY] = []


analytics_agent = LlmAgent(
    name="analytics_agent",
    model=os.environ.get("ANALYTICS_AGENT_MODEL")
    or os.environ.get("AGENT_MODEL", "gemini-2.5-flash"),
    instruction=_INSTRUCTION,
    code_executor=_build_executor(),
    before_model_callback=throttle_before_model,
    after_agent_callback=_commit_figures,
)
