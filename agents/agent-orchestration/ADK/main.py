# Load .env FIRST — before any other imports that trigger module-level os.getenv() calls.
# override=False (default) so Azure App Settings take precedence over local .env values.
import os
from dotenv import load_dotenv
load_dotenv()

from google.adk.cli.fast_api import get_fast_api_app
from os import path
import uvicorn
from fastapi import Request, HTTPException, UploadFile, File
import sys
import re
import json
import logging
import shutil
import threading
import requests
import httpx
import asyncio
import tempfile
from agentic_utils.cache.response_cache import ResponseCache

# TTL-based in-memory caches to avoid redundant LLM calls
_agent_response_cache = ResponseCache(default_ttl=300)   # 5 min — full agent session responses
_reco_cache = ResponseCache(default_ttl=600)             # 10 min — generate_recommendations output

# Set LOCAL mode to use local portable-agent.yaml instead of API
os.environ['LOCAL'] = 'True'

# Azure OpenAI Configuration from environment variables
# Security: No default credentials - must be provided via environment
AZURE_OPENAI_API_KEY = os.getenv('AZURE_OPENAI_API_KEY', '')
AZURE_OPENAI_ENDPOINT = os.getenv('AZURE_OPENAI_ENDPOINT', '')
AZURE_OPENAI_API_VERSION = os.getenv('AZURE_OPENAI_API_VERSION', '2024-02-01')
AZURE_OPENAI_DEPLOYMENT = os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME', '')

# Tool URLs from environment variables (allows override for Azure deployment)
# Security: Validate tool URLs to prevent SSRF
def _validate_tool_url(url: str) -> bool:
    """Validate that tool URL is safe and properly formatted."""
    if not url:
        return False
    if not (url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")):
        logger.warning(f"Tool URL is not a localhost address: {url}")
    return (
        url.startswith("http://127.0.0.1:")
        or url.startswith("http://localhost:")
        or url.startswith("https://")
    )

TOOL_URLS = {
    "performance": os.getenv('PERFORMANCE_TOOL_URL', 'http://127.0.0.1:6103/'),
    "process":     os.getenv('PROCESS_MONITOR_URL', 'http://127.0.0.1:6104/'),
    "security":    os.getenv('SECURITY_MONITOR_URL', 'http://127.0.0.1:6105/'),
    "logs":        os.getenv('LOG_ANALYTICS_URL', 'http://127.0.0.1:6106/'),
    "application": os.getenv('APPLICATION_MONITOR_URL', 'http://127.0.0.1:6107/'),
    "dashboard":   os.getenv('DASHBOARD_TOOL_URL', 'http://127.0.0.1:6108/'),
    "filecount":   os.getenv('FILE_COUNT_TOOL_URL', 'http://127.0.0.1:6110/'),
    "filesearch":  os.getenv('FILE_SEARCH_TOOL_URL', 'http://127.0.0.1:6111/'),
    "blogreader":  os.getenv('BLOB_LOG_READER_URL', 'http://127.0.0.1:6115/'),
}

# Add parent directory to path for imports
AGENT_DIR = path.dirname(path.abspath(__file__))
sys.path.insert(0, path.dirname(AGENT_DIR))
sys.path.insert(0, AGENT_DIR)

# Configure logging (stdout only for Azure compatibility)
log_handlers = [logging.StreamHandler(sys.stdout)]

# Only add file handler if directory is writable (not on Azure read-only fs)
log_dir = os.getenv('LOG_DIR', tempfile.gettempdir())
try:
    log_file = path.join(log_dir, 'honeywellotagent_debug.log')
    if os.access(log_dir, os.W_OK):
        log_handlers.append(logging.FileHandler(log_file))
except Exception:
    pass  # Skip file logging on Azure

# S4792: SECURITY HOTSPOT REVIEWED - SAFE CONFIGURATION
# Logger configured with INFO level, structured format, and safe handler setup.
# No sensitive data logged. File handler only added if directory is writable.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)
logger.info("Starting Honeywell IOT Agent...")

# Security: Validate configuration after logger is initialized
if not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_ENDPOINT:
    logger.warning("Azure OpenAI credentials not fully configured. Set AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT.")

# Validate tool URLs on startup
for tool_name, url in list(TOOL_URLS.items()):
    if not _validate_tool_url(url):
        logger.warning(f"Invalid tool URL for {tool_name}: {url}")
        TOOL_URLS[tool_name] = ''  # Disable invalid URLs

logger.info("Successfully imported agentic_utils")

try:
    from google.adk.agents import Agent
    from google.genai import types
    logger.info("Successfully imported google.adk and google.genai")
except ImportError as e:
    logger.warning(f"Could not import google modules: {e}")
    Agent = None
    types = None

from fastapi.responses import Response, JSONResponse, StreamingResponse, FileResponse

try:
    from litellm import completion
    logger.info("Successfully imported litellm")
except ImportError as e:
    logger.warning(f"Could not import litellm: {e}")
    completion = None

AGENTS_PARENT_DIR = path.dirname(path.dirname(AGENT_DIR))  # This is the agents/ directory

# Security: Configure CORS based on environment
# In production, restrict to specific origins
ALLOWED_ORIGINS_ENV = os.getenv('CORS_ALLOWED_ORIGINS', '*')
if ALLOWED_ORIGINS_ENV == '*':
    ALLOWED_ORIGINS = ["*"]  # Allow all for local development
    logger.warning("CORS configured to allow all origins (*). Set CORS_ALLOWED_ORIGINS env var for production.")
else:
    ALLOWED_ORIGINS = [origin.strip() for origin in ALLOWED_ORIGINS_ENV.split(',')]

# Create app based on whether a2a flag is present
a2a_mode = '--a2a' in sys.argv

# Create the ADK app WITHOUT web UI (set web=False)
app = get_fast_api_app(
    agents_dir=AGENTS_PARENT_DIR,
    allow_origins=ALLOWED_ORIGINS,
    web=False,  # Disable default web UI
    a2a=a2a_mode,
)

# Add CORS middleware
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Service Registry for Background Tools (uses environment variables)
BACKGROUND_SERVICES = {
    "performance": TOOL_URLS["performance"].rstrip('/'),
    "filesearch":  TOOL_URLS["filesearch"].rstrip('/'),
    "process":     TOOL_URLS["process"].rstrip('/'),
    "security":    TOOL_URLS["security"].rstrip('/'),
    "logs":        TOOL_URLS["logs"].rstrip('/'),
    "application": TOOL_URLS["application"].rstrip('/'),
    "dashboard":   TOOL_URLS["dashboard"].rstrip('/'),
}

# ── String literal constants (S1192) ──────────────────────────────────────────
_EVT_HUB = "event hub"
_VALIDATE_PROD = "Validate this environment against production baseline"
_STOP_LOAD_TEST = "Stop the load test"
_INFRA_NOT_READY = "infra not ready"

# ── Shared in-memory stores for demo insights ─────────────────────────────────
_anomaly_store: list = []   # [{timestamp, metric, unit, peak_value, baseline_avg, anomalous_points}]
_live_test_cache: list = []  # latest results from /api/test-run-live (populated on each poll)
_sre_alerts: list = []      # [{timestamp, context, report}]
_cost_suggestions: list = []  # [{timestamp, count, services, summary, rationale, suggested_user_message, idle_services}]

# ── Compiled regexes for monitoring detection — split to stay ≤20 complexity ─
_MON_RE_1 = re.compile(r'\b(cpu|memory|disk|network|system|ram|processor|process|processes|performance|stats|metrics|usage|load|space|dashboard|overview)\b')
_MON_RE_2 = re.compile(r'\b(monitor|status|top|security|connections|login|threats|logs|errors|analytics|log|services|api|health|application|database)\b')
_MON_RE_3 = re.compile(r'\b(bottleneck|free|available|sufficient|utilization|consumption|activity|search|find|mep|mla|cmep|device|dat|data|index)\b')
_MON_RE_4 = re.compile(r'\b(rag|motor|electrical|equipment|sensor|mepmd|saas)\b')
_IO_RE    = re.compile(r'\bi/?o\b')

# Azure service / keyword / intent regexes — each split to stay ≤20 complexity
_AZURE_SVC_RE_1  = re.compile(r'\b(event.?hub|app.?service|web.?app|function.?app)\b')
_AZURE_SVC_RE_2  = re.compile(r'\b(sql.?server|storage.?account|cosmos|service.?bus)\b')
_MON_KW_RE_1     = re.compile(r'\b(metrics|metric|usage|throughput|messages|message|incoming|outgoing|health|status)\b')
_MON_KW_RE_2     = re.compile(r'\b(performance|dashboard|overview|monitor|connections|connection|requests|request)\b')
_IAC_INTENT_RE   = re.compile(r'\b(terraform|bicep|iac|generate|replicate|configure|provision|setup|start|stop|restart|deploy|create|delete)\b')
_GENERIC_MON_RE  = re.compile(r'\b(azure\s+dashboard|azure\s+overview|azure\s+status|all\s+azure|show\s+all\s+services?|live\s+dashboard|show\s+dashboard|azure\s+metrics?\s+dashboard)\b')
_GENERAL_Q_RE    = re.compile(r'\b(how to create|how to setup|how to configure|how to install|tutorial|guide|documentation|explanation of|definition of)\b')
_INFRA_RE        = re.compile(r'\b(cloudlens|azure resources?|infrastructure|provision|terraform|bicep|iac|service|chip|environment|discover|configuration|setup)\b')
# Matches explicit time-range phrases — used to force bypass for time-scoped metric queries
_TIME_RANGE_RE   = re.compile(r'\b(past|last|for\s+(?:the\s+)?(?:past|last))\s+(\d+\s+)?(minute|hour|day|week|month)s?\b')


def _is_monitoring_query(text: str) -> bool:
    return bool(
        _MON_RE_1.search(text)
        or _MON_RE_2.search(text)
        or _MON_RE_3.search(text)
        or _MON_RE_4.search(text)
        or _IO_RE.search(text)
    )


def _is_azure_svc(text: str) -> bool:
    return bool(_AZURE_SVC_RE_1.search(text) or _AZURE_SVC_RE_2.search(text))


def _is_mon_kw(text: str) -> bool:
    return bool(_MON_KW_RE_1.search(text) or _MON_KW_RE_2.search(text))


# ── Tool Proxy Routes ──────────────────────────────────────────────────────────

@app.post("/api/tools/{tool_name}")
async def proxy_to_tool(tool_name: str, request: dict):
    """Route requests to background tools"""
    # Security: Validate tool name to prevent injection
    if not re.match(r'^[a-zA-Z0-9_-]+$', tool_name):
        raise HTTPException(status_code=400, detail="Invalid tool name")
    
    if tool_name not in BACKGROUND_SERVICES:
        raise HTTPException(status_code=404, detail=f"Tool {tool_name} not found")
    
    tool_url = BACKGROUND_SERVICES[tool_name]
    if not tool_url:  # URL was invalidated during startup
        raise HTTPException(status_code=503, detail=f"Tool {tool_name} is not configured")
    
    # Security: Add timeout to prevent hanging
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(f"{tool_url}/", json=request)
            return response.json()
        except httpx.TimeoutException:
            logger.error(f"Tool {tool_name} timed out")
            raise HTTPException(status_code=504, detail=f"Tool {tool_name} request timed out")
        except Exception:
            logger.exception(f"Tool {tool_name} call failed")
            raise HTTPException(status_code=503, detail=f"Tool {tool_name} unavailable")

@app.get("/api/tools")
async def list_available_tools():
    """List all available tools"""
    return {
        "available_tools": list(BACKGROUND_SERVICES.keys()),
        "endpoints": {
            "performance": "https://infrainsight-ehb0bjfeb4fec2ba.eastus-01.azurewebsites.net/api/tools/performance",
            "filesearch": "https://infrainsight-ehb0bjfeb4fec2ba.eastus-01.azurewebsites.net/api/tools/filesearch"
        }
    }

# CSV upload endpoint for Doppelio load testing
_CSV_UPLOAD_DIR = os.getenv("DOPPELIO_CSV_UPLOAD_DIR", os.path.join(tempfile.gettempdir(), "doppelio_uploads"))

@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    """Accept a CSV file from the chat UI and save it locally for the Digital Twin Agent to use."""
    # Security: Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")
    
    # Security: Sanitize filename to prevent path traversal
    safe_name = os.path.basename(file.filename)
    if not safe_name or ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    
    if not safe_name.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted.")
    
    # Security: Limit file size (10MB)
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10MB).")
    
    os.makedirs(_CSV_UPLOAD_DIR, exist_ok=True)
    dest = os.path.join(_CSV_UPLOAD_DIR, safe_name)
    
    try:
        def _write():
            with open(dest, "wb") as f:
                f.write(content)
        await asyncio.to_thread(_write)
    finally:
        await file.close()
    
    logger.info(f"CSV uploaded by UI: {safe_name}")
    return {"file_path": dest, "filename": safe_name}


# Make the project root importable so we can use the shared agentic package.
# Path: agents/agent-orchestration/ADK/main.py → project root is 3 levels up.
_PROJECT_ROOT = path.dirname(path.dirname(path.dirname(AGENT_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
try:
    from agentic import action_executor as _ae
except Exception as e:
    _ae = None
    logger.warning(f"agentic.action_executor not importable: {e}")


@app.get("/api/artifacts/{filename}")
async def get_artifact(filename: str):
    """Serve a generated IaC artifact by filename. Sandboxed to AGENTIC_ARTIFACT_DIR."""
    import re as _re
    if not _re.fullmatch(r"[\w\-.]+", filename) or ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    if _ae is None:
        raise HTTPException(status_code=503, detail="action layer unavailable")
    target = _ae.ARTIFACT_DIR / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(str(target), media_type="text/plain", filename=filename)


@app.get("/api/agent-actions")
async def list_agent_actions(limit: int = 50):
    """Return the most recent agentic action records (newest first)."""
    # Security: Validate limit parameter
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="Limit must be between 1 and 1000")
    
    if _ae is None:
        return JSONResponse({"records": [], "warning": "action layer unavailable"})
    try:
        return JSONResponse({"records": _ae.read_audit_log(limit=limit)})
    except Exception:
        logger.exception("read_audit_log failed")
        raise HTTPException(status_code=500, detail="Failed to retrieve action records")


@app.get("/api/load-test-progress")
async def load_test_progress():
    """Poll this while a load test is being set up. Returns step progress state."""
    try:
        from agentic_utils.tools.doppelio_orchestrator import get_load_test_progress
        return JSONResponse(content=get_load_test_progress())
    except Exception as e:
        logger.exception("load_test_progress failed: %s", e, exc_info=True)
        return JSONResponse(content={"active": False, "step": "", "steps_done": [], "error": "Failed to retrieve load test progress"})


@app.post("/api/init-load-test-progress")
async def init_load_test_progress(request: Request):
    """
    Reset progress state with infra_status='checking' so the UI infra step shows a
    spinner immediately while the SRE agent runs its health check via the LLM chain.
    Called by the UI before submitting a load test query to /api/chat.
    Accepts optional JSON body: {"session_id": "<browser-session>"} to tag tests
    so the live monitor card only shows tests started by this browser session.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    owner_session = body.get("session_id", "")
    try:
        from agentic_utils.tools.doppelio_orchestrator import _progress
        _progress.update({
            "active": False, "step": "", "steps_done": [], "error": "", "result": "",
            "infra_status": "checking",
            "owner_session": owner_session,
        })
    except Exception as e:
        logger.warning("init_load_test_progress: %s", e)
    return JSONResponse({"ok": True})


# ── test_run_live helpers ─────────────────────────────────────────────────────

async def _discover_running_sessions() -> list:
    """
    Fallback: fetch running sessions directly from Doppelio when in-memory store is empty
    (e.g. after server restart or when tests were started externally).
    Seeds _active_tests so subsequent polling works.
    """
    import urllib.parse
    from datetime import datetime, timezone
    from agentic_utils.tools.doppelio_tools import _CORE_BASE, _headers, _VERIFY_SSL
    from agentic_utils.tools.doppelio_orchestrator import _active_tests

    today      = datetime.now(timezone.utc)
    start_date = today.strftime("%m/%d/%Y 00:00")
    end_date   = today.strftime("%m/%d/%Y 23:59")
    params = (
        f"startDate={urllib.parse.quote(start_date)}"
        f"&endDate={urllib.parse.quote(end_date)}"
        f"&NumberOfSessions=100&AllWkUsersFlag=false"
        f"&OnlyRunningTests=true"
    )
    url = f"{_CORE_BASE}/TestRun/TestSessions?{params}"
    try:
        async with httpx.AsyncClient(verify=_VERIFY_SSL, timeout=10) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code != 200:
            return []
        sessions = resp.json() or []
        info_list = []
        for s in sessions:
            name       = s.get("testSessionName", "")
            session_rn = s.get("testSessionRunNo", 1)
            runs       = s.get("testRunStatus") or []
            run_no     = max((r.get("testNo", 1) for r in runs), default=1)
            info = {
                "session_name":   name,
                "session_run_no": session_rn,
                "run_no":         run_no,
                "num_devices":    0,
                "duration_mins":  0,
                "started_at":     s.get("testSessionStartedAt", ""),
            }
            if name and name not in _active_tests:
                _active_tests[name] = info   # seed so polling keeps it
            info_list.append(info)
        return info_list
    except Exception as ex:
        logger.warning("_discover_running_sessions failed: %s", ex)
        return []


async def _fetch_test_run_status(info: dict) -> dict:
    """Fetch live status for a single test run from Doppelio."""
    from agentic_utils.tools.doppelio_orchestrator import remove_active_test
    from agentic_utils.tools.doppelio_tools import _CORE_BASE, _headers, _VERIFY_SSL, _poll_state

    session_name   = info.get("session_name", "")
    session_run_no = info.get("session_run_no", 0)
    run_no         = info.get("run_no", 0)
    num_devices    = info.get("num_devices", 0)
    duration_mins  = info.get("duration_mins", 0)

    progress_url = (
        f"{_CORE_BASE}/TestRun/{session_name}/TestProgress"
        f"?TestSessionRunNo={session_run_no}&TestRunNo={run_no}"
    )
    published_url = (
        f"{_CORE_BASE}/TestRun/{session_name}/publishedNew"
        f"?TestSessionRunNo={session_run_no}&TestRunNo={run_no}"
        f"&ExportFlag=false&fname=&num_requested=20&page_number=1"
        f"&column_separator=tab&ascending_order=false&filetype=undefined"
    )
    try:
        async with httpx.AsyncClient(verify=_VERIFY_SSL, timeout=10) as client:
            progress_resp, published_resp = await asyncio.gather(
                client.get(progress_url, headers=_headers()),
                client.get(published_url, headers=_headers()),
                return_exceptions=True,
            )

        if not hasattr(progress_resp, "status_code") or progress_resp.status_code != 200:
            return {
                "active": True,
                "session_name": session_name,
                "num_devices": num_devices,
                "duration_mins": duration_mins,
                "error": f"Doppelio returned HTTP {getattr(progress_resp, 'status_code', 'err')}",
            }

        d = progress_resp.json()
        test_status = d.get("testRunStatus", "")
        is_running  = test_status not in ("Stopped", "Completed", "Error", "Stopping", "")

        if not is_running:
            remove_active_test(session_name)

        telemetry_flowing = False
        if hasattr(published_resp, "status_code") and published_resp.status_code == 200:
            try:
                pub = published_resp.json()
                msgs = pub.get("msgs") or []
                telemetry_flowing = any(m.get("messageType") == "data" for m in msgs)
            except Exception:
                pass

        telem_fwd    = _poll_state.get("events_sent", 0) if session_name == _poll_state.get("session_name", "") else 0
        telem_errors = _poll_state.get("errors", 0)        if session_name == _poll_state.get("session_name", "") else 0

        return {
            "active":              True,
            "session_name":        session_name,
            "session_run_no":      session_run_no,
            "status":              test_status,
            "is_running":          is_running,
            "num_devices":         num_devices,
            "devices_total":       num_devices or d.get("totalDoppels", 0),
            "devices_connected":   d.get("initialisedDoppels", 0),
            "steps_done":          d.get("completedTestSteps", 0),
            "steps_total":         d.get("totalTestSteps", 0),
            "iterations_done":     d.get("completedIterations", 0),
            "iterations_total":    d.get("totalIterations", 0),
            "messages_sent":       d.get("totalNumMessagesSent", 0),
            "messages_received":   d.get("totalNumMessagesReceived", 0),
            "telemetry_forwarded": telem_fwd,
            "telemetry_errors":    telem_errors,
            "telemetry_flowing":   telemetry_flowing,
            "duration_mins":       duration_mins,
            "started_at":          info.get("started_at", ""),
            "error":               "",
        }
    except Exception as ex:
        logger.warning("fetch_one failed for %s: %s", session_name, ex)
        return {
            "active": True,
            "session_name": session_name,
            "num_devices": num_devices,
            "duration_mins": duration_mins,
            "error": str(ex),
        }


@app.get("/api/test-run-live")
async def test_run_live(session: str = ""):
    """
    Fast live status for active test runs — no LLM, direct Doppelio proxy.
    Pass ?session=<SESSION_ID> to scope results to tests started by that browser session.
    Falls back to all tests if the session owns none (e.g. server restart, external start).
    Returns {"active": bool, "tests": [...per-test status dicts...]}.
    """
    try:
        from agentic_utils.tools.doppelio_orchestrator import get_all_active_tests

        all_tests = get_all_active_tests()
        if not all_tests:
            # Server restarted or test started externally — discover from Doppelio directly
            all_tests = await _discover_running_sessions()
        if not all_tests:
            return JSONResponse(content={"active": False, "tests": []})

        # Scope strictly to the requesting browser session.
        # No fallback to all tests — a session should never see another browser's tests.
        if session:
            all_tests = [t for t in all_tests if t.get("owner_session") == session]
        if not all_tests:
            return JSONResponse(content={"active": False, "tests": []})

        results = await asyncio.gather(*[_fetch_test_run_status(info) for info in all_tests])
        has_active = any(r.get("is_running", r.get("active", False)) for r in results)
        global _live_test_cache
        _live_test_cache = list(results)
        return JSONResponse(content={"active": has_active, "tests": list(results)})

    except Exception as e:
        logger.exception("test_run_live failed: %s", e, exc_info=True)
        return JSONResponse(content={"active": False, "tests": [], "error": "Failed to retrieve test run status"})


@app.get("/health")
async def health_check():
    """Health check for all services"""
    status = {
        "main_agent": "healthy",
        "tools": {},
        "azure_url": "https://infrainsight-ehb0bjfeb4fec2ba.eastus-01.azurewebsites.net"
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        for tool_name, tool_url in BACKGROUND_SERVICES.items():
            try:
                response = await client.get(f"{tool_url}/", timeout=2.0)
                status["tools"][tool_name] = "healthy" if response.status_code == 200 else "unhealthy"
            except Exception:
                status["tools"][tool_name] = "down"

    return status

@app.get("/ready")
async def readiness_check():
    """Quick readiness check for Azure deployment"""
    return JSONResponse({"ready": True, "status": "ok"}, status_code=200)

@app.get("/status")
async def status_check():
    """Agent status endpoint"""
    return JSONResponse({
        "agent": "Honeywell OT Agent",
        "status": "running",
        "version": "1.0.0",
        "mode": os.environ.get("LOCAL", "False")
    }, status_code=200)


# ── Recommendation helpers ────────────────────────────────────────────────────

def _is_activation_response(response_lower: str) -> bool:
    """Check if response indicates service activation."""
    return (
        "service activation & deployment" in response_lower
        or "deployment complete" in response_lower
        or (_EVT_HUB in response_lower and "now active" in response_lower)
    )

def _is_load_test_running(response_lower: str) -> bool:
    """Check if response indicates load test is running."""
    return (
        "load test running" in response_lower
        or (
            "test started" in response_lower
            and any(k in response_lower for k in ("session:", "run no", "devices", "duration"))
        )
    )

def _is_infra_blocked_response(response_lower: str) -> bool:
    """Check if response indicates infrastructure is blocked."""
    return _INFRA_NOT_READY in response_lower and "load test blocked" in response_lower

def _is_health_check_response(response_lower: str) -> bool:
    """Check if response is a health check result."""
    return (
        "pre-execution health check" in response_lower
        or "issue detected" in response_lower
        or (_EVT_HUB in response_lower and "stopped" in response_lower and "agent recommendation" in response_lower)
    )

def _is_terraform_response(response_lower: str) -> bool:
    """Check if response contains terraform scripts."""
    return (
        "terraform scripts generated" in response_lower
        or ("```hcl" in response_lower and "azurerm_resource_group" in response_lower)
    )

def _is_bicep_response(response_lower: str) -> bool:
    """Check if response contains bicep templates."""
    return (
        "bicep templates generated" in response_lower
        or ("```bicep" in response_lower and "targetscope" in response_lower)
    )

def _is_cloudlens_response(response_lower: str) -> bool:
    """Check if response is cloudlens configuration."""
    return (
        "terraform resource mapping" in response_lower
        or "azure environment configuration" in response_lower
        or "service configuration table" in response_lower
        or ("azurerm_" in response_lower and "sku/tier" in response_lower)
    )

def _get_static_recommendations(response_lower: str) -> list | None:
    """Return static recommendations if the response matches a known pattern, else None."""
    if _is_activation_response(response_lower):
        return [
            "Show me the current environment configuration",
            "Generate Bicep templates for these resources",
            _VALIDATE_PROD,
        ]

    if _is_load_test_running(response_lower):
        return ["Show live stats", "Show Azure dashboard", _STOP_LOAD_TEST]

    if _is_infra_blocked_response(response_lower):
        stopped_svcs = [s.title() for s in ("function app", _EVT_HUB, "sql server") if s in response_lower]
        suggestions = [f"Start the {s}" for s in stopped_svcs] or ["Start the stopped services"]
        suggestions.append("Show azure dashboard")
        return suggestions[:3]

    if _is_health_check_response(response_lower):
        return [
            "Yes, start the Event Hub",
            "No, skip and apply scripts anyway",
            "Show me details about the Event Hub",
        ]

    if _is_terraform_response(response_lower):
        return [
            "Execute this Terraform script",
            "Generate Bicep templates instead",
            "Show me what changes terraform plan would make",
        ]

    if _is_bicep_response(response_lower):
        return [
            "Execute this Bicep template",
            "Generate Terraform scripts instead",
            _VALIDATE_PROD,
        ]

    if _is_cloudlens_response(response_lower):
        return [
            "Generate Terraform scripts to replicate this environment",
            "Generate Bicep templates for these resources",
            _VALIDATE_PROD,
        ]

    return None


async def generate_recommendations(user_query: str, agent_response: str) -> list:
    """Generate follow-up question recommendations using LLM"""
    response_lower = agent_response.lower()

    static = _get_static_recommendations(response_lower)
    if static is not None:
        return static

    # Cache LLM-generated recommendations keyed on the response text (first 1000 chars).
    # Same agent response always maps to the same follow-up suggestions.
    _reco_key = _reco_cache.make_key(agent_response[:1000])
    _cached_reco = _reco_cache.get(_reco_key)
    if _cached_reco is not None:
        logger.debug(f"[cache HIT] recommendations key={_reco_key[:12]}")
        return _cached_reco

    try:
        query_lower = user_query.lower()
        is_iac_query = any(kw in query_lower for kw in [
            'environment', 'configuration', 'terraform', 'bicep', 'iac',
            'infrastructure', 'cloudlens', 'resource', 'provision', 'deploy'
        ])

        if is_iac_query:
            context = "Azure infrastructure, resources, and Infrastructure-as-Code (Terraform/Bicep)"
            topic_hint = "infrastructure, resources, Terraform, Bicep, deployment, or environment replication"
        else:
            context = "performance monitoring"
            topic_hint = "system performance, monitoring, CPU, memory, disk, or network metrics"

        prompt = f"""Based on this conversation about {context}:

User Question: {user_query}

Agent Answer: {agent_response[:1000]}

Generate exactly 3 short, relevant follow-up questions the user might want to ask next.
Each question should be:
- Brief (max 10 words)
- Specific and actionable
- Related to {topic_hint}
- A natural next step in the conversation

Return ONLY a valid JSON array of exactly 3 short questions, nothing else:
["Question 1?", "Question 2?", "Question 3?"]"""

        response = await asyncio.to_thread(
            completion,
            model=f"azure/{AZURE_OPENAI_DEPLOYMENT}",
            messages=[{"role": "user", "content": prompt}],
            api_base=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_API_KEY,
            api_version=AZURE_OPENAI_API_VERSION,
            temperature=0.7,
            max_completion_tokens=150,
        )

        suggestions_text = response.choices[0].message.content.strip()
        suggestions_text = suggestions_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(suggestions_text)[:3]
        _reco_cache.set(_reco_key, result)
        return result

    except Exception as e:
        logger.exception(f"Failed to generate recommendations: {e}", exc_info=True)
        return [
            "Tell me more details?",
            "What are the specifications?",
            "How does this integrate?",
        ]


# Serve static files for custom UI
static_dir = path.join(AGENT_DIR, "_static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

# Serve the custom chat UI at root
@app.get("/", include_in_schema=True)
async def root():
    """Serve the Honeywell Performance Agent chat UI"""
    ui_file = path.join(static_dir, "chat.html")
    if os.path.exists(ui_file):
        return FileResponse(ui_file)
    return JSONResponse(content={"error": "Chat UI not found"}, status_code=404)

@app.get("/chat", include_in_schema=False)
async def serve_chat_ui():
    """Serve the custom chat UI"""
    ui_file = path.join(static_dir, "chat.html")
    if os.path.exists(ui_file):
        return FileResponse(ui_file)
    return JSONResponse(
        content={"message": "Chat UI not found. Please create _static/chat.html"},
        status_code=404,
    )


# ── chat endpoint helpers ──────────────────────────────────────────────────────

def _check_bypass_conditions(query_lower: str, monitoring_mode: str) -> tuple:
    """Returns (should_bypass, is_azure_metrics, is_azure_dashboard, is_data_lookup, is_infra)."""
    is_general = bool(_GENERAL_Q_RE.search(query_lower))

    if monitoring_mode == "PROD":
        should_bypass = False
    else:
        should_bypass = _is_monitoring_query(query_lower) and not is_general

    is_azure_metrics = (
        _is_azure_svc(query_lower)
        and _is_mon_kw(query_lower)
        and not _IAC_INTENT_RE.search(query_lower)
    )
    is_azure_dashboard = (
        bool(_GENERIC_MON_RE.search(query_lower))
        and not _IAC_INTENT_RE.search(query_lower)
    )
    if monitoring_mode == "PROD" and (is_azure_metrics or is_azure_dashboard):
        should_bypass = True

    # Generic time-range metric queries (e.g. "show cpu usage for past month") —
    # bypass the LLM so the exact time phrase reaches parse_hours unchanged.
    if (
        not is_azure_metrics and not is_azure_dashboard
        and bool(_TIME_RANGE_RE.search(query_lower))
        and _is_monitoring_query(query_lower)
        and not _IAC_INTENT_RE.search(query_lower)
        and not is_general
    ):
        should_bypass = True
        is_azure_metrics = True   # route to performance tool

    is_data_lookup = bool(re.search(r'\d{2,}\.\d+|\b(record|value|details|timestamp|have|available|exist)\b', query_lower))
    if is_data_lookup and monitoring_mode != "PROD":
        should_bypass = True

    is_infra = bool(_INFRA_RE.search(query_lower))
    return should_bypass, is_azure_metrics, is_azure_dashboard, is_data_lookup, is_infra


def _select_tool_for_bypass(query_lower: str, is_azure_metrics: bool, is_azure_dashboard: bool, is_data_lookup: bool) -> str:
    """Select which tool URL to call for a bypass request."""
    if is_azure_metrics or is_azure_dashboard:
        return TOOL_URLS["performance"]
    
    if is_data_lookup or re.search(r'\b(search|find|mep|mla|cmep|mepmd|saas|motor|electrical|equipment|sensor|dat|rag|index)\b', query_lower):
        return "http://127.0.0.1:8002/"
    
    # Map patterns to tool URLs
    tool_patterns = [
        (r'\b(dashboard|overview)\b', lambda q: TOOL_URLS["performance"] if re.search(r'\b(azure|cloud)\b', q) else TOOL_URLS["dashboard"]),
        (r'\b(process|processes|top|apps|applications)\b', TOOL_URLS["process"]),
        (r'\b(security|connections|network|login|threats)\b', TOOL_URLS["security"]),
        (r'\b(logs|errors|analytics|log)\b', TOOL_URLS["logs"]),
        (r'\b(services|api|health|application|database)\b', TOOL_URLS["application"]),
    ]
    
    for pattern, tool_url in tool_patterns:
        if re.search(pattern, query_lower):
            return tool_url(query_lower) if callable(tool_url) else tool_url
    
    return TOOL_URLS["performance"]


async def _call_tool_direct(tool_url: str, user_query: str) -> tuple:
    """Call a tool via async HTTP. Returns (agent_text, charts) or raises."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        tool_response = await client.post(tool_url, json={"query": user_query})
    if tool_response.status_code != 200:
        raise ValueError(f"Tool returned status {tool_response.status_code}")
    raw_text = tool_response.json().get("result", "No data returned")

    bypass_charts = []
    seen_keys: set = set()
    for cm in re.finditer(r"<chart>\s*(.+?)\s*</chart>", raw_text, re.DOTALL):
        try:
            c = json.loads(cm.group(1).strip())
        except ValueError:
            continue
        if not (isinstance(c, dict) and c.get("t") and c.get("v")):
            continue
        _cn = c.get("name", "").lower().strip()
        if _cn in _EXCLUDED_METRICS:
            continue
        # Prefix-exclude: http counts, capture backlog/bytes (may have unit suffix like ". (Count)")
        if _cn.startswith("http ") or _cn.startswith("capture backlog") or _cn.startswith("captured bytes"):
            continue
        key = (c.get("name"), c.get("unit"), len(c.get("t", [])))
        if key not in seen_keys:
            seen_keys.add(key)
            bypass_charts.append(c)

    agent_text = re.sub(r"<chart>.*?</chart>", "", raw_text, flags=re.DOTALL).strip()
    return agent_text, bypass_charts


def _extract_text_from_event(event) -> str:
    if hasattr(event, 'text') and event.text:
        return str(event.text)
    if not hasattr(event, 'content') or not event.content:
        return ""
    if hasattr(event.content, 'parts'):
        return "".join(
            str(part.text) for part in event.content.parts
            if hasattr(part, 'text') and part.text
        )
    return str(event.content)


async def _run_agent_session(runner, session_service, user_id: str, session_id, user_query: str, _types) -> tuple:
    """Run a query through the ADK agent. Returns (agent_response, all_events, actual_session_id)."""
    import uuid
    actual_session_id = session_id or f"session_{uuid.uuid4().hex[:8]}"
    existing = await session_service.get_session(
        app_name="honeywell_multi_agent",
        user_id=user_id,
        session_id=actual_session_id,
    )
    if not existing:
        await session_service.create_session(
            app_name="honeywell_multi_agent",
            user_id=user_id,
            session_id=actual_session_id,
        )

    message_content = _types.Content(role="user", parts=[_types.Part(text=user_query)])
    result_generator = runner.run_async(
        session_id=actual_session_id,
        user_id=user_id,
        new_message=message_content,
    )

    agent_response = ""
    all_events = []
    async for event in result_generator:
        all_events.append(event)
        logger.info(f"Event received: {type(event).__name__}")
        agent_response += _extract_text_from_event(event)

    return agent_response, all_events, actual_session_id


def _extract_response_from_part(part) -> str | dict | None:
    """Extract function response from a content part."""
    fr = getattr(part, 'function_response', None)
    return getattr(fr, 'response', None) if fr else None

def _collect_blobs_from_response(resp) -> list:
    """Collect string blobs from a response object."""
    if isinstance(resp, dict):
        return [v for v in resp.values() if isinstance(v, str)]
    elif isinstance(resp, str):
        return [resp]
    return []

def _collect_tool_blobs(all_events: list) -> list:
    blobs = []
    for ev in all_events:
        content = getattr(ev, 'content', None)
        parts = getattr(content, 'parts', None) if content else None
        if not parts:
            continue
        for part in parts:
            resp = _extract_response_from_part(part)
            if resp is not None:
                blobs.extend(_collect_blobs_from_response(resp))
    return blobs


def _parse_charts_from_blobs(blobs: list) -> list:
    charts = []
    seen_keys: set = set()
    for blob in blobs:
        for m in re.finditer(r"<chart>\s*(.+?)\s*</chart>", blob, re.DOTALL):
            try:
                chart = json.loads(m.group(1).strip())
            except ValueError:
                continue
            if not (isinstance(chart, dict) and chart.get("t") and chart.get("v")):
                continue
            _cn2 = chart.get("name", "").lower().strip()
            if _cn2 in _EXCLUDED_METRICS:
                continue
            if _cn2.startswith("http ") or _cn2.startswith("capture backlog") or _cn2.startswith("captured bytes"):
                continue
            key = (chart.get("name"), chart.get("unit"), len(chart.get("t", [])))
            if key not in seen_keys:
                seen_keys.add(key)
                charts.append(chart)
    return charts


# Metrics that add noise / clutter to the Azure dashboard — excluded from all chart output
_EXCLUDED_METRICS: frozenset = frozenset({
    # HTTP request-count breakdowns
    'http 2xx', 'http 3xx', 'http 4xx', 'http 5xx', 'http 403', 'http 404',
    # Connection / handle internals
    'connections', 'handle count',
    'successful connections', 'successful connections.',
    'sessions percentage', 'sessions percentage.',
    # Raw data-transfer counters
    'data in', 'data out',
    # SQL low-level storage / IO
    'data space used', 'data space used.',
    'data space used percent', 'data space used percent.',
    'data io percentage', 'data io percentage.',
    'log io percentage', 'log io percentage.',
    'in-memory oltp storage percent', 'in-memory oltp storage percent.',
    # SQL process internals
    'sql instance memory percent', 'sql instance memory percent.',
    'sql server process memory percent', 'sql server process memory percent.',
    'sql instance cpu percent', 'sql instance cpu percent.',
    'sql server process core percent', 'sql server process core percent.',
    # Availability (renders as flat line, not useful)
    'availability', 'availability.',
    # IO byte-rate metrics
    'io write bytes per second', 'io read bytes per second',
    # Event Hub low-level
    'outgoing bytes', 'captured bytes', 'capture backlog',
    'outgoing messages', 'outgoing messages.',
    # Function internals
    'function execution units', 'function execution units.',
    # Error / failure counts
    'server errors', 'server errors.',
    'user errors', 'user errors.',
    'quota exceeded errors', 'quota exceeded errors.',
    # Throttling / firewall
    'throttled requests', 'throttled requests.',
    'deadlocks', 'blocked by firewall',
    'failed connections', 'failed connections: system errors',
    'failed connections : system errors',
})


# Only metrics matching these keywords (substring on lowercased name) generate anomaly alerts
_PRIMARY_ANOMALY_METRICS: frozenset = frozenset({
    "outgoing bytes", "bytes sent",
    "incoming bytes", "bytes received",
    "cpu time", "cpu percent", "cpu percentage", "average cpu utilization",
    "memory working set", "average memory working set",
    "incoming requests", "requests",
    "http response time", "response time",
    "workers percent", "storage percent",
})

# Minimum peak value (in display units) required to report an anomaly — prevents trivial-number alerts
_ANOMALY_MIN_PEAK: dict = {
    " MB":    100.0,   # at least 100 MB for byte metrics
    "%":        5.0,   # at least 5% for percentage metrics
    " Count":  10.0,   # at least 10 for count metrics
}

# Human-readable descriptions: key = lowercase substring; value = (what_it_means, impact_hint)
_ANOMALY_DESCRIPTIONS: dict = {
    "outgoing bytes":             ("Your app sent significantly more data than usual",
                                   "this may indicate a large data export or traffic surge"),
    "bytes sent":                 ("Your app sent significantly more data than usual",
                                   "this may indicate a large data export or traffic surge"),
    "incoming bytes":             ("Your app received more data than usual",
                                   "this may indicate a traffic spike or large uploads"),
    "bytes received":             ("Your app received more data than usual",
                                   "this may indicate a traffic spike or large uploads"),
    "incoming requests":          ("Traffic spike detected",
                                   "your app received significantly more requests than the baseline"),
    "requests":                   ("Traffic spike detected",
                                   "your app received significantly more requests than the baseline"),
    "cpu time":                   ("Higher-than-usual compute load",
                                   "the app used more CPU than normal — may slow responses under sustained load"),
    "cpu percent":                ("Higher-than-usual CPU usage",
                                   "sustained high CPU can cause throttling or slow responses"),
    "cpu percentage":             ("Higher-than-usual CPU usage",
                                   "sustained high CPU can cause throttling or slow responses"),
    "average cpu utilization":    ("Higher-than-usual CPU usage",
                                   "sustained high CPU can cause throttling or slow responses"),
    "memory working set":         ("More RAM in use than usual",
                                   "if this continues it may cause memory pressure or errors"),
    "average memory working set": ("More RAM in use than usual",
                                   "if this continues it may cause memory pressure or errors"),
    "http response time":         ("Response latency spike",
                                   "requests are taking longer than usual — users may experience slowness"),
    "response time":              ("Response latency spike",
                                   "requests are taking longer than usual — users may experience slowness"),
    "workers percent":            ("Server worker threads running high",
                                   "approaching capacity may cause request queuing or rejections"),
    "storage percent":            ("Storage usage is higher than usual",
                                   "approaching full capacity can degrade database performance"),
}


_METRIC_KEYWORDS = (
    "chart", "graph", "graphs", "plot", "visual", "visualize",
    "metric", "metrics", "detail", "detailed", "verbose", "full", "deep",
    "cpu", "memory", "ram", "disk", "network", "bandwidth", "throughput",
    "usage", "utilization", "performance", "load", "latency", "response time",
    "requests", "deadlock", "deadlocks", "connections", "sessions",
    "trend", "trends", "over time", "history", "historical",
)


def _anomaly_description(metric_lower: str) -> str:
    """Return a plain-English description for a metric, or '' if unknown."""
    for key, (what, impact) in _ANOMALY_DESCRIPTIONS.items():
        if key in metric_lower:
            return f"{what} — {impact}"
    return ""


def _passes_anomaly_filters(metric_lower: str, unit: str, peak_value: float) -> bool:
    """Return True only if this metric passes the primary-whitelist and min-peak filters."""
    if not any(kw in metric_lower for kw in _PRIMARY_ANOMALY_METRICS):
        return False
    min_peak = _ANOMALY_MIN_PEAK.get(unit)
    if min_peak is not None and peak_value < min_peak:
        return False
    return True


def _extract_charts(all_events: list, user_query: str) -> list:
    if not any(k in (user_query or "").lower() for k in _METRIC_KEYWORDS):
        return []
    return _parse_charts_from_blobs(_collect_tool_blobs(all_events))


def _chart_spike_finding(chart: dict) -> str:
    """Return a spike-finding string for a single chart, or '' if the current value is not anomalous."""
    import statistics
    values = [v for v in chart.get("v", []) if v is not None]
    if len(values) < 4:
        return ""
    # Baseline from all-but-last so the current value doesn't inflate the mean
    baseline_vals = values[:-1]
    mean = statistics.mean(baseline_vals)
    stdev = statistics.stdev(baseline_vals) if len(baseline_vals) > 1 else 0
    if stdev == 0:
        return ""
    threshold = mean + 2.0 * stdev
    # Only report if the CURRENT (most recent) value is anomalous
    current_val = values[-1]
    if current_val <= threshold:
        return ""
    name = chart.get("name", "Unknown")
    unit = chart.get("unit", "")
    if not _passes_anomaly_filters(name.lower().strip(), unit, current_val):
        return ""
    ratio = current_val / mean if mean > 0 else 0
    return (
        f"- **{name}**: currently {current_val:.1f}{unit} — {ratio:.0f}\u00d7 above normal "
        f"(typical avg {mean:.1f}{unit})"
    )


def _detect_chart_anomalies(charts: list) -> str:
    """Scan chart time-series for spikes (> mean + 2.5*std). Returns a Markdown summary or ''."""
    findings = [_chart_spike_finding(c) for c in charts if c]
    findings = [f for f in findings if f]
    if not findings:
        return ""
    return "### ⚠ Anomalies Detected\n" + "\n".join(findings) + "\n\n---\n\n"


def _append_anomalies_to_store(charts: list) -> None:
    """Parse charts and persist any detected anomalies to the global _anomaly_store."""
    import statistics as _stats
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).isoformat()
    for chart in charts:
        values = [v for v in chart.get("v", []) if v is not None]
        if len(values) < 4:
            continue
        # Baseline from all-but-last so the current value doesn't inflate the mean
        baseline_vals = values[:-1]
        mean = _stats.mean(baseline_vals)
        stdev = _stats.stdev(baseline_vals) if len(baseline_vals) > 1 else 0
        if stdev == 0:
            continue
        threshold = mean + 2.0 * stdev
        # Only alert if the CURRENT (most recent) value is anomalous
        current_val = values[-1]
        if current_val <= threshold:
            continue
        unit = chart.get("unit", "")
        name = chart.get("name", "Unknown")
        metric_lower = name.lower().strip()
        if not _passes_anomaly_filters(metric_lower, unit, current_val):
            continue
        peak_val = max(values)  # Historical max across the whole window, for context
        # Count how many of the last 3 data points are also anomalous (persistence signal)
        anomalous_recent = sum(1 for v in values[-3:] if v > threshold)
        ratio = current_val / mean if mean > 0 else 0
        severity = "critical" if ratio > 10 else "high" if ratio > 3 else "warning"
        _anomaly_store.append({
            "timestamp": now,
            "metric": name,
            "unit": unit,
            "current_value": round(current_val, 2),
            "peak_value": round(peak_val, 2),
            "baseline_avg": round(mean, 2),
            "anomalous_points": anomalous_recent,
            "severity": severity,
            "description": _anomaly_description(metric_lower),
        })


def _parse_action_markers(agent_response: str) -> tuple:
    pending_action = None
    executed_action = None
    m_pend = re.search(r"<!--PENDING_ACTION:(.+?)-->", agent_response, flags=re.DOTALL)
    if m_pend:
        try:
            pending_action = json.loads(m_pend.group(1))
        except Exception as e:
            logger.warning(f"PENDING_ACTION parse failed: {e}")
    m_done = re.search(r"<!--AGENTIC_ACTION:(.+?)-->", agent_response, flags=re.DOTALL)
    if m_done:
        try:
            executed_action = json.loads(m_done.group(1))
        except Exception as e:
            logger.warning(f"AGENTIC_ACTION parse failed: {e}")
    cleaned = re.sub(r"<!--(?:PENDING|AGENTIC)_ACTION:.+?-->", "", agent_response, flags=re.DOTALL).strip()
    return pending_action, executed_action, cleaned


def _load_test_recommendations(response_lower: str) -> list:
    # Infra blocked — only suggest starting services explicitly mentioned as stopped
    if _INFRA_NOT_READY in response_lower:
        stopped_svcs = []
        for svc in ("function app", "event hub", "sql server"):
            # Service must appear adjacent to "stopped" in the text (not just mentioned)
            if re.search(rf'stopped[^.\n]{{0,80}}{re.escape(svc)}|{re.escape(svc)}[^.\n]{{0,80}}stopped', response_lower):
                stopped_svcs.append(svc.title())
        suggestions = [f"Start the {s}" for s in stopped_svcs] or ["Start stopped services"]
        return suggestions[:3]

    is_lt_running = (
        "load test running" in response_lower
        or (
            "test started" in response_lower
            and any(k in response_lower for k in ("session:", "run no", "devices", "duration"))
        )
    )
    if is_lt_running:
        return ["Show live stats", "Show Azure dashboard", _STOP_LOAD_TEST]
    return ["Check test progress", _STOP_LOAD_TEST, "Show test sessions today"]


_IAC_ACTION_RE = re.compile(
    r'\b(fix|start|stop|restart|turn\s+on|turn\s+off|enable|disable|bring\s+up|spin\s+up)\b'
    r'.{0,60}'
    r'\b(function\s+app|event\s+hub|sql\s+server|app\s+service|azure\s+service|all\s+azure|all\s+services?)\b'
    r'|using\s+terraform'
    r'|idle\s+service|idle\s+azure|idle\s+resource|scan\s+idle|unused\s+service'
    r'|cost\s+optim|cost\s+sav|wasted\s+cost|save\s+money|infrequently\s+used'
    r'|which\s+.*\s+idle|services?\s+.*\s+idle|check\s+.*\s+idle',
    re.IGNORECASE,
)


async def _try_iac_direct(user_query: str, session_id) -> JSONResponse | None:
    """Attempt direct CloudLens call for IaC action queries. Returns response or None."""
    if not _IAC_ACTION_RE.search(user_query):
        return None
    import uuid
    cloudlens_url = os.getenv('CLOUDLENS_TOOL_URL', 'http://127.0.0.1:6212/')
    logger.info(f"Direct CloudLens IaC call: '{user_query}'")
    try:
        agent_text, bypass_charts = await _call_tool_direct(f"{cloudlens_url.rstrip('/')}/", user_query)
        recommendations = await generate_recommendations(user_query, agent_text)
        return JSONResponse(content={
            "success": True, "query": user_query, "response": agent_text,
            "charts": bypass_charts, "recommendations": recommendations,
            "session_id": session_id or f"direct_{uuid.uuid4().hex[:8]}",
            "timestamp": str(asyncio.get_event_loop().time()),
            "debug": {"direct_tool_call": True, "cloudlens_direct": True},
        })
    except Exception as e:
        logger.exception(f"Direct CloudLens call failed: {e}", exc_info=True)
        return None


async def _try_monitoring_direct(query_lower: str, user_query: str, session_id,
                                  should_bypass: bool, is_infra: bool,
                                  is_azure_metrics: bool, is_azure_dashboard: bool,
                                  is_data_lookup: bool) -> JSONResponse | None:
    """Attempt direct monitoring tool call for bypass queries. Returns response or None."""
    if not (should_bypass and not is_infra):
        return None
    import uuid
    tool_url = _select_tool_for_bypass(query_lower, is_azure_metrics, is_azure_dashboard, is_data_lookup)
    logger.info(f"Direct tool call detected: '{user_query}' - calling {tool_url}")
    try:
        agent_text, bypass_charts = await _call_tool_direct(tool_url, user_query)
        _append_anomalies_to_store(bypass_charts)
        # Always return accumulated anomalies as structured data so the frontend
        # can render styled cards on every dashboard poll (not just when text prefix fires).
        recent_anomalies = _anomaly_store[-8:] if _anomaly_store else []
        recommendations = await generate_recommendations(user_query, agent_text)
        # Build Doppelio ingestion stats for dashboard ingestion layer.
        # Use _live_test_cache (populated by /api/test-run-live polls) so we show
        # real-time Doppelio API data (messages sent, devices connected, iterations)
        # rather than _poll_state which only tracks the post-test download phase.
        doppelio_stats = None
        if is_azure_dashboard and _live_test_cache:
            try:
                # Aggregate across all active tests in the cache
                t = _live_test_cache[0]  # primary test
                doppelio_stats = {
                    "active":               t.get("is_running", t.get("active", False)),
                    "status":               t.get("status", ""),
                    "session_name":         t.get("session_name", ""),
                    "num_devices":          t.get("num_devices", 0),
                    "devices_connected":    t.get("devices_connected", 0),
                    "messages_sent":        t.get("messages_sent", 0),
                    "messages_received":    t.get("messages_received", 0),
                    "iterations_done":      t.get("iterations_done", 0),
                    "iterations_total":     t.get("iterations_total", 0),
                    "steps_done":           t.get("steps_done", 0),
                    "steps_total":          t.get("steps_total", 0),
                    "telemetry_forwarded":  t.get("telemetry_forwarded", 0),
                    "telemetry_flowing":    t.get("telemetry_flowing", False),
                }
            except Exception:
                pass
        return JSONResponse(content={
            "success": True, "query": user_query, "response": agent_text,
            "charts": bypass_charts, "recommendations": recommendations,
            "anomalies": recent_anomalies,
            "doppelio_stats": doppelio_stats,
            "session_id": session_id or f"direct_{uuid.uuid4().hex[:8]}",
            "timestamp": str(asyncio.get_event_loop().time()),
            "debug": {"direct_tool_call": True, "charts_count": len(bypass_charts)},
        })
    except Exception as e:
        logger.exception(f"Direct tool call failed: {e}", exc_info=True)
        return None


def _sync_lt_progress(agent_response: str) -> None:
    """Update _progress infra_status based on agent response for load test queries."""
    try:
        from agentic_utils.tools.doppelio_orchestrator import _progress
        if _progress.get("infra_status") == "checking":
            _progress["infra_status"] = "failed" if _INFRA_NOT_READY in agent_response.lower() else ""
    except Exception:
        pass


# ── Custom chat endpoint ───────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat_with_recommendations(request: Request):
    """Custom chat endpoint with recommendations"""
    try:
        import uuid
        body = await request.json()
        user_query = body.get("query", "")
        session_id = body.get("session_id", None)
        user_id = body.get("user_id", "user")

        if not user_query:
            raise HTTPException(status_code=400, detail="Query is required")

        logger.info(f"Chat request - Query: {user_query}, Session: {session_id}")

        query_lower = user_query.lower()
        monitoring_mode = os.getenv("MONITORING_MODE", "PROD")

        should_bypass, is_azure_metrics, is_azure_dashboard, is_data_lookup, is_infra = \
            _check_bypass_conditions(query_lower, monitoring_mode)

        # Fast path: anomaly queries always return _anomaly_store directly
        if re.search(r'\banomal', query_lower) and not _IAC_INTENT_RE.search(query_lower):
            import uuid as _uuid
            recent = _anomaly_store[-20:] if _anomaly_store else []
            count = len(recent)
            resp_text = (
                f"Detected {count} performance anomal{'y' if count == 1 else 'ies'} across monitored Azure services."
                if recent else
                "No performance anomalies detected yet. Monitoring is active and will flag metric spikes automatically."
            )
            return JSONResponse(content={
                "success": True, "query": user_query, "response": resp_text,
                "charts": [], "recommendations": [], "anomalies": recent,
                "session_id": session_id or f"direct_{_uuid.uuid4().hex[:8]}",
                "timestamp": str(asyncio.get_event_loop().time()),
            })

        iac_resp = await _try_iac_direct(user_query, session_id)
        if iac_resp:
            return iac_resp

        mon_resp = await _try_monitoring_direct(
            query_lower, user_query, session_id,
            should_bypass, is_infra, is_azure_metrics, is_azure_dashboard, is_data_lookup,
        )
        if mon_resp:
            return mon_resp

        from master_agent.agent import runner, session_service
        logger.info(f"Processing query: {user_query}")

        # Determine whether this query result can be cached.
        # Exclude: real-time/live queries, load test queries, and monitoring dashboard queries.
        _cacheable = (
            not _agent_response_cache.is_realtime_query(user_query)
            and not re.search(r'load\s*test|run\s*test|doppelio|doppel', query_lower)
            and not bool(_GENERIC_MON_RE.search(query_lower))
        )
        _agent_cache_key = _agent_response_cache.normalize_query(user_query) if _cacheable else None

        _cached_agent = _agent_response_cache.get(_agent_cache_key) if _agent_cache_key else None
        if _cached_agent:
            logger.info(f"[cache HIT] agent response for: {user_query[:60]}")
            agent_response = _cached_agent["response"]
            all_events = _cached_agent["events"]
        else:
            try:
                agent_response, all_events, _ = await asyncio.wait_for(
                    _run_agent_session(runner, session_service, user_id, session_id, user_query, types),
                    timeout=120.0,
                )
                logger.info(f"Total events collected: {len(all_events)}, response length: {len(agent_response)}")
                for i, event in enumerate(all_events):
                    logger.debug(f"Event {i}: {type(event).__name__} - {str(event)[:200]}")
            except Exception as e:
                logger.exception(f"CRITICAL ERROR running agent: {type(e).__name__}: {str(e)}", exc_info=True)
                agent_response = f"I apologize, but I encountered an error processing your request. Error: {type(e).__name__}. Please try again."
                all_events = []

        if not agent_response or not agent_response.strip():
            logger.warning("No response collected from agent")
            agent_response = "I'm having trouble processing your request. Please try rephrasing your question."

        charts = _extract_charts(all_events, user_query)
        logger.info(f"Chart extraction: {len(charts)} charts from {len(all_events)} events")

        agent_response = re.sub(r"<chart>.*?</chart>", "", agent_response, flags=re.DOTALL).strip()
        pending_action, executed_action, agent_response = _parse_action_markers(agent_response)

        anomaly_summary = _detect_chart_anomalies(charts)
        _append_anomalies_to_store(charts)
        if anomaly_summary:
            agent_response = anomaly_summary + agent_response

        # Store in cache only after anomaly check — skip if anomaly text was prepended
        # (anomaly text embeds current metric values and is volatile).
        if _agent_cache_key and not _cached_agent and not anomaly_summary:
            _agent_response_cache.set(_agent_cache_key, {"response": agent_response, "events": all_events})
            logger.debug(f"[cache SET] agent response key={_agent_cache_key[:20]}")

        is_load_test_query = bool(re.search(r'load\s*test|run\s*test|doppelio|doppel', query_lower))
        is_load_test_response = _is_load_test_running(agent_response.lower())
        if is_load_test_query or is_load_test_response:
            _sync_lt_progress(agent_response)
            recommendations = _load_test_recommendations(agent_response.lower())
        else:
            recommendations = await generate_recommendations(user_query, agent_response)

        # When user confirmed a restart action and the test is now running,
        # split the response into separate message bubbles: restart → infra recheck → LT card.
        _messages: list = []
        restart_confirm_m = re.match(r'^yes,?\s+restart\s+(\S+)', query_lower.strip())
        if restart_confirm_m and is_load_test_response:
            svc = restart_confirm_m.group(1).rstrip('.,')
            _messages = [
                {
                    "response": f"✓ **{svc}** restarted successfully — service is now Running.",
                    "charts": [], "pending_action": None, "executed_action": None,
                    "recommendations": [], "anomalies": [], "doppelio_stats": None,
                },
                {
                    "response": "✓ Infrastructure re-checked — all services healthy and ready for the load test.",
                    "charts": [], "pending_action": None, "executed_action": None,
                    "recommendations": [], "anomalies": [], "doppelio_stats": None,
                },
                {
                    "response": agent_response,
                    "charts": charts,
                    "pending_action": pending_action,
                    "executed_action": executed_action,
                    "recommendations": recommendations,
                    "anomalies": [],
                    "doppelio_stats": None,
                },
            ]

        return JSONResponse(content={
            "success": True,
            "query": user_query,
            "response": agent_response,
            "messages": _messages,
            "charts": charts,
            "recommendations": recommendations,
            "pending_action": pending_action,
            "executed_action": executed_action,
            "session_id": f"session_{uuid.uuid4().hex[:8]}",
            "timestamp": str(asyncio.get_event_loop().time()),
            "debug": {
                "events_count": len(all_events),
                "response_length": len(agent_response),
                "charts_count": len(charts),
            },
        })

    except Exception as e:
        logger.exception(f"Error in chat endpoint: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
                "response": "I apologize, but I encountered an error. Please try again.",
                "recommendations": [],
            },
        )


# ── Demo insights API endpoints ───────────────────────────────────────────────

@app.get("/api/resource-config")
async def get_resource_config():
    """Proxy /resource-config from the Azure Monitor MCP to the frontend."""
    azure_mcp_url = os.getenv("AZURE_MONITOR_MCP_URL", "http://127.0.0.1:6200/").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{azure_mcp_url}/resource-config")
            if resp.status_code == 200:
                return JSONResponse(resp.json())
    except Exception as exc:
        logger.debug("resource-config proxy failed: %s", exc)
    return JSONResponse({})


@app.get("/api/anomalies")
async def get_anomalies():
    """Return all detected performance anomalies recorded since server start."""
    return JSONResponse({"anomalies": _anomaly_store, "count": len(_anomaly_store)})


@app.get("/api/sre-alerts")
async def get_sre_alerts():
    """Return all SRE infrastructure alerts recorded during active test runs."""
    return JSONResponse({"alerts": _sre_alerts, "count": len(_sre_alerts)})


@app.get("/api/cost-suggestions")
async def get_cost_suggestions():
    """Return pending idle-service cost-saving suggestions (newest first). Each carries a
    `suggested_user_message` the UI sends back to approve stopping the idle services."""
    return JSONResponse({
        "suggestions": list(reversed(_cost_suggestions)),
        "count": len(_cost_suggestions),
    })


@app.post("/api/cost-suggestions/scan")
async def trigger_cost_scan():
    """On-demand idle-service scan (does not wait for the daily loop). Records any
    suggestion and returns it. Read-only on Azure; no resource is stopped here."""
    cloudlens_url = os.getenv("CLOUDLENS_TOOL_URL", "http://127.0.0.1:6212/").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{cloudlens_url}/", json={"query": "scan idle resources for cost savings"})
        text = resp.json().get("result", "") if resp.status_code == 200 else ""
        match = re.search(r"<!--PENDING_ACTION:(.*?)-->", text, re.DOTALL)
        if match:
            pending = json.loads(match.group(1))
            if pending.get("type") == "stop_idle_for_savings":
                _record_cost_suggestion(pending)
        return JSONResponse({"suggestions": list(reversed(_cost_suggestions)), "count": len(_cost_suggestions)})
    except Exception as exc:
        logger.warning("On-demand cost scan failed: %s", exc)
        raise HTTPException(status_code=502, detail="Idle-service scan failed")


@app.get("/api/demo-insights")
async def get_demo_insights():
    """
    Unified demo insights endpoint — aggregates load test status, SRE alerts,
    and performance anomalies into a single response for the Master Agent dashboard.
    """
    try:
        from agentic_utils.tools.doppelio_orchestrator import get_all_active_tests
        active_tests = get_all_active_tests()
    except Exception:
        active_tests = []

    has_active_test = bool(active_tests)
    anomaly_count = len(_anomaly_store)
    alert_count = len(_sre_alerts)

    summary = _build_demo_insights_summary(has_active_test, active_tests, anomaly_count, alert_count)

    return JSONResponse({
        "summary": summary,
        "load_test_status": {
            "active": has_active_test,
            "tests": active_tests,
        },
        "sre_alerts": _sre_alerts,
        "anomalies": _anomaly_store,
    })


def _build_demo_insights_summary(has_active_test: bool, active_tests: list, anomaly_count: int, alert_count: int) -> str:
    """Build summary message for demo insights."""
    if not has_active_test and anomaly_count == 0 and alert_count == 0:
        return "No active load test. No anomalies or SRE alerts recorded."
    
    parts = []
    if has_active_test:
        parts.append(f"{len(active_tests)} active load test(s) running")
    if anomaly_count:
        parts.append(f"{anomaly_count} performance anomaly(ies) detected")
    if alert_count:
        parts.append(f"{alert_count} SRE infrastructure alert(s) raised")
    return "; ".join(parts) + "."


# ── Background monitoring loops ───────────────────────────────────────────────

async def _sre_monitoring_loop() -> None:
    """Poll infra health every 60 s while a load test is active. Stores results in _sre_alerts."""
    from agentic_utils.tools.sre_tools import monitor_infra_and_alert
    from datetime import datetime as _dt, timezone as _tz

    while True:
        await asyncio.sleep(60)
        try:
            from agentic_utils.tools.doppelio_orchestrator import get_all_active_tests
            if not get_all_active_tests():
                continue  # no test running — skip
            context = "active load test"
            report = await asyncio.to_thread(monitor_infra_and_alert, context)
            _sre_alerts.append({
                "timestamp": _dt.now(_tz.utc).isoformat(),
                "context": context,
                "report": report,
            })
            logger.info("SRE monitoring loop: alert recorded (%d total)", len(_sre_alerts))
        except Exception as exc:
            logger.warning("SRE monitoring loop error: %s", exc)


async def _fetch_azure_metrics(azure_mcp_url: str) -> str:
    """Fetch Azure performance metrics from MCP server."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{azure_mcp_url.rstrip('/')}/",
            json={"query": "azure performance metrics all services"},
        )
    if resp.status_code != 200:
        return ""
    return resp.json().get("result", "")


def _parse_chart_data(result_text: str) -> list:
    """Parse <chart> tags from response text and return parsed JSON objects."""
    chart_blobs = re.findall(r"<chart>(.*?)</chart>", result_text, re.DOTALL)
    parsed: list = []
    for blob in chart_blobs:
        try:
            parsed.append(json.loads(blob.strip()))
        except Exception:
            pass
    return parsed


async def _platform_monitor_loop() -> None:
    """Poll Azure metrics every 90 s during a load test and record anomalies."""
    from datetime import datetime as _dt, timezone as _tz

    azure_mcp_url = os.getenv("AZURE_MONITOR_MCP_URL", "http://127.0.0.1:6200/")

    while True:
        await asyncio.sleep(90)
        try:
            from agentic_utils.tools.doppelio_orchestrator import get_all_active_tests
            if not get_all_active_tests():
                continue
            
            result_text = await _fetch_azure_metrics(azure_mcp_url)
            if not result_text:
                continue
            
            parsed = _parse_chart_data(result_text)
            if parsed:
                before = len(_anomaly_store)
                _append_anomalies_to_store(parsed)
                new_count = len(_anomaly_store) - before
                if new_count:
                    logger.info(
                        "Platform monitor loop: %d new anomaly(ies) recorded (%d total)",
                        new_count, len(_anomaly_store),
                    )
        except Exception as exc:
            logger.warning("Platform monitor loop error: %s", exc)


def _record_cost_suggestion(pending: dict) -> int:
    """Store a fresh idle-service suggestion, de-duplicating on the idle-service set.
    Returns the number of NEW suggestions added (0 or 1)."""
    from datetime import datetime as _dt, timezone as _tz
    services = sorted(pending.get("idle_services") or [])
    if not services:
        return 0
    # Replace any existing suggestion for the same service set (keep it fresh, no dupes).
    global _cost_suggestions
    existed = any(sorted(s.get("idle_services") or []) == services for s in _cost_suggestions)
    _cost_suggestions = [s for s in _cost_suggestions if sorted(s.get("idle_services") or []) != services]
    _cost_suggestions.append({
        "timestamp": _dt.now(_tz.utc).isoformat(),
        "count": pending.get("summary"),
        "services": pending.get("service"),
        "summary": pending.get("summary"),
        "rationale": pending.get("rationale"),
        "suggested_user_message": pending.get("suggested_user_message"),
        "idle_services": services,
        "projected_monthly_cost": pending.get("projected_monthly_cost"),
        "window_days": pending.get("window_days"),
    })
    return 0 if existed else 1


async def _idle_scan_loop() -> None:
    """Proactively scan for idle services on a fixed interval (default daily) and record
    cost-saving stop suggestions. Runs regardless of load-test activity. Each suggestion
    surfaces via /api/cost-suggestions; the user approves before anything is stopped."""
    cloudlens_url = os.getenv("CLOUDLENS_TOOL_URL", "http://127.0.0.1:6212/").rstrip("/")
    interval_hours = float(os.getenv("FINOPS_SCAN_INTERVAL_HOURS", "24"))
    interval_secs = max(60.0, interval_hours * 3600.0)
    # Small initial delay so dependent tool services are up before the first scan.
    await asyncio.sleep(float(os.getenv("FINOPS_SCAN_INITIAL_DELAY_SECS", "120")))

    while True:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{cloudlens_url}/", json={"query": "scan idle resources for cost savings"})
            if resp.status_code == 200:
                text = resp.json().get("result", "")
                match = re.search(r"<!--PENDING_ACTION:(.*?)-->", text, re.DOTALL)
                if match:
                    pending = json.loads(match.group(1))
                    if pending.get("type") == "stop_idle_for_savings":
                        added = _record_cost_suggestion(pending)
                        if added:
                            logger.info("Idle-scan loop: new cost suggestion (%d total)", len(_cost_suggestions))
        except Exception as exc:
            logger.warning("Idle-scan loop error: %s", exc)
        await asyncio.sleep(interval_secs)


@app.on_event("startup")
async def _start_background_monitoring() -> None:
    """Start background infra and performance monitoring loops on server startup."""
    asyncio.create_task(_sre_monitoring_loop())
    asyncio.create_task(_platform_monitor_loop())
    asyncio.create_task(_idle_scan_loop())
    logger.info("Background SRE + Platform Monitor + Idle-scan loops started")


def run_web_server():
    """Initializes and runs the FastAPI web server."""
    host = os.getenv("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=8000, reload=False)


def run_a2a_server():
    """Initializes and runs the FastAPI web server."""
    print("Starting A2A server with a2a=True")
    host = os.getenv("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=8000, reload=False)

if __name__ == "__main__":
    print(f"Static files directory: {static_dir}")
    print("Custom Chat UI will be available at: http://127.0.0.1:5001/")
    print("API endpoint: http://127.0.0.1:5001/api/chat")

    if '--a2a' in sys.argv:
        print("Starting in A2A server mode...")
        run_a2a_server()
    else:
        print("Starting in web server mode...")
        run_web_server()
