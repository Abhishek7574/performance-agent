"""Blob Log Reader — FastAPI microservice on port 6115.

Discovery-first approach: automatically finds all Azure Diagnostic Settings
log containers for a given service, navigates the time-based folder hierarchy,
and returns a structured error summary.

Simplified API:
  POST / { service_name, hours_back }
  → internally builds: resourceId=/SUBSCRIPTIONS/.../SITES/{SERVICE}/y=/m=/d=/h=/m=00/
  → scans ALL insights-* containers for that prefix
  → parses JSON blobs, detects error patterns, returns summary
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv(override=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("blobreader")

app = FastAPI(title="Blob Log Reader", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Azure config (read at import time; re-read per-request if needed) ─────────
_STORAGE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "")
_SUBSCRIPTION_ID = os.getenv("AZURE_SUBSCRIPTION_ID", "")
_RESOURCE_GROUP  = os.getenv("AZURE_RESOURCE_GROUP", "")
_TENANT_ID       = os.getenv("AZURE_TENANT_ID", "")
_CLIENT_ID       = os.getenv("AZURE_CLIENT_ID", "")
_CLIENT_SECRET   = os.getenv("AZURE_CLIENT_SECRET", "")

# ── Error pattern detection ───────────────────────────────────────────────────
_PATTERNS: Dict[str, re.Pattern] = {
    "OutOfMemoryException": re.compile(r"out.?of.?memory|OutOfMemory", re.IGNORECASE),
    "high_error_rate":      re.compile(r'"resultCode"\s*:\s*"[45]\d{2}"|"statusCode"\s*:\s*[45]\d{2}', re.IGNORECASE),
    "application_crash":    re.compile(r"unhandled.?exception|application.?crash|fatal error", re.IGNORECASE),
    "connection_failure":   re.compile(r"connection.?refused|failed.?to.?connect|timeout.*connect", re.IGNORECASE),
    "process_exit":         re.compile(r"process.?exit|exit.?code\s+[^0\s]", re.IGNORECASE),
}

_LOG_LEVEL_FIELD_NAMES = ("level", "Level", "severityLevel", "SeverityLevel", "Severity")
_ERROR_LEVELS          = {"error", "critical", "err", "crit", "3", "4"}  # numeric levels for App Insights
_WARNING_LEVELS        = {"warning", "warn", "2"}


# ── BlobServiceClient factory ─────────────────────────────────────────────────
def _build_client():
    """Build a BlobServiceClient from env-var Service Principal credentials."""
    from azure.identity import ClientSecretCredential
    from azure.storage.blob import BlobServiceClient

    storage_account = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", _STORAGE_ACCOUNT)
    if not storage_account:
        raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is not set")

    credential = ClientSecretCredential(
        tenant_id=os.getenv("AZURE_TENANT_ID", _TENANT_ID),
        client_id=os.getenv("AZURE_CLIENT_ID", _CLIENT_ID),
        client_secret=os.getenv("AZURE_CLIENT_SECRET", _CLIENT_SECRET),
    )
    return BlobServiceClient(
        account_url=f"https://{storage_account}.blob.core.windows.net",
        credential=credential,
    )


# ── Path construction ─────────────────────────────────────────────────────────
def _build_arm_prefix(service_name: str, provider: str = "MICROSOFT.WEB/SITES") -> str:
    """Construct the ARM resource ID prefix as Azure writes it (uppercase).

    Example output:
      resourceId=/SUBSCRIPTIONS/XXXXXX/RESOURCEGROUPS/RG-XXX/PROVIDERS/MICROSOFT.WEB/SITES/INFRAINSIGHT/
    """
    sub = os.getenv("AZURE_SUBSCRIPTION_ID", _SUBSCRIPTION_ID).upper()
    rg  = os.getenv("AZURE_RESOURCE_GROUP", _RESOURCE_GROUP).upper()
    svc = service_name.upper()
    return (
        f"resourceId=/SUBSCRIPTIONS/{sub}"
        f"/RESOURCEGROUPS/{rg}"
        f"/PROVIDERS/{provider}/{svc}/"
    )


def _build_time_prefixes(arm_prefix: str, hours_back: int) -> List[str]:
    """Return one blob prefix string per hour within [now-hours_back, now]."""
    now = datetime.now(timezone.utc)
    prefixes = []
    for h in range(hours_back):
        dt = now - timedelta(hours=h)
        time_path = (
            f"y={dt.year}/m={dt.month:02d}/"
            f"d={dt.day:02d}/h={dt.hour:02d}/m=00/"
        )
        prefixes.append(arm_prefix + time_path)
    return prefixes


# ── Log parsing ───────────────────────────────────────────────────────────────
def _parse_level(entry: Dict[str, Any]) -> str:
    for field in _LOG_LEVEL_FIELD_NAMES:
        val = entry.get(field)
        if val is not None:
            return str(val).lower()
    return ""


def _parse_blob_content(raw: bytes) -> List[Dict[str, Any]]:
    """Parse newline-delimited JSON or JSON array blobs."""
    text = raw.decode("utf-8", errors="replace")
    entries = []
    # Try array first
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # Newline-delimited JSON (most common for Diagnostic Settings)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, list):
                entries.extend(obj)
            elif isinstance(obj, dict):
                # Some blobs wrap records in {"records": [...]}
                if "records" in obj and isinstance(obj["records"], list):
                    entries.extend(obj["records"])
                else:
                    entries.append(obj)
        except json.JSONDecodeError:
            continue
    return entries


def _detect_patterns_in_text(text: str) -> List[str]:
    return [name for name, pat in _PATTERNS.items() if pat.search(text)]


# ── Core discovery logic ──────────────────────────────────────────────────────
def discover_service_logs(
    service_name: str,
    hours_back: int = 2,
    max_blobs_per_container: int = 10,
) -> Dict[str, Any]:
    """
    1. List ALL containers in the storage account.
    2. For each insights-* container, list blobs matching the service ARM prefix
       within the requested time window.
    3. Download up to max_blobs_per_container blobs per container and parse them.
    4. Return a structured summary with error counts and detected patterns.
    """
    client = _build_client()

    # Try both web app (MICROSOFT.WEB/SITES) and function app (same provider)
    # and Application Insights (MICROSOFT.INSIGHTS/COMPONENTS)
    providers = [
        "MICROSOFT.WEB/SITES",
        "MICROSOFT.INSIGHTS/COMPONENTS",
    ]
    arm_prefixes = [_build_arm_prefix(service_name, p) for p in providers]

    results_by_container: Dict[str, List[Dict[str, Any]]] = {}
    containers_searched = 0

    try:
        container_list = list(client.list_containers())
    except Exception as e:
        return {"error": f"Failed to list containers: {type(e).__name__}: {e}"}

    for container in container_list:
        cname = container["name"]
        if not cname.startswith("insights-"):
            continue
        containers_searched += 1

        container_client = client.get_container_client(cname)
        found_blobs = []

        for arm_prefix in arm_prefixes:
            time_prefixes = _build_time_prefixes(arm_prefix, hours_back)
            for tp in time_prefixes:
                try:
                    blobs = list(container_client.list_blobs(name_starts_with=tp))
                    found_blobs.extend(blobs)
                except Exception as e:
                    logger.warning(f"[{cname}] list_blobs prefix={tp}: {e}")
                    continue

        if not found_blobs:
            continue

        # Cap per container to avoid timeouts
        found_blobs = found_blobs[:max_blobs_per_container]
        entries = []
        for blob in found_blobs:
            try:
                data = container_client.download_blob(blob.name).readall()
                parsed = _parse_blob_content(data)
                entries.extend(parsed)
            except Exception as e:
                logger.warning(f"[{cname}/{blob.name}] download failed: {e}")

        if entries:
            results_by_container[cname] = entries

    # Aggregate across all containers
    all_entries: List[Dict[str, Any]] = []
    for entries in results_by_container.values():
        all_entries.extend(entries)

    error_entries = [e for e in all_entries if _parse_level(e) in _ERROR_LEVELS]
    warning_entries = [e for e in all_entries if _parse_level(e) in _WARNING_LEVELS]

    # Detect patterns across all raw entry text
    all_text = " ".join(
        json.dumps(e, ensure_ascii=False) for e in all_entries
    )
    detected_patterns = _detect_patterns_in_text(all_text)

    # Sample up to 5 error entries (trim large fields)
    error_samples = []
    for entry in error_entries[:5]:
        sample = {}
        for key in ("timestamp", "timeStamp", "time", "level", "Level",
                    "message", "Message", "exceptionType", "resultCode",
                    "operationName", "properties"):
            if key in entry:
                val = entry[key]
                sample[key] = str(val)[:300] if isinstance(val, str) else val
        error_samples.append(sample)

    # Build human-readable summary
    containers_with_data = list(results_by_container.keys())
    if not all_entries:
        summary = (
            f"No log data found for '{service_name}' in the last {hours_back} hour(s). "
            f"Searched {containers_searched} containers. "
            "Check that Diagnostic Settings are enabled and the storage account name is correct."
        )
    elif not error_entries:
        summary = (
            f"✅ '{service_name}' logs look clean. "
            f"{len(all_entries)} entries across "
            f"{len(containers_with_data)} container(s). No errors detected."
        )
    else:
        pattern_str = ", ".join(detected_patterns) if detected_patterns else "generic errors"
        summary = (
            f"⚠️ Found {len(error_entries)} error(s) and {len(warning_entries)} warning(s) "
            f"across {len(containers_with_data)} container(s) in the last {hours_back} hour(s). "
            f"Patterns detected: {pattern_str}."
        )

    return {
        "service": service_name,
        "time_window_hours": hours_back,
        "containers_searched": containers_searched,
        "containers_with_data": containers_with_data,
        "total_log_entries": len(all_entries),
        "error_count": len(error_entries),
        "warning_count": len(warning_entries),
        "detected_patterns": detected_patterns,
        "error_samples": error_samples,
        "summary": summary,
    }


# ── Request / response models ─────────────────────────────────────────────────
class BlobLogRequest(BaseModel):
    query: str = ""
    service_name: str = ""
    hours_back: int = 2
    max_blobs_per_container: int = 10


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.post("/")
async def read_logs(req: BlobLogRequest):
    """Main endpoint — discover and return log summary for the given service."""
    service = req.service_name.strip()
    if not service:
        # Try to extract service name from free-text query
        # e.g. "check infrainsight logs" → "infrainsight"
        m = re.search(r"\b([a-z][a-z0-9\-]{2,})\b", req.query.lower())
        service = m.group(1) if m else os.getenv("AZURE_WEBAPP_NAME", "infrainsight")

    if not _STORAGE_ACCOUNT and not os.getenv("AZURE_STORAGE_ACCOUNT_NAME"):
        return {
            "error": "AZURE_STORAGE_ACCOUNT_NAME is not set",
            "hint": "Add it to your .env file",
        }

    try:
        result = discover_service_logs(
            service_name=service,
            hours_back=max(1, min(req.hours_back, 48)),
            max_blobs_per_container=min(req.max_blobs_per_container, 20),
        )
    except Exception as e:
        logger.exception("discover_service_logs failed")
        return {"error": f"{type(e).__name__}: {e}", "service": service}

    # Format result as a readable text block for the agent
    result["result"] = _format_markdown(result)
    return result


def _format_markdown(r: Dict[str, Any]) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}"

    lines = [
        f"## Blob Log Analysis — {r['service']}",
        f"**Time window:** last {r['time_window_hours']} hour(s)",
        f"**Containers searched:** {r['containers_searched']}  |  "
        f"**With data:** {len(r['containers_with_data'])}",
        f"**Total entries:** {r['total_log_entries']}  |  "
        f"**Errors:** {r['error_count']}  |  "
        f"**Warnings:** {r['warning_count']}",
    ]

    if r["containers_with_data"]:
        lines.append("\n**Containers with log data:**")
        for c in r["containers_with_data"]:
            lines.append(f"  - `{c}`")

    if r["detected_patterns"]:
        lines.append("\n**Detected error patterns:**")
        for p in r["detected_patterns"]:
            lines.append(f"  - `{p}`")

    if r["error_samples"]:
        lines.append("\n**Sample errors (up to 5):**")
        for i, s in enumerate(r["error_samples"], 1):
            ts  = s.get("timestamp") or s.get("timeStamp") or s.get("time", "")
            msg = s.get("message") or s.get("Message") or s.get("exceptionType", "")
            rc  = s.get("resultCode", "")
            lines.append(f"  {i}. [{ts}] {msg}" + (f" (code: {rc})" if rc else ""))

    lines.append(f"\n---\n{r['summary']}")
    return "\n".join(lines)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "storage_account_configured": bool(
            os.getenv("AZURE_STORAGE_ACCOUNT_NAME", _STORAGE_ACCOUNT)
        ),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6115)
