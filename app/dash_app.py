"""Plotly Dash frontend. Mounted under the FastAPI shell (see server.py).

The streaming surfaces (chat, trajectory, KPI store) are driven from the
browser by assets/agui.js via `window.dash_clientside.set_props` — NOT from
callback return values. Normal server-side callbacks render the results
table + charts, the KPI chips, the last query, and the analysis figures from
the `kpis` store once it's populated.

Layout: a dark "Instrument" theme in two tabs — a Dashboard tab (KPIs +
forecast on top, ask bar + last query, scrollable chat/trajectory, an Analysis
panel for code-interpreter figures, then the query result) and a read-only
Models tab (BQML model list + ML.EVALUATE metrics). Every component id used by
agui.js / the callback graph is preserved.
"""

import os

from dash import (
    Dash,
    Input,
    Output,
    State,
    callback,
    ClientsideFunction,
    clientside_callback,
    dash_table,
    dcc,
    html,
    no_update,
)

from . import model_monitor
import plotly.graph_objects as go
import plotly.io as pio

# ---- Theme constants (mirror app/assets/style.css :root tokens) ----
BG = "#0B0F0E"
INK = "#E8ECEB"
INK_SOFT = "#8A9794"
INK_FAINT = "#5A6663"
LINE = "rgba(255,255,255,0.10)"
GRID = "rgba(255,255,255,0.06)"
ACCENT = "#F4B860"        # amber — bars / primary series
ACCENT_2 = "#4FD1C5"      # mint — forecast / code
BAND = "rgba(79,209,197,0.16)"
MONO = "'JetBrains Mono', ui-monospace, monospace"
DISP = "'Martian Mono', ui-monospace, monospace"

# ---- Shared dark Plotly template (themes every figure, incl. empty ones) ----
pio.templates["instrument"] = go.layout.Template(
    layout=dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=MONO, color=INK, size=12),
        colorway=[ACCENT, ACCENT_2, "#C2602F", INK_SOFT],
        margin=dict(l=48, r=24, t=28, b=40),
        xaxis=dict(gridcolor=GRID, zerolinecolor=GRID, linecolor=LINE,
                   tickfont=dict(color=INK_FAINT), title=dict(font=dict(color=INK_SOFT))),
        yaxis=dict(gridcolor=GRID, zerolinecolor=GRID, linecolor=LINE,
                   tickfont=dict(color=INK_FAINT), title=dict(font=dict(color=INK_SOFT))),
        legend=dict(font=dict(color=INK_SOFT), bgcolor="rgba(0,0,0,0)"),
        title=dict(font=dict(color=INK)),
    )
)
pio.templates.default = "instrument"
_TPL = pio.templates["instrument"]


def _empty_fig():
    """An empty figure carrying the dark template (so the chart area is
    transparent/dark even before any data — Plotly.js can't see the Python
    default template, so it must be embedded in the figure)."""
    return go.Figure(layout=dict(template=_TPL))


dash_app = Dash(
    __name__,
    title="agentic-dash",
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
)

_MODEL = os.environ.get("AGENT_MODEL", "gemini-2.5-flash")


# ---- small layout helpers ----------------------------------------------------
def _panel_head(title, meta="", tick_class="tick"):
    return html.Div(className="panel-head", children=[
        html.Span(className=tick_class),
        html.H2(title),
        html.Span(meta, className="meta"),
    ])


def _chip(label, value, sub, unit=None, mono=False):
    val_kids = [value] if unit is None else [value, html.Span(unit, className="k-unit")]
    return html.Div(className="kpi-chip", children=[
        html.Span(label, className="k-label"),
        html.Span(val_kids, className="k-val mono" if mono else "k-val"),
        html.Span(sub, className="k-sub"),
    ])


_TABLE_KW = dict(
    page_size=8,
    style_as_list_view=True,
    style_table={"overflowX": "auto"},
    style_header={
        "backgroundColor": "transparent", "color": INK_FAINT, "fontFamily": MONO,
        "fontSize": "10.5px", "letterSpacing": "0.08em", "textTransform": "uppercase",
        "border": "none", "borderBottom": f"1px solid {LINE}", "textAlign": "left",
        "padding": "10px 12px",
    },
    style_cell={
        "backgroundColor": "transparent", "color": INK_SOFT, "fontFamily": MONO,
        "fontSize": "13px", "border": "none",
        "borderBottom": "1px solid rgba(255,255,255,0.045)", "padding": "9px 12px",
        "textAlign": "left",
    },
    style_data_conditional=[{"if": {"row_index": "odd"},
                             "backgroundColor": "rgba(255,255,255,0.015)"}],
)


# ---- Dashboard tab (user view) ----------------------------------------------
_dashboard = html.Div(id="tab-dashboard-panel", children=[
    # KPI hero row (filled by render_chips)
    html.Div(id="kpi-chips", className="kpis-row reveal d2", children=[
        _chip("rows", "—", "in result"),
        _chip("columns", "—", "in result"),
        _chip("forecast", "—", "horizon"),
    ]),

    # Forecast hero (top, wide)
    html.Div(className="panel panel-forecast reveal d3", children=[
        _panel_head("FORECAST", "ARIMA_PLUS · 80% interval", "tick tick-3"),
        dcc.Graph(id="forecast-chart", figure=_empty_fig(),
                  config={"displayModeBar": False}, style={"height": "260px"}),
    ]),

    # Ask bar + last query (below forecast)
    html.Div(className="ask-block reveal d4", children=[
        html.Div(className="search-bar", children=[
            html.Div(className="field", children=[
                html.Span(">", className="caret"),
                dcc.Input(id="prompt", type="text",
                          placeholder="ask about the data — or “plot …” to run Python"),
            ]),
            html.Button([html.Span("▶", className="arrow"), " RUN"], id="run", n_clicks=0),
            html.Span("idle", id="run-status"),
        ]),
        html.Div(id="last-query", className="last-query"),
    ]),

    # Conversation: chat (scrollable) | trajectory
    html.Div(className="panes reveal d4", children=[
        html.Div(className="panel panel-chat", children=[
            _panel_head("CHAT", "session", "tick"),
            dcc.Markdown(id="chat", dangerously_allow_html=True, link_target="_blank"),
        ]),
        html.Div(className="panel panel-traj", children=[
            _panel_head("TRAJECTORY", "tool calls", "tick tick-2"),
            html.Pre(id="trajectory", style={"whiteSpace": "pre-wrap"}),
        ]),
    ]),

    # Analysis: code-executed figures (filled by render_analysis)
    html.Div(className="panel panel-analysis reveal d5", children=[
        _panel_head("ANALYSIS", "vertex code interpreter · matplotlib", "tick tick-code"),
        html.Div(id="analysis", children=html.Div(
            "No code-execution output yet — ask “plot …” to run Python.",
            className="empty-note")),
    ]),

    # Query result: chart + table
    html.Div(className="panel panel-results reveal d5", children=[
        _panel_head("QUERY RESULT", "", "tick"),
        html.Div(className="results-grid", children=[
            dcc.Graph(id="kpi-chart", figure=_empty_fig(),
                      config={"displayModeBar": False}, style={"height": "280px"}),
            html.Div(className="tbl-wrap", children=[
                dash_table.DataTable(id="kpi-table", **_TABLE_KW),
            ]),
        ]),
    ]),
])


# ---- Models tab (read-only; populated from state when an eval runs) ----------
_models = html.Div(id="tab-models-panel", style={"display": "none"}, children=[
    html.Div(id="models-content", children=html.Div(
        "No model evaluation yet — ask the agent to evaluate a BQML model "
        "(e.g. “evaluate arima_sales on the test set”) to populate this tab.",
        className="empty-note")),
])


dash_app.layout = html.Div([
    html.Div(className="scanlines"),
    dcc.Store(id="kpis"),
    html.Div(className="wrap", children=[
        # Header
        html.Div(className="app-header reveal d1", children=[
            html.Div(className="brand", children=[
                html.H1(["agentic_dash", html.Span("▍", className="cursor")],
                        className="wordmark"),
                html.P("Ask the sticker-sales data in plain English — SQL, BQML "
                       "forecasts & on-the-fly Python, streamed live.", className="tagline"),
            ]),
            html.Div(className="readout", children=[
                html.Span([html.Span(className="led"), " LIVE"], className="status-pill"),
                html.Span(f"{_MODEL} · vertex-ci", className="clock"),
            ]),
        ]),

        # Tab selector (content panels are rendered below and toggled clientside)
        dcc.Tabs(id="tabs", value="dashboard", className="tabbar", children=[
            dcc.Tab(label="DASHBOARD", value="dashboard",
                    className="tab", selected_className="tab--selected"),
            dcc.Tab(label="MODELS", value="models",
                    className="tab", selected_className="tab--selected"),
        ]),

        _dashboard,
        _models,

        html.P("agentic-dash · AG-UI over SSE · BigQuery + BQML ARIMA_PLUS + Vertex "
               "Code Interpreter", className="foot reveal d5"),
    ]),
])


# ---- Server callbacks (read off the kpis store) ------------------------------
@callback(
    Output("kpi-table", "data"),
    Output("kpi-table", "columns"),
    Output("kpi-chart", "figure"),
    Input("kpis", "data"),
    prevent_initial_call=True,
)
def render_results(state):
    """Render the result table + a bar chart, choosing a label x and a numeric y
    instead of blindly barring the first two columns."""
    rows = (state or {}).get("rows") or []
    if not rows:
        return [], [], _empty_fig()
    keys = list(rows[0].keys())
    columns = [{"name": k, "id": k} for k in keys]

    def is_num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    numeric_keys = [k for k in keys if any(is_num(r.get(k)) for r in rows)]
    label_keys = [k for k in keys if k not in numeric_keys]
    fig = _empty_fig()
    if numeric_keys:
        x_key = label_keys[0] if label_keys else keys[0]
        y_key = next((k for k in numeric_keys if k != x_key), numeric_keys[0])
        fig = go.Figure(go.Bar(x=[r.get(x_key) for r in rows],
                               y=[r.get(y_key) for r in rows],
                               marker_color=ACCENT))
        fig.update_layout(template=_TPL, xaxis_title=x_key, yaxis_title=y_key)
    return rows, columns, fig


@callback(
    Output("forecast-chart", "figure"),
    Input("kpis", "data"),
    prevent_initial_call=True,
)
def render_forecast(state):
    rows = (state or {}).get("forecast") or []
    if not rows:
        return _empty_fig()
    x = [r["date"] for r in rows]
    fig = go.Figure()
    # 80% confidence band: upper bound, then lower with fill between the two.
    fig.add_trace(go.Scatter(x=x, y=[r.get("hi") for r in rows],
                             line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=[r.get("lo") for r in rows], fill="tonexty",
                             fillcolor=BAND, line=dict(width=0),
                             name="80% interval", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=[r.get("actual") for r in rows], mode="lines",
                             name="actual", line=dict(color=INK, width=2)))
    fig.add_trace(go.Scatter(x=x, y=[r.get("forecast") for r in rows], mode="lines",
                             name="forecast", line=dict(color=ACCENT_2, width=2.4, dash="dash")))
    meta = (state or {}).get("forecast_series") or {}
    title = " / ".join(str(meta.get(k, "")) for k in ("country", "store", "product"))
    fig.update_layout(template=_TPL, title=title or "Forecast",
                      xaxis_title="date", yaxis_title="num_sold")
    return fig


@callback(
    Output("kpi-chips", "children"),
    Input("kpis", "data"),
    prevent_initial_call=True,
)
def render_chips(state):
    state = state or {}
    rows = state.get("rows") or []
    n_rows = state.get("row_count", len(rows))
    n_cols = len(rows[0]) if rows else 0
    series = state.get("forecast_series") or {}
    if series:
        fc = (str(series.get("horizon", "—")), f"{series.get('product', '')} forecast")
    else:
        fc = ("—", "horizon")
    return [
        _chip("rows", f"{n_rows:,}" if isinstance(n_rows, int) else "—", "in result"),
        _chip("columns", str(n_cols), "in result"),
        _chip("forecast", fc[0], fc[1]),
    ]


@callback(
    Output("last-query", "children"),
    Input("kpis", "data"),
    prevent_initial_call=True,
)
def render_last_query(state):
    sql = (state or {}).get("last_query")
    if not sql:
        return ""
    return [html.Span("last_query", className="lq-label"), html.Code(" ".join(sql.split()))]


@callback(
    Output("analysis", "children"),
    Input("kpis", "data"),
    prevent_initial_call=True,
)
def render_analysis(state):
    """Render whatever the analytics sub-agent surfaced this run.

    The Analysis card is tolerant — it shows whichever of these is present:
    - `state["analytics_agent_output"]`: the LLM's written findings (always
      populated when `call_analytics_agent` ran, even if no figure landed).
    - `state["figures"]`: data-URI PNGs from the code-executor (populated by
      the figure-bridge in `analytics_agent.py`'s after_agent_callback).
    """
    state = state or {}
    findings = (state.get("analytics_agent_output") or "").strip()
    figures = state.get("figures") or []
    if not findings and not figures:
        return html.Div("No code-execution output yet — ask “plot …” to run Python.",
                        className="empty-note")
    children: list = []
    if findings:
        children.append(html.Div(className="analysis-findings", children=[
            html.Span("findings", className="figure-tag"),
            dcc.Markdown(findings),
        ]))
    for uri in figures:
        children.append(html.Div(className="figure-frame", children=[
            html.Span("figure.png", className="figure-tag"),
            html.Img(src=uri),
        ]))
    return children


@callback(
    Output("models-content", "children"),
    Input("tabs", "value"),
    prevent_initial_call=True,
)
def render_models(tab):
    """Monitoring view: BQML model list + per-series ML.EVALUATE accuracy.

    Fires on tab change. When `tab == 'models'` we pull (cached) rows from
    `app.model_monitor` — a pure read-only BigQuery monitor, no agent
    involvement. For other tabs we leave the panel untouched so the cached
    render survives tab toggles."""
    if tab != "models":
        return no_update
    models = model_monitor.get_models()
    evals = model_monitor.get_model_eval()
    if not models and not evals:
        return html.Div(
            "No model monitoring data — check BigQuery credentials and that "
            "the configured dataset contains BQML models.",
            className="empty-note")

    children = []
    if models:
        cols = list(models[0].keys())
        children.append(html.Div(className="panel reveal d2", children=[
            _panel_head("MODELS", f"{len(models)} model(s)", "tick"),
            html.Div(className="tbl-wrap", children=[
                dash_table.DataTable(
                    data=models, columns=[{"name": c, "id": c} for c in cols], **_TABLE_KW),
            ]),
        ]))
    if evals:
        cols = list(evals[0].keys())
        children.append(html.Div(className="panel panel-forecast reveal d3", children=[
            _panel_head("EVALUATION", "ML.EVALUATE", "tick tick-3"),
            html.Div(className="tbl-wrap", children=[
                dash_table.DataTable(
                    data=evals, columns=[{"name": c, "id": c} for c in cols], **_TABLE_KW),
            ]),
        ]))
    return children


# Toggle which tab panel is visible (keep both mounted so streaming targets
# and callback outputs always exist in the DOM).
clientside_callback(
    """function(tab) {
        var show = {}, hide = {display: 'none'};
        return tab === 'models' ? [hide, show] : [show, hide];
    }""",
    Output("tab-dashboard-panel", "style"),
    Output("tab-models-panel", "style"),
    Input("tabs", "value"),
)


# Browser-side streamer: POSTs RunAgentInput to /agui, reads the SSE, and pushes
# deltas into #chat / #trajectory / the kpis store via set_props.
clientside_callback(
    ClientsideFunction(namespace="agui", function_name="run"),
    Output("run-status", "children"),
    Input("run", "n_clicks"),
    State("prompt", "value"),
    prevent_initial_call=True,
)
