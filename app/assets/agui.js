// AG-UI SSE consumer for Dash. Registered as window.dash_clientside.agui.run and
// wired to the Run button via a clientside_callback in dash_app.py.
//
// There is no Python browser client for AG-UI, so the stream is read here in JS:
// POST RunAgentInput -> read the text/event-stream -> push deltas into Dash
// components by id with set_props. The callback's declared Output is only used
// for the final status; live updates happen through set_props.
//
// Chat accumulates across Runs (multi-turn session transcript); trajectory is
// per-Run and shows real tool args, result summaries, and timing.

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

function _truncate(s, n) {
  s = String(s).replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}

window.dash_clientside.agui = {
  // Multi-turn conversation, persisted across Runs for this page session.
  history: [],

  run: async function (n_clicks, prompt) {
    const no_update = window.dash_clientside.no_update;
    if (!n_clicks || !prompt) return no_update;
    const sp = window.dash_clientside.set_props;
    const agui = window.dash_clientside.agui;
    const uuid = () => crypto.randomUUID();

    // ---- chat (accumulates across Runs) ----
    function renderChat() {
      const md = agui.history
        .map((t) => (t.role === "user" ? "**You**" : "**assistant**") + "\n\n" + (t.content || "_…_"))
        .join("\n\n---\n\n");
      sp("chat", { children: md });
      setTimeout(() => { const el = document.getElementById("chat"); if (el) el.scrollTop = el.scrollHeight; }, 0);
    }

    // Build the request messages from prior history + the new user turn, then
    // add the display turns (a user bubble + an assistant turn we stream into).
    const priorMsgs = agui.history.map((t) => ({ id: t.id, role: t.role, content: t.content }));
    const userId = uuid();
    const messages = priorMsgs.concat([{ id: userId, role: "user", content: prompt }]);
    agui.history.push({ id: userId, role: "user", content: prompt });
    const asst = { id: uuid(), role: "assistant", content: "" };
    agui.history.push(asst);
    renderChat();

    // ---- trajectory (per Run) ----
    let traj = [];
    const argsBuf = {}; // toolCallId -> accumulated args JSON
    const startT = {};  // toolCallId -> performance.now()
    const renderTraj = () => sp("trajectory", { children: traj.join("\n") });
    sp("trajectory", { children: "" });
    sp("run-status", { children: "running…" });

    let state = {};

    const body = {
      threadId: "dash-thread", // stable thread; full history is sent in messages
      runId: uuid(),
      state: {},
      tools: [],
      context: [],
      forwardedProps: {},
      messages: messages,
    };

    let res;
    try {
      res = await fetch("/agui", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
      });
    } catch (e) {
      asst.content = "_(request failed)_"; renderChat();
      return "request failed: " + e;
    }
    if (!res.ok || !res.body) {
      asst.content = "_(error)_"; renderChat();
      return "error: HTTP " + res.status;
    }

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
        try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        switch (ev.type) {
          case "TEXT_MESSAGE_CONTENT":
            asst.content += ev.delta || "";
            renderChat();
            break;

          case "TOOL_CALL_START":
            startT[ev.toolCallId] = performance.now();
            argsBuf[ev.toolCallId] = "";
            traj.push("→ " + (ev.toolCallName || ev.toolCallId || "tool"));
            renderTraj();
            break;

          case "TOOL_CALL_ARGS":
            argsBuf[ev.toolCallId] = (argsBuf[ev.toolCallId] || "") + (ev.delta || "");
            break;

          case "TOOL_CALL_END": {
            const raw = argsBuf[ev.toolCallId];
            if (raw) {
              let lines;
              try {
                const obj = JSON.parse(raw);
                lines = Object.entries(obj).map(([k, v]) =>
                  "    " + k + ": " + _truncate(typeof v === "string" ? v : JSON.stringify(v), 100));
              } catch {
                lines = ["    " + _truncate(raw, 100)];
              }
              traj.push(lines.join("\n"));
              renderTraj();
            }
            break;
          }

          case "TOOL_CALL_RESULT": {
            const ms = startT[ev.toolCallId] ? Math.round(performance.now() - startT[ev.toolCallId]) : null;
            let summary = "";
            try {
              const c = typeof ev.content === "string" ? JSON.parse(ev.content) : ev.content;
              if (c && c.row_count != null) summary = c.row_count + " rows";
              else if (c && c.series) summary = c.series;
              else if (c && c.status) summary = c.status;
            } catch { /* non-JSON result */ }
            let l = "✓ result";
            if (summary) l += " · " + summary;
            if (ms != null) l += " · " + ms + "ms";
            traj.push(l);
            renderTraj();
            break;
          }

          case "STATE_SNAPSHOT":
            state = ev.snapshot || {};
            sp("kpis", { data: state });
            break;

          case "STATE_DELTA":
            state = applyPatch(state, ev.delta);
            sp("kpis", { data: state });
            break;

          case "RUN_ERROR":
            asst.content = asst.content || "_(run error)_";
            renderChat();
            return "run error: " + (ev.message || "");
        }
      }
    }
    if (!asst.content) { asst.content = "_(no response)_"; renderChat(); }
    return "done";
  },
};
