"""Log remediation tools for the SRE agent.

Three FunctionTools:
  1. fetch_and_analyze_logs      — calls the Blob Log Reader (port 6115) and returns
                                   a formatted Markdown error summary.
  2. remediate_log_errors        — presents a PENDING_ACTION card (confirmed=False),
                                   then executes a restart via action_executor (confirmed=True).
  3. diagnose_and_fix_service    — SINGLE-CALL ORCHESTRATOR: checks service health,
                                   auto-fetches blob logs if issue detected, detects root
                                   cause, and returns a PENDING_ACTION confirm card.
                                   Called automatically whenever the agent detects a service
                                   is down/erroring — no explicit user "check logs" needed.

Restart = stop_service() → 5 s pause → start_service() using the same ARM-gated
allowlist pattern as the rest of the agentic layer (AGENTIC_RESTART_ALLOWLIST).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import requests
from google.adk.tools import FunctionTool

# ── Import action_executor from root/agentic/ ─────────────────────────────────
_ROOT = Path(__file__).resolve().parents[4]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import agentic.action_executor as _action_executor  # type: ignore
except Exception:
    _action_executor = None  # pragma: no cover

# ── Import azure_rest for live health checks ──────────────────────────────────
_AZURE_REST_DIR = _ROOT / "mcp_servers" / "Azure_Monitor_MCP"
if str(_AZURE_REST_DIR) not in sys.path:
    sys.path.insert(0, str(_AZURE_REST_DIR))

try:
    import azure_rest as _az  # type: ignore
except Exception:
    _az = None  # pragma: no cover

# Map service name → azure_rest service key used by check_service_connectivity
_SERVICE_KEY_MAP = {
    # Webapp
    "infrainsight": "webapp",
    # Function app: try to match env var
    os.getenv("AZURE_FUNCTION_APP_NAME", "function-10112"): "function",
    # Fallback: if name contains "function" → "function", else → "webapp"
}


def _resolve_service_key(service_name: str) -> str:
    """Map a human-readable service name to the azure_rest service key."""
    direct = _SERVICE_KEY_MAP.get(service_name.lower())
    if direct:
        return direct
    if "function" in service_name.lower():
        return "function"
    return "webapp"

# ── Config ────────────────────────────────────────────────────────────────────
BLOB_LOG_READER_URL = os.getenv("BLOB_LOG_READER_URL", "http://127.0.0.1:6115/")

# Remediation allowlist — separate from start/stop allowlists so log-driven
# restarts require explicit opt-in (default "" = deny-all for safety).
_RESTART_ALLOWLIST_ENV = "AGENTIC_RESTART_ALLOWLIST"

# ── Remediation playbook ──────────────────────────────────────────────────────
_PLAYBOOK = {
    "OutOfMemoryException": {
        "action": "restart",
        "severity": "high",
        "rationale": "OutOfMemoryException detected — restart clears heap pressure",
    },
    "high_error_rate": {
        "action": "restart",
        "severity": "high",
        "rationale": "High 4xx/5xx error rate — restart to recover unhealthy state",
    },
    "application_crash": {
        "action": "restart",
        "severity": "critical",
        "rationale": "Application crash detected — restart to recover",
    },
    "connection_failure": {
        "action": "health_check",
        "severity": "medium",
        "rationale": "Connection failures — check dependency health before acting",
    },
    "process_exit": {
        "action": "restart",
        "severity": "high",
        "rationale": "Process exit detected — restart to recover",
    },
}

_RESOURCE_TYPE_WEBAPP = "microsoft.web/sites"


def _build_resource_id(service_name: str) -> str:
    sub = os.getenv("AZURE_SUBSCRIPTION_ID", "")
    rg  = os.getenv("AZURE_RESOURCE_GROUP", "")
    return (
        f"/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/sites/{service_name}"
    )


def _allowlist_permits(resource_id: str) -> tuple[bool, str]:
    """Check AGENTIC_RESTART_ALLOWLIST.
    Returns (permitted, reason).
    None/unset → allow_all; '' → deny_all; list → whitelist."""
    raw = os.getenv(_RESTART_ALLOWLIST_ENV)
    if raw is None:
        return True, "allow_all"
    stripped = raw.strip()
    if not stripped:
        return False, (
            "AGENTIC_RESTART_ALLOWLIST is empty (deny-all). "
            "Set it to 'microsoft.web/sites:<resource_id>' to enable log-driven restarts."
        )
    allow_key = f"{_RESOURCE_TYPE_WEBAPP}:{resource_id}"
    if allow_key in {x.strip() for x in stripped.split(",") if x.strip()}:
        return True, "whitelisted"
    return False, f"'{allow_key}' is not in AGENTIC_RESTART_ALLOWLIST"


# ── Tool 1: fetch_and_analyze_logs ────────────────────────────────────────────

def fetch_and_analyze_logs(
    service_name: str,
    hours_back: int = 2,
) -> str:
    """Fetch Azure Diagnostic Settings logs for an Azure service from Blob Storage
    and return a structured error summary.

    Automatically discovers all log containers (insights-logs-appserviceconsolelogs,
    insights-logs-appservicehttplogs, insights-logs-functionapplogs, etc.) for the
    given service and the requested time window. Parses JSON blobs, counts errors/
    warnings, and detects common failure patterns.

    Args:
        service_name: The Azure resource name (e.g. 'infrainsight', 'function-10112').
                      Defaults to AZURE_WEBAPP_NAME if not provided.
        hours_back:   How many hours of logs to scan (1–48, default 2).

    Returns:
        A Markdown-formatted summary with error counts, detected patterns, and
        sample error messages. If no errors found, confirms logs are clean.
    """
    if not service_name:
        service_name = os.getenv("AZURE_WEBAPP_NAME", "infrainsight")

    hours_back = max(1, min(int(hours_back), 48))

    url = BLOB_LOG_READER_URL.rstrip("/") + "/"
    try:
        resp = requests.post(
            url,
            json={
                "service_name": service_name,
                "hours_back": hours_back,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return (
                f"**Blob Log Reader error:** HTTP {resp.status_code}. "
                "Ensure the service is running on port 6115 and AZURE_STORAGE_ACCOUNT_NAME is set."
            )
        data = resp.json()
        if "error" in data:
            return f"**Log fetch error:** {data['error']}"
        return data.get("result", json.dumps(data, indent=2))
    except requests.exceptions.ConnectionError:
        return (
            "**Blob Log Reader is not reachable** (port 6115). "
            "Start it with: `uvicorn main:app --port 6115` in tools/blobreader/CODE_EXECUTION/"
        )
    except Exception as e:
        return f"**Unexpected error fetching logs:** {type(e).__name__}: {e}"


# ── Tool 2: remediate_log_errors ──────────────────────────────────────────────

def remediate_log_errors(
    service_name: str,
    detected_patterns: str = "",
    confirmed: bool = False,
) -> str:
    """Remediate errors detected in Azure Blob Storage logs for an Azure service.

    Two-phase workflow (ask-first):
      1. confirmed=False (default) — returns a PENDING_ACTION card summarising
         the proposed fix. The UI renders this as a confirmation strip before
         any real Azure action is taken. ALWAYS call with confirmed=False first.
      2. confirmed=True — executes the restart (stop → wait 5 s → start) using
         the same ARM-gated allowlist as the rest of the agentic layer.

    Args:
        service_name:      Azure resource name (e.g. 'infrainsight').
        detected_patterns: Comma-separated list of error patterns found by
                           fetch_and_analyze_logs (e.g. 'OutOfMemoryException,high_error_rate').
        confirmed:         Set to True only after the user has confirmed the action card.

    Returns:
        PENDING_ACTION HTML comment (confirmed=False) or remediation result (confirmed=True).
    """
    if not service_name:
        service_name = os.getenv("AZURE_WEBAPP_NAME", "infrainsight")

    patterns = [p.strip() for p in detected_patterns.split(",") if p.strip()]
    resource_id = _build_resource_id(service_name)

    # Determine the highest-severity action from playbook
    action = "health_check"
    rationale_parts = []
    for p in patterns:
        pb = _PLAYBOOK.get(p)
        if not pb:
            continue
        if pb["action"] == "restart":
            action = "restart"
        rationale_parts.append(pb["rationale"])

    if not rationale_parts:
        rationale_parts = ["Error patterns detected in logs — manual inspection recommended"]

    combined_rationale = "; ".join(rationale_parts)

    # ── Phase 1: return PENDING_ACTION card ──────────────────────────────────
    if not confirmed:
        pending = {
            "type": "restart_service",
            "service": service_name,
            "resource_id": resource_id,
            "resource_type": _RESOURCE_TYPE_WEBAPP,
            "action": action,
            "patterns": patterns,
            "rationale": combined_rationale,
            "requires_confirmation": True,
            "suggested_user_message": f"Yes, restart {service_name}",
        }
        lines = [
            f"## Log-Driven Remediation — {service_name}",
            f"",
            f"**Detected patterns:** {', '.join(patterns) if patterns else 'generic errors'}",
            f"**Proposed action:** {action.upper()} `{service_name}`",
            f"**Rationale:** {combined_rationale}",
            f"",
            f"Confirm to proceed with the restart.",
            f"",
            f"<!--PENDING_ACTION:{json.dumps(pending, separators=(',', ':'))}-->",
        ]
        return "\n".join(lines)

    # ── Phase 2: execute remediation ─────────────────────────────────────────
    permitted, reason = _allowlist_permits(resource_id)
    if not permitted:
        return (
            f"**Remediation blocked:** {reason}\n\n"
            "To enable log-driven restarts, add the resource to AGENTIC_RESTART_ALLOWLIST:\n"
            f"```\nAGENTIC_RESTART_ALLOWLIST={_RESOURCE_TYPE_WEBAPP}:{resource_id}\n```"
        )

    if action == "health_check":
        return (
            f"**Health check recommended** for `{service_name}` — no destructive action taken.\n"
            f"**Reason:** {combined_rationale}\n\n"
            "Run a health check or inspect dependency services before restarting."
        )

    if _action_executor is None:
        return (
            "**action_executor is unavailable** — cannot execute restart. "
            "Ensure the agentic/ directory is on the Python path."
        )

    results = []

    # Stop
    stop_result = _action_executor.stop_service(
        service_name=service_name,
        rationale=f"Log-driven restart: {combined_rationale}",
        resource_type=_RESOURCE_TYPE_WEBAPP,
        resource_id=resource_id,
        before_state="Running",
    )
    results.append(f"**Stop:** {stop_result.get('summary', str(stop_result))}")

    if stop_result.get("real_action_taken"):
        time.sleep(5)

    # Start
    start_result = _action_executor.start_service(
        service_name=service_name,
        rationale=f"Log-driven restart: {combined_rationale}",
        resource_type=_RESOURCE_TYPE_WEBAPP,
        resource_id=resource_id,
        before_state="Stopped",
    )
    results.append(f"**Start:** {start_result.get('summary', str(start_result))}")

    success = start_result.get("real_action_taken", False)
    status_icon = "✅" if success else "⚠️"

    agentic_record = {
        "type": "restart_service",
        "service": service_name,
        "resource_id": resource_id,
        "patterns": patterns,
        "rationale": combined_rationale,
        "stop_result": stop_result,
        "start_result": start_result,
    }

    lines = [
        f"## Remediation Result — {service_name}",
        f"",
        *results,
        f"",
        f"{status_icon} Restart {'completed' if success else 'attempted (check logs for details)'}.",
        f"",
        f"<!--AGENTIC_ACTION:{json.dumps(agentic_record, separators=(',', ':'))}-->",
    ]
    return "\n".join(lines)


# ── Tool 3: diagnose_and_fix_service (single-call orchestrator) ───────────────

def _fetch_logs_internal(service_name: str, hours_back: int = 2) -> dict:
    """Internal helper: call Blob Log Reader and return parsed JSON response dict.
    Returns {} with an 'unavailable' key if the service is unreachable."""
    url = os.getenv("BLOB_LOG_READER_URL", "http://127.0.0.1:6115/").rstrip("/") + "/"
    try:
        resp = requests.post(
            url,
            json={"service_name": service_name, "hours_back": hours_back},
            timeout=45,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"unavailable": f"HTTP {resp.status_code}"}
    except requests.exceptions.ConnectionError:
        return {"unavailable": "Blob Log Reader not reachable (port 6115)"}
    except Exception as e:
        return {"unavailable": f"{type(e).__name__}: {e}"}


def diagnose_and_fix_service(service_name: str, context: str = "") -> str:
    """Single-call orchestrator for proactive self-healing.

    Automatically called by the SRE agent whenever a service is detected as
    Stopped or erroring — no explicit "check logs" instruction from the user needed.

    Steps:
      1. Check live service health via Azure ARM.
      2. If Stopped/Erroring → fetch the last 2 hours of blob logs.
      3. Detect root-cause error patterns from those logs.
      4. Return a PENDING_ACTION confirmation card so the user can approve the fix.

    If the Blob Log Reader or storage account is unavailable the tool still returns
    a PENDING_ACTION card (status-only, no log details) so the fix is not blocked.

    Args:
        service_name: Azure resource name (e.g. 'infrainsight', 'function-10112').
        context:      Brief description of what triggered this call
                      (e.g. 'pre-load-test check', 'user fix request').

    Returns:
        Markdown with health status + log-derived root cause + PENDING_ACTION card.
        Returns a "healthy" notice if no issues are detected.
    """
    if not service_name:
        service_name = os.getenv("AZURE_WEBAPP_NAME", "infrainsight")

    resource_id = _build_resource_id(service_name)
    service_key = _resolve_service_key(service_name)

    # ── Step 1: Live health check ────────────────────────────────────────────
    health = {"healthy": None, "state": "Unknown"}
    if _az is not None:
        try:
            health = _az.check_service_connectivity(service_key)
        except Exception as e:
            health = {"healthy": False, "state": f"Check failed: {e}"}

    state = health.get("state", "Unknown")
    is_healthy = health.get("healthy", True)

    # If clearly healthy with no indication of errors, short-circuit
    if is_healthy and state.lower() in ("running", "healthy", "active"):
        # Still worth checking logs if context says there were errors
        if not context or "error" not in context.lower() and "down" not in context.lower():
            return (
                f"## Service Diagnosis — {service_name}\n\n"
                f"✅ **Status:** {state} — service appears healthy.\n\n"
                f"No issues detected. No action needed."
            )

    # ── Step 2: Fetch blob logs ───────────────────────────────────────────────
    log_data = _fetch_logs_internal(service_name, hours_back=2)
    log_unavailable = log_data.get("unavailable")
    detected_patterns: list = log_data.get("detected_patterns", [])
    error_count: int = log_data.get("error_count", 0)
    log_summary_text: str = log_data.get("summary", "")
    error_samples: list = log_data.get("error_samples", [])

    # ── Step 3: Build rationale from health + patterns ───────────────────────
    rationale_parts = []
    if state.lower() in ("stopped", "not found", "unreachable"):
        rationale_parts.append(f"Service is {state}")
    for p in detected_patterns:
        pb = _PLAYBOOK.get(p)
        if pb:
            rationale_parts.append(pb["rationale"])
    if error_count and not rationale_parts:
        rationale_parts.append(f"{error_count} errors detected in recent logs")
    if not rationale_parts:
        rationale_parts.append(context or "Service issue detected — restart recommended")

    combined_rationale = "; ".join(rationale_parts)

    # Determine action (restart unless all patterns map to health_check)
    action = "health_check"
    if state.lower() in ("stopped", "disabled", "not found", "unreachable"):
        action = "restart"
    else:
        for p in detected_patterns:
            pb = _PLAYBOOK.get(p)
            if pb and pb["action"] == "restart":
                action = "restart"
                break

    # ── Step 4: Build output with PENDING_ACTION card ─────────────────────────
    pending = {
        "type": "restart_service",
        "service": service_name,
        "resource_id": resource_id,
        "resource_type": _RESOURCE_TYPE_WEBAPP,
        "action": action,
        "patterns": detected_patterns,
        "rationale": combined_rationale,
        "requires_confirmation": True,
        "triggered_by": context or "automatic detection",
        "suggested_user_message": f"Yes, restart {service_name}",
    }

    lines = [
        f"## Service Diagnosis — {service_name}",
        f"",
        f"**Live status:** {state}",
    ]

    if log_unavailable:
        lines.append(f"**Log analysis:** Unavailable — {log_unavailable}")
    else:
        lines.append(
            f"**Log analysis:** {error_count} error(s) in last 2 hours"
            + (f" — patterns: `{'`, `'.join(detected_patterns)}`" if detected_patterns else "")
        )
        if error_samples:
            lines.append("\n**Sample errors:**")
            for i, s in enumerate(error_samples[:3], 1):
                ts  = s.get("timestamp") or s.get("timeStamp") or ""
                msg = s.get("message") or s.get("Message") or s.get("exceptionType", "")
                lines.append(f"  {i}. {('[' + ts + '] ') if ts else ''}{str(msg)[:200]}")

    lines += [
        f"",
        f"**Root cause:** {combined_rationale}",
        f"**Proposed fix:** {action.upper()} `{service_name}`",
        f"",
        f"Confirm to proceed.",
        f"",
        f"<!--PENDING_ACTION:{json.dumps(pending, separators=(',', ':'))}-->",
    ]

    return "\n".join(lines)


# ── FunctionTool wrappers ─────────────────────────────────────────────────────
fetch_and_analyze_logs_tool    = FunctionTool(func=fetch_and_analyze_logs)
remediate_log_errors_tool      = FunctionTool(func=remediate_log_errors)
diagnose_and_fix_service_tool  = FunctionTool(func=diagnose_and_fix_service)
