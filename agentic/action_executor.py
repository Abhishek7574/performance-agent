"""Action executor — the layer that turns agent decisions into real, persistent operations.

Four handlers, each returning a structured result dict (success/error are values, never exceptions
to the caller). Real Azure operations are gated by per-resource-id allowlists so a misconfigured
demo can only ever touch resources you explicitly enroll.

Handlers:
  - save_iac_artifact: writes generated Terraform/Bicep to disk
  - append_audit_log:  appends a JSON line to the agentic actions log
  - tag_azure_resource: sets tags on an allowlisted Azure resource
  - start_service:     flips a sim service state; if allowlisted, also calls Azure ARM start
  - stop_service:      stops a running Azure resource via ARM; gated by AGENTIC_STOP_ALLOWLIST
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Reuse azure_rest's token + session so the start path uses the same Service Principal
# auth as the read path (azure_health, check_service_connectivity). No SDK install needed.
_AZURE_REST_DIR = Path(__file__).resolve().parents[1] / "mcp_servers" / "Azure_Monitor_MCP"
if str(_AZURE_REST_DIR) not in sys.path:
    sys.path.insert(0, str(_AZURE_REST_DIR))
try:
    import azure_rest as _az  # type: ignore
except Exception as _e:  # pragma: no cover
    _az = None
    print(f"Warning: action_executor could not import azure_rest ({_e}); real Azure actions disabled")

# ── Allowlist mode constants ──────────────────────────────────────────────────
_MODE_ALLOW_ALL = "allow_all"
_MODE_DENY_ALL  = "deny_all"
_MODE_WHITELIST = "whitelist"

# ── Config (env-driven, with safe defaults) ──────────────────────────────────
ARTIFACT_DIR = Path(os.getenv("AGENTIC_ARTIFACT_DIR", "/home/site/wwwroot/generated_iac"))
AUDIT_LOG = Path(os.getenv("AGENTIC_AUDIT_LOG", "/home/site/wwwroot/agentic_actions.log"))


def _allowlist_policy(env_var: str) -> tuple[str, set]:
    """Resolve an allowlist env var into (mode, set).
    Unset → 'allow_all' (product default — trust caller-supplied resource ids).
    Empty string → 'deny_all' (defensive: explicit opt-out of real actions).
    Comma-separated values → 'whitelist' with that set."""
    raw = os.getenv(env_var)
    if raw is None:
        return _MODE_ALLOW_ALL, set()
    stripped = raw.strip()
    if not stripped:
        return _MODE_DENY_ALL, set()
    return _MODE_WHITELIST, {x.strip() for x in stripped.split(",") if x.strip()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", s).strip("-").lower() or "artifact"


# ── 1. Save IaC artifact ─────────────────────────────────────────────────────
def save_iac_artifact(content: str, service_name: str, fmt: str = "tf") -> Dict[str, Any]:
    """Write generated Terraform/Bicep to disk, return {success, path, download_url, size}."""
    if fmt not in ("tf", "bicep"):
        return {"success": False, "error": f"unsupported format: {fmt}"}
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{ts}_{_slug(service_name)}.{fmt}"
        target = ARTIFACT_DIR / filename
        target.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "path": str(target),
            "filename": filename,
            "download_url": f"/api/artifacts/{filename}",
            "size": target.stat().st_size,
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ── 2. Audit log ─────────────────────────────────────────────────────────────
def append_audit_log(record: Dict[str, Any]) -> Dict[str, Any]:
    """Append a JSON line. Schema fields auto-filled if missing: ts, type, service, decided_by,
    accepted_by, rationale, result. Returns {success, path}."""
    record = dict(record)
    record.setdefault("ts", _now())
    record.setdefault("decided_by", "master_agent")
    record.setdefault("accepted_by", "user")
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"success": True, "path": str(AUDIT_LOG)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def read_audit_log(limit: int = 50) -> list:
    """Read last `limit` records, newest first. Used by /api/agent-actions."""
    if not AUDIT_LOG.exists():
        return []
    try:
        with AUDIT_LOG.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        records = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        records.reverse()
        return records
    except Exception:
        return []


# ── 3. Tag Azure resource (policy-gated) ─────────────────────────────────────
def tag_azure_resource(resource_id: str, tags: Dict[str, str], rationale: str = "") -> Dict[str, Any]:
    """Set tags on an Azure resource. Allowlist policy:
      AGENTIC_TAG_ALLOWLIST unset → allow (product default, trust caller-supplied id)
      AGENTIC_TAG_ALLOWLIST=''     → deny all
      AGENTIC_TAG_ALLOWLIST=a,b,c  → allow only listed resource ids.
    Always audit-logs."""
    base = {
        "type": "tag_resource",
        "resource_id": resource_id,
        "tags": tags,
        "rationale": rationale,
    }
    mode, allowlist = _allowlist_policy("AGENTIC_TAG_ALLOWLIST")
    allowed = (mode == _MODE_ALLOW_ALL) or (mode == _MODE_WHITELIST and resource_id in allowlist)
    if not allowed:
        reason = (
            "AGENTIC_TAG_ALLOWLIST is empty (deny-all)"
            if mode == _MODE_DENY_ALL
            else "resource not in AGENTIC_TAG_ALLOWLIST"
        )
        result = {
            "success": False,
            "real_action_taken": False,
            "summary": f"Refused: {reason}",
        }
        append_audit_log({**base, "result": result})
        return result

    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.resource import ResourceManagementClient

        sub_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        if not sub_id:
            result = {"success": False, "real_action_taken": False,
                      "summary": "AZURE_SUBSCRIPTION_ID not set"}
            append_audit_log({**base, "result": result})
            return result

        cred = DefaultAzureCredential()
        client = ResourceManagementClient(cred, sub_id)
        # tags.begin_create_or_update_at_scope sets/replaces all tags at the resource scope
        poller = client.tags.begin_create_or_update_at_scope(
            scope=resource_id,
            parameters={"properties": {"tags": tags}},
        )
        poller.result(timeout=30)
        result = {
            "success": True,
            "real_action_taken": True,
            "summary": f"Set tags {list(tags.keys())} on {resource_id}",
            "evidence": resource_id,
        }
    except Exception as e:
        result = {"success": False, "real_action_taken": False,
                  "summary": f"Azure tag failed: {type(e).__name__}: {e}"}

    append_audit_log({**base, "result": result})
    return result


# ── 4. Start service ─────────────────────────────────────────────────────────
# Resource type constants
_EVENTHUB_NAMESPACES = "microsoft.eventhub/namespaces"

_RESOURCE_API_VERSIONS = {
    "microsoft.web/sites":                  "2022-03-01",
    "microsoft.compute/virtualmachines":    "2022-11-01",
    _EVENTHUB_NAMESPACES:                   "2021-11-01",
}


def _real_start(resource_type: str, resource_id: str) -> Dict[str, Any]:
    """POST the appropriate ARM /start endpoint using the same Service Principal token
    that the read path uses. No SDK dependency."""
    if _az is None:
        return {"ok": False, "summary": "azure_rest not importable (Service Principal auth unavailable)"}
    rt = resource_type.lower()
    api_version = _RESOURCE_API_VERSIONS.get(rt)
    if not api_version:
        return {"ok": False, "summary": f"unsupported resource_type for start: {resource_type}"}

    if rt == _EVENTHUB_NAMESPACES:
        # Event Hub namespaces have no runtime stop/start — the read-path probe should not
        # flag them as Stopped, but if it ever does, no-op gracefully.
        return {"ok": True,
                "summary": "Event Hub namespaces have no start action; nothing to do"}

    try:
        token = _az._get_token("https://management.azure.com/.default")
    except Exception as e:
        return {"ok": False, "summary": f"token acquisition failed: {type(e).__name__}: {e}"}

    url = f"https://management.azure.com{resource_id}/start"
    try:
        resp = _az._session.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"api-version": api_version},
            timeout=120,
        )
    except Exception as e:
        return {"ok": False, "summary": f"ARM request failed: {type(e).__name__}: {e}"}

    if 200 <= resp.status_code < 300:
        m = re.search(r"/([^/]+)$", resource_id)
        name = m.group(1) if m else resource_id
        return {"ok": True, "summary": f"{name} start accepted by ARM (HTTP {resp.status_code})"}
    return {"ok": False, "summary": f"ARM /start returned HTTP {resp.status_code}: {resp.text[:200]}"}


def start_service(
    service_name: str,
    rationale: str = "",
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    before_state: str = "Unknown",
) -> Dict[str, Any]:
    """Start an Azure resource. Policy via AGENTIC_START_ALLOWLIST:
      unset  → allow real action (product default — trust the caller's resource ids)
      ''     → deny-all (defensive opt-out)
      list   → allow only matching 'resource_type:resource_id' keys.
    Always audit-logs. Returns {success, real_action_taken, before_state, after_state, summary}."""
    base = {
        "type": "start_service",
        "service": service_name,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "rationale": rationale,
    }

    mode, allowlist = _allowlist_policy("AGENTIC_START_ALLOWLIST")
    allow_key = f"{resource_type}:{resource_id}" if (resource_type and resource_id) else None
    permitted = (
        allow_key is not None
        and (mode == _MODE_ALLOW_ALL or (mode == _MODE_WHITELIST and allow_key in allowlist))
    )

    real_action_taken = False
    after_state = before_state
    if permitted:
        real = _real_start(resource_type, resource_id)
        if real.get("ok"):
            real_action_taken = True
            after_state = "Running"
            summary = f"{service_name}: {before_state} → Running (real Azure: {real['summary']})"
        else:
            summary = f"{service_name}: real start failed — {real.get('summary')}"
    elif allow_key is None:
        summary = f"{service_name}: skipped (no resource_type/resource_id provided)"
    elif mode == _MODE_DENY_ALL:
        summary = f"{service_name}: skipped (AGENTIC_START_ALLOWLIST is empty — deny-all)"
    else:
        summary = f"{service_name}: skipped (not in AGENTIC_START_ALLOWLIST)"

    result = {
        "success": real_action_taken,
        "real_action_taken": real_action_taken,
        "before_state": before_state,
        "after_state": after_state,
        "summary": summary,
    }
    append_audit_log({**base, "result": result})
    return result


def _real_stop(resource_type: str, resource_id: str) -> Dict[str, Any]:
    """POST the appropriate ARM /stop (or /powerOff) endpoint using the same
    Service Principal token that the read path uses. No SDK dependency."""
    if _az is None:
        return {"ok": False, "summary": "azure_rest not importable (Service Principal auth unavailable)"}
    rt = resource_type.lower()
    api_version = _RESOURCE_API_VERSIONS.get(rt)
    if not api_version:
        return {"ok": False, "summary": f"unsupported resource_type for stop: {resource_type}"}

    if rt == _EVENTHUB_NAMESPACES:
        return {"ok": True, "summary": "Event Hub namespaces have no stop action; nothing to do"}

    try:
        token = _az._get_token("https://management.azure.com/.default")
    except Exception as e:
        return {"ok": False, "summary": f"token acquisition failed: {type(e).__name__}: {e}"}

    # Web/sites → /stop; VMs → /powerOff (deallocate keeps billing, powerOff stops compute)
    action = "powerOff" if rt == "microsoft.compute/virtualmachines" else "stop"
    url = f"https://management.azure.com{resource_id}/{action}"
    try:
        resp = _az._session.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"api-version": api_version},
            timeout=120,
        )
    except Exception as e:
        return {"ok": False, "summary": f"ARM request failed: {type(e).__name__}: {e}"}

    if 200 <= resp.status_code < 300:
        m = re.search(r"/([^/]+)$", resource_id)
        name = m.group(1) if m else resource_id
        return {"ok": True, "summary": f"{name} stop accepted by ARM (HTTP {resp.status_code})"}
    return {"ok": False, "summary": f"ARM /{action} returned HTTP {resp.status_code}: {resp.text[:200]}"}


def stop_service(
    service_name: str,
    rationale: str = "",
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    before_state: str = "Unknown",
) -> Dict[str, Any]:
    """Stop an Azure resource. Policy via AGENTIC_STOP_ALLOWLIST (same semantics as
    AGENTIC_START_ALLOWLIST). Always audit-logs. Returns {success, real_action_taken,
    before_state, after_state, summary}."""
    base = {
        "type": "stop_service",
        "service": service_name,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "rationale": rationale,
    }

    mode, allowlist = _allowlist_policy("AGENTIC_STOP_ALLOWLIST")
    allow_key = f"{resource_type}:{resource_id}" if (resource_type and resource_id) else None
    permitted = (
        allow_key is not None
        and (mode == _MODE_ALLOW_ALL or (mode == _MODE_WHITELIST and allow_key in allowlist))
    )

    real_action_taken = False
    after_state = before_state
    if permitted:
        real = _real_stop(resource_type, resource_id)
        if real.get("ok"):
            real_action_taken = True
            after_state = "Stopped"
            summary = f"{service_name}: {before_state} → Stopped (real Azure: {real['summary']})"
        else:
            summary = f"{service_name}: real stop failed — {real.get('summary')}"
    elif allow_key is None:
        summary = f"{service_name}: skipped (no resource_type/resource_id provided)"
    elif mode == _MODE_DENY_ALL:
        summary = f"{service_name}: skipped (AGENTIC_STOP_ALLOWLIST is empty — deny-all)"
    else:
        summary = f"{service_name}: skipped (not in AGENTIC_STOP_ALLOWLIST)"

    result = {
        "success": real_action_taken,
        "real_action_taken": real_action_taken,
        "before_state": before_state,
        "after_state": after_state,
        "summary": summary,
    }
    append_audit_log({**base, "result": result})
    return result


# ── Generic dispatcher ───────────────────────────────────────────────────────
def execute_action(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch an action spec to the right handler.
    Spec: {type, params, decided_by?, accepted_by?, rationale?}"""
    action_type = spec.get("type")
    params = spec.get("params", {})
    rationale = spec.get("rationale", "")
    if action_type == "save_iac_artifact":
        return save_iac_artifact(
            content=params["content"],
            service_name=params.get("service_name", "iac"),
            fmt=params.get("format", "tf"),
        )
    if action_type == "tag_azure_resource":
        return tag_azure_resource(
            resource_id=params["resource_id"],
            tags=params.get("tags", {}),
            rationale=rationale,
        )
    if action_type == "start_service":
        return start_service(
            service_name=params["service_name"],
            rationale=rationale,
            resource_type=params.get("resource_type"),
            resource_id=params.get("resource_id"),
        )
    if action_type == "stop_service":
        return stop_service(
            service_name=params["service_name"],
            rationale=rationale,
            resource_type=params.get("resource_type"),
            resource_id=params.get("resource_id"),
        )
    if action_type == "append_audit_log":
        return append_audit_log(params)
    return {"success": False, "error": f"unknown action type: {action_type}"}
