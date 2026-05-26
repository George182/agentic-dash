// AG-UI SSE consumer for Dash. Registered as window.dash_clientside.agui.run and
// wired to the Run button via a clientside_callback in dash_app.py.
//
// There is no Python browser client for AG-UI, so the stream is read here in JS:
// POST RunAgentInput -> read the text/event-stream -> push deltas into Dash
// components by id with set_props. The callback's declared Output is only used
// for the final status; live updates happen through set_props.

window.dash_clientside = window.dash_clientside || {};

// Minimal RFC 6902 JSON Patch applier (add / replace / remove), enough for
// AG-UI STATE_DELTA events.
function applyPatch(state, ops) {
  for (const op of ops || []) {
    const parts = op.path
      .split("/")
      .slice(1)
      .map((p) => p.replace(/~1/g, "/").replace(/~0/g, "~"));
    let obj = state;
    for (let i = 0; i < parts.length - 1; i++) obj = obj[parts[i]];
    const key = parts[parts.length - 1];
    if (op.op === "remove") {
      Array.isArray(obj) ? obj.splice(+key, 1) : delete obj[key];
    } else if (op.op === "add" && Array.isArray(obj)) {
      key === "-" ? obj.push(op.value) : obj.splice(+key, 0, op.value);
    } else {
      obj[key] = op.value;
    }
  }
  return state;
}

window.dash_clientside.agui = {
  run: async function (n_clicks, prompt) {
    const no_update = window.dash_clientside.no_update;
    if (!n_clicks || !prompt) return no_update;
    const sp = window.dash_clientside.set_props;

    let chat = "";
    const traj = [];
    let state = {};
    sp("chat", { children: "" });
    sp("trajectory", { children: "" });
    sp("run-status", { children: "running…" });

    const body = {
      threadId: "dash-thread",
      runId: crypto.randomUUID(),
      state: {},
      tools: [],
      context: [],
      forwardedProps: {},
      messages: [{ id: crypto.randomUUID(), role: "user", content: prompt }],
    };

    let res;
    try {
      res = await fetch("/agui", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        },
        body: JSON.stringify(body),
      });
    } catch (e) {
      return "request failed: " + e;
    }
    if (!res.ok || !res.body) return "error: HTTP " + res.status;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const frames = buf.split("\n\n");
      buf = frames.pop(); // keep trailing partial frame
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let ev;
        try {
          ev = JSON.parse(line.slice(6));
        } catch {
          continue;
        }
        switch (ev.type) {
          case "TEXT_MESSAGE_CONTENT":
            chat += ev.delta || "";
            sp("chat", { children: chat });
            break;
          case "TOOL_CALL_START":
            traj.push("→ " + (ev.toolCallName || ev.toolCallId || "tool"));
            sp("trajectory", { children: traj.join("\n") });
            break;
          case "TOOL_CALL_RESULT":
            traj.push("✓ result");
            sp("trajectory", { children: traj.join("\n") });
            break;
          case "STATE_SNAPSHOT":
            state = ev.snapshot || {};
            sp("kpis", { data: state });
            break;
          case "STATE_DELTA":
            state = applyPatch(state, ev.delta);
            sp("kpis", { data: state });
            break;
          case "RUN_ERROR":
            return "run error: " + (ev.message || "");
        }
      }
    }
    return "done";
  },
};
