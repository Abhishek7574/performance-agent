"""Autonomous Doppelio load-test orchestrator — single tool, zero user interaction."""
import csv
import os
import re
import time
import logging
import threading
from datetime import datetime

import requests
import urllib3
from google.adk.tools import FunctionTool

from agentic_utils.tools.doppelio_tools import (
    _WEB_BASE, _CORE_BASE, _headers, _json_headers, _VERIFY_SSL,
    _FUNC_URL, _DEFAULT_POLL_INTERVAL, start_metadata_polling,
    _get_test_progress_raw,
)
from agentic_utils.tools.db_registration import (
    register_load_test_devices,
    update_session_completed_duration,
)

# Security: Only disable warnings if SSL verification is explicitly disabled in config
# Consider enabling SSL verification in production
if not _VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
_NO_ACTIVE_TESTS_MSG = "No active load tests."

# ── Progress state (polled by /api/load-test-progress) ────────────────────────
_progress: dict = {
    "active":        False,
    "step":          "",
    "steps_done":    [],
    "total_steps":   7,
    "error":         "",
    "result":        "",
    "infra_status":  "",   # "checking" | "ok" | "failed"
}

def _model_prefix(model_name: str) -> str:
    """Convert a doppel model name to a safe lowercase hyphenated prefix.

    Examples:
        "Sensor"         → "sensor"
        "AI Sensor"      → "ai-sensor"
        "Smart Device v2" → "smart-device-v2"
    """
    prefix = model_name.lower().strip()
    prefix = re.sub(r"[^a-z0-9]+", "-", prefix)
    prefix = prefix.strip("-")
    return prefix or "load"


def _set_step(step: str):
    """Update the current progress step."""
    _progress["active"] = True
    _progress["step"] = step
    _progress["steps_done"].append(step)
    _progress["error"] = ""
    logger.info(f"[progress] {step}")


def _finish_progress(result: str = "", error: str = ""):
    _progress["active"] = False
    _progress["step"] = "done" if not error else "error"
    _progress["result"] = result
    _progress["error"] = error


def get_load_test_progress() -> dict:
    """Return current orchestration progress. Called by /api/load-test-progress."""
    return dict(_progress)



# ── Active test sessions (polled by /api/test-run-live) ───────────────────────
# Keyed by session_name so multiple concurrent tests are all tracked.
_active_tests: dict = {}   # { session_name: { ...info... } }


def get_active_test_info() -> dict:
    """Return the most-recently started test (backward compat for single-test callers)."""
    if not _active_tests:
        return {"session_name": "", "session_run_no": 0, "run_no": 0,
                "num_devices": 0, "duration_mins": 0, "started_at": ""}
    return dict(sorted(_active_tests.values(),
                        key=lambda x: x.get("started_at", ""), reverse=True)[0])


def get_all_active_tests() -> list:
    """Return info for every currently tracked test session."""
    return list(_active_tests.values())


def remove_active_test(session_name: str) -> None:
    """Remove a test from the active set (called after it stops)."""
    _active_tests.pop(session_name, None)


# ── Error-recovery state ───────────────────────────────────────────────────────
# Test cases that have been disqualified due to internal errors
_bad_test_cases: set = set()   # {(suite_name, case_name), ...}

# Doppels for which we've already attempted to stop the blocking session once.
# A second lock on the same doppel means the stop didn't work → change doppel.
_stop_attempted_for: set = set()   # {doppel_name, ...}


def _parse_lock_owner(err_str: str) -> tuple:
    """Extract (session_name, session_run_no) from a Doppelio lock error message.

    Expected format:
        'Doppel:... is locked by: Test Session:<name> Session No:<N> Test No:<M>'
    Returns ("", 0) if the format does not match.
    """
    m = re.search(
        r'locked by[:\s]+Test Session:(\S+)\s+Session No:(\d+)',
        err_str, re.IGNORECASE,
    )
    if m:
        return m.group(1), int(m.group(2))
    return "", 0

# Threshold: if this many devices hit "Internal error when starting test step"
# we consider the test case broken and swap it out.
_INTERNAL_ERROR_PATTERN = "internal error when starting test step"
_ERROR_THRESHOLD_COUNT  = 10    # absolute — more than 10 errors → bad TC
_ERROR_THRESHOLD_PCT    = 0.15  # relative — >15 % of devices erroring → bad TC


def attach_to_session(session_name: str, session_run_no: int, run_no: int = 1) -> str:
    """
    Attach the live monitor to an existing running test session.

    Use this after get_running_tests() — pick a session and call this with its
    testSessionName and testSessionRunNo so the live monitor card tracks it.

    Args:
        session_name: The testSessionName of the running session.
        session_run_no: The testSessionRunNo from get_running_tests().
        run_no: The run number within the session (default 1).

    Returns:
        Confirmation that the live monitor is now tracking this session.
    """
    _active_tests[session_name] = {
        "session_name":   session_name,
        "session_run_no": session_run_no,
        "run_no":         run_no,
        "num_devices":    0,   # unknown for externally-started sessions
        "duration_mins":  0,
        "started_at":     "",
    }
    return (
        f"Live monitor attached to session '{session_name}' "
        f"(sessionRunNo={session_run_no}, runNo={run_no}). "
        "The test monitor card will now show live metrics for this session."
    )


def _get_latest_running_from_doppelio() -> tuple[str, int]:
    """
    Fallback: query Doppelio directly for running sessions and return the most recent one.
    Returns (session_name, session_run_no) or ("", 0) if none found.
    """
    try:
        from datetime import datetime as _dt
        today = _dt.utcnow()
        start = today.strftime("%m/%d/%Y 00:00")
        end   = today.strftime("%m/%d/%Y 23:59")
        import urllib.parse
        params = (
            f"startDate={urllib.parse.quote(start)}"
            f"&endDate={urllib.parse.quote(end)}"
            f"&NumberOfSessions=100&AllWkUsersFlag=false&OnlyRunningTests=true"
        )
        url = f"{_CORE_BASE}/TestRun/TestSessions?{params}"
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code in (200, 404):
            sessions = resp.json() if resp.status_code == 200 else []
            if sessions:
                # Pick the most recently started session
                latest = sorted(sessions, key=lambda s: s.get("testSessionStartedAt", ""), reverse=True)[0]
                return latest.get("testSessionName", ""), latest.get("testSessionRunNo", 0)
    except Exception as e:
        logger.warning(f"_get_latest_running_from_doppelio error: {e}")
    return "", 0


def _parse_stop_response(resp) -> str:
    """Parse the stop test response and extract the human-readable message."""
    try:
        body = resp.json()
        raw_msg = body.get("error") or body.get("message") or ""
        if body.get("statusCode", -1) == 0 or "stopping" in raw_msg.lower():
            return raw_msg or "Test stopped successfully."
        return raw_msg or "Test stop command issued."
    except Exception:
        raw = resp.text.strip().strip('"')
        m = re.match(r'^Status:\d+\s+Error:\s*', raw, re.IGNORECASE)
        return raw[m.end():].strip() if m else (raw or "Test stopped successfully.")


def _cleanup_after_stop(session_name: str) -> None:
    """Clean up tracking state after stopping a test session."""
    import threading
    from agentic_utils.tools.doppelio_tools import _poll_state, _stop_polling, _post_test_download, _FUNC_URL

    if _poll_state.get("active") and _poll_state.get("session_name") == session_name:
        doppel_name    = _poll_state.get("doppel_name", "")
        num_devices    = _poll_state.get("num_devices", 0)
        session_run_no = _poll_state.get("session_run_no", 0)
        run_no         = _poll_state.get("run_no", 0)
        if _FUNC_URL and doppel_name and num_devices > 0:
            threading.Thread(
                target=_post_test_download,
                args=(session_name, session_run_no, run_no, doppel_name, num_devices),
                daemon=True,
            ).start()

    if _poll_state.get("active"):
        _stop_polling()
    remove_active_test(session_name)


def stop_active_test() -> str:
    """Stop the most recently started running load test.

    Use this when the user says 'stop the test', 'stop the load test', or 'cancel the test'
    without specifying a session name. Checks in-memory tracking first, then queries
    Doppelio directly as fallback so it works even after a server restart.

    Returns:
        Confirmation that the test was stopped, or an error if no active test is found.
    """
    # Primary: use in-memory tracking, scoped to the current user's session
    owner = _progress.get("owner_session", "")
    if owner and _active_tests:
        owned = {k: v for k, v in _active_tests.items() if v.get("owner_session") == owner}
    else:
        owned = _active_tests

    if owned:
        latest = dict(sorted(owned.values(), key=lambda x: x.get("started_at", ""), reverse=True)[0])
    else:
        latest = {}
    session_name   = latest.get("session_name", "")
    session_run_no = latest.get("session_run_no", 0)

    # Fallback: query Doppelio directly only when in-memory is completely empty (e.g. server restart)
    if not session_name and not _active_tests:
        session_name, session_run_no = _get_latest_running_from_doppelio()

    if not session_name:
        return "No active test session found. The test may have already completed or been stopped earlier."

    url = f"{_CORE_BASE}/TestRun/{session_name}/StopTestSession?TestSessionRunNo={session_run_no}"
    try:
        resp = requests.post(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            _cleanup_after_stop(session_name)
            return f"✓ Load test stopped — session **{session_name}** (run {session_run_no}) has been stopped successfully."
        return f"ERROR stopping test (HTTP {resp.status_code}): {resp.text}"
    except Exception as e:
        logger.error(f"stop_active_test error: {e}")
        return f"ERROR: {e}"


def stop_all_tests() -> str:
    """Stop all load test sessions started by the current user in this session.

    Use this when the user says 'stop all tests', 'stop all load tests', or 'cancel all tests'.
    Only stops tests tracked in-memory for the current user — never touches other users' tests.

    Returns:
        Summary of which sessions were stopped and any errors encountered.
    """
    try:
        # Scope to the current user's session — never stop other users' tests
        owner = _progress.get("owner_session", "")
        if owner:
            to_stop = {k: v for k, v in _active_tests.items() if v.get("owner_session") == owner}
        else:
            to_stop = dict(_active_tests)

        if not to_stop:
            return _NO_ACTIVE_TESTS_MSG

        results = []
        from agentic_utils.tools.doppelio_tools import _poll_state, _stop_polling
        for sname, info in list(to_stop.items()):
            srunno   = info.get("session_run_no", 0)
            stop_url = f"{_CORE_BASE}/TestRun/{sname}/StopTestSession?TestSessionRunNo={srunno}"
            try:
                r = requests.post(stop_url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
                if r.status_code == 200:
                    results.append(f"• {sname} (run {srunno}) — stopped")
                    remove_active_test(sname)
                else:
                    results.append(f"• {sname} (run {srunno}) — ERROR HTTP {r.status_code}")
            except Exception as ex:
                results.append(f"• {sname} (run {srunno}) — ERROR {ex}")

        if _poll_state.get("active"):
            import threading
            from agentic_utils.tools.doppelio_tools import _post_test_download, _FUNC_URL
            doppel_name    = _poll_state.get("doppel_name", "")
            num_devices    = _poll_state.get("num_devices", 0)
            sname          = _poll_state.get("session_name", "")
            session_run_no = _poll_state.get("session_run_no", 0)
            run_no         = _poll_state.get("run_no", 0)
            if _FUNC_URL and doppel_name and num_devices > 0 and sname:
                threading.Thread(
                    target=_post_test_download,
                    args=(sname, session_run_no, run_no, doppel_name, num_devices),
                    daemon=True,
                ).start()
            _stop_polling()

        return f"Stopped {len(to_stop)} session(s):\n" + "\n".join(results)
    except Exception as e:
        logger.error(f"stop_all_tests error: {e}")
        return f"ERROR: {e}"


# ── Config ─────────────────────────────────────────────────────────────────────
def _resolve_sample_csv() -> str:
    """Resolve sample CSV — checks fixed repo location first, then env override, then cwd."""
    # 1. Fixed repo location: agents/digital_twin_agent/doppel_sample.csv
    #    (relative to this file: ../../agents/digital_twin_agent/doppel_sample.csv)
    repo_fixed = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "agents", "digital_twin_agent", "doppel_sample.csv")
    )
    if os.path.exists(repo_fixed):
        return repo_fixed
    raw = os.getenv("DOPPELIO_SAMPLE_CSV_PATH", "doppel_sample.csv")
    if os.path.isabs(raw):
        return raw
    # 2. Same folder as this file
    alongside = os.path.join(os.path.dirname(__file__), raw)
    if os.path.exists(alongside):
        return alongside
    # 3. cwd (the ADK dir when running normally)
    candidate = os.path.join(os.getcwd(), raw)
    if os.path.exists(candidate):
        return candidate
    # 4. Fallback: repo_fixed path so error message points to expected location
    return repo_fixed

# Resolved lazily in _generate_csv so cwd changes during startup don't matter
_CSV_ID_COL = os.getenv("DOPPELIO_CSV_ID_COLUMN", "device_id").lstrip("$")
# S5443: SECURITY HOTSPOT REVIEWED - SAFE CONFIGURATION
# /tmp directory usage is safe for temporary CSV uploads in containerized environment.
# Files are generated at runtime, cleaned up after upload, and container is ephemeral.
_CSV_UPLOAD_DIR = os.getenv("DOPPELIO_CSV_UPLOAD_DIR", "/tmp/doppelio_uploads")

_MAX_MODEL_RETRIES = 5


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_id_pattern(value: str):
    """
    Split an ID value like 'device_manager_111001' into (base, start_int, pad_width).
    Returns (base, start_int, pad_width) or (value + '_', 1, 6) as fallback.
    """
    m = re.match(r'^(.*?)(\d+)$', value)
    if m:
        base = m.group(1)
        num_str = m.group(2)
        return base, int(num_str), len(num_str)
    return value + '_', 1, 6


def _generate_csv(num_devices: int, start_offset: int = 0) -> str:
    """Read sample CSV and generate a new one with num_devices rows.

    start_offset shifts device IDs so appended batches don't collide with existing ones.
    """
    sample_path = os.path.normpath(_resolve_sample_csv())
    if not os.path.exists(sample_path):
        raise FileNotFoundError(
            f"Sample CSV not found at '{sample_path}'. "
            "Set DOPPELIO_SAMPLE_CSV_PATH in .env to the correct path."
        )

    with open(sample_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        sample_rows = [row for row in reader]

    if not sample_rows:
        raise ValueError("Sample CSV has no data rows.")

    if _CSV_ID_COL not in fieldnames:
        raise ValueError(
            f"Column '{_CSV_ID_COL}' not found in sample CSV. "
            f"Available columns: {list(fieldnames)}. "
            "Set DOPPELIO_CSV_ID_COLUMN in .env."
        )

    base, start_num, pad_width = _parse_id_pattern(sample_rows[0][_CSV_ID_COL])

    os.makedirs(_CSV_UPLOAD_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = os.path.join(_CSV_UPLOAD_DIR, f"loadtest_{num_devices}devices_{ts}.csv")

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i in range(num_devices):
            template = dict(sample_rows[i % len(sample_rows)])
            template[_CSV_ID_COL] = f"{base}{start_num + start_offset + i:{pad_width}d}"
            writer.writerow(template)

    logger.info(f"Generated CSV: {out_path} ({num_devices} rows)")
    return out_path


def _upload_csv(file_path: str) -> str:
    """Upload CSV to Doppelio, return uploaded filename token."""
    url = f"{_WEB_BASE}/Doppel/Addcsvfile"
    with open(file_path, "rb") as f:
        resp = requests.post(
            url, headers=_headers(),
            files={"file": (os.path.basename(file_path), f, "text/csv")},
            timeout=30, verify=_VERIFY_SSL,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"CSV upload failed (HTTP {resp.status_code}): {resp.text}")
    token = resp.text.strip().strip('"')
    logger.info(f"CSV uploaded, token: {token}")
    return token


def _get_models() -> list:
    """Return list of doppel model names."""
    resp = requests.get(f"{_WEB_BASE}/DoppelModel/", headers=_headers(), timeout=30, verify=_VERIFY_SSL)
    if resp.status_code != 200:
        raise RuntimeError(f"get_doppel_models failed (HTTP {resp.status_code}): {resp.text}")
    return [m["name"] for m in resp.json()]


def _create_doppels(uploaded_filename: str, doppel_name: str, model_name: str) -> int:
    """Create load doppels. Returns count or raises on error."""
    url = (
        f"{_WEB_BASE}/Doppel/AddBulk"
        f"?filenameuploaded={uploaded_filename}&certType=0&columnseparator=,"
    )
    payload = {
        "doppelName": doppel_name,
        "doppelModelName": model_name,
        "forWhichTestEnvironment": "default",
        "deviceId": f"${_CSV_ID_COL}",
        "pfxPassword": "",
        "doppelGroupName": doppel_name,
        "isDoppelForLoadTest": True,
        "accessToken": "",
        "attributes": [],
    }
    resp = requests.post(url, headers=_json_headers(), json=payload, timeout=30, verify=_VERIFY_SSL)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
    data = resp.json()
    msg = data.get("message", "")

    # Doppelio async responses — poll until doppels appear (up to 15 min for provisioning).
    _async_phrases = ("upload in progress", "provisioning new capacity", "provisioning")
    if any(p in msg.lower() for p in _async_phrases):
        is_provisioning = "provisioning" in msg.lower()
        label = "Provisioning capacity" if is_provisioning else "Upload"
        logger.info(f"[Doppelio] {label} in progress for '{doppel_name}', polling up to 15 min...")
        max_attempts = 90  # 90 × 10s = 15 min
        for attempt in range(max_attempts):
            time.sleep(10)
            count = _verify_doppels(model_name)
            elapsed = (attempt + 1) * 10
            logger.info(f"  verify attempt {attempt + 1}/{max_attempts} ({elapsed}s): {count} doppels")
            if count > 0:
                return count
            # Update progress step text so the UI shows a live countdown
            _progress["step"] = (
                f"Provisioning {model_name} capacity… {elapsed}s elapsed "
                f"(Doppelio timeout: 15 min)"
            )
        raise RuntimeError(
            f"Doppels not found after 15 min provisioning wait for model '{model_name}'."
        )

    # Treat genuinely bad messages as errors — but exclude informational phrases
    # like "Errors (if any)" that Doppelio includes in success messages.
    msg_lower = msg.lower()
    is_error = (
        msg_lower.startswith("error")
        or "upload failed" in msg_lower
        or "invalid" in msg_lower
    )
    if is_error:
        raise RuntimeError(msg)

    return data.get("count", 0)


def _verify_doppels(model_name: str) -> int:
    """Return number of load doppels found for model."""
    url = (
        f"{_WEB_BASE}/Doppel/{model_name}/model"
        f"?loadTestDoppelsFlag=true&ForWhichTestEnvironment=default"
    )
    resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
    if resp.status_code != 200:
        return 0
    clones = [d for d in resp.json() if d.get("isDoppelForLoadTest")]
    return len(clones)


def _get_all_load_doppels() -> list:
    """Return flat list of {name, loadCount, model} for all model-prefixed load doppels."""
    url = f"{_WEB_BASE}/Doppel/GetDoppelsNew"
    resp = requests.get(url, headers=_headers(), timeout=20, verify=_VERIFY_SSL)
    resp.raise_for_status()
    result = []
    for model_group in resp.json():
        model = model_group.get("dopModel", "")
        prefix = _model_prefix(model)
        for env in model_group.get("dopLst", []):
            for d in env.get("doppels", []):
                name = d.get("name", "")
                count = d.get("loadCount", 0)
                if name.startswith(f"{prefix}-") and count > 0:
                    result.append({"name": name, "loadCount": count, "model": model})
    return result


def _find_best_doppel(num_devices: int, model_name: str, exclude: set = None) -> dict:
    """Find the best existing doppel to reuse or extend for model_name.

    Returns:
    - Smallest with loadCount >= num_devices → reuse (loadFactor limits actual connections)
    - Largest with loadCount < num_devices → extend by delta
    - None if no model-prefixed doppels exist for this model
    """
    try:
        all_doppels = _get_all_load_doppels()
    except Exception as e:
        logger.warning(f"Could not fetch existing doppels: {e}")
        return {}
    candidates = [
        d for d in all_doppels
        if d["model"] == model_name and d["name"] not in (exclude or set())
    ]
    if not candidates:
        return {}
    sufficient = [d for d in candidates if d["loadCount"] >= num_devices]
    if sufficient:
        return min(sufficient, key=lambda d: d["loadCount"])
    return max(candidates, key=lambda d: d["loadCount"])


def _append_doppels(doppel_name: str, model_name: str,
                    existing_count: int, delta: int) -> int:
    """Append delta new devices to an existing doppel via updateBulk appendMode."""
    csv_path = _generate_csv(delta, start_offset=existing_count)
    token = _upload_csv(csv_path)

    url = (
        f"{_WEB_BASE}/Doppel/updateBulk"
        f"?filenameuploaded={token}&appendMode=true&columnseparator=,&certType=0"
    )
    payload = {
        "doppelName": doppel_name,
        "doppelModelName": model_name,
        "forWhichTestEnvironment": "default",
        "deviceId": f"${_CSV_ID_COL}",
        "pfxPassword": "",
        "doppelGroupName": doppel_name,
        "isDoppelForLoadTest": True,
        "accessToken": "",
        "attributes": [],
    }
    resp = requests.put(url, headers=_json_headers(), json=payload, timeout=30, verify=_VERIFY_SSL)
    if resp.status_code != 200:
        raise RuntimeError(f"updateBulk failed (HTTP {resp.status_code}): {resp.text}")
    data = resp.json()
    msg = data.get("message", "")

    _async_phrases = ("upload in progress", "provisioning new capacity", "provisioning")
    if any(p in msg.lower() for p in _async_phrases):
        logger.info(f"[Doppelio] Append in progress for '{doppel_name}', polling up to 15 min...")
        for attempt in range(90):
            time.sleep(10)
            elapsed = (attempt + 1) * 10
            _progress["step"] = f"Appending {delta} devices to '{doppel_name}'… {elapsed}s elapsed"
            count = _verify_doppels(model_name)
            logger.info(f"  append verify {attempt + 1}/90 ({elapsed}s): {count} total doppels")
            if count >= existing_count + delta:
                return count
        raise RuntimeError(f"Append not confirmed after 15 min for '{doppel_name}'.")

    msg_lower = msg.lower()
    if msg_lower.startswith("error") or "failed" in msg_lower or "invalid" in msg_lower:
        raise RuntimeError(msg)

    return data.get("count", existing_count + delta)


def _has_test_steps(suite_name: str, case_name: str) -> bool:
    """Return True if the test case has at least one step defined."""
    url = f"{_WEB_BASE}/TestCase/{suite_name}/{case_name}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=15, verify=_VERIFY_SSL)
        if resp.status_code != 200:
            return False
        data = resp.json()
        # Steps may be under 'testSteps', 'steps', or similar key
        steps = (data.get("teststeps") or data.get("testSteps")
                 or data.get("steps") or data.get("testStep") or [])
        return len(steps) > 0
    except Exception:
        return False


def _update_step(url: str, step: dict, step_name: str, update_type: str) -> bool:
    """Update a test step via API. Returns True if successful."""
    try:
        r = requests.put(url, headers=_json_headers(), json=step, timeout=30, verify=_VERIFY_SSL)
        if r.status_code == 200:
            logger.info(f"Step '{step_name}': {update_type}")
            return True
        logger.warning(f"Step '{step_name}' {update_type} failed (HTTP {r.status_code}): {r.text}")
    except Exception as e:
        logger.warning(f"Step '{step_name}' {update_type} error: {e}")
    return False


def _handle_disconnect_step(step: dict, put_url: str, step_name: str) -> None:
    """Configure disconnect-style steps to 1 iteration for auto-completion."""
    step["runParams"]["completionType"]    = "iteration"
    step["runParams"]["numIterations"]     = 1
    step["runParams"]["durationMins"]      = 0
    step["runParams"]["stopByCommandMode"] = False
    _update_step(put_url, step, step_name, "stopByCommandMode disabled, set to 1 iteration")


def _handle_time_step(step: dict, put_url: str, step_name: str, duration_mins: float) -> bool:
    """Configure time-based step duration. Returns True if successful."""
    step["runParams"]["durationMins"]  = duration_mins
    step["runParams"]["numIterations"] = 0
    return _update_step(put_url, step, step_name, f"durationMins set to {duration_mins}")


def _apply_fallback_duration(step_info: tuple, duration_mins: float) -> None:
    """Convert first non-disconnect step to time-based as fallback."""
    step, put_url, step_name = step_info
    step["runParams"]["completionType"]  = "time"
    step["runParams"]["durationMins"]    = duration_mins
    step["runParams"]["numIterations"]   = 0
    logger.warning(
        f"Step '{step_name}': no time-based step found — converting to time-based "
        f"with durationMins={duration_mins} to enforce original test duration."
    )
    _update_step(put_url, step, step_name, f"fallback duration set to {duration_mins}")


def _configure_test_steps(suite_name: str, case_name: str, duration_mins: float) -> None:
    """
    Fetch all steps for the test case and:
    - Set the time-based step (completionType=time) duration to duration_mins
    - Set any stopByCommandMode step to 1 iteration so the test auto-completes
    Logs warnings but does not raise — a misconfigured step should not block the test.
    """
    url = f"{_WEB_BASE}/TestStep/{suite_name}/{case_name}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code != 200:
            logger.warning(f"_configure_test_steps: could not fetch steps (HTTP {resp.status_code})")
            return
        steps = resp.json()
    except Exception as e:
        logger.warning(f"_configure_test_steps: fetch error: {e}")
        return

    found_time_step = False
    first_non_disconnect = None

    for step in steps:
        step_name  = step.get("stepName", "")
        model_name = step.get("doppelModelName", "")
        rp         = step.get("runParams", {})
        put_url    = f"{_WEB_BASE}/TestStep/{suite_name}/{case_name}/{step_name}?NewDoppelModel={model_name}"

        if rp.get("stopByCommandMode"):
            _handle_disconnect_step(step, put_url, step_name)
        elif rp.get("completionType") == "time":
            if _handle_time_step(step, put_url, step_name, duration_mins):
                found_time_step = True
        else:
            if first_non_disconnect is None:
                first_non_disconnect = (step, put_url, step_name)

    if not found_time_step and first_non_disconnect is not None:
        _apply_fallback_duration(first_non_disconnect, duration_mins)


def _get_test_suite(exclude: set = None) -> tuple:
    """Return (suite_name, test_case_name) for the first suite/case that has steps.

    Args:
        exclude: Optional set of (suite_name, case_name) tuples to skip.
                 Used to avoid reusing test cases that caused internal errors.
    """
    resp = requests.get(f"{_WEB_BASE}/TestSuite/", headers=_headers(), timeout=30, verify=_VERIFY_SSL)
    if resp.status_code != 200:
        raise RuntimeError(f"get_test_suites failed (HTTP {resp.status_code}): {resp.text}")
    for suite in resp.json():
        suite_name = suite["name"]
        for case in suite.get("testCases", []):
            case_name = case["name"]
            if exclude and (suite_name, case_name) in exclude:
                logger.info(f"Skipping suite '{suite_name}' / case '{case_name}' (excluded due to prior errors).")
                continue
            if _has_test_steps(suite_name, case_name):
                logger.info(f"Selected suite '{suite_name}' / case '{case_name}' (has steps).")
                return suite_name, case_name
    suffix = " (all candidates excluded due to prior errors)" if exclude else ""
    raise RuntimeError(
        f"No test suites with test cases that have steps found{suffix}. "
        "Please add steps to a test case in Doppelio first."
    )


def _run_test(session_name: str, suite: str, case: str, doppel: str,
              connect_sec: int, idle_mins: int, num_devices: int = 0) -> dict:
    """Start the test run. Returns {testSessionRunNo, runNo, status}."""
    # Scale ramp profile so all devices connect in ~2 min regardless of count.
    # stepSize = ceil(num_devices / 30) so ~30 ramp steps total.
    # rampIntervalSec = 5s per step → ~2.5 min ramp for any device count.
    # Doppelio constraint: stepSize MUST be < loadFactor.
    # loadFactor also acts as the max concurrent device scale — must be >= num_devices.
    step_size = max(10, (num_devices + 29) // 30) if num_devices else 10
    load_factor = max(num_devices, step_size + 1)
    url = f"{_CORE_BASE}/TestRun/StartSingleTest"
    payload = {
        "testSessionName": session_name,
        "testSessionFriendlyName": session_name,
        "testEnvironment": "default",
        "closeSessionFlag": True,
        "sessionIdleTimeOutMins": idle_mins,
        "loadFactor": load_factor,
        "connectTimespanInSec": connect_sec,
        "rampingProfile": {"rampIntervalSec": 5, "stepSize": step_size},
        "processDataBetweenTests": False,
        "samplerCount": 0,
        "hasNetworkProfileFaults": False,
        "capturePerDoppelMetrics": True,
        "userDefinedDoppelWeight": 1,
        "testExecParams": {
            "tsName": suite,
            "tcName": case,
            "fastforwardMode": False,
            "runFromNegativeDeltaDays": 0,
            "runFromAbsoluteTimeHours": 0,
            "runFromAbsoluteTimeMins": 0,
            "numTimesToRunTestCase": 1,
            "loadFactor": load_factor,
            "commonDoppelNames": [doppel],
        },
    }
    resp = requests.post(url, headers=_json_headers(), json=payload, timeout=30, verify=_VERIFY_SSL)
    if resp.status_code != 200:
        raise RuntimeError(f"StartSingleTest HTTP {resp.status_code}: {resp.text}")
    data = resp.json()
    # Surface API-level error messages
    err = data.get("message", "") or data.get("error", "")
    if err and ("error" in err.lower() or "cannot" in err.lower() or "invalid" in err.lower()):
        raise RuntimeError(err)

    # ── Post-start Fault check ─────────────────────────────────────────────────
    # StartSingleTest may return HTTP 200 but the session can immediately transition
    # to "Fault" (e.g. doppel locked by another session).  Wait briefly then check
    # the /Details endpoint so the caller gets a proper RuntimeError to handle.
    session_run_no = data.get("testSessionRunNo", 1)
    time.sleep(3)
    details_url = (
        f"{_CORE_BASE}/TestRun/{session_name}/Details"
        f"?TestSessionNo={session_run_no}"
    )
    try:
        det_resp = requests.get(details_url, headers=_headers(), timeout=15, verify=_VERIFY_SSL)
        if det_resp.status_code == 200:
            det = det_resp.json()
            if det.get("testSessionStatus") == "Fault":
                fault_msg = det.get("testSessionMessage", "Session faulted immediately after start.")
                logger.warning(f"_run_test: session '{session_name}' faulted — {fault_msg}")
                raise RuntimeError(fault_msg)
    except RuntimeError:
        raise  # re-raise fault errors
    except Exception as det_ex:
        logger.warning(f"_run_test: /Details check failed (non-fatal): {det_ex}")

    return data


# ── Error monitoring helpers ───────────────────────────────────────────────────

def _poll_test_progress(session_name: str, session_run_no: int, run_no: int) -> dict:
    """Fetch current TestProgress from Doppelio. Returns {} on failure."""
    url = (
        f"{_CORE_BASE}/TestRun/{session_name}/TestProgress"
        f"?TestSessionRunNo={session_run_no}&TestRunNo={run_no}"
    )
    try:
        resp = requests.get(url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.warning(f"_poll_test_progress error: {e}")
    return {}


def _count_internal_errors(progress: dict) -> int:
    """Count errors matching the internal test-step error pattern."""
    errors = progress.get("testRunErrors") or []
    return sum(
        1 for e in errors
        if _INTERNAL_ERROR_PATTERN in e.get("message", "").lower()
    )


def _handle_clean_completion(session_name: str, error_count: int, saw_stopped: bool) -> tuple:
    """Handle test completion checks. Returns (should_break, new_saw_stopped)."""
    if not saw_stopped:
        logger.info(
            f"[error-monitor] {session_name} stopped (errors={error_count}). "
            "Doing one final error check..."
        )
        time.sleep(5)
        return (False, True)
    logger.info(f"[error-monitor] {session_name} completed normally (errors={error_count}).")
    return (True, saw_stopped)


def _persist_completed_duration(session_name: str, session_run_no: int, run_no: int) -> None:
    """Fetch completedDurationInSecs from Doppelio after test ends and write to DB. Non-fatal."""
    try:
        prog = _get_test_progress_raw(session_name, session_run_no, run_no)
        if prog is None:
            logger.warning("[duration-persist] No progress data for '%s'", session_name)
            return
        secs = prog.get("completedDurationInSecs")
        if secs is None:
            logger.warning("[duration-persist] completedDurationInSecs absent for '%s'", session_name)
            return
        result = update_session_completed_duration(session_name, int(secs))
        logger.info("[duration-persist] '%s': %s", session_name, result)
    except Exception as exc:
        logger.warning("[duration-persist] Non-fatal for '%s': %s", session_name, exc)


def _stop_failing_test(session_name: str, session_run_no: int, status: str, is_running: bool) -> None:
    """Stop a test that has exceeded error thresholds."""
    _terminal = ("Stopped", "Completed", "Failed", "Error", "Stopping")
    if status not in _terminal and is_running:
        stop_url = f"{_CORE_BASE}/TestRun/{session_name}/StopTestSession?TestSessionRunNo={session_run_no}"
        try:
            requests.post(stop_url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
            logger.info(f"[error-monitor] Stop issued for {session_name}.")
        except Exception as ex:
            logger.warning(f"[error-monitor] Stop request failed: {ex}")
    remove_active_test(session_name)
    time.sleep(10)


def _prepare_replacement_test(num_devices: int, duration_mins: int) -> tuple:
    """Find alternative test case and configure steps. Returns (next_suite, next_case, new_session)."""
    next_suite, next_case = _get_test_suite(exclude=_bad_test_cases)
    try:
        _configure_test_steps(next_suite, next_case, duration_mins)
    except Exception as ex:
        logger.warning(f"[error-monitor] _configure_test_steps error for '{next_case}': {ex}")
    ts = datetime.utcnow().strftime("%Y%m%d%H%M")
    new_session = f"load-{num_devices}d-{duration_mins}m-{ts}"
    return (next_suite, next_case, new_session)


def _select_doppel_for_retry(
    doppel_name: str, num_devices: int, model_name: str,
    locked_in_recovery: set, retry_idx: int,
) -> str | None:
    """Select which doppel to use for a retry. Returns doppel name or None if unavailable."""
    if retry_idx == 0:
        return doppel_name
    best = _find_best_doppel(num_devices, model_name, exclude=locked_in_recovery) or {}
    if not best:
        logger.error("[error-monitor] No alternative doppel available after lock error.")
        _finish_progress(error=f"Recovery failed: doppel '{doppel_name}' locked and no alternative found.")
        return None
    name = best.get("name")
    if not name:
        logger.error("[error-monitor] Alternative doppel is missing 'name' field; recovery failed.")
        _finish_progress(error=f"Recovery failed: doppel '{doppel_name}' locked and alternative has no valid name.")
        return None
    _set_step(f"Retrying with doppel '{name}' (previous doppel locked)...")
    return name


def _handle_locked_doppel(try_doppel: str, ex: RuntimeError, locked_in_recovery: set) -> None:
    """Record a locked doppel, stop the blocking session, and wait for release."""
    locked_in_recovery.add(try_doppel)
    blocking_session, blocking_run_no = _parse_lock_owner(str(ex))
    if blocking_session:
        blocker_url = (
            f"{_CORE_BASE}/TestRun/{blocking_session}"
            f"/StopTestSession?TestSessionRunNo={blocking_run_no}"
        )
        try:
            requests.post(blocker_url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
            logger.info(f"[error-monitor] Stopped blocker '{blocking_session}'.")
        except Exception as se:
            logger.warning(f"[error-monitor] Could not stop blocker: {se}")
    logger.warning(f"[error-monitor] Doppel '{try_doppel}' locked. Switching doppel...")
    time.sleep(5)


def _retry_with_locked_doppel_recovery(
    new_session: str, next_suite: str, next_case: str, doppel_name: str,
    connect_sec: int, idle_mins: int, num_devices: int, model_name: str,
) -> tuple:
    """Retry test with automatic doppel lock recovery. Returns (result, updated_doppel_name)."""
    locked_in_recovery: set = set()
    for _retry in range(3):
        try_doppel = _select_doppel_for_retry(
            doppel_name, num_devices, model_name, locked_in_recovery, _retry
        )
        if try_doppel is None:
            return (None, doppel_name)
        try:
            result = _run_test(new_session, next_suite, next_case, try_doppel, connect_sec, idle_mins, num_devices)
            return (result, try_doppel)
        except RuntimeError as ex:
            is_lock = "locked by" in str(ex).lower() or "initialisation failed" in str(ex).lower()
            if not is_lock:
                logger.error(f"[error-monitor] _run_test failed (non-lock): {ex}")
                _finish_progress(error=f"Retry with suite='{next_suite}' / case='{next_case}' failed: {ex}")
                return (None, doppel_name)
            _handle_locked_doppel(try_doppel, ex, locked_in_recovery)
    return (None, doppel_name)


def _check_error_threshold(error_count: int, num_devices: int) -> bool:
    """Check if error count exceeds configured thresholds."""
    return (
        error_count >= _ERROR_THRESHOLD_COUNT
        or (num_devices > 0 and error_count / num_devices >= _ERROR_THRESHOLD_PCT)
    )


def _initiate_test_recovery(session_name: str, session_run_no: int, status: str, is_running: bool,
                            suite_name: str, case_name: str, error_count: int) -> None:
    """Stop failing test and mark test case as bad."""
    logger.warning(
        f"[error-monitor] {session_name}: {error_count} internal-error(s) on "
        f"'{case_name}' (threshold={_ERROR_THRESHOLD_COUNT}). Stopping & switching TC."
    )
    _bad_test_cases.add((suite_name, case_name))
    _stop_failing_test(session_name, session_run_no, status, is_running)


def _start_replacement_test(
    new_session: str, next_suite: str, next_case: str, doppel_name: str,
    connect_sec: int, idle_mins: int, num_devices: int, model_name: str,
    duration_mins: int
) -> tuple:
    """Start replacement test and update active tests tracking."""
    result, doppel_name = _retry_with_locked_doppel_recovery(
        new_session, next_suite, next_case, doppel_name,
        connect_sec, idle_mins, num_devices, model_name,
    )

    if result is None:
        _active_tests.clear()
        return None, None, None, None, None

    new_session_run_no = result.get("testSessionRunNo", 1)
    new_run_no = result.get("runNo", 1)

    _active_tests[new_session] = {
        "session_name": new_session,
        "session_run_no": new_session_run_no,
        "run_no": new_run_no,
        "num_devices": num_devices,
        "duration_mins": duration_mins,
        "started_at": datetime.utcnow().isoformat(),
        "doppel_name": doppel_name,
        "owner_session": _progress.get("owner_session", ""),
    }

    logger.info(
        f"[error-monitor] Replacement test started: {new_session} "
        f"(suite={next_suite}, case={next_case}, doppel={doppel_name})"
    )

    return new_session, new_session_run_no, new_run_no, next_suite, next_case


def _poll_and_validate_progress(
    session_name: str, session_run_no: int, run_no: int,
    empty_polls: int, max_empty_polls: int
) -> tuple:
    """Poll test progress and validate response. Returns (progress, new_empty_polls, should_break)."""
    progress = _poll_test_progress(session_name, session_run_no, run_no)
    if not progress:
        empty_polls += 1
        if empty_polls >= max_empty_polls:
            logger.warning(
                f"[error-monitor] {session_name}: {empty_polls} consecutive "
                "empty progress responses — exiting monitor."
            )
            return None, empty_polls, True
        return None, empty_polls, False
    return progress, 0, False


def _handle_test_status_check(
    progress: dict, num_devices: int, session_name: str, error_count: int, saw_stopped: bool
) -> tuple:
    """Check test status and handle normal completion. Returns (should_continue, should_break, new_saw_stopped)."""
    is_running = progress.get("isTestRunning", True)
    exceeds_threshold = _check_error_threshold(error_count, num_devices)
    
    if not exceeds_threshold:
        if not is_running:
            should_break, saw_stopped = _handle_clean_completion(session_name, error_count, saw_stopped)
            return False, should_break, saw_stopped
        return True, False, saw_stopped
    
    return False, False, saw_stopped


def _monitor_and_recover_test(
    session_name: str, session_run_no: int, run_no: int,
    suite_name: str, case_name: str,
    doppel_name: str, num_devices: int, duration_mins: int,
    connect_sec: int, idle_mins: int,
    model_name: str = "",
) -> None:
    """
    Background thread: polls TestProgress every 15 s.

    Important: does NOT rely on `_active_tests` membership to decide whether to
    keep running.  The /api/test-run-live endpoint removes a session from
    _active_tests as soon as isTestRunning=False — but Doppelio only populates
    testRunErrors *after* the test ends, so we must do at least one post-stop
    poll to catch those errors before deciding to exit.

    If `_INTERNAL_ERROR_PATTERN` errors exceed `_ERROR_THRESHOLD_COUNT` (or
    `_ERROR_THRESHOLD_PCT` % of devices), the current test is stopped, the
    failing (suite, case) pair is added to `_bad_test_cases`, and the test is
    restarted with the next available suite/case.  The thread then continues
    monitoring the replacement test using the updated identifiers.
    """
    poll_interval  = 15
    max_empty_polls = 3
    empty_polls     = 0
    saw_stopped     = False

    logger.info(f"[error-monitor] Watching {session_name} (suite={suite_name}, case={case_name})")

    _completed_normally = False

    while True:
        time.sleep(poll_interval)

        progress, empty_polls, should_break = _poll_and_validate_progress(
            session_name, session_run_no, run_no, empty_polls, max_empty_polls
        )
        if should_break:
            break
        if progress is None:
            continue

        status = progress.get("testRunStatus", "")
        error_count = _count_internal_errors(progress)

        should_continue, should_break, saw_stopped = _handle_test_status_check(
            progress, num_devices, session_name, error_count, saw_stopped
        )
        if should_break:
            _completed_normally = True
            break
        if should_continue:
            continue

        # Recovery path
        is_running = progress.get("isTestRunning", True)
        _initiate_test_recovery(session_name, session_run_no, status, is_running,
                               suite_name, case_name, error_count)

        try:
            next_suite, next_case, new_session = _prepare_replacement_test(num_devices, duration_mins)
        except RuntimeError as ex:
            logger.error(f"[error-monitor] No alternative test case available: {ex}")
            _finish_progress(
                error=(
                    f"Test stopped: '{case_name}' produced {error_count} internal errors "
                    f"and no alternative test case is available."
                )
            )
            break

        _set_step(
            f"Switching to suite='{next_suite}' / case='{next_case}' "
            f"('{case_name}' had {error_count} internal errors) — restarting test..."
        )

        new_session, new_session_run_no, new_run_no, next_suite, next_case = _start_replacement_test(
            new_session, next_suite, next_case, doppel_name,
            connect_sec, idle_mins, num_devices, model_name, duration_mins
        )

        if new_session is None:
            break

        # Register the replacement test's devices so the new suite/case is captured.
        db_result = register_load_test_devices(
            doppel_name=doppel_name,
            model_name=model_name,
            suite_name=next_suite,
            num_devices=num_devices,
            test_run_name=new_session,
            total_duration_secs=duration_mins * 60,
        )
        logger.info("DB registration (replacement test): %s", db_result)

        # Continue monitoring the replacement test
        session_name    = new_session
        session_run_no  = new_session_run_no
        run_no          = new_run_no
        suite_name      = next_suite
        case_name       = next_case
        saw_stopped     = False
        empty_polls     = 0

    if _completed_normally:
        _persist_completed_duration(session_name, session_run_no, run_no)


# ── Main orchestration tool ────────────────────────────────────────────────────

def _handle_doppel_reuse(best: dict, log: list) -> tuple:
    """Handle reuse of existing doppel. Returns (doppel_name, create_needed)."""
    doppel_name = best["name"]
    log.append(f"Reusing '{doppel_name}' ({best['loadCount']} devices) — skipping create.")
    _set_step(f"Reusing existing doppel '{doppel_name}'...")
    return (doppel_name, False)


def _handle_doppel_extend(best: dict, num_devices: int, model: str, ts: str, attempt: int, log: list) -> tuple:
    """Handle extending existing doppel. Returns (doppel_name, create_needed)."""
    delta = num_devices - best["loadCount"]
    doppel_name = best["name"]
    _set_step(f"Extending '{doppel_name}' by {delta} devices (have {best['loadCount']}, need {num_devices})...")
    try:
        new_count = _append_doppels(doppel_name, model, best["loadCount"], delta)
        log.append(f"Extended '{doppel_name}' to {new_count} devices.")
        return (doppel_name, False)
    except RuntimeError as e:
        log.append(f"Extend failed for '{model}': {e}. Creating new doppels instead...")
        return (f"{_model_prefix(model)}-{num_devices}d-{ts}-m{attempt}", True)


def _create_fresh_doppels(num_devices: int, model: str, doppel_name: str, log: list) -> tuple:
    """Create fresh doppels via CSV + upload + create + verify. Returns (success, error_msg)."""
    _set_step(f"Generating CSV with {num_devices} devices...")
    try:
        csv_path = _generate_csv(num_devices)
        log.append(f"CSV ready: {os.path.basename(csv_path)}")
    except (FileNotFoundError, ValueError) as e:
        return (False, f"CSV generation failed: {e}. Trying next model...")

    _set_step(f"Uploading CSV to Doppelio (model '{model}')...")
    try:
        token = _upload_csv(csv_path)
    except RuntimeError as e:
        return (False, f"CSV upload failed: {e}. Trying next model...")

    _set_step(f"Creating {num_devices} doppels with model '{model}'...")
    try:
        count = _create_doppels(token, doppel_name, model)
        log.append(f"Created {count} doppels with model '{model}'.")
    except RuntimeError as e:
        return (False, f"Create doppels failed for '{model}': {e}. Trying next model...")

    _set_step(f"Verifying doppels for model '{model}'...")
    verified = _verify_doppels(model)
    log.append(f"Verified {verified} load doppels.")
    return (True, "")


def _handle_test_run_errors(
    e: RuntimeError, model: str, doppel_name: str, log: list,
    failed_models: set, locked_doppels: set
) -> tuple:
    """Handle test run errors. Returns (should_advance_model, should_continue, should_raise)."""
    err_str = str(e)
    if "does not define doppel" in err_str.lower() or "doppel to use" in err_str.lower():
        log.append(f"Model '{model}' incompatible with test steps: {e}. Trying next model...")
        failed_models.add(model)
        return (True, False, False)

    if "locked by" in err_str.lower() or '"statuscode":19' in err_str.lower() or "statuscode\":19" in err_str.lower():
        blocking_session, blocking_run_no = _parse_lock_owner(err_str)
        already_tried_stop = doppel_name in _stop_attempted_for

        if blocking_session and not already_tried_stop:
            _stop_attempted_for.add(doppel_name)
            _set_step(f"Stopping blocking session '{blocking_session}' (locked doppel '{doppel_name}')...")
            stop_url = f"{_CORE_BASE}/TestRun/{blocking_session}/StopTestSession?TestSessionRunNo={blocking_run_no}"
            try:
                requests.post(stop_url, headers=_headers(), timeout=30, verify=_VERIFY_SSL)
                log.append(f"Stopped blocking session '{blocking_session}'. Retrying doppel '{doppel_name}'...")
            except Exception as stop_ex:
                log.append(f"Stop attempt for '{blocking_session}' failed: {stop_ex}. Will try different doppel.")
            time.sleep(5)
            return (False, True, False)

        locked_doppels.add(doppel_name)
        log.append(f"Doppel '{doppel_name}' still locked after stop attempt. Trying a different doppel for '{model}'...")
        return (False, True, False)

    return (False, False, True)


def _start_monitoring_and_telemetry(
    session_name: str, session_run_no: int, run_no: int,
    suite_name: str, case_name: str, doppel_name: str,
    num_devices: int, duration_mins: int, connect_sec: int,
    idle_mins: int, chosen_model: str, log: list
) -> None:
    """Start error-monitoring thread and telemetry monitor."""
    monitor_thread = threading.Thread(
        target=_monitor_and_recover_test,
        args=(
            session_name, session_run_no, run_no,
            suite_name, case_name,
            doppel_name, num_devices, duration_mins,
            connect_sec, idle_mins,
            chosen_model,
        ),
        daemon=True,
        name=f"error-monitor-{session_name}",
    )
    monitor_thread.start()
    logger.info(f"Error-monitor thread started for {session_name}.")

    if _FUNC_URL:
        _set_step("Starting post-test telemetry monitor...")
        poll_result = start_metadata_polling(
            session_name, session_run_no, run_no,
            doppel_name=doppel_name,
            num_devices=num_devices,
        )
        log.append(poll_result)
    else:
        logger.info("DOPPELIO_FUNCTION_APP_ENDPOINT not set — skipping telemetry monitor.")


def _initialize_load_test_progress() -> None:
    """Initialize progress tracking and clear recovery state."""
    _progress.update({
        "active": True, "step": "", "steps_done": [], "error": "", "result": "",
        "infra_status": "checking",
    })
    _stop_attempted_for.clear()


def _setup_test_configuration(num_devices: int, duration_mins: int, ts: str, suite_name: str, case_name: str, log: list) -> tuple:
    """Configure test parameters and update test steps."""
    duration_mins = int(duration_mins)
    session_name = f"load-{num_devices}d-{duration_mins}m-{ts}"
    connect_sec = max(60, min(300, num_devices // 100))
    idle_mins = duration_mins + 15

    _set_step(f"Configuring test steps for {duration_mins} min duration...")
    _configure_test_steps(suite_name, case_name, duration_mins)
    log.append(f"Test steps configured: duration={duration_mins} min, disconnect set to 1 iteration.")

    return session_name, connect_sec, idle_mins, duration_mins


def _try_create_doppels(num_devices: int, model: str, ts: str, attempt: int, locked_doppels: set, log: list) -> tuple:
    """Try to find or create doppels for the given model and device count."""
    doppel_name = f"{_model_prefix(model)}-{num_devices}d-{ts}-m{attempt}"
    _set_step(f"Checking existing doppels for model '{model}'...")
    best = _find_best_doppel(num_devices, model, exclude=locked_doppels)
    create_needed = False

    if best and best["loadCount"] >= num_devices:
        doppel_name, create_needed = _handle_doppel_reuse(best, log)
    elif best:
        doppel_name, create_needed = _handle_doppel_extend(best, num_devices, model, ts, attempt, log)
    else:
        if not best and locked_doppels:
            log.append(f"No unlocked doppels available for model '{model}'. Trying next model...")
            return None, None, True  # Signal to try next model
        create_needed = True

    if create_needed:
        success, error_msg = _create_fresh_doppels(num_devices, model, doppel_name, log)
        if not success:
            log.append(error_msg)
            return None, None, True  # Signal to try next model

    return doppel_name, best, False


def _process_test_failure(
    e: RuntimeError, model: str, doppel_name: str, log: list,
    failed_models: set, locked_doppels: set, model_idx: int
) -> tuple:
    """Process test failure and determine next action."""
    should_advance, should_continue, should_raise = _handle_test_run_errors(
        e, model, doppel_name, log, failed_models, locked_doppels
    )
    
    next_idx = model_idx + 1 if should_advance else model_idx
    
    if should_raise:
        raise e
    
    return next_idx, should_continue


def _execute_test_with_model_loop(
    models: list, num_devices: int, ts: str, session_name: str,
    suite_name: str, case_name: str, connect_sec: int, idle_mins: int, log: list
) -> tuple:
    """Execute test with automatic model fallback."""
    chosen_model = None
    result = None
    locked_doppels: set = set()
    failed_models: set = set()
    model_idx = 0
    total_attempts = 0
    MAX_TOTAL = max(_MAX_MODEL_RETRIES, len(models)) * 4
    doppel_name = None

    while model_idx < len(models) and total_attempts < MAX_TOTAL:
        total_attempts += 1
        model = models[model_idx]

        if model in failed_models:
            model_idx += 1
            continue

        attempt = model_idx + 1
        doppel_name, best, should_skip = _try_create_doppels(
            num_devices, model, ts, attempt, locked_doppels, log
        )

        if should_skip:
            failed_models.add(model)
            model_idx += 1
            continue

        _set_step(f"Starting test session '{session_name}'...")
        try:
            result = _run_test(session_name, suite_name, case_name, doppel_name, connect_sec, idle_mins, num_devices)
            chosen_model = model
            break
        except RuntimeError as e:
            model_idx, should_continue = _process_test_failure(
                e, model, doppel_name, log, failed_models, locked_doppels, model_idx
            )
            if should_continue:
                continue

    return chosen_model, result, doppel_name


def _run_load_test_auto_impl(num_devices: int, duration_mins: int) -> str:
    """Synchronous implementation — called from the async wrapper via run_in_executor."""
    ts = datetime.utcnow().strftime("%Y%m%d%H%M")
    log = []  # step-by-step audit trail for the return message

    _initialize_load_test_progress()

    # ── Infra pre-check (defense-in-depth) ────────────────────────────────────
    # The SRE agent calls CloudLens before transferring here, but LLMs can skip
    # that step when infra was recently verified. This secondary gate ensures a
    # stopped Function App always blocks the test, regardless of agent behavior.
    _set_step("Checking infrastructure health...")
    _cloudlens_url = os.getenv("CLOUDLENS_TOOL_URL", "http://127.0.0.1:6212/").rstrip("/")
    try:
        infra_resp = requests.post(
            f"{_cloudlens_url}/",
            json={"query": "pre-load-test infra check: Function App, Event Hub, SQL Server"},
            timeout=20,
            verify=_VERIFY_SSL,
        )
        infra_text = infra_resp.json().get("result", "") if infra_resp.status_code == 200 else ""
    except Exception:
        infra_text = ""  # CloudLens unreachable — fall through; SRE agent was primary gate

    if "INFRA NOT READY" in infra_text:
        _progress["infra_status"] = "failed"

        # Run diagnose_and_fix_service for each stopped service so the agent
        # always returns a full diagnosis + PENDING_ACTION card, even when the
        # SRE agent skipped its own CloudLens check and this secondary gate fired.
        # Service names appear as (`name`) in the INFRA NOT READY text.
        stopped_names = re.findall(r'\(`([^`]+)`\)', infra_text)
        diagnosis_parts = []
        for svc_name in stopped_names:
            try:
                from agentic_utils.tools.log_remediation_tools import diagnose_and_fix_service
                diagnosis_parts.append(
                    diagnose_and_fix_service(svc_name, context="pre-load-test check")
                )
            except Exception as diag_e:
                logger.warning("diagnose_and_fix_service skipped for %s: %s", svc_name, diag_e)

        _finish_progress(error="Infra not ready")
        combined = infra_text
        if diagnosis_parts:
            combined += "\n\n" + "\n\n---\n\n".join(diagnosis_parts)
        return combined

    _progress["infra_status"] = "ok"

    try:
        # ── Step 1: Fetch doppel models ────────────────────────────────────────
        _set_step("Fetching doppel models...")
        models = _get_models()
        if not models:
            _finish_progress(error="No doppel models found in Doppelio.")
            return "ERROR: No doppel models found in Doppelio."

        _set_step("Fetching test suites...")
        suite_name, case_name = _get_test_suite()
        log.append(f"Selected suite '{suite_name}' / case '{case_name}'.")

        session_name, connect_sec, idle_mins, duration_mins = _setup_test_configuration(
            num_devices, duration_mins, ts, suite_name, case_name, log
        )

        chosen_model, result, doppel_name = _execute_test_with_model_loop(
            models, num_devices, ts, session_name,
            suite_name, case_name, connect_sec, idle_mins, log
        )

        if not chosen_model or result is None:
            msg = "ERROR: Could not start test with any available doppel model.\n" + "\n".join(log)
            _finish_progress(error=msg)
            return msg

        session_run_no = result.get("testSessionRunNo", 1)
        run_no = result.get("runNo", 1)
        status = result.get("status", "unknown")

        _active_tests[session_name] = {
            "session_name":   session_name,
            "session_run_no": session_run_no,
            "run_no":         run_no,
            "num_devices":    num_devices,
            "duration_mins":  duration_mins,
            "started_at":     datetime.utcnow().isoformat(),
            "doppel_name":    doppel_name,
            "owner_session":  _progress.get("owner_session", ""),
        }

        # Register the confirmed device list in Azure SQL DB.
        # Runs after successful test start so only the final working
        # doppel/suite configuration (post any error-recovery switches) is recorded.
        _set_step("Registering devices in DB...")
        db_result = register_load_test_devices(
            doppel_name=doppel_name,
            model_name=chosen_model,
            suite_name=suite_name,
            num_devices=num_devices,
            test_run_name=session_name,
            total_duration_secs=duration_mins * 60,
        )
        logger.info("DB registration result: %s", db_result)

        _start_monitoring_and_telemetry(
            session_name, session_run_no, run_no,
            suite_name, case_name, doppel_name,
            num_devices, duration_mins, connect_sec,
            idle_mins, chosen_model, log
        )

        summary = (
            f"Load test running.\n"
            f"• Devices   : {num_devices:,}\n"
            f"• Duration  : {duration_mins} min\n"
            f"• Session   : {session_name}\n"
            f"• Run No    : {session_run_no} / {run_no}\n"
            f"• Status    : {status}"
        )
        _finish_progress(result=summary)
        return summary

    except Exception as e:
        logger.error(f"run_load_test_auto error: {e}")
        trail = "\n".join(f"  {l}" for l in log)
        msg = f"ERROR: {e}\n\nSteps completed before failure:\n{trail}"
        _finish_progress(error=str(e))
        return msg


async def run_load_test_auto(num_devices: int, duration_mins: int) -> str:
    """
    Fully autonomous load-test runner. Generates a device CSV, uploads it,
    picks a doppel model, creates and verifies doppels, selects a test suite,
    starts the test, and begins streaming telemetry to the configured Function App.

    Args:
        num_devices: Total number of virtual devices to simulate (e.g. 4000).
        duration_mins: How long to run the test in minutes (e.g. 60 for 1 hour, 3 for 3 minutes).

    Returns:
        A summary of what was started, or an error description.
    """
    import asyncio
    import functools
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, functools.partial(_run_load_test_auto_impl, num_devices, duration_mins)
    )


run_load_test_auto_tool  = FunctionTool(func=run_load_test_auto)
attach_to_session_tool   = FunctionTool(func=attach_to_session)
stop_active_test_tool    = FunctionTool(func=stop_active_test)
stop_all_tests_tool      = FunctionTool(func=stop_all_tests)
