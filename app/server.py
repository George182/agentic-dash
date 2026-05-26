"""FastAPI shell: AG-UI SSE endpoint + the Dash app, in one container.

Order matters: the `/agui` route is registered BEFORE the Dash app is mounted
at "/", so the catch-all WSGI mount doesn't shadow it.
"""

import os

from a2wsgi import WSGIMiddleware
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from agent.data_science_agent import root_agent  # noqa: E402
from app.dash_app import dash_app  # noqa: E402
from ag_ui_adk import ADKAgent, add_adk_fastapi_endpoint  # noqa: E402

app = FastAPI(title="agentic-dash")

# 1) AG-UI endpoint first (POST /agui -> text/event-stream).
_agui_agent = ADKAgent(
    adk_agent=root_agent,
    user_id=os.environ.get("AGUI_USER_ID", "dashboard"),
    use_in_memory_services=True,
)
add_adk_fastapi_endpoint(app, _agui_agent, path="/agui")

# 2) Dash (a Flask/WSGI app) mounted at root last.
app.mount("/", WSGIMiddleware(dash_app.server))
