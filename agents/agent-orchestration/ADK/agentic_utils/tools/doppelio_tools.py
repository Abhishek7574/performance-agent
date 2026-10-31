"""Doppelio load-testing tools for the Digital Twin Agent."""
import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timezone

import urllib3
import requests
from google.adk.tools import FunctionTool

# Suppress SSL warnings — Doppelio endpoint may use a cert not in the local trust store
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ── Env config ────────────────────────────────────────────────────────────────
# Note: These are loaded at startup. For token specifically, use _get_token() for fresh reads
_WEB_BASE  = os.getenv("DOPPELIO_WEB_API_BASE", "https://api.doppelio.com/doppelioWebAPI/api")
_CORE_BASE = os.getenv("DOPPELIO_CORE_BASE",    "https://api.doppelio.com/doppelio/api")
_FUNC_URL  = os.getenv("DOPPELIO_FUNCTION_APP_ENDPOINT", "")
_DEFAULT_POLL_INTERVAL   = int(os.getenv("DOPPELIO_POLL_INTERVAL_SECS", "30"))
_VERIFY_SSL              = os.getenv("DOPPELIO_VERIFY_SSL", "false").lower() == "true"
_WORKSPACE               = os.getenv("DOPPELIO_WORKSPACE", "AiTemp")
_POST_TEST_DELAY         = int(os.getenv("DOPPELIO_POST_TEST_DELAY_SECS", "180"))
_DOWNLOAD_MAX_RETRIES    = int(os.getenv("DOPPELIO_DOWNLOAD_MAX_RETRIES", "10"))
_DOWNLOAD_RETRY_INTERVAL = int(os.getenv("DOPPELIO_DOWNLOAD_RETRY_INTERVAL_SECS", "120"))

def _get_token() -> str:
    """Load token fresh from environment on each call (fixes Azure env var updates without redeployment)."""
    return os.getenv("DOPPELIO_TOKEN", "")

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Accept": "application/json, text/plain, */*",
    }

def _json_headers() -> dict:
    return {**_headers(), "Content-Type": "application/json"}


# ── Background telemetry download state ───────────────────────────────────────
_poll_state: dict = {
    "active":          False,
    "thread":          None,
    "stop_event":      None,
    "session_name":    "",
    "session_run_no":  0,
    "run_no":          0,
    "doppel_name":     "",
    "num_devices":     0,
    "errors":          0,
    "status":          "idle",
    "download_active":   False,
    "doppels_total":     0,
    "doppels_done":      0,
    "doppels_succeeded": 0,
    "doppels_failed":    [],   # clone names that returned empty or errored in pass 1
    "doppels_retried":   0,
    "events_sent":       0,
    "download_status":   "",
}


# ── 1. Upload CSV file ─────────────────────────────────────────────────────────
def upload_csv_file(file_path: str) -> str:
    """
    Upload a CSV file containing device definitions to Doppelio.

    Args:
        file_path: Absolute local path to the CSV file to upload.

    Returns:
        The uploaded filename token (e.g. '0333stqb') used in subsequent create_doppels call,
        or an error message.
    """
    if not os.path.exists(file_path):
        return f"ERROR: File not found at '{file_path}'. Please provide a valid local file path."

    url = f"{_WEB_BASE}/Doppel/Addcsvfile"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(url, headers=_headers(), files={"file": (os.path.basename(file_path), f, "text/csv")}, timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            token = resp.text.strip().strip('"')
            return f"CSV uploaded successfully. Uploaded filename token: **{token}**\nUse this token in the next step to create doppels."
        return f"ERROR uploading CSV (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"upload_csv_file error: {e}")
        return f"ERROR: {e}"


# ── 2. Create doppels ─────────────────────────────────────────────────────────
def create_doppels(
    uploaded_filename: str,
    doppel_name: str,
    doppel_model_name: str,
    device_id: str,
    doppel_group_name: str = "",
) -> str:
    """
    Bulk-create load-test doppels from a previously uploaded CSV file.

    Args:
        uploaded_filename: Token returned by upload_csv_file (e.g. '0333stqb').
        doppel_name: Name for the doppel group (e.g. 'Test-Device-7').
        doppel_model_name: Friendly model name (e.g. 'Test-Device').
        device_id: The column header from the CSV file that contains the device ID values.
        doppel_group_name: Group name; defaults to doppel_name if omitted.

    Returns:
        Confirmation message with count of doppels created, or error.
    """
    group = doppel_group_name or doppel_name
    url = f"{_WEB_BASE}/Doppel/AddBulk?filenameuploaded={uploaded_filename}&certType=0&columnseparator=,"
    payload = {
        "doppelName": doppel_name,
        "doppelModelName": doppel_model_name,
        "forWhichTestEnvironment": "default",
        "deviceId": device_id,
        "pfxPassword": "",
        "doppelGroupName": group,
        "isDoppelForLoadTest": True,
        "accessToken": "",
        "attributes": [],
    }
    try:
        resp = requests.post(url, headers=_json_headers(), json=payload, timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            data = resp.json()
            msg  = data.get("message", "")
            count = data.get("count", "?")
            return (
                f"Doppels created successfully.\n"
                f"- Message : {msg}\n"
                f"- Count   : {count} load doppels\n"
                f"- Group   : {group}\n"
                f"- Model   : {doppel_model_name}\n\n"
                f"Next: verify doppels or proceed to run a test."
            )
        return f"ERROR creating doppels (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"create_doppels error: {e}")
        return f"ERROR: {e}"


# ── 3. Verify doppels ─────────────────────────────────────────────────────────
def verify_doppels(doppel_model_name: str) -> str:
    """
    Verify that doppels exist for a given doppel model, including load-test doppels.

    Args:
        doppel_model_name: The model name to check (e.g. 'Test-Device').

    Returns:
        List of doppels with their details, or error.
    """
    url = f"{_WEB_BASE}/Doppel/{doppel_model_name}/model?loadTestDoppelsFlag=true&ForWhichTestEnvironment=default"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            doppels = resp.json()
            if not doppels:
                return f"No doppels found for model '{doppel_model_name}'."
            base   = [d for d in doppels if not d.get("isDoppelForLoadTest")]
            clones = [d for d in doppels if d.get("isDoppelForLoadTest")]
            lines  = [f"Found **{len(doppels)}** doppel(s) for model **{doppel_model_name}**:\n"]
            if base:
                lines.append(f"Base doppel: {base[0]['doppelName']} (deviceId: {base[0]['deviceId']})")
            lines.append(f"Load-test clones: {len(clones)}")
            for d in clones[:5]:
                lines.append(f"  - {d['doppelName']} | deviceId: {d['deviceId']}")
            if len(clones) > 5:
                lines.append(f"  ... and {len(clones) - 5} more")
            lines.append("\nDoppels are ready. You can now run a test.")
            return "\n".join(lines)
        return f"ERROR verifying doppels (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"verify_doppels error: {e}")
        return f"ERROR: {e}"


# ── 4. Get doppel models ──────────────────────────────────────────────────────
def get_doppel_models() -> str:
    """
    List all available doppel models in the workspace.

    Returns:
        Formatted list of doppel model names and categories.
    """
    url = f"{_WEB_BASE}/DoppelModel/"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            models = resp.json()
            if not models:
                return "No doppel models found."
            lines = ["Available doppel models:"]
            for m in models:
                lines.append(f"• {m['name']}")
            return "\n".join(lines)
        return f"ERROR fetching doppel models (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"get_doppel_models error: {e}")
        return f"ERROR: {e}"


# ── 5. Get test suites ────────────────────────────────────────────────────────
def get_test_suites() -> str:
    """
    List all test suites and their test cases.

    Returns:
        Formatted list of test suites and test cases. If a suite has multiple test cases,
        the agent should ask the user which one to use.
    """
    url = f"{_WEB_BASE}/TestSuite/"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            suites = resp.json()
            if not suites:
                return "No test suites found."
            lines = ["Available test suites:"]
            for s in suites:
                cases = s.get("testCases", [])
                if not cases:
                    lines.append(f"• {s['name']} (no test cases)")
                elif len(cases) == 1:
                    lines.append(f"• {s['name']} — test case: {cases[0]['name']}")
                else:
                    lines.append(f"• {s['name']} — multiple test cases:")
                    for c in cases:
                        lines.append(f"  - {c['name']}")
            return "\n".join(lines)
        return f"ERROR fetching test suites (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"get_test_suites error: {e}")
        return f"ERROR: {e}"


# ── 6. Run test ────────────────────────────────────────────────────────────────
def run_test(
    test_session_name: str,
    test_suite_name: str,
    test_case_name: str,
    doppel_name: str,
) -> str:
    """
    Start a single load-test run.

    Args:
        test_session_name: Unique name for this test session (e.g. 'load-test-3').
        test_suite_name: Name of the test suite to run (e.g. 'Test-Device-Suite').
        test_case_name: Name of the test case within the suite (e.g. 'Test-Device-Case').
        doppel_name: The doppel (device group) name to use in the test (e.g. 'Test-Device-7').

    Returns:
        Test session details including testSessionId, testSessionRunNo, runNo, and status.
        IMPORTANT: Save testSessionRunNo and runNo — they are required for status, progress, and stop calls.
    """
    url = f"{_CORE_BASE}/TestRun/StartSingleTest"
    payload = {
        "testSessionName": test_session_name,
        "testSessionFriendlyName": test_session_name,
        "testEnvironment": "default",
        "closeSessionFlag": True,
        "sessionIdleTimeOutMins": 30,
        "loadFactor": 5,
        "connectTimespanInSec": 60,
        "rampingProfile": {
            "rampIntervalSec": 10,
            "stepSize": 10,
        },
        "processDataBetweenTests": False,
        "samplerCount": 0,
        "hasNetworkProfileFaults": False,
        "capturePerDoppelMetrics": True,
        "userDefinedDoppelWeight": 1,
        "testExecParams": {
            "tsName": test_suite_name,
            "tcName": test_case_name,
            "fastforwardMode": False,
            "runFromNegativeDeltaDays": 0,
            "runFromAbsoluteTimeHours": 0,
            "runFromAbsoluteTimeMins": 0,
            "numTimesToRunTestCase": 1,
            "loadFactor": 5,
            "commonDoppelNames": [doppel_name],
        },
    }
    try:
        resp = requests.post(url, headers=_json_headers(), json=payload, timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            data = resp.json()
            session_id      = data.get("testSessionId", "")
            session_run_no  = data.get("testSessionRunNo", "")
            run_no          = data.get("runNo", "")
            status          = data.get("status", "")
            return (
                f"Test started successfully.\n"
                f"session_name={test_session_name}\n"
                f"session_id={session_id}\n"
                f"testSessionRunNo={session_run_no}\n"
                f"runNo={run_no}\n"
                f"status={status}"
            )
        return f"ERROR starting test (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"run_test error: {e}")
        return f"ERROR: {e}"


# ── 7. Get test status ────────────────────────────────────────────────────────
def get_test_status(test_session_name: str, test_session_run_no: int, test_run_no: int) -> str:
    """
    Get the current status of a test run.

    Args:
        test_session_name: The session name used when starting the test.
        test_session_run_no: The testSessionRunNo returned by run_test.
        test_run_no: The runNo returned by run_test.

    Returns:
        Formatted status report for the test session and its run(s).
    """
    url = f"{_CORE_BASE}/TestRun/{test_session_name}/Status?TestSessionNo={test_session_run_no}&RunNo={test_run_no}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            data = resp.json()
            session_status = data.get("testSessionStatus", "")
            is_running     = data.get("isTestRunning", False)
            message        = data.get("testSessionMessage", "")
            lines = [
                f"**Test Status — {test_session_name}**",
                f"- Session status : {session_status}",
                f"- Is running     : {is_running}",
                f"- Message        : {message}",
            ]
            for run in data.get("testRunStatus", []):
                lines.append(
                    f"- Run {run.get('runNo')}: {run.get('testStatus')} | "
                    f"Suite: {run.get('tsName')} / {run.get('tcName')} | "
                    f"Errors: {run.get('hasErrors', False)}"
                )
            return "\n".join(lines)
        return f"ERROR fetching test status (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"get_test_status error: {e}")
        return f"ERROR: {e}"


# ── 8. Get test progress ──────────────────────────────────────────────────────
def _get_test_progress_raw(
    test_session_name: str, test_session_run_no: int, test_run_no: int
) -> "dict | None":
    """Return the raw TestProgress JSON dict from Doppelio, or None on any error."""
    url = (
        f"{_CORE_BASE}/TestRun/{test_session_name}/TestProgress"
        f"?TestSessionRunNo={test_session_run_no}&TestRunNo={test_run_no}"
    )
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("_get_test_progress_raw HTTP %s for '%s'", resp.status_code, test_session_name)
    except Exception as e:
        logger.warning("_get_test_progress_raw error: %s", e)
    return None


def get_test_progress(test_session_name: str, test_session_run_no: int, test_run_no: int) -> str:
    """
    Get detailed real-time progress of a test run, including doppel counts and message metrics.

    Args:
        test_session_name: The session name used when starting the test.
        test_session_run_no: The testSessionRunNo returned by run_test.
        test_run_no: The runNo returned by run_test.

    Returns:
        Formatted progress report with iteration counts, doppel stats, and message metrics.
    """
    d = _get_test_progress_raw(test_session_name, test_session_run_no, test_run_no)
    if d is None:
        return f"ERROR fetching test progress for '{test_session_name}'"

    total_steps  = d.get("totalTestSteps", 0)
    done_steps   = d.get("completedTestSteps", 0)
    total_dop    = d.get("totalDoppels", 0)
    init_dop     = d.get("initialisedDoppels", 0)
    total_iter   = d.get("totalIterations", 0)
    done_iter    = d.get("completedIterations", 0)
    msgs_sent    = d.get("totalNumMessagesSent", 0)
    msgs_recv    = d.get("totalNumMessagesReceived", 0)
    doppels_used = d.get("doppelsUsed", [])
    total_dur    = d.get("totalDurationInSecs")
    done_dur     = d.get("completedDurationInSecs")

    lines = [
        f"**Test Progress — {test_session_name}** (Run {test_run_no})",
        f"- Status             : {d.get('testRunStatus', '')}",
        f"- Steps              : {done_steps}/{total_steps}",
        f"- Doppels init       : {init_dop}/{total_dop}",
        f"- Iterations         : {done_iter}/{total_iter}",
        f"- Messages sent      : {msgs_sent}",
        f"- Messages received  : {msgs_recv}",
        f"- Planned duration   : {total_dur}s" if total_dur is not None else "- Planned duration   : N/A",
        f"- Completed duration : {done_dur}s"  if done_dur  is not None else "- Completed duration : N/A",
        f"- Doppels in test    : {', '.join(doppels_used) if doppels_used else 'none yet'}",
    ]
    if d.get("testRunErrors"):
        lines.append(f"- Errors: {d['testRunErrors']}")
    return "\n".join(lines)


def _parse_stop_response_json(body: dict) -> str:
    """Extract message from JSON response."""
    raw_msg = body.get("error") or body.get("message") or ""
    if body.get("statusCode", -1) == 0 or "stopping" in raw_msg.lower():
        return raw_msg or "Test stopped successfully."
    return raw_msg or "Test stop command issued."

def _parse_stop_response_text(text: str) -> str:
    """Extract message from plain text response."""
    raw = text.strip().strip('"')
    m = re.match(r'^Status:\d+\s+Error:\s*', raw, re.IGNORECASE)
    return raw[m.end():].strip() if m else (raw or "Test stopped successfully.")

def _cleanup_after_stop(test_session_name: str) -> None:
    """Clean up polling state and tracking after stopping a test."""
    if _poll_state["active"]:
        _stop_polling()
    try:
        from agentic_utils.tools.doppelio_orchestrator import remove_active_test
        remove_active_test(test_session_name)
    except Exception:
        pass

# ── 9. Stop test ──────────────────────────────────────────────────────────────
def stop_test(test_session_name: str, test_session_run_no: int) -> str:
    """
    Stop a running test session and all its doppels.

    Args:
        test_session_name: The session name of the running test.
        test_session_run_no: The testSessionRunNo of the running session.

    Returns:
        Confirmation that the stop command was issued.
    """
    url = f"{_CORE_BASE}/TestRun/{test_session_name}/StopTestSession?TestSessionRunNo={test_session_run_no}"
    try:
        resp = requests.post(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            _cleanup_after_stop(test_session_name)
            return f"✓ Load test stopped — session **{test_session_name}** (run {test_session_run_no}) has been stopped successfully."
        return f"ERROR stopping test (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"stop_test error: {e}")
        return f"ERROR: {e}"


def _fmt_started(raw: str) -> str:
    """Shorten an ISO/datetime started-at string to HH:MM."""
    if not raw or raw == "N/A":
        return "—"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(raw[:19], fmt[:len(fmt)]).strftime("%H:%M")
        except Exception:
            continue
    return raw[:16]  # fallback: truncate


def _session_sort_key(s: dict) -> tuple:
    st = (s.get("testSessionStatus") or "").upper()
    return (0 if st == "RUNNING" else 1, s.get("testSessionName", ""))


def _cap_sessions(sessions: list) -> tuple:
    """Cap to 10 (list is already sorted: running first, time desc). Returns (capped_list, cap_note_str)."""
    capped = sessions[:10]
    note   = f" (showing {len(capped)} of {len(sessions)})" if len(sessions) > 10 else ""
    return capped, note


def _build_sessions_response(sessions: list, label: str, only_running: bool) -> str:
    """Build a Markdown pipe-table response for test sessions."""
    if not sessions:
        return "No test sessions are currently running." if only_running else f"No test sessions found ({label})."

    # Sort: running first, then by start time descending (most recent first)
    sessions = sorted(sessions, key=lambda s: s.get("testSessionStartedAt") or "", reverse=True)
    running  = [s for s in sessions if (s.get("testSessionStatus") or "").upper() == "RUNNING"]
    others   = [s for s in sessions if (s.get("testSessionStatus") or "").upper() != "RUNNING"]
    sessions = running + others
    cap_note = ""
    if not only_running and len(sessions) > 10:
        sessions, cap_note = _cap_sessions(sessions)

    header = f"**{len(sessions)} {'running ' if only_running else ''}test session(s){cap_note} — {label}**\n\n"

    rows = ["| Session | Run No | Status | Started | Runs |", "|---|---|---|---|---|"]
    for s in sessions:
        name    = s.get("testSessionName", "—")
        run_no  = str(s.get("testSessionRunNo", "—"))
        status  = (s.get("testSessionStatus") or "—").upper()
        started = _fmt_started(s.get("testSessionStartedAt", ""))
        runs    = s.get("testRunStatus", [])
        run_summary = ", ".join(f"#{r['testNo']} {r['testStatus']}" for r in runs) if runs else "—"
        rows.append(f"| {name} | {run_no} | {status} | {started} | {run_summary} |")

    return header + "\n".join(rows)

# ── 10. Get test sessions ─────────────────────────────────────────────────────
def get_test_sessions(start_date: str = "", end_date: str = "", only_running: bool = False) -> str:
    """
    List test sessions for today (or a given date range).

    Args:
        start_date: Start of date range in 'MM/DD/YYYY HH:MM' format. Defaults to today 00:00.
        end_date: End of date range in 'MM/DD/YYYY HH:MM' format. Defaults to today 23:59.
        only_running: If True, return only currently active/running sessions.

    Returns:
        Formatted list of test sessions with their statuses.
    """
    today = datetime.now(timezone.utc)
    if not start_date:
        start_date = today.strftime("%m/%d/%Y 00:00")
    if not end_date:
        end_date = today.strftime("%m/%d/%Y 23:59")

    import urllib.parse
    params = (
        f"startDate={urllib.parse.quote(start_date)}"
        f"&endDate={urllib.parse.quote(end_date)}"
        f"&NumberOfSessions=100&AllWkUsersFlag=false"
        f"&OnlyRunningTests={'true' if only_running else 'false'}"
    )
    url = f"{_CORE_BASE}/TestRun/TestSessions?{params}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 404:
            return "No test sessions are currently running." if only_running else f"No test sessions found ({start_date} → {end_date})."
        if resp.status_code == 200:
            sessions = resp.json()
            label = "running" if only_running else f"{start_date} → {end_date}"
            return _build_sessions_response(sessions, label, only_running)
        return f"ERROR fetching test sessions (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"get_test_sessions error: {e}")
        return f"ERROR: {e}"


def get_running_tests() -> str:
    """
    List all currently active/running test sessions (today).

    Returns:
        Formatted list of running sessions with names, run numbers, and suite/case info.
        Use the sessionRunNo and runNo values to call get_test_progress or attach_to_session.
    """
    return get_test_sessions(only_running=True)


# ── 11–13. Metadata polling ────────────────────────────────────────────────────

def _stop_polling():
    """Internal: signal the polling thread to stop."""
    if _poll_state["stop_event"]:
        _poll_state["stop_event"].set()
    _poll_state["active"] = False
    _poll_state["status"] = "stopped"


def _forward_to_function_app(batch: list) -> bool:
    """Internal: POST a batch of data payloads to the Azure Function App."""
    if not _FUNC_URL:
        logger.warning("DOPPELIO_FUNCTION_APP_ENDPOINT not configured — skipping forward.")
        return False
    try:
        resp = requests.post(_FUNC_URL, json=batch, headers={"Content-Type": "application/json"}, timeout=15, verify=_VERIFY_SSL)
        return resp.status_code < 300
    except Exception as e:
        logger.exception(f"forward_to_function_app error: {e}")
        return False


def _status_monitor_loop(
    session_name: str, session_run_no: int, run_no: int,
    doppel_name: str, num_devices: int, interval: int,
):
    """Background thread: poll test status until completion, then trigger post-test download."""
    stop_event = _poll_state["stop_event"]

    while not stop_event.is_set():
        try:
            status_url = (
                f"{_CORE_BASE}/TestRun/{session_name}/Status"
                f"?TestSessionNo={session_run_no}&RunNo={run_no}"
            )
            st_resp = requests.get(status_url, headers=_headers(), timeout=15, verify=_VERIFY_SSL)
            if st_resp.status_code == 200:
                session_status = st_resp.json().get("testSessionStatus", "")
                if session_status in ("Stopped", "Completed", "Error", "Stopping"):
                    logger.info(
                        f"Test '{session_name}' is '{session_status}' — scheduling post-test download."
                    )
                    _poll_state["status"] = f"monitoring-complete ({session_status})"
                    _poll_state["active"] = False
                    if _FUNC_URL and doppel_name and num_devices > 0:
                        t = threading.Thread(
                            target=_post_test_download,
                            args=(session_name, session_run_no, run_no, doppel_name, num_devices),
                            daemon=True,
                        )
                        t.start()
                    elif not _FUNC_URL:
                        logger.warning("DOPPELIO_FUNCTION_APP_ENDPOINT not set — skipping download.")
                    return
        except Exception as e:
            logger.exception(f"status_monitor_loop error: {e}")
            _poll_state["errors"] += 1

        stop_event.wait(timeout=interval)

    _poll_state["active"] = False
    _poll_state["status"] = "stopped"


def _download_doppel_telemetry(
    doppel_name: str, session_name: str, session_run_no: int, run_no: int
) -> list:
    """Download telemetry TSV for one doppel clone. Returns list of row dicts or []."""
    import csv as csv_mod
    file_name = f"{doppel_name}_TelemetryLogs"
    url = (
        f"{_CORE_BASE}/TestRunMetrics/DownloadDoppelTelemetryData"
        f"?workspace={_WORKSPACE}"
        f"&TestSessionName={session_name}"
        f"&TestSessionNo={session_run_no}"
        f"&TestRunNo={run_no}"
        f"&doppelName={doppel_name}"
        f"&FileName={file_name}"
    )
    resp = requests.post(url, headers=_headers(), timeout=60, verify=_VERIFY_SSL)
    if resp.status_code != 200 or not resp.text.strip():
        return []

    lines = resp.text.splitlines()
    # Skip "sep=..." line if present
    if lines and lines[0].strip().startswith("sep="):
        lines = lines[1:]
    if len(lines) < 2:
        return []

    reader = csv_mod.DictReader(lines, delimiter="\t")
    rows = []
    for row in reader:
        if row.get("Type", "").strip().lower() != "data":
            continue
        payload_str = row.get("Payload", "")
        try:
            parsed_payload = json.loads(payload_str) if payload_str.strip() else {}
        except (json.JSONDecodeError, TypeError):
            parsed_payload = {"rawPayload": payload_str}

        rows.append({
            "testSessionName":  session_name,
            "testSessionRunNo": session_run_no,
            "testRunNo":        run_no,
            "publishedAt":      row.get("Publish At(India Standard Time)", "").lstrip("'"),
            "doppelName":       row.get("Doppel", ""),
            "direction":        row.get("Direction", ""),
            "testStep":         row.get("TestStep", ""),
            "connection":       row.get("Connection", ""),
            "format":           row.get("Format", ""),
            "type":             row.get("Type", ""),
            "topic":            row.get("Topic", ""),
            "deviceTimeUtc":    row.get("Device Time(UTC)", "").lstrip("'"),
            "msgId":            row.get("MsgID", ""),
            "error":            row.get("Error", ""),
            **parsed_payload,
        })
    return rows


def _push_events(clone: str, events: list) -> bool:
    """Push a list of events in batches to the Function App. Returns True if all batches succeeded."""
    _BATCH_SIZE = 100
    all_ok = True
    for i in range(0, len(events), _BATCH_SIZE):
        batch = events[i:i + _BATCH_SIZE]
        ok = _forward_to_function_app(batch)
        if ok:
            _poll_state["events_sent"] += len(batch)
        else:
            _poll_state["errors"] += 1
            logger.exception(f"[post_test_download] Function App push failed for '{clone}' batch {i // _BATCH_SIZE + 1}")
            all_ok = False
    return all_ok


def _init_download_state(num_devices: int) -> None:
    """Initialize download state in poll_state dict."""
    _poll_state.update({
        "download_active":   True,
        "download_status":   f"waiting {_POST_TEST_DELAY}s for data to become available...",
        "doppels_total":     num_devices,
        "doppels_done":      0,
        "doppels_succeeded": 0,
        "doppels_failed":    [],
        "doppels_retried":   0,
        "events_sent":       0,
    })

def _download_clone_pass1(clone: str, session_name: str, session_run_no: int, run_no: int) -> bool:
    """Attempt to download and push events for a single clone (Pass 1). Returns True if successful."""
    try:
        events = _download_doppel_telemetry(clone, session_name, session_run_no, run_no)
    except Exception as e:
        logger.exception(f"[post_test_download] Pass 1 error for '{clone}': {e}")
        _poll_state["errors"] += 1
        return False
    
    if not events:
        logger.warning(f"[post_test_download] Pass 1 empty: '{clone}' — queued for retry.")
        return False
    
    ok = _push_events(clone, events)
    if ok:
        _poll_state["doppels_succeeded"] += 1
        logger.info(f"[post_test_download] Pass 1 OK: '{clone}' — {len(events)} events pushed.")
        return True
    
    logger.warning(f"[post_test_download] Pass 1 push failed for '{clone}' — queued for retry.")
    return False

def _retry_clone_download(clone: str, session_name: str, session_run_no: int, run_no: int) -> list:
    """Retry downloading clone telemetry with multiple attempts. Returns events list."""
    events = []
    for attempt in range(_DOWNLOAD_MAX_RETRIES):
        _poll_state["download_status"] = (
            f"pass 2 — retrying '{clone}' (attempt {attempt + 1}/{_DOWNLOAD_MAX_RETRIES})"
        )
        try:
            events = _download_doppel_telemetry(clone, session_name, session_run_no, run_no)
            if events:
                break
            logger.info(
                f"[post_test_download] Pass 2 still empty for '{clone}' "
                f"(attempt {attempt + 1}/{_DOWNLOAD_MAX_RETRIES}), "
                f"retrying in {_DOWNLOAD_RETRY_INTERVAL}s..."
            )
        except Exception as e:
            logger.exception(f"[post_test_download] Pass 2 error for '{clone}': {e}")
            _poll_state["errors"] += 1
        
        if attempt < _DOWNLOAD_MAX_RETRIES - 1:
            time.sleep(_DOWNLOAD_RETRY_INTERVAL)
    
    return events

def _process_clone_retry(clone: str, session_name: str, session_run_no: int, run_no: int) -> bool:
    """Process a single clone retry in Pass 2. Returns True if successful."""
    events = _retry_clone_download(clone, session_name, session_run_no, run_no)
    
    if not events:
        logger.warning(
            f"[post_test_download] Pass 2 gave up on '{clone}' "
            f"after {_DOWNLOAD_MAX_RETRIES} attempts."
        )
        return False
    
    ok = _push_events(clone, events)
    if ok:
        _poll_state["doppels_failed"].remove(clone)
        _poll_state["doppels_retried"] += 1
        logger.info(f"[post_test_download] Pass 2 OK: '{clone}' — {len(events)} events pushed.")
        return True
    
    logger.warning(f"[post_test_download] Pass 2 push failed for '{clone}' — will remain in failed list.")
    return False

def _finalize_download_state() -> None:
    """Finalize and log download completion status."""
    still_failed = list(_poll_state["doppels_failed"])
    total_events = _poll_state["events_sent"]
    total_errors = _poll_state["errors"]
    _poll_state.update({
        "download_active": False,
        "download_status": (
            f"complete — {total_events} events pushed, "
            f"{_poll_state['doppels_succeeded'] + _poll_state['doppels_retried']} doppels OK, "
            f"{len(still_failed)} permanently failed, "
            f"{total_errors} errors"
        ),
    })
    if still_failed:
        logger.warning(f"[post_test_download] Permanently failed clones: {still_failed}")
    logger.info(f"[post_test_download] Done. {total_events} events sent, {total_errors} errors.")

def _post_test_download(
    session_name: str, session_run_no: int, run_no: int,
    doppel_name: str, num_devices: int,
):
    """
    Background thread: wait for data availability, then download per-clone telemetry
    and push to Function App in two passes:
      Pass 1 — attempt each clone once; track successes and failures.
      Pass 2 — retry all failed/empty clones with full retry logic.
    """
    clones = [f"{doppel_name}-c{i}" for i in range(1, num_devices + 1)]
    _init_download_state(num_devices)
    
    logger.info(
        f"[post_test_download] Waiting {_POST_TEST_DELAY}s before downloading "
        f"telemetry for '{session_name}' ({num_devices} clones)."
    )
    time.sleep(_POST_TEST_DELAY)

    # ── Pass 1: single attempt per clone ──────────────────────────────────────
    logger.info(f"[post_test_download] Pass 1 — sweeping {num_devices} clone(s) of '{doppel_name}'.")
    for clone in clones:
        _poll_state["download_status"] = f"pass 1 — {_poll_state['doppels_done'] + 1}/{num_devices}: {clone}"
        
        success = _download_clone_pass1(clone, session_name, session_run_no, run_no)
        if not success:
            _poll_state["doppels_failed"].append(clone)
        
        _poll_state["doppels_done"] += 1

    failed = list(_poll_state["doppels_failed"])
    logger.info(
        f"[post_test_download] Pass 1 done. "
        f"Succeeded: {_poll_state['doppels_succeeded']}, Failed/empty: {len(failed)}."
    )

    # ── Pass 2: retry failed/empty clones ─────────────────────────────────────
    if failed:
        logger.info(f"[post_test_download] Pass 2 — retrying {len(failed)} failed clone(s).")
        _poll_state["download_status"] = f"pass 2 — retrying {len(failed)} failed clone(s)..."
        
        for clone in failed:
            _process_clone_retry(clone, session_name, session_run_no, run_no)

    _finalize_download_state()


def start_metadata_polling(
    test_session_name: str,
    test_session_run_no: int,
    test_run_no: int,
    doppel_name: str = "",
    num_devices: int = 0,
    poll_interval_secs: int = 0,
) -> str:
    """
    Start a background thread that monitors test status and, upon completion,
    downloads per-doppel telemetry via DownloadDoppelTelemetryData and forwards
    it to the configured Azure Function App.

    Args:
        test_session_name: The session name of the running test.
        test_session_run_no: The testSessionRunNo returned by run_test.
        test_run_no: The runNo returned by run_test.
        doppel_name: Base doppel name (without -cN suffix), e.g. ai-load-5000d-...-m1.
        num_devices: Total number of devices (clones c1..cN to download).
        poll_interval_secs: Seconds between status polls (default: DOPPELIO_POLL_INTERVAL_SECS env var, fallback 30).

    Returns:
        Confirmation that monitoring has started.
    """
    if _poll_state["active"]:
        return (
            f"Telemetry monitor is already active for session '{_poll_state['session_name']}'. "
            "Call stop_metadata_polling() first if you want to restart."
        )

    interval = poll_interval_secs if poll_interval_secs > 0 else _DEFAULT_POLL_INTERVAL

    stop_event = threading.Event()
    _poll_state.update({
        "active":          True,
        "stop_event":      stop_event,
        "session_name":    test_session_name,
        "session_run_no":  test_session_run_no,
        "run_no":          test_run_no,
        "doppel_name":     doppel_name,
        "num_devices":     num_devices,
        "errors":          0,
        "status":          "monitoring",
        "download_active":   False,
        "doppels_total":     0,
        "doppels_done":      0,
        "doppels_succeeded": 0,
        "doppels_failed":    [],
        "doppels_retried":   0,
        "events_sent":       0,
        "download_status":   "",
    })

    t = threading.Thread(
        target=_status_monitor_loop,
        args=(test_session_name, test_session_run_no, test_run_no, doppel_name, num_devices, interval),
        daemon=True,
    )
    t.start()
    _poll_state["thread"] = t

    func_status = (
        f"will download and forward to {_FUNC_URL}"
        if _FUNC_URL
        else "WARNING: DOPPELIO_FUNCTION_APP_ENDPOINT not set, data will not be forwarded"
    )
    return (
        f"Telemetry monitor started for session '{test_session_name}'. "
        f"Checking status every {interval}s; on completion {func_status}."
    )


def stop_metadata_polling() -> str:
    """
    Stop the active background status monitor thread.
    Any in-progress telemetry download will continue to completion.

    Returns:
        Summary of events forwarded and errors.
    """
    if not _poll_state["active"] and not _poll_state.get("stop_event"):
        if _poll_state["download_active"]:
            return (
                f"Status monitor already stopped; telemetry download still in progress "
                f"({_poll_state['doppels_done']}/{_poll_state['doppels_total']} doppels done)."
            )
        return "No active telemetry monitor to stop."

    _stop_polling()
    return (
        f"Telemetry monitor stopped.\n"
        f"- Events forwarded to Function App: {_poll_state['events_sent']}\n"
        f"- Errors encountered              : {_poll_state['errors']}\n"
        f"- Download active                 : {_poll_state['download_active']}"
    )


def get_metadata_polling_status() -> str:
    """
    Get the current status of the background telemetry monitor and download task.

    Returns:
        Status report including download progress, events sent, and errors.
    """
    if not _poll_state["active"] and _poll_state["status"] == "idle":
        return "Telemetry monitor is not running. Call start_metadata_polling() to begin."

    return (
        f"**Telemetry Monitor Status**\n"
        f"- Monitor state  : {_poll_state['status']}\n"
        f"- Session        : {_poll_state['session_name']} "
        f"(run #{_poll_state['session_run_no']}, test #{_poll_state['run_no']})\n"
        f"- Doppel         : {_poll_state['doppel_name'] or '(unknown)'} ({_poll_state['num_devices']} devices)\n"
        f"- Download active: {_poll_state['download_active']}\n"
        f"- Download status: {_poll_state['download_status'] or 'not started'}\n"
        f"- Doppels total  : {_poll_state['doppels_total']}\n"
        f"- Pass 1 done    : {_poll_state['doppels_done']}/{_poll_state['doppels_total']}\n"
        f"- Pass 1 success : {_poll_state['doppels_succeeded']}\n"
        f"- Pass 1 failed  : {len(_poll_state['doppels_failed'])} {_poll_state['doppels_failed'] or ''}\n"
        f"- Pass 2 retried : {_poll_state['doppels_retried']}\n"
        f"- Events sent    : {_poll_state['events_sent']}\n"
        f"- Errors         : {_poll_state['errors']}\n"
        f"- Endpoint       : {_FUNC_URL or '(not configured)'}"
    )


# ── 14. Get test case steps ───────────────────────────────────────────────────
def get_test_case_steps(test_suite_name: str, test_case_name: str) -> str:
    """
    Get all steps for a given test case, including their full configuration.

    Args:
        test_suite_name: Name of the test suite (e.g. 'Test-Device-Suite').
        test_case_name: Name of the test case (e.g. 'Test-Device-Case').

    Returns:
        List of steps with their names, doppel models, duration, and publish interval.
    """
    url = f"{_WEB_BASE}/TestStep/{test_suite_name}/{test_case_name}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            steps = resp.json()
            if not steps:
                return f"No steps found for test case '{test_case_name}'."
            lines = [f"**Steps for {test_suite_name} / {test_case_name}:**\n"]
            for s in steps:
                rp = s.get("runParams", {})
                completion = rp.get("completionType", "")
                duration = f"{rp.get('durationMins')} mins" if completion == "time" else f"{rp.get('numIterations')} iterations"
                lines.append(
                    f"- **{s['stepName']}** | model: {s.get('doppelModelName')} | "
                    f"duration: {duration} | publishInterval: {rp.get('publishIntervalInSecs')}s | "
                    f"stopByCommand: {rp.get('stopByCommandMode')}"
                )
            return "\n".join(lines)
        return f"ERROR fetching test steps (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.exception(f"get_test_case_steps error: {e}")
        return f"ERROR: {e}"


# ── 15. Update test step duration ─────────────────────────────────────────────
def update_test_step_duration(
    test_suite_name: str,
    test_case_name: str,
    step_name: str,
    doppel_model_name: str,
    duration_mins: float,
) -> str:
    """
    Update the durationMins of a specific test step, keeping all other config unchanged.

    Fetches the current step config, changes only durationMins (and sets completionType
    to 'time'), then PUTs it back.

    Args:
        test_suite_name: Name of the test suite (e.g. 'Test-Device-Suite').
        test_case_name: Name of the test case (e.g. 'Test-Device-Case').
        step_name: Name of the step to update (e.g. 'send_telemetry').
        doppel_model_name: Doppel model name for the step (e.g. 'Test-Device').
        duration_mins: New duration in minutes (e.g. 10).

    Returns:
        Confirmation of the update or error message.
    """
    get_url = f"{_WEB_BASE}/TestStep/{test_suite_name}/{test_case_name}/{step_name}?NewDoppelModel={doppel_model_name}"
    try:
        get_resp = requests.get(get_url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if get_resp.status_code != 200:
            return f"ERROR fetching step '{step_name}' (HTTP {get_resp.status_code}): {get_resp.text}"

        payload = get_resp.json()

        # Change only durationMins and ensure completionType is time-based
        payload["runParams"]["completionType"] = "time"
        payload["runParams"]["durationMins"] = duration_mins
        payload["runParams"]["numIterations"] = 0

        put_url = f"{_WEB_BASE}/TestStep/{test_suite_name}/{test_case_name}/{step_name}?NewDoppelModel={doppel_model_name}"
        put_resp = requests.put(put_url, headers=_json_headers(), json=payload, timeout=30, verify=_VERIFY_SSL)
        if put_resp.status_code == 200:
            return (
                f"Step '{step_name}' updated successfully.\n"
                f"- Suite    : {test_suite_name}\n"
                f"- Case     : {test_case_name}\n"
                f"- Duration : {duration_mins} mins"
            )
        return f"ERROR updating step '{step_name}' (HTTP {put_resp.status_code}): {put_resp.text}"
    except Exception as e:
        logger.exception(f"update_test_step_duration error: {e}")
        return f"ERROR: {e}"


# ── 16. Update test step completion mode ──────────────────────────────────────
def update_test_step_completion(
    test_suite_name: str,
    test_case_name: str,
    step_name: str,
    doppel_model_name: str,
    num_iterations: int = 1,
) -> str:
    """
    Switch a test step to run for a fixed number of iterations instead of waiting
    for a stop command. Use this to fix 'disconnect' or similar steps that are
    configured as 'Publish Once and Wait till stopped by Command', which causes
    the test to never auto-complete.

    Fetches current step config, sets completionType='iteration', stopByCommandMode=False,
    numIterations=num_iterations, then PUTs it back.

    Args:
        test_suite_name: Name of the test suite (e.g. 'Test-Device-Suite').
        test_case_name: Name of the test case (e.g. 'Test-Device-Case').
        step_name: Name of the step to fix (e.g. 'disconnect').
        doppel_model_name: Doppel model name for the step (e.g. 'Test-Device').
        num_iterations: How many iterations to run (default: 1).

    Returns:
        Confirmation of the update or error message.
    """
    get_url = f"{_WEB_BASE}/TestStep/{test_suite_name}/{test_case_name}/{step_name}?NewDoppelModel={doppel_model_name}"
    try:
        get_resp = requests.get(get_url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if get_resp.status_code != 200:
            return f"ERROR fetching step '{step_name}' (HTTP {get_resp.status_code}): {get_resp.text}"

        payload = get_resp.json()

        payload["runParams"]["completionType"]  = "iteration"
        payload["runParams"]["numIterations"]   = num_iterations
        payload["runParams"]["durationMins"]    = 0
        payload["runParams"]["stopByCommandMode"] = False

        put_url = f"{_WEB_BASE}/TestStep/{test_suite_name}/{test_case_name}/{step_name}?NewDoppelModel={doppel_model_name}"
        put_resp = requests.put(put_url, headers=_json_headers(), json=payload, timeout=30, verify=_VERIFY_SSL)
        if put_resp.status_code == 200:
            return (
                f"Step '{step_name}' updated to run {num_iterations} iteration(s) and auto-complete.\n"
                f"- Suite           : {test_suite_name}\n"
                f"- Case            : {test_case_name}\n"
                f"- stopByCommand   : False\n"
                f"- numIterations   : {num_iterations}"
            )
        return f"ERROR updating step '{step_name}' (HTTP {put_resp.status_code}): {put_resp.text}"
    except Exception as e:
        logger.exception(f"update_test_step_completion error: {e}")
        return f"ERROR: {e}"


# ── Exported FunctionTool instances ───────────────────────────────────────────
upload_csv_file_tool          = FunctionTool(func=upload_csv_file)
create_doppels_tool           = FunctionTool(func=create_doppels)
verify_doppels_tool           = FunctionTool(func=verify_doppels)
get_doppel_models_tool        = FunctionTool(func=get_doppel_models)
get_test_suites_tool          = FunctionTool(func=get_test_suites)
run_test_tool                 = FunctionTool(func=run_test)
get_test_status_tool          = FunctionTool(func=get_test_status)
get_test_progress_tool        = FunctionTool(func=get_test_progress)
stop_test_tool                = FunctionTool(func=stop_test)
get_test_sessions_tool        = FunctionTool(func=get_test_sessions)
get_running_tests_tool        = FunctionTool(func=get_running_tests)
start_metadata_polling_tool   = FunctionTool(func=start_metadata_polling)
stop_metadata_polling_tool    = FunctionTool(func=stop_metadata_polling)
get_metadata_polling_status_tool = FunctionTool(func=get_metadata_polling_status)
get_test_case_steps_tool           = FunctionTool(func=get_test_case_steps)
update_test_step_duration_tool     = FunctionTool(func=update_test_step_duration)
update_test_step_completion_tool   = FunctionTool(func=update_test_step_completion)

ALL_DOPPELIO_TOOLS = [
    upload_csv_file_tool,
    create_doppels_tool,
    verify_doppels_tool,
    get_doppel_models_tool,
    get_test_suites_tool,
    run_test_tool,
    get_test_status_tool,
    get_test_progress_tool,
    stop_test_tool,
    get_test_sessions_tool,
    get_running_tests_tool,
    start_metadata_polling_tool,
    stop_metadata_polling_tool,
    get_metadata_polling_status_tool,
    get_test_case_steps_tool,
    update_test_step_duration_tool,
    update_test_step_completion_tool,
]
