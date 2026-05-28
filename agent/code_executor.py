"""Code executor that stages sandbox image outputs for an `after_agent_callback`.

Why a two-step (executor stages → callback commits) design instead of writing
state from the executor directly:

- `AgentEngineSandboxCodeExecutor.execute_code` is **synchronous**, but ADK's
  artifact API (`callback_context.save_artifact`) and the state-delta channel
  are both designed around `async` callbacks/tools. The async surfaces are
  also the ones that write through the `State` proxy (so state changes are
  emitted as `event.actions.state_delta` and forwarded by `AgentTool` to the
  parent session — see `google/adk/tools/agent_tool.py`).
- `Session.state` is a raw `dict[str, Any]`; direct writes from the executor
  are visible inside the sub-agent's session but NOT forwarded to the parent.
  So the executor only **stages** raw file bytes here, in a `temp:`-prefixed
  state key. The companion `after_agent_callback` in `analytics_agent.py`
  drains the stage and surfaces each PNG through both canonical ADK channels:
  the artifact service (auto-forwarded to the parent via
  `ForwardingArtifactService`) and `state["figures"]` via the State proxy.

This keeps the sandbox executor's behaviour identical to the upstream
`google/adk-samples` data-science agent — the only addition is the stage.
"""

from __future__ import annotations

from typing_extensions import override

from google.adk.agents.invocation_context import InvocationContext
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.code_executors.code_execution_utils import (
    CodeExecutionInput,
    CodeExecutionResult,
)

STAGE_KEY = "temp:_pending_figures"


class CapturingAgentEngineSandboxExecutor(AgentEngineSandboxCodeExecutor):
    """Stages each `image/*` output file in `session.state[STAGE_KEY]`."""

    @override
    def execute_code(
        self,
        invocation_context: InvocationContext,
        code_execution_input: CodeExecutionInput,
    ) -> CodeExecutionResult:
        result = super().execute_code(invocation_context, code_execution_input)

        staged: list[dict] = []
        for f in result.output_files:
            if not (f.mime_type or "").startswith("image/"):
                continue
            content = f.content.encode() if isinstance(f.content, str) else f.content
            staged.append({"name": f.name, "mime_type": f.mime_type, "content": content})

        if staged:
            existing = invocation_context.session.state.get(STAGE_KEY) or []
            invocation_context.session.state[STAGE_KEY] = existing + staged

        return result
