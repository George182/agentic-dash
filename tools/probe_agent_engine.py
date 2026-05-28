"""Probe Agent Engine + sandbox creation (Option 2 path).

Mirrors what AgentEngineSandboxCodeExecutor does at first call:
  1. List Agent Engines; if none, create one.
  2. List sandboxes under it; if none, create one with 1-year TTL.
  3. Print the resource names for reuse via env vars.

Read-only on first sub-step; only creates resources if none exist.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env")

project = os.environ.get("GOOGLE_CLOUD_PROJECT")
location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
print(f"[probe] project={project!r} location={location!r}")

import vertexai
from vertexai import types

client = vertexai.Client(project=project, location=location)
print(f"[probe] vertexai.Client OK ({vertexai.__version__})")

# ----- 1. Agent Engines -----
print("\n[probe] listing existing Agent Engines ...")
engines = list(client.agent_engines.list())
for e in engines:
    name = getattr(e.api_resource, "name", "?") if hasattr(e, "api_resource") else getattr(e, "name", "?")
    display = getattr(e.api_resource, "display_name", "") if hasattr(e, "api_resource") else getattr(e, "display_name", "")
    print(f"   - {name}  display_name={display!r}")
print(f"   ({len(engines)} total)")

if engines:
    engine = engines[0]
    engine_name = engine.api_resource.name if hasattr(engine, "api_resource") else engine.name
    print(f"[probe] REUSING existing Agent Engine: {engine_name}")
else:
    print("[probe] no Agent Engine found — creating one ...")
    try:
        engine = client.agent_engines.create()
        engine_name = engine.api_resource.name
        print(f"[probe] CREATED Agent Engine: {engine_name}")
    except Exception as e:
        print(f"[probe] FAILED to create Agent Engine: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

# ----- 2. Sandboxes under that engine -----
print(f"\n[probe] listing sandboxes under {engine_name} ...")
try:
    sandboxes = list(client.agent_engines.sandboxes.list(name=engine_name))
    for s in sandboxes:
        sname = getattr(s, "name", "?")
        sstate = getattr(s, "state", "?")
        print(f"   - {sname}  state={sstate}")
    print(f"   ({len(sandboxes)} total)")
except Exception as e:
    print(f"[probe] sandboxes.list failed: {type(e).__name__}: {e}")
    sandboxes = []

if sandboxes:
    sb = sandboxes[0]
    sandbox_name = sb.name
    print(f"[probe] REUSING existing sandbox: {sandbox_name}")
else:
    print("[probe] no sandbox — creating one (1-year TTL) ...")
    try:
        operation = client.agent_engines.sandboxes.create(
            spec={"code_execution_environment": {}},
            name=engine_name,
            config=types.CreateAgentEngineSandboxConfig(
                display_name="agentic_dash_probe_sandbox",
                ttl="31536000s",
            ),
        )
        sandbox_name = operation.response.name
        print(f"[probe] CREATED sandbox: {sandbox_name}")
    except Exception as e:
        print(f"[probe] FAILED to create sandbox: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)

# ----- 3. Smoke-test execute_code (real plot + PNG output_files) -----
print("\n[probe] executing trivial matplotlib code in the sandbox ...")
code = (
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "plt.figure()\n"
    "plt.plot([1,2,3],[1,4,9])\n"
    "plt.title('probe')\n"
    "plt.savefig('out.png')\n"
    "print('done')\n"
)
try:
    resp = client.agent_engines.sandboxes.execute_code(
        name=sandbox_name,
        input_data={"code": code},
    )
    print(f"[probe] execute_code returned {len(getattr(resp,'outputs',[]) or [])} output(s)")
    for o in (resp.outputs or []):
        mt = getattr(o, "mime_type", "?")
        fname = ""
        meta = getattr(o, "metadata", None)
        if meta and getattr(meta, "attributes", None):
            fname = meta.attributes.get("file_name", b"")
            if isinstance(fname, (bytes, bytearray)):
                fname = fname.decode("utf-8", errors="replace")
        size = len(getattr(o, "data", b"") or b"")
        print(f"   - mime={mt}  file_name={fname!r}  size={size} bytes")
except Exception as e:
    print(f"[probe] execute_code failed: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(3)

print("\n[probe] SUCCESS — paste these into .env to skip lazy creation next time:")
print(f"AGENT_ENGINE_RESOURCE_NAME={engine_name}")
print(f"AGENT_ENGINE_SANDBOX_RESOURCE_NAME={sandbox_name}")
