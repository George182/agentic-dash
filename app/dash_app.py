"""Plotly Dash frontend. Mounted under the FastAPI shell (see server.py).

The streaming surfaces (chat, trajectory, KPI store) are driven from the
browser by assets/agui.js via `window.dash_clientside.set_props` — NOT from
callback return values. A normal server-side callback renders the results
table + chart from the `kpis` store once it's populated.
"""

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
)
import plotly.graph_objects as go

dash_app = Dash(__name__)
dash_app.title = "agentic-dash"

dash_app.layout = html.Div(
    style={"maxWidth": "1100px", "margin": "0 auto", "fontFamily": "sans-serif",
           "padding": "1rem"},
    children=[
        html.H2("agentic-dash"),
        html.Div(
            style={"display": "flex", "gap": "0.5rem", "alignItems": "center"},
            children=[
                dcc.Input(id="prompt", type="text",
                          placeholder="Ask about the dataset…",
                          style={"flex": 1}),
                html.Button("Run", id="run", n_clicks=0),
                html.Span(id="run-status", style={"color": "#888"}),
            ],
        ),
        html.Div(
            style={"display": "flex", "gap": "2rem", "marginTop": "1rem"},
            children=[
                html.Div([html.H4("Chat"), dcc.Markdown(id="chat")],
                         style={"flex": 1}),
                html.Div([html.H4("Agent trajectory"),
                          html.Pre(id="trajectory",
                                   style={"whiteSpace": "pre-wrap"})],
                         style={"flex": 1}),
            ],
        ),
        html.H4("Results"),
        dcc.Store(id="kpis"),
        dcc.Graph(id="kpi-chart"),
        dash_table.DataTable(id="kpi-table", page_size=10,
                             style_table={"overflowX": "auto"}),
        html.H4("Forecast"),
        dcc.Graph(id="forecast-chart"),
    ],
)


@callback(
    Output("kpi-table", "data"),
    Output("kpi-table", "columns"),
    Output("kpi-chart", "figure"),
    Input("kpis", "data"),
    prevent_initial_call=True,
)
def render_results(state):
    rows = (state or {}).get("rows") or []
    if not rows:
        return [], [], go.Figure()
    keys = list(rows[0].keys())
    columns = [{"name": k, "id": k} for k in keys]
    fig = go.Figure()
    if len(keys) >= 2:
        fig = go.Figure(go.Bar(x=[r[keys[0]] for r in rows],
                               y=[r[keys[1]] for r in rows]))
        fig.update_layout(xaxis_title=keys[0], yaxis_title=keys[1],
                          margin=dict(l=20, r=20, t=20, b=20))
    return rows, columns, fig


@callback(
    Output("forecast-chart", "figure"),
    Input("kpis", "data"),
    prevent_initial_call=True,
)
def render_forecast(state):
    rows = (state or {}).get("forecast") or []
    if not rows:
        return go.Figure()
    x = [r["date"] for r in rows]
    fig = go.Figure()
    # 80% confidence band: upper bound, then lower with fill between the two.
    fig.add_trace(go.Scatter(x=x, y=[r.get("hi") for r in rows],
                             line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=[r.get("lo") for r in rows], fill="tonexty",
                             fillcolor="rgba(0,100,200,0.15)", line=dict(width=0),
                             name="80% interval", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=[r.get("actual") for r in rows], mode="lines",
                             name="actual", line=dict(color="#444")))
    fig.add_trace(go.Scatter(x=x, y=[r.get("forecast") for r in rows], mode="lines",
                             name="forecast", line=dict(color="#0064c8", dash="dash")))
    meta = (state or {}).get("forecast_series") or {}
    title = " / ".join(str(meta.get(k, "")) for k in ("country", "store", "product"))
    fig.update_layout(title=title or "Forecast", xaxis_title="date",
                      yaxis_title="num_sold", margin=dict(l=20, r=20, t=40, b=20))
    return fig


# Browser-side streamer: POSTs RunAgentInput to /agui, reads the SSE, and pushes
# deltas into #chat / #trajectory / the kpis store via set_props.
clientside_callback(
    ClientsideFunction(namespace="agui", function_name="run"),
    Output("run-status", "children"),
    Input("run", "n_clicks"),
    State("prompt", "value"),
    prevent_initial_call=True,
)
