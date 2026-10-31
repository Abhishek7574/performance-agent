"""Database insights for load test sessions.

Rich visual reports using Unicode box-drawing / block characters (no extra deps).
Each data function returns a structured dict usable by the LLM; the combined
get_session_insights() renders all six into one formatted terminal report.

Insights:
  1. get_step_funnel       – per-step success / error rates   (bar chart)
  2. get_top_errors        – ranked error patterns             (ranked table)
  3. get_message_gaps      – msg_no sequence integrity         (gap table)
  4. get_model_health      – doppel model coverage + err rate  (scorecard grid)
  5. get_run_comparison    – cross-run regression view         (delta table)
  6. get_direction_balance – IN / OUT direction anomalies      (balance table)
  7. get_session_insights  – all-in-one combined report
"""

import logging
import os
import re

from google.adk.tools import FunctionTool

from agentic_utils.tools.db_registration import _get_db_connection
from agentic_utils.tools.telemetry_validation_tools import _resolve_session_name

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_DEVICES_TABLE   = os.getenv("LOAD_TEST_DEVICES_TABLE",  "LoadTestDevices")
_TELEMETRY_TABLE = os.getenv("TELEMETRY_EVENTS_TABLE",   "TELEMETRY_EVENTS")
_SESSIONS_TABLE  = os.getenv("LOAD_TEST_SESSIONS_TABLE", "LoadTestSessions")

for _t in (_DEVICES_TABLE, _TELEMETRY_TABLE, _SESSIONS_TABLE):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', _t):
        raise ValueError(f"Invalid table name: {_t!r}")

# ── Visual helpers ────────────────────────────────────────────────────────────

_BAR_WIDTH = 22


def _bar(pct: float) -> str:
    """22-char Unicode block bar for a 0–100 percentage."""
    filled = round(pct / 100 * _BAR_WIDTH)
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _badge(pct: float) -> str:
    if pct >= 95:
        return "✓ PASS"
    if pct >= 80:
        return "⚠ WARN"
    return "✗ FAIL"


def _section(title: str, width: int = 66) -> str:
    pad = width - len(title) - 6
    return f"\n━━━  {title}  {'━' * max(pad, 2)}\n"


def _rule(width: int = 64) -> str:
    return "─" * width


def _trunc(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _fmt_duration(secs) -> str:
    if secs is None:
        return "—"
    m, s = divmod(int(secs), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _delta_str(curr, base, *, higher_is_worse: bool = True, is_pct: bool = False) -> str:
    """Return a compact delta label like '↑12✗' or '↓0.30%✓'."""
    if base == 0:
        return "—"
    diff = curr - base
    if diff == 0:
        return "—"
    arrow  = "↑" if diff > 0 else "↓"
    marker = ("✗" if diff > 0 else "✓") if higher_is_worse else ("✓" if diff > 0 else "✗")
    val    = f"{abs(diff):.2f}%" if is_pct else f"{abs(diff):,}"
    return f"{arrow}{val}{marker}"


# ── 1. Step Funnel ────────────────────────────────────────────────────────────

def get_step_funnel(test_session_name: str, *, _conn=None) -> dict:
    """Per-step success and error rates for a test session.

    Args:
        test_session_name: Full or partial session name (auto-resolved).
        _conn: Optional shared pyodbc connection (caller manages lifecycle).

    Returns:
        dict with keys: test_session_name, steps (list), status.
        Each step: { test_step, total, success_count, error_count, success_pct }
    """
    test_session_name = _resolve_session_name(test_session_name)
    _own_conn = _conn is None
    conn = None
    try:
        conn = _conn if _conn is not None else _get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT
                    test_step,
                    COUNT(*)                                             AS total,
                    SUM(CASE WHEN error IS NULL     THEN 1 ELSE 0 END)  AS success_count,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)  AS error_count
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                  AND test_step IS NOT NULL
                GROUP BY test_step
                ORDER BY total DESC
                """,
                (test_session_name,),
            )
            rows = cur.fetchall()
        finally:
            cur.close()

        steps = [
            {
                "test_step":     r[0],
                "total":         r[1],
                "success_count": r[2],
                "error_count":   r[3],
                "success_pct":   round(r[2] / r[1] * 100, 1) if r[1] else 0.0,
            }
            for r in rows
        ]
        return {"test_session_name": test_session_name, "steps": steps, "status": "ok"}
    except Exception as exc:
        logger.warning("get_step_funnel failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if _own_conn and conn:
            conn.close()


def _fmt_step_funnel(data: dict) -> str:
    if data.get("status") == "error":
        return f"  ERROR: {data.get('error')}\n"

    steps = data.get("steps", [])
    if not steps:
        return "  No test-step data found for this session.\n"

    col = max(max(len(s["test_step"]) for s in steps), 16)
    hdr = f"  {'Step':<{col}}  {'Total':>8}  {'Errors':>7}  {'':>{_BAR_WIDTH}}  Success"
    lines = [hdr, "  " + _rule(len(hdr) - 2)]
    for s in sorted(steps, key=lambda x: x["success_pct"]):
        lines.append(
            f"  {s['test_step']:<{col}}"
            f"  {s['total']:>8,}"
            f"  {s['error_count']:>7,}"
            f"  {_bar(s['success_pct'])}"
            f"  {s['success_pct']:5.1f}%  {_badge(s['success_pct'])}"
        )
    return "\n".join(lines) + "\n"


# ── 2. Top Errors ─────────────────────────────────────────────────────────────

def get_top_errors(test_session_name: str, top_n: int = 10, *, _conn=None) -> dict:
    """Ranked error patterns – most frequent errors across all test steps.

    Args:
        test_session_name: Full or partial session name (auto-resolved).
        top_n: Max number of error entries to return (1–50, default 10).
        _conn: Optional shared pyodbc connection (caller manages lifecycle).

    Returns:
        dict with keys: test_session_name, total_error_events, total_affected_devices,
        top_errors (list), status.
        Each entry: { rank, error, test_step, occurrences, affected_devices }
    """
    top_n = max(1, min(int(top_n), 50))
    test_session_name = _resolve_session_name(test_session_name)
    _own_conn = _conn is None
    conn = None
    try:
        conn = _conn if _conn is not None else _get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT TOP ({top_n})
                    error,
                    test_step,
                    COUNT(*)                  AS occurrences,
                    COUNT(DISTINCT device_id) AS affected_devices
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                  AND error IS NOT NULL
                GROUP BY error, test_step
                ORDER BY occurrences DESC
                """,
                (test_session_name,),
            )
            rows = cur.fetchall()

            cur.execute(
                f"""
                SELECT
                    COUNT(*)                  AS total_error_events,
                    COUNT(DISTINCT device_id) AS affected_devices
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                  AND error IS NOT NULL
                """,
                (test_session_name,),
            )
            summary = cur.fetchone()
        finally:
            cur.close()

        top_errors = [
            {
                "rank":             i + 1,
                "error":            r[0],
                "test_step":        r[1],
                "occurrences":      r[2],
                "affected_devices": r[3],
            }
            for i, r in enumerate(rows)
        ]
        return {
            "test_session_name":      test_session_name,
            "total_error_events":     summary[0] if summary else 0,
            "total_affected_devices": summary[1] if summary else 0,
            "top_errors":             top_errors,
            "status":                 "ok",
        }
    except Exception as exc:
        logger.warning("get_top_errors failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if _own_conn and conn:
            conn.close()


def _fmt_top_errors(data: dict) -> str:
    if data.get("status") == "error":
        return f"  ERROR: {data.get('error')}\n"

    errors  = data.get("top_errors", [])
    total   = data.get("total_error_events", 0)
    affected = data.get("total_affected_devices", 0)

    if not errors:
        return "  No errors recorded   ✓\n"

    lines = [f"  Total error events: {total:,}  ·  Affected devices: {affected:,}"]
    lines.append("")
    hdr = f"  {'#':>3}  {'Count':>7}  {'Devices':>7}  {'Step':<22}  Error"
    lines.append(hdr)
    lines.append("  " + _rule(len(hdr) - 2))
    for e in errors:
        step = _trunc(e["test_step"] or "—", 22)
        err  = _trunc(e["error"]     or "",  52)
        lines.append(
            f"  {e['rank']:>3}  {e['occurrences']:>7,}  {e['affected_devices']:>7}  {step:<22}  {err}"
        )
    return "\n".join(lines) + "\n"


# ── 3. Message Gaps ───────────────────────────────────────────────────────────

def get_message_gaps(test_session_name: str, *, _conn=None) -> dict:
    """Detect devices with gaps in their msg_no sequence.

    A gap means some message numbers in [min, max] were never received.

    Args:
        test_session_name: Full or partial session name (auto-resolved).
        _conn: Optional shared pyodbc connection (caller manages lifecycle).

    Returns:
        dict with keys: test_session_name, devices_with_gaps, total_gaps,
        gap_devices (list), status.
        Each entry: { device_id, first_msg, last_msg, received, expected, gap_count }
    """
    test_session_name = _resolve_session_name(test_session_name)
    _own_conn = _conn is None
    conn = None
    try:
        conn = _conn if _conn is not None else _get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT
                    device_id,
                    MIN(msg_no)                              AS first_msg,
                    MAX(msg_no)                              AS last_msg,
                    COUNT(*)                                 AS received,
                    MAX(msg_no) - MIN(msg_no) + 1            AS expected,
                    MAX(msg_no) - MIN(msg_no) + 1 - COUNT(*) AS gap_count
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                  AND msg_no IS NOT NULL
                GROUP BY device_id
                HAVING MAX(msg_no) - MIN(msg_no) + 1 > COUNT(*)
                ORDER BY gap_count DESC
                """,
                (test_session_name,),
            )
            rows = cur.fetchall()
        finally:
            cur.close()

        gap_devices = [
            {
                "device_id": r[0],
                "first_msg": r[1],
                "last_msg":  r[2],
                "received":  r[3],
                "expected":  r[4],
                "gap_count": r[5],
            }
            for r in rows
        ]
        return {
            "test_session_name": test_session_name,
            "devices_with_gaps": len(gap_devices),
            "total_gaps":        sum(d["gap_count"] for d in gap_devices),
            "gap_devices":       gap_devices,
            "status":            "ok",
        }
    except Exception as exc:
        logger.warning("get_message_gaps failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if _own_conn and conn:
            conn.close()


def _fmt_message_gaps(data: dict) -> str:
    if data.get("status") == "error":
        return f"  ERROR: {data.get('error')}\n"

    devices = data.get("gap_devices", [])
    n       = data.get("devices_with_gaps", 0)
    total   = data.get("total_gaps", 0)

    if not devices:
        return "  All devices have continuous msg_no sequences   ✓\n"

    lines = [f"  {n} device(s) with gaps  ·  {total:,} total missing messages   ✗", ""]
    hdr = f"  {'Device':<36}  {'Received':>9}  {'Expected':>9}  {'Gaps':>7}"
    lines.append(hdr)
    lines.append("  " + _rule(len(hdr) - 2))
    for d in devices[:20]:
        lines.append(
            f"  {d['device_id']:<36}"
            f"  {d['received']:>9,}"
            f"  {d['expected']:>9,}"
            f"  {d['gap_count']:>7,}"
        )
    if n > 20:
        lines.append(f"  … and {n - 20} more device(s)")
    return "\n".join(lines) + "\n"


# ── 4. Model Health ───────────────────────────────────────────────────────────

def get_model_health(test_session_name: str, *, _conn=None) -> dict:
    """Doppel model health matrix – coverage and error rate per device model.

    Joins LoadTestDevices (registered) with TELEMETRY_EVENTS to compute
    per-model coverage % and error rate.

    Args:
        test_session_name: Full or partial session name (auto-resolved).
        _conn: Optional shared pyodbc connection (caller manages lifecycle).

    Returns:
        dict with keys: test_session_name, models (list), status.
        Each model: { doppel_model, registered, with_telemetry, coverage_pct,
                      total_msgs, total_errors, error_rate_pct }
    """
    test_session_name = _resolve_session_name(test_session_name)
    _own_conn = _conn is None
    conn = None
    try:
        conn = _conn if _conn is not None else _get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT
                    d.doppel_model,
                    COUNT(DISTINCT d.device_id)                                      AS registered,
                    COUNT(DISTINCT t.device_id)                                      AS with_telemetry,
                    ISNULL(SUM(CASE WHEN t.error IS NOT NULL THEN 1 ELSE 0 END), 0)  AS total_errors,
                    ISNULL(COUNT(t.id), 0)                                           AS total_msgs
                FROM {_DEVICES_TABLE} d
                LEFT JOIN {_TELEMETRY_TABLE} t
                    ON  d.device_id         = t.device_id
                    AND t.test_session_name = ?
                WHERE d.test_run_name = ?
                GROUP BY d.doppel_model
                ORDER BY registered DESC
                """,
                (test_session_name, test_session_name),
            )
            rows = cur.fetchall()
        finally:
            cur.close()

        models = []
        for r in rows:
            reg, w_tel, errs, msgs = r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0
            models.append({
                "doppel_model":   r[0],
                "registered":     reg,
                "with_telemetry": w_tel,
                "coverage_pct":   round(w_tel / reg * 100, 1) if reg else 0.0,
                "total_msgs":     msgs,
                "total_errors":   errs,
                "error_rate_pct": round(errs / msgs * 100, 2) if msgs else 0.0,
            })
        return {"test_session_name": test_session_name, "models": models, "status": "ok"}
    except Exception as exc:
        logger.warning("get_model_health failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if _own_conn and conn:
            conn.close()


def _fmt_model_health(data: dict) -> str:
    if data.get("status") == "error":
        return f"  ERROR: {data.get('error')}\n"

    models = data.get("models", [])
    if not models:
        return "  No model data (LoadTestDevices may be empty for this session).\n"

    col = max(max(len(m["doppel_model"]) for m in models), 14)
    hdr = (
        f"  {'Model':<{col}}"
        f"  {'Reg':>5}  {'Telemetry':>9}  {'Coverage':>9}"
        f"  {'Messages':>10}  {'Errors':>8}  {'Err%':>6}  Status"
    )
    lines = [hdr, "  " + _rule(len(hdr) - 2)]
    for m in models:
        lines.append(
            f"  {m['doppel_model']:<{col}}"
            f"  {m['registered']:>5}"
            f"  {m['with_telemetry']:>9}"
            f"  {m['coverage_pct']:>8.1f}%"
            f"  {m['total_msgs']:>10,}"
            f"  {m['total_errors']:>8,}"
            f"  {m['error_rate_pct']:>5.2f}%"
            f"  {_badge(m['coverage_pct'])}"
        )
    return "\n".join(lines) + "\n"


# ── 5. Run Comparison ─────────────────────────────────────────────────────────

def get_run_comparison(test_session_name: str, *, _conn=None) -> dict:
    """Compare sub-runs (test_session_run_no) within a test session.

    Shows devices active, total messages, error rate, and duration per run_no
    with delta markers relative to the first run.

    Args:
        test_session_name: Full or partial session name (auto-resolved).
        _conn: Optional shared pyodbc connection (caller manages lifecycle).

    Returns:
        dict with keys: test_session_name, runs (list), status.
        Each run: { test_session_run_no, device_count, total_msgs, error_count,
                    error_rate_pct, test_start, test_end, duration_sec }
    """
    test_session_name = _resolve_session_name(test_session_name)
    _own_conn = _conn is None
    conn = None
    try:
        conn = _conn if _conn is not None else _get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT
                    test_session_run_no,
                    COUNT(DISTINCT device_id)                                             AS device_count,
                    COUNT(*)                                                              AS total_msgs,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)                   AS error_count,
                    CAST(
                        SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) * 100.0
                        / NULLIF(COUNT(*), 0)
                    AS DECIMAL(6,3))                                                      AS error_rate_pct,
                    MIN(logged_at)                                                        AS test_start,
                    MAX(logged_at)                                                        AS test_end,
                    DATEDIFF(SECOND, MIN(logged_at), MAX(logged_at))                     AS duration_sec
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                GROUP BY test_session_run_no
                ORDER BY test_session_run_no
                """,
                (test_session_name,),
            )
            rows = cur.fetchall()
        finally:
            cur.close()

        runs = [
            {
                "test_session_run_no": r[0],
                "device_count":        r[1],
                "total_msgs":          r[2],
                "error_count":         r[3],
                "error_rate_pct":      float(r[4]) if r[4] is not None else 0.0,
                "test_start":          str(r[5]) if r[5] else None,
                "test_end":            str(r[6]) if r[6] else None,
                "duration_sec":        r[7],
            }
            for r in rows
        ]
        return {"test_session_name": test_session_name, "runs": runs, "status": "ok"}
    except Exception as exc:
        logger.warning("get_run_comparison failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if _own_conn and conn:
            conn.close()


def _fmt_run_comparison(data: dict) -> str:
    if data.get("status") == "error":
        return f"  ERROR: {data.get('error')}\n"

    runs = data.get("runs", [])
    if not runs:
        return "  No run data found.\n"
    if len(runs) == 1:
        r = runs[0]
        return (
            f"  Single run (run_no={r['test_session_run_no']})"
            f"  ·  {r['device_count']:,} devices"
            f"  ·  {r['total_msgs']:,} msgs"
            f"  ·  {r['error_rate_pct']:.2f}% err"
            f"  ·  {_fmt_duration(r['duration_sec'])}\n"
        )

    base = runs[0]
    hdr  = (
        f"  {'Run':>4}"
        f"  {'Devices':>8}"
        f"  {'Messages':>10}"
        f"  {'Errors':>8}"
        f"  {'Err%':>7}"
        f"  {'Duration':>9}"
        f"  vs Run-1"
    )
    lines = [hdr, "  " + _rule(len(hdr) - 2)]
    for r in runs:
        is_base = r["test_session_run_no"] == base["test_session_run_no"]
        if is_base:
            note = "← baseline"
        else:
            note = _delta_str(
                r["error_rate_pct"], base["error_rate_pct"],
                higher_is_worse=True, is_pct=True,
            )
        lines.append(
            f"  {str(r['test_session_run_no'] or '—'):>4}"
            f"  {r['device_count']:>8,}"
            f"  {r['total_msgs']:>10,}"
            f"  {r['error_count']:>8,}"
            f"  {r['error_rate_pct']:>6.2f}%"
            f"  {_fmt_duration(r['duration_sec']):>9}"
            f"  {note}"
        )
    return "\n".join(lines) + "\n"


# ── 5b. Cross-Session Comparison ─────────────────────────────────────────────

def compare_sessions(session_name_1: str, session_name_2: str) -> str:
    """Compare aggregate metrics between two different test sessions side-by-side.

    Args:
        session_name_1: Full or partial name of the first session (auto-resolved).
        session_name_2: Full or partial name of the second session (auto-resolved).

    Returns:
        Human-readable formatted comparison report string, beginning with the
        'SESSION COMPARISON' header that the UI renderer detects.
    """
    s1 = _resolve_session_name(session_name_1)
    s2 = _resolve_session_name(session_name_2)

    conn = None
    try:
        conn = _get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT
                    test_session_name,
                    COUNT(DISTINCT test_session_run_no)                                AS run_count,
                    COUNT(DISTINCT device_id)                                          AS devices_active,
                    COUNT(*)                                                           AS total_msgs,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)                AS error_count,
                    CAST(
                        SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) * 100.0
                        / NULLIF(COUNT(*), 0)
                    AS DECIMAL(6,3))                                                   AS error_rate_pct,
                    MIN(logged_at)                                                     AS test_start,
                    MAX(logged_at)                                                     AS test_end,
                    DATEDIFF(SECOND, MIN(logged_at), MAX(logged_at))                  AS duration_sec
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name IN (?, ?)
                GROUP BY test_session_name
                """,
                (s1, s2),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
    except Exception as exc:
        logger.warning("compare_sessions failed: %s", exc)
        return f"  ERROR: {exc}\n"
    finally:
        if conn:
            conn.close()

    # Fetch planned + actual duration from LoadTestSessions (graceful: table may not exist yet)
    sess_durations: dict = {}
    try:
        _conn2 = _get_db_connection()
        try:
            _sc = _conn2.cursor()
            try:
                _sc.execute(
                    f"""
                    SELECT session_name, total_duration_secs, completed_duration_secs
                    FROM {_SESSIONS_TABLE}
                    WHERE session_name IN (?, ?)
                    """,
                    (s1, s2),
                )
                sess_durations = {r[0]: {"planned": r[1], "actual": r[2]} for r in _sc.fetchall()}
            finally:
                _sc.close()
        finally:
            _conn2.close()
    except Exception as _sess_exc:
        logger.debug("LoadTestSessions query skipped (table may not exist yet): %s", _sess_exc)

    # Map results by session name
    raw = {r[0]: r for r in rows}

    def _extract(name):
        r = raw.get(name)
        if r is None:
            return None
        return {
            "run_count":      int(r[1]),
            "devices_active": int(r[2]),
            "total_msgs":     int(r[3]),
            "error_count":    int(r[4]),
            "error_rate_pct": float(r[5]) if r[5] is not None else 0.0,
            "duration_sec":   r[8],
        }

    d1 = _extract(s1)
    d2 = _extract(s2)

    if d1 is None and d2 is None:
        return (
            f"  SESSION COMPARISON  ·  {s1}  vs  {s2}\n\n"
            f"  No data found for either session.\n"
        )

    def _short(n):
        return n.replace("load-", "", 1)

    def _v(d, key, fmt=None):
        if d is None:
            return "not found"
        v = d.get(key)
        if v is None:
            return "N/A"
        return fmt(v) if fmt else str(v)

    def _avg(d):
        if d is None:
            return None
        return round(d["total_msgs"] / d["devices_active"], 2) if d["devices_active"] else 0.0

    def _delta(v1, v2, higher_is_worse=False):
        if v1 is None or v2 is None:
            return "—"
        try:
            diff = float(v2) - float(v1)
        except (TypeError, ValueError):
            return "—"
        if diff == 0:
            return "="
        arrow  = "↑" if diff > 0 else "↓"
        marker = ("✗" if diff > 0 else "✓") if higher_is_worse else ("✓" if diff > 0 else "✗")
        label  = f"{abs(diff):,.0f}" if abs(diff) >= 1 else f"{abs(diff):.2f}"
        return f"{arrow}{label}{marker}"

    avg1 = _avg(d1)
    avg2 = _avg(d2)

    W_M, W_V = 20, 22          # column widths: metric name, value

    def _row(label, v1, v2, delta):
        return f"  {label:<{W_M}}  {str(v1):<{W_V}}  {str(v2):<{W_V}}  {delta}"

    sh1 = _short(s1)
    sh2 = _short(s2)
    hdr = f"  {'Metric':<{W_M}}  {sh1:<{W_V}}  {sh2:<{W_V}}  Δ"

    lines = [
        f"  SESSION COMPARISON  ·  {s1}  vs  {s2}",
        "",
        hdr,
        "  " + _rule(len(hdr) - 2),
        _row("Runs",            _v(d1, "run_count"),      _v(d2, "run_count"),
             _delta(d1["run_count"] if d1 else None,      d2["run_count"] if d2 else None)),
        _row("Devices Active",  _v(d1, "devices_active"), _v(d2, "devices_active"),
             _delta(d1["devices_active"] if d1 else None, d2["devices_active"] if d2 else None)),
        _row("Total Messages",
             f"{d1['total_msgs']:,}" if d1 else "not found",
             f"{d2['total_msgs']:,}" if d2 else "not found",
             _delta(d1["total_msgs"] if d1 else None,     d2["total_msgs"] if d2 else None)),
        _row("Errors",
             f"{d1['error_count']:,}" if d1 else "not found",
             f"{d2['error_count']:,}" if d2 else "not found",
             _delta(d1["error_count"] if d1 else None,    d2["error_count"] if d2 else None,
                    higher_is_worse=True)),
        _row("Error Rate",
             f"{d1['error_rate_pct']:.2f}%" if d1 else "not found",
             f"{d2['error_rate_pct']:.2f}%" if d2 else "not found",
             _delta(d1["error_rate_pct"] if d1 else None, d2["error_rate_pct"] if d2 else None,
                    higher_is_worse=True)),
        _row("Avg Msgs/Device",
             f"{avg1:.2f}" if avg1 is not None else "N/A",
             f"{avg2:.2f}" if avg2 is not None else "N/A",
             _delta(avg1, avg2)),
    ]

    # Duration rows — prefer LoadTestSessions values; fall back to telemetry DATEDIFF
    sd1 = sess_durations.get(s1)
    sd2 = sess_durations.get(s2)
    planned1 = sd1["planned"] if sd1 else None
    planned2 = sd2["planned"] if sd2 else None
    actual1  = (sd1["actual"] if sd1 else None) or (d1.get("duration_sec") if d1 else None)
    actual2  = (sd2["actual"] if sd2 else None) or (d2.get("duration_sec") if d2 else None)

    lines += [
        _row("Planned Duration",
             _fmt_duration(planned1) if planned1 is not None else "N/A",
             _fmt_duration(planned2) if planned2 is not None else "N/A",
             "—"),
        _row("Actual Duration",
             _fmt_duration(actual1) if actual1 is not None else "N/A",
             _fmt_duration(actual2) if actual2 is not None else "N/A",
             _delta(actual1, actual2)),
        "",
    ]

    # KEY DIFFERENCES
    diffs = []
    if d1 and d2:
        msg_diff = d2["total_msgs"] - d1["total_msgs"]
        if msg_diff != 0:
            pct = abs(msg_diff) / d1["total_msgs"] * 100 if d1["total_msgs"] else 0
            diffs.append(
                f"Total Messages: Session 2 sent {pct:.1f}% "
                f"{'more' if msg_diff > 0 else 'fewer'} "
                f"({d2['total_msgs']:,} vs {d1['total_msgs']:,})"
            )
        err_diff = d2["error_rate_pct"] - d1["error_rate_pct"]
        if abs(err_diff) >= 0.01:
            diffs.append(
                f"Error Rate: Session 2 was {abs(err_diff):.2f}% "
                f"{'higher' if err_diff > 0 else 'lower'} "
                f"({d2['error_rate_pct']:.2f}% vs {d1['error_rate_pct']:.2f}%)"
            )
        elif d1["error_count"] == 0 and d2["error_count"] == 0:
            diffs.append("Both sessions had zero errors — identical error rate (0.0%)")
        if avg1 is not None and avg2 is not None and abs(avg2 - avg1) >= 0.1:
            diffs.append(
                f"Avg Msgs/Device: {avg2:.2f} vs {avg1:.2f} "
                f"({'higher' if avg2 > avg1 else 'lower'} in Session 2)"
            )
        dev_diff = d2["devices_active"] - d1["devices_active"]
        if dev_diff != 0:
            diffs.append(
                f"Devices Active: Session 2 had {abs(dev_diff)} "
                f"{'more' if dev_diff > 0 else 'fewer'} devices "
                f"({d2['devices_active']} vs {d1['devices_active']})"
            )

    for label, planned, actual in [("Session 1", planned1, actual1), ("Session 2", planned2, actual2)]:
        if planned and actual is not None and planned > 0:
            eff = round(actual / planned * 100, 1)
            diffs.append(
                f"{label} ran {eff}% of planned time "
                f"({_fmt_duration(actual)} actual vs {_fmt_duration(planned)} planned)"
            )

    lines.append("  KEY DIFFERENCES")
    if diffs:
        for d in diffs:
            lines.append(f"  • {d}")
    else:
        lines.append("  • Sessions are identical across all measured metrics.")

    return "\n".join(lines) + "\n"


# ── 6. Direction Balance ──────────────────────────────────────────────────────

def get_direction_balance(test_session_name: str, *, _conn=None) -> dict:
    """IN / OUT message direction balance – flags devices missing a direction
    that the rest of the session uses.

    Args:
        test_session_name: Full or partial session name (auto-resolved).
        _conn: Optional shared pyodbc connection (caller manages lifecycle).

    Returns:
        dict with keys: test_session_name, total_devices, inbound_total,
        outbound_total, session_has_inbound, session_has_outbound,
        imbalanced_count, imbalanced_devices (list), status.
        Each imbalanced entry: { device_id, inbound, outbound, total, issue }
    """
    test_session_name = _resolve_session_name(test_session_name)
    _own_conn = _conn is None
    conn = None
    try:
        conn = _conn if _conn is not None else _get_db_connection()
        cur  = conn.cursor()
        try:
            cur.execute(
                f"""
                SELECT
                    device_id,
                    SUM(CASE WHEN direction = 'IN'  THEN 1 ELSE 0 END) AS inbound,
                    SUM(CASE WHEN direction = 'OUT' THEN 1 ELSE 0 END) AS outbound,
                    COUNT(*)                                             AS total
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                  AND direction IS NOT NULL
                GROUP BY device_id
                ORDER BY device_id
                """,
                (test_session_name,),
            )
            rows = cur.fetchall()

            # Count messages that have no direction set (excluded from balance calc)
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM {_TELEMETRY_TABLE}
                WHERE test_session_name = ?
                  AND direction IS NULL
                """,
                (test_session_name,),
            )
            null_row = cur.fetchone()
            null_direction_count = int(null_row[0]) if null_row else 0
        finally:
            cur.close()

        if not rows:
            return {
                "test_session_name":        test_session_name,
                "total_devices":            0,
                "inbound_total":            0,
                "outbound_total":           0,
                "null_direction_count":     null_direction_count,
                "session_has_inbound":      False,
                "session_has_outbound":     False,
                "imbalanced_count":         0,
                "imbalanced_devices":       [],
                "status":                   "ok",
            }

        all_in  = sum(r[1] for r in rows)
        all_out = sum(r[2] for r in rows)
        has_in  = all_in  > 0
        has_out = all_out > 0

        imbalanced = []
        for r in rows:
            dev_id, inb, outb, tot = r
            issues = []
            if has_in  and inb  == 0:
                issues.append("no IN")
            if has_out and outb == 0:
                issues.append("no OUT")
            if issues:
                imbalanced.append({
                    "device_id": dev_id,
                    "inbound":   inb,
                    "outbound":  outb,
                    "total":     tot,
                    "issue":     " & ".join(issues),
                })

        return {
            "test_session_name":    test_session_name,
            "total_devices":        len(rows),
            "inbound_total":        all_in,
            "outbound_total":       all_out,
            "null_direction_count": null_direction_count,
            "session_has_inbound":  has_in,
            "session_has_outbound": has_out,
            "imbalanced_count":     len(imbalanced),
            "imbalanced_devices":   imbalanced,
            "status":               "ok",
        }
    except Exception as exc:
        logger.warning("get_direction_balance failed: %s", exc)
        return {"test_session_name": test_session_name, "status": "error", "error": str(exc)}
    finally:
        if _own_conn and conn:
            conn.close()


def _fmt_direction_balance(data: dict) -> str:
    if data.get("status") == "error":
        return f"  ERROR: {data.get('error')}\n"

    total = data.get("total_devices", 0)
    if total == 0:
        return "  No direction data found (direction column may be NULL for this session).\n"

    inb   = data.get("inbound_total",  0)
    outb  = data.get("outbound_total", 0)
    null_n = data.get("null_direction_count", 0)
    imb   = data.get("imbalanced_devices", [])
    imb_n = data.get("imbalanced_count", 0)
    all_m = inb + outb
    in_pct  = round(inb  / all_m * 100, 1) if all_m else 0
    out_pct = round(outb / all_m * 100, 1) if all_m else 0

    null_note = f"  ·  {null_n:,} without direction (NULL)" if null_n else ""
    lines = [
        f"  {total:,} devices  ·  "
        f"IN: {inb:,} ({in_pct}%)  ·  OUT: {outb:,} ({out_pct}%){null_note}"
    ]

    if not imb:
        lines.append("  All devices have the expected direction(s)   ✓")
        return "\n".join(lines) + "\n"

    lines.append(f"  {imb_n} device(s) missing an expected direction   ✗")
    lines.append("")
    hdr = f"  {'Device':<36}  {'IN':>8}  {'OUT':>8}  Issue"
    lines.append(hdr)
    lines.append("  " + _rule(len(hdr) - 2))
    for d in imb[:20]:
        lines.append(
            f"  {d['device_id']:<36}"
            f"  {d['inbound']:>8,}"
            f"  {d['outbound']:>8,}"
            f"  {d['issue']}"
        )
    if imb_n > 20:
        lines.append(f"  … and {imb_n - 20} more device(s)")
    return "\n".join(lines) + "\n"


# ── Combined report ───────────────────────────────────────────────────────────

def get_session_insights(test_session_name: str) -> str:
    """Full insights report for a test session.

    Runs all six insight checks over a SINGLE shared DB connection and renders
    them as a formatted report with Unicode box-drawing characters and block-bar
    visuals.  One connection avoids 6x TCP handshake + Azure AD token overhead.

    Includes:
      • Test Step Funnel  – success/error rate per step with bar chart
      • Top Errors        – top-10 error patterns ranked by frequency
      • Message Gaps      – devices with missing msg_no sequences
      • Model Health      – per doppel-model coverage and error rate
      • Run Comparison    – per run_no regression table with baseline deltas
      • Direction Balance – IN / OUT direction anomalies per device

    Args:
        test_session_name: Full or partial session name (auto-resolved).

    Returns:
        Human-readable formatted insights report string.
    """
    resolved = _resolve_session_name(test_session_name)

    # Open ONE connection shared across all six queries to avoid repeated
    # TCP connect + Azure AD token acquisition (each costs 10-30s on cold path).
    shared_conn = None
    try:
        shared_conn = _get_db_connection()
        logger.info("get_session_insights: DB connection established for %s", resolved)
    except Exception as exc:
        logger.warning(
            "get_session_insights: DB connection failed (%s) — each query will reconnect individually",
            exc,
        )

    try:
        funnel    = get_step_funnel(resolved,      _conn=shared_conn)
        errors    = get_top_errors(resolved,        _conn=shared_conn)
        gaps      = get_message_gaps(resolved,      _conn=shared_conn)
        models    = get_model_health(resolved,      _conn=shared_conn)
        runs      = get_run_comparison(resolved,    _conn=shared_conn)
        direction = get_direction_balance(resolved, _conn=shared_conn)
    finally:
        if shared_conn:
            try:
                shared_conn.close()
            except Exception:
                pass

    W      = 66
    inner  = W - 2
    sess   = _trunc(resolved, inner - 26)
    title  = f"  SESSION INSIGHTS  ·  {sess}"
    header = "\n".join([
        "╔" + "═" * inner + "╗",
        "║" + f"{title:<{inner}}" + "║",
        "╚" + "═" * inner + "╝",
    ])

    parts = [
        header,
        _section("TEST STEP FUNNEL",          W) + _fmt_step_funnel(funnel),
        _section("TOP ERRORS",                W) + _fmt_top_errors(errors),
        _section("MESSAGE SEQUENCE INTEGRITY", W) + _fmt_message_gaps(gaps),
        _section("DOPPEL MODEL HEALTH",        W) + _fmt_model_health(models),
        _section("RUN COMPARISON",             W) + _fmt_run_comparison(runs),
        _section("DIRECTION BALANCE",          W) + _fmt_direction_balance(direction),
        "━" * W + "\n",
    ]
    return "\n".join(parts)


# ── FunctionTool exports ──────────────────────────────────────────────────────

get_step_funnel_tool       = FunctionTool(func=get_step_funnel)
get_top_errors_tool        = FunctionTool(func=get_top_errors)
get_message_gaps_tool      = FunctionTool(func=get_message_gaps)
get_model_health_tool      = FunctionTool(func=get_model_health)
get_run_comparison_tool    = FunctionTool(func=get_run_comparison)
get_direction_balance_tool = FunctionTool(func=get_direction_balance)
get_session_insights_tool  = FunctionTool(func=get_session_insights)
compare_sessions_tool      = FunctionTool(func=compare_sessions)

ALL_INSIGHTS_TOOLS = [
    get_step_funnel_tool,
    get_top_errors_tool,
    get_message_gaps_tool,
    get_model_health_tool,
    get_run_comparison_tool,
    get_direction_balance_tool,
    get_session_insights_tool,
    compare_sessions_tool,
]
