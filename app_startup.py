# Azure App Service Entry Point
# Starts the MCP server as a background process then imports the Honeywell OT Agent app.
# Used by Azure as: gunicorn app_startup:app  OR  uvicorn app_startup:app

import os
import sys
import subprocess
import time
import threading
import traceback

print("=" * 50)
print("HONEYWELL OT AGENT - AZURE STARTUP")
print("=" * 50)

# ── Path setup ────────────────────────────────────────────────────────────────
root_dir = os.path.dirname(os.path.abspath(__file__))
adk_dir  = os.path.join(root_dir, 'agents', 'agent-orchestration', 'ADK')
agent_dir = os.path.join(root_dir, 'agents', 'agent-orchestration')
mcp_dir  = os.path.join(root_dir, 'mcp_servers', 'Azure_Monitor_MCP', 'MCP')

print(f"Root dir: {root_dir}")
print(f"ADK dir:  {adk_dir}")
print(f"MCP dir:  {mcp_dir}")
print(f"ADK dir exists: {os.path.exists(adk_dir)}")
print(f"MCP dir exists: {os.path.exists(mcp_dir)}")

sys.path.insert(0, root_dir)
sys.path.insert(0, agent_dir)
sys.path.insert(0, adk_dir)   # inserted last → sits at [0], found first

# ── Environment ───────────────────────────────────────────────────────────────
os.environ['LOCAL'] = 'True'
# S5443: SECURITY HOTSPOT REVIEWED - SAFE CONFIGURATION
# /tmp is safe for containerized log storage. Logs are ephemeral and non-sensitive.
os.environ['LOG_DIR'] = '/tmp'

# Validate port number to prevent injection
try:
    port = int(os.environ.get('WEBSITES_PORT', os.environ.get('PORT', '8000')))
    if not (1024 <= port <= 65535):
        port = 8000
except ValueError:
    port = 8000
print(f"Using port: {port}")

# ── Agentic action layer config (visible in App Service log stream) ──────────
print("--- Agentic action layer ---")
print(f"  AGENTIC_ARTIFACT_DIR  = {os.environ.get('AGENTIC_ARTIFACT_DIR', '/home/site/wwwroot/generated_iac')}")
print(f"  AGENTIC_AUDIT_LOG     = {os.environ.get('AGENTIC_AUDIT_LOG', '/home/site/wwwroot/agentic_actions.log')}")
print(f"  AGENTIC_TAG_ALLOWLIST entries:   {len([x for x in os.environ.get('AGENTIC_TAG_ALLOWLIST', '').split(',') if x.strip()])}")
print(f"  AGENTIC_START_ALLOWLIST entries: {len([x for x in os.environ.get('AGENTIC_START_ALLOWLIST', '').split(',') if x.strip()])}")
print("  (empty allowlists = sim-only; populate to enable real Azure operations)")

# ── Start MCP server in background (non-blocking) ──────────────────────────────
mcp_process = None
mcp_started = False

def start_mcp_server():
    global mcp_process, mcp_started
    if os.path.exists(mcp_dir):
        try:
            # Security: Use subprocess with explicit arguments, no shell
            # Inherit stdout/stderr so MCP logs flow to App Service log stream.
            # PIPE without a reader deadlocks once the buffer fills.
            mcp_process = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=mcp_dir,
                shell=False,  # Explicit: prevent shell injection
                stdin=subprocess.DEVNULL,  # No stdin needed
            )
            print(f"MCP server starting on port 6200 (pid {mcp_process.pid})", flush=True)
            time.sleep(2)
            mcp_started = True
        except Exception:
            # Security: Don't leak detailed error info
            print(f"Warning: Could not start MCP server", flush=True)
            mcp_started = False
    else:
        print(f"Warning: MCP directory not found", flush=True)

# Start MCP in a background thread to prevent blocking
mcp_thread = threading.Thread(target=start_mcp_server, daemon=True)
mcp_thread.start()

# ── Start tool microservices (mirrors run-universal.py) ──────────────────────
# Each tool is a FastAPI app the agent calls at 127.0.0.1:<port>. Spawning them
# inside the same App Service container so the agent can reach them locally.
TOOL_SERVICES = [
    {"name": "Performance Tool",    "subdir": "tools/performancetool/CODE_EXECUTION",    "port": 6103},
    {"name": "Process Monitor",     "subdir": "tools/processmonitor/CODE_EXECUTION",     "port": 6104},
    {"name": "Security Monitor",    "subdir": "tools/securitymonitor/CODE_EXECUTION",    "port": 6105},
    {"name": "Log Analytics",       "subdir": "tools/loganalytics/CODE_EXECUTION",       "port": 6106},
    {"name": "Application Monitor", "subdir": "tools/applicationmonitor/CODE_EXECUTION", "port": 6107},
    {"name": "Dashboard Tool",      "subdir": "tools/dashboardtool/CODE_EXECUTION",      "port": 6108},
    {"name": "File Count Tool",     "subdir": "tools/filecount/CODE_EXECUTION",          "port": 6110},
    {"name": "File Search Tool",    "subdir": "tools/filesearch/CODE_EXECUTION",         "port": 6111},
    {"name": "CloudLens IaC Tool",  "subdir": "tools/cloudlens_iac/CODE_EXECUTION",      "port": 6212},
    {"name": "RAG Search",          "subdir": "tools/ragsearch/CODE_EXECUTION",          "port": 8002},
    {"name": "Blob Log Reader",     "subdir": "tools/blobreader/CODE_EXECUTION",         "port": 6115},
]

tool_processes = []

def start_tool_services():
    for tool in TOOL_SERVICES:
        tool_path = os.path.join(root_dir, tool["subdir"])
        if not os.path.exists(tool_path):
            print(f"WARN: tool path missing, skipping {tool['name']}: {tool_path}", flush=True)
            continue
        try:
            # Security: Use subprocess with explicit arguments, no shell
            # Inherit stdout/stderr — PIPE without reader deadlocks the subprocess.
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "main:app",
                 "--host", "127.0.0.1", "--port", str(tool["port"])],
                cwd=tool_path,
                shell=False,  # Explicit: prevent shell injection
                stdin=subprocess.DEVNULL,  # No stdin needed
            )
            tool_processes.append((tool["name"], tool["port"], proc))
            print(f"  Started {tool['name']} on port {tool['port']} (pid {proc.pid})", flush=True)
        except Exception:
            # Security: Don't leak detailed error info
            print(f"  Failed to start {tool['name']}", flush=True)
    # Give tools a moment, then report which actually came up
    time.sleep(8)
    for name, port, proc in tool_processes:
        rc = proc.poll()
        if rc is not None:
            print(f"  ❌ {name} (port {port}) DIED with exit code {rc}", flush=True)
        else:
            print(f"  ✅ {name} (port {port}) running", flush=True)

tool_thread = threading.Thread(target=start_tool_services, daemon=True)
tool_thread.start()

# ── Change CWD to ADK directory (required for relative imports in main.py) ───
# BUT: Save original cwd so we can restore it if needed for relative file ops
original_cwd = os.getcwd()
if os.path.exists(adk_dir):
    os.chdir(adk_dir)
    print(f"Changed working directory to: {os.getcwd()}")
else:
    print(f"Warning: ADK directory not found: {adk_dir}")

# ── Import the actual agent app ───────────────────────────────────────────────
app = None
error_message = None
error_traceback = None

try:
    print("Importing Honeywell OT Agent app...")
    from main import app
    print("Successfully imported Honeywell OT Agent app")
except Exception as e:
    error_message = str(e)
    error_traceback = traceback.format_exc()
    print(f"Import failed: {e}")
    print(f"Traceback:\n{error_traceback}")

# ── Fallback app if import failed ─────────────────────────────────────────────
if app is None:
    print("Creating fallback app...")
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    
    app = FastAPI(title="Honeywell OT Agent - Fallback")

    @app.get("/")
    def root():
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "message": error_message or "Unknown import error",
                "hint": "Check application logs for details",
                "error_traceback": error_traceback[:500] if error_traceback else None,
                "paths": {
                    "root_dir": root_dir,
                    "adk_dir": adk_dir,
                    "cwd": os.getcwd(),
                }
            }
        )

    @app.get("/health")
    def health():
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "ready": False, "error": error_message}
        )

    @app.get("/debug")
    def debug():
        return {
            "python_version": sys.version,
            "cwd": os.getcwd(),
            "original_cwd": original_cwd,
            "env_vars": {
                "WEBSITES_PORT": os.environ.get('WEBSITES_PORT'),
                "PORT": os.environ.get('PORT'),
                "LOCAL": os.environ.get('LOCAL'),
            },
            "files_in_adk": os.listdir(adk_dir) if os.path.exists(adk_dir) else [],
            "mcp_started": mcp_started,
        }
else:
    # Add health endpoint to main app if it doesn't have one
    if not hasattr(app, 'get'):
        print("Warning: Main app doesn't have FastAPI routing")
    else:
        print("Main app loaded successfully with all endpoints")

print("Startup complete, app ready")

if __name__ == "__main__":
    import uvicorn
    print(f"Starting server on 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

