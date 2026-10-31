"""Telemetry validation tools for post-test analysis.

Cross-references LoadTestDevices (registered devices) with TELEMETRY_EVENTS
(ingested telemetry) to answer:
  - Were all expected devices registered?
  - Did all devices send telemetry?
  - Which devices are missing data?
  - Which devices reported errors?
  - Did all devices send a uniform number of messages?
"""
import logging
import os
import re

from google.adk.tools import FunctionTool

from agentic_utils.tools.db_registration import _get_db_connection

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
_DEVICES_TABLE   = os.getenv("LOAD_TEST_DEVICES_TABLE", "LoadTestDevices")
_TELEMETRY_TABLE = os.getenv("TELEMETRY_EVENTS_TABLE",  "TELEMETRY_EVENTS")

for _t in (_DEVICES_TABLE, _TELEMETRY_TABLE):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', _t):
        raise ValueError(f"Invalid table name: {_t!r}")

# Devices whose message count deviates more than this from the average are outliers
_OUTLIER_THRESHOLD_PCT = 0.20


# ── Session name resolution ──────────────────────────────────────────────────

def _resolve_session_name(raw: str) -> str:
    """Resolve a partial or mis-typed session name to the full name in the DB.

    Steps:
      1. Strip whitespace.
      2. Exact match in TELEMETRY_EVENTS (most recent).
      3. Exact match in LoadTestDevices.
      4. LIKE '%fragment%' in TELEMETRY_EVENTS (most recent).
      5. LIKE '%fragment%' in LoadTestDevices.
      6. Fall back to the stripped input unchanged.
    """
    name = (raw or "").strip()
    if not name:
        return name

    conn = None
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        try:
            for sql, params in [
                # exact — telemetry
                (f"SELECT TOP 1 test_session_name FROM {_TELEMETRY_TABLE} "
                 f"WHERE test_session_name = ? ORDER BY logged_at DESC", (name,)),
                # exact — devices
                (f"SELECT TOP 1 test_run_name FROM {_DEVICES_TABLE} "
                 f"WHERE test_run_name = ? ORDER BY timestamp DESC", (name,)),
                # partial — telemetry
                (f"SELECT TOP 1 test_session_name FROM {_TELEMETRY_TABLE} "
                 f"WHERE test_session_name LIKE ? ORDER BY logged_at DESC", (f"%{name}%",)),
                # partial — devices
                (f"SELECT TOP 1 test_run_name FROM {_DEVICES_TABLE} "
                 f"WHERE test_run_name LIKE ? ORDER BY timestamp DESC", (f"%{name}%",)),
            ]:
                cursor.execute(sql, params)
                row = cursor.fetchone()
                if row and row[0]:
                    return row[0]
        finally:
            cursor.close()
    except Exception as exc:
        logger.debug("_resolve_session_name lookup failed: %s", exc)
    finally:
        if conn:
            conn.close()

    return name  # Fall back to stripped input


# ── Report formatter ─────────────────────────────────────────────────────────

def _format_validation_report(session: str, results: dict) -> str:
    """Format the validation results dict as a human-readable text report."""
    overall = results.get("overall_status", "")

    lines = [f"Validation Report — {session}", "─" * 50, ""]

    # ── Registration ──────────────────────────────────────────────────────────
    reg = results.get("registration", {})
    if reg.get("status") == "error":
        lines.append(f"  Device Registration   ERROR: {reg.get('error', 'unknown')}")
    else:
        count = reg.get("registered_count", 0)
        lines.append(f"  Device Registration   {count} registered   {'✓' if count > 0 else '✗'}")

    # ── Coverage ──────────────────────────────────────────────────────────────
    cov = results.get("coverage", {})
    if cov.get("status") == "error":
        lines.append(f"  Telemetry Coverage    ERROR: {cov.get('error', 'unknown')}")
    elif cov.get("status") == "no_devices":
        lines.append(f"  Telemetry Coverage    No devices registered")
    else:
        w = cov.get("devices_with_telemetry", 0)
        tot = cov.get("total_registered", 0)
        pct = cov.get("coverage_pct", 0.0)
        lines.append(f"  Telemetry Coverage    {w} / {tot} ({pct}%)   {'✓' if w == tot else '✗'}")

    # ── Errors ────────────────────────────────────────────────────────────────
    err = results.get("errors", {})
    if err.get("status") == "error":
        lines.append(f"  Error Devices         ERROR: {err.get('error', 'unknown')}")
    else:
        ec = err.get("error_device_count", 0)
        lines.append(f"  Error Devices         {ec}   {'✓' if ec == 0 else '✗'}")

    # ── Uniformity ────────────────────────────────────────────────────────────
    uni = results.get("uniformity", {})
    if uni.get("status") == "error":
        lines.append(f"  Message Uniformity    ERROR: {uni.get('error', 'unknown')}")
    elif uni.get("status") == "no_data":
        lines.append(f"  Message Uniformity    No telemetry data")
    else:
        is_unif = uni.get("is_uniform", False)
        avg = uni.get("avg_messages", 0)
        oc = uni.get("outlier_count", 0)
        if is_unif:
            lines.append(f"  Message Uniformity    Uniform ({avg:.0f} msgs/device)   ✓")
        else:
            lines.append(f"  Message Uniformity    {oc} outlier(s), avg {avg:.1f} msgs   ✗")

    lines.append("")

    # ── Missing devices detail ────────────────────────────────────────────────
    miss = results.get("missing", {})
    if miss.get("status") != "error" and miss.get("missing_count", 0) > 0:
        missing_list = miss.get("missing_devices", [])
        total_miss = miss.get("missing_count", 0)
        lines.append(f"  Missing Devices ({total_miss}):")
        shown = missing_list[:10]
        lines.append("    " + ", ".join(shown))
        if total_miss > 10:
            lines.append(f"    … and {total_miss - 10} more")
        lines.append("")

    # ── Error devices detail ──────────────────────────────────────────────────
    if err.get("status") != "error" and err.get("error_device_count", 0) > 0:
        error_devs = err.get("error_devices", [])
        lines.append(f"  Error Devices ({err['error_device_count']}):")
        for d in error_devs[:5]:
            lines.append(f"    • {d['device_id']} — {d['error_count']} error(s): {d['sample_error']}")
        if len(error_devs) > 5:
            lines.append(f"    … and {len(error_devs) - 5} more")
        lines.append("")

    # ── Outlier detail ────────────────────────────────────────────────────────
    if uni.get("status") not in ("error", "no_data") and uni.get("outlier_count", 0) > 0:
        outlier_list = uni.get("outliers", [])
        lines.append(f"  Message Count Outliers:")
        for o in outlier_list[:5]:
            lines.append(f"    • {o['device_id']} — {o['msg_count']} msgs ({o['delta_pct']:+.1f}% from avg)")
        if len(outlier_list) > 5:
            lines.append(f"    … and {len(outlier_list) - 5} more")
        lines.append("")

    # ── Overall ───────────────────────────────────────────────────────────────
    lines.append("─" * 50)
    lines.append(f"  Overall: {'HEALTHY ✓' if overall == 'healthy' else 'ISSUES FOUND ✗'}")

    return "\n".join(lines)


# ── Tool functions ───────────────────────────────────────────────────────────

def check_device_registration(test_session_name: str) -> dict:
    """Count how many devices were registered for a test session.

    Args:
        test_session_name: The test session name (full or partial — auto-resolved).

    Returns:
        dict with keys: test_session_name, registered_count, status
    """
    test_session_name = _resolve_session_name(test_session_name)
    conn = None
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT COUNT(*) FROM {_DEVICES_TABLE} WHERE test_run_name = ?",
                (test_session_name,),
            )
            count = cursor.fetchone()[0]
        finally:
            cursor.close()

        return {
            "test_session_name": test_session_name,
            "registered_count": count,
            "status": "ok" if count > 0 else "no_devices",
        }
    except Exception as exc:
        logger.warning("check_device_registration failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if conn:
            conn.close()


def check_telemetry_coverage(test_session_name: str) -> dict:
    """How many registered devices have at least one telemetry row.

    Args:
        test_session_name: The test session name (full or partial — auto-resolved).

    Returns:
        dict with keys: test_session_name, total_registered, devices_with_telemetry,
        devices_without_telemetry, coverage_pct, status
    """
    test_session_name = _resolve_session_name(test_session_name)
    conn = None
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                SELECT
                    d.device_id,
                    CASE WHEN t.device_id IS NOT NULL THEN 1 ELSE 0 END AS has_telemetry
                FROM {_DEVICES_TABLE} d
                LEFT JOIN (
                    SELECT DISTINCT device_id
                    FROM {_TELEMETRY_TABLE}
                    WHERE test_session_name = ?
                ) t ON d.device_id = t.device_id
                WHERE d.test_run_name = ?
                """,
                (test_session_name, test_session_name),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()

        if not rows:
            return {"test_session_name": test_session_name, "status": "no_devices",
                    "total_registered": 0, "devices_with_telemetry": 0,
                    "devices_without_telemetry": 0, "coverage_pct": 0.0}

        total = len(rows)
        with_telemetry = sum(1 for r in rows if r[1] == 1)
        without_telemetry = total - with_telemetry
        coverage_pct = round(with_telemetry / total * 100, 1)

        return {
            "test_session_name": test_session_name,
            "total_registered": total,
            "devices_with_telemetry": with_telemetry,
            "devices_without_telemetry": without_telemetry,
            "coverage_pct": coverage_pct,
            "status": "ok",
        }
    except Exception as exc:
        logger.warning("check_telemetry_coverage failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if conn:
            conn.close()


def get_missing_devices(test_session_name: str) -> dict:
    """List device IDs that were registered but sent zero telemetry.

    Args:
        test_session_name: The test session name (full or partial — auto-resolved).

    Returns:
        dict with keys: test_session_name, missing_count, missing_devices (list), status
    """
    test_session_name = _resolve_session_name(test_session_name)
    conn = None
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                SELECT d.device_id
                FROM {_DEVICES_TABLE} d
                WHERE d.test_run_name = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM {_TELEMETRY_TABLE} t
                      WHERE t.test_session_name = ?
                        AND t.device_id = d.device_id
                  )
                ORDER BY d.device_id
                """,
                (test_session_name, test_session_name),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()

        missing = [r[0] for r in rows]
        return {
            "test_session_name": test_session_name,
            "missing_count": len(missing),
            "missing_devices": missing,
            "status": "ok",
        }
    except Exception as exc:
        logger.warning("get_missing_devices failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if conn:
            conn.close()


def get_error_devices(test_session_name: str) -> dict:
    """Devices that have telemetry rows where error IS NOT NULL.

    Args:
        test_session_name: The test session name (full or partial — auto-resolved).

    Returns:
        dict with keys: test_session_name, error_device_count, error_devices (list), status.
        Each entry in error_devices: { device_id, error_count, sample_error, affected_steps }
    """
    test_session_name = _resolve_session_name(test_session_name)
    conn = None
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                SELECT
                    device_id,
                    COUNT(*)                    AS error_count,
                    MIN(error)                  AS sample_error,
                    COUNT(DISTINCT test_step)   AS affected_steps
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                  AND error IS NOT NULL
                GROUP BY device_id
                ORDER BY error_count DESC
                """,
                (test_session_name,),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()

        error_devices = [
            {
                "device_id": r[0],
                "error_count": r[1],
                "sample_error": r[2],
                "affected_steps": r[3],
            }
            for r in rows
        ]
        return {
            "test_session_name": test_session_name,
            "error_device_count": len(error_devices),
            "error_devices": error_devices,
            "status": "ok",
        }
    except Exception as exc:
        logger.warning("get_error_devices failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if conn:
            conn.close()


def check_message_count_uniformity(test_session_name: str) -> dict:
    """Check if all devices sent a uniform number of messages.

    Devices whose message count deviates >20% from the average are flagged as outliers.

    Args:
        test_session_name: The test session name (full or partial — auto-resolved).

    Returns:
        dict with keys: test_session_name, device_count, min_messages, max_messages,
        avg_messages, is_uniform, outlier_count, outliers (list), status.
        Each outlier entry: { device_id, msg_count, delta_pct }
    """
    test_session_name = _resolve_session_name(test_session_name)
    conn = None
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                SELECT device_id, COUNT(*) AS msg_count
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                GROUP BY device_id
                ORDER BY msg_count DESC
                """,
                (test_session_name,),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()

        if not rows:
            return {
                "test_session_name": test_session_name,
                "status": "no_data",
                "device_count": 0,
            }

        counts = [r[1] for r in rows]
        avg = sum(counts) / len(counts)

        outliers = [
            {
                "device_id": r[0],
                "msg_count": r[1],
                "delta_pct": round((r[1] - avg) / avg * 100, 1),
            }
            for r in rows
            if abs(r[1] - avg) / avg > _OUTLIER_THRESHOLD_PCT
        ]

        return {
            "test_session_name": test_session_name,
            "device_count": len(rows),
            "min_messages": min(counts),
            "max_messages": max(counts),
            "avg_messages": round(avg, 1),
            "is_uniform": len(outliers) == 0,
            "outlier_count": len(outliers),
            "outliers": outliers,
            "status": "ok",
        }
    except Exception as exc:
        logger.warning("check_message_count_uniformity failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if conn:
            conn.close()


def validate_test_run(test_session_name: str) -> str:
    """Comprehensive validation report for a completed test run.

    Combines all five checks: device registration, telemetry coverage, missing devices,
    error devices, and message count uniformity. Accepts full or partial session names
    (e.g. '100d-5m-202606091515' resolves to 'load-100d-5m-202606091515').
    Each check runs independently — a failure in one does not abort the others.

    Args:
        test_session_name: The test session name (full or partial, extra spaces trimmed).

    Returns:
        Human-readable validation report string.
    """
    resolved = _resolve_session_name(test_session_name)

    checks = {
        "registration": lambda: check_device_registration(resolved),
        "coverage":     lambda: check_telemetry_coverage(resolved),
        "missing":      lambda: get_missing_devices(resolved),
        "errors":       lambda: get_error_devices(resolved),
        "uniformity":   lambda: check_message_count_uniformity(resolved),
    }

    results: dict = {}
    for name, fn in checks.items():
        try:
            results[name] = fn()
        except Exception as exc:
            results[name] = {"status": "error", "error": str(exc)}

    healthy_statuses = {"ok", "no_devices", "no_data"}
    all_ok = all(r.get("status") in healthy_statuses for r in results.values())
    results["overall_status"] = "healthy" if all_ok else "issues_found"

    return _format_validation_report(resolved, results)


# ── FunctionTool exports ─────────────────────────────────────────────────────

check_device_registration_tool      = FunctionTool(func=check_device_registration)
check_telemetry_coverage_tool       = FunctionTool(func=check_telemetry_coverage)
get_missing_devices_tool            = FunctionTool(func=get_missing_devices)
get_error_devices_tool              = FunctionTool(func=get_error_devices)
check_message_count_uniformity_tool = FunctionTool(func=check_message_count_uniformity)
validate_test_run_tool              = FunctionTool(func=validate_test_run)

ALL_VALIDATION_TOOLS = [
    check_device_registration_tool,
    check_telemetry_coverage_tool,
    get_missing_devices_tool,
    get_error_devices_tool,
    check_message_count_uniformity_tool,
    validate_test_run_tool,
]
