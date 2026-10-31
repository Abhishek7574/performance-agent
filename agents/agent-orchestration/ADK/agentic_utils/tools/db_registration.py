"""Azure SQL device registration for load test sessions.

Downloads the Doppelio bulk-doppel CSV (or ZIP), parses device rows,
and inserts them into an Azure SQL table via Entra Service-Principal auth.

Called automatically by the orchestrator **after** a test is confirmed running —
never before, so only the final working doppel/suite/test-case is recorded.
If the DB write fails for any reason, the error is logged and the function returns
a 'skipped' result so it never blocks an in-progress test.
"""
import csv
import io
import logging
import os
import re
import struct
import zipfile

import time

import requests

try:
    from azure.identity import DefaultAzureCredential
    _AZURE_IDENTITY_AVAILABLE = True
except ImportError:
    DefaultAzureCredential = None  # type: ignore[assignment,misc]
    _AZURE_IDENTITY_AVAILABLE = False

from agentic_utils.tools.doppelio_tools import _WEB_BASE, _VERIFY_SSL, _headers

try:
    import pyodbc
    _PYODBC_AVAILABLE = True
except ImportError:
    _PYODBC_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
_DB_SERVER     = os.getenv("AZURE_DATABASE_SERVER", "")
_DB_NAME       = os.getenv("AZURE_DATABASE_NAME", "")
_TABLE_NAME    = os.getenv("LOAD_TEST_DEVICES_TABLE", "LoadTestDevices")

# Validate table name is safe to interpolate into SQL (operator-controlled env var,
# but still sanitised to prevent any misconfiguration).
if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', _TABLE_NAME):
    raise ValueError(f"Invalid LOAD_TEST_DEVICES_TABLE name: {_TABLE_NAME!r}")

_SESSIONS_TABLE = os.getenv("LOAD_TEST_SESSIONS_TABLE", "LoadTestSessions")
if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', _SESSIONS_TABLE):
    raise ValueError(f"Invalid LOAD_TEST_SESSIONS_TABLE name: {_SESSIONS_TABLE!r}")

# pyodbc attribute ID for Azure AD access-token authentication
_SQL_COPT_SS_ACCESS_TOKEN = 1256

# ── Credential / token cache (module-level singletons) ─────────────────────────
# DefaultAzureCredential is expensive to construct (probes env + IMDS).
# Reuse a single instance and cache the DB token to avoid repeated auth round-trips.
_DB_SCOPE = "https://database.windows.net/.default"
_TOKEN_EXPIRY_BUFFER = 120  # refresh 2 minutes before expiry
_credential: "DefaultAzureCredential | None" = None
_cached_token: "str | None" = None
_token_expires_at: float = 0.0


def _get_db_token() -> str:
    """Return a valid Azure SQL access token, refreshing only when near expiry."""
    global _credential, _cached_token, _token_expires_at
    if _credential is None:
        _credential = DefaultAzureCredential()
    now = time.time()
    if _cached_token is None or now >= _token_expires_at - _TOKEN_EXPIRY_BUFFER:
        tok = _credential.get_token(_DB_SCOPE)
        _cached_token = tok.token
        _token_expires_at = float(tok.expires_on)
    return _cached_token

_CREATE_TABLE_SQL = f"""
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '{_TABLE_NAME}'
)
BEGIN
    CREATE TABLE {_TABLE_NAME} (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        device_id       NVARCHAR(255) NOT NULL,
        doppel_id       NVARCHAR(255) NOT NULL,
        doppel_model    NVARCHAR(255) NOT NULL,
        timestamp       DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        test_run_name   NVARCHAR(255) NOT NULL,
        test_suite_name NVARCHAR(255) NOT NULL
    )
END
"""

_INSERT_SQL = f"""
INSERT INTO {_TABLE_NAME}
    (device_id, doppel_id, doppel_model, test_run_name, test_suite_name)
VALUES (?, ?, ?, ?, ?)
"""

_CREATE_SESSIONS_TABLE_SQL = f"""
IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '{_SESSIONS_TABLE}'
)
BEGIN
    CREATE TABLE {_SESSIONS_TABLE} (
        id                      INT IDENTITY(1,1) PRIMARY KEY,
        session_name            NVARCHAR(255) NOT NULL,
        total_duration_secs     INT           NOT NULL,
        completed_duration_secs INT           NULL,
        registered_at           DATETIME2     NOT NULL DEFAULT GETUTCDATE(),
        completed_at            DATETIME2     NULL
    )
END
"""

_INSERT_SESSION_SQL = f"""
INSERT INTO {_SESSIONS_TABLE} (session_name, total_duration_secs)
VALUES (?, ?)
"""

_UPDATE_SESSION_SQL = f"""
UPDATE {_SESSIONS_TABLE}
SET completed_duration_secs = ?,
    completed_at             = GETUTCDATE()
WHERE session_name           = ?
  AND completed_duration_secs IS NULL
"""


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_db_connection():
    """Return a pyodbc connection authenticated via Managed Identity (or DefaultAzureCredential)."""
    if not _PYODBC_AVAILABLE:
        raise RuntimeError(
            "pyodbc is not installed — run: pip install pyodbc"
        )
    if not _AZURE_IDENTITY_AVAILABLE:
        raise RuntimeError(
            "azure-identity is not installed — run: pip install azure-identity"
        )
    missing = [k for v, k in [
        (_DB_SERVER, "AZURE_DATABASE_SERVER"),
        (_DB_NAME,   "AZURE_DATABASE_NAME"),
    ] if not v]
    if missing:
        raise RuntimeError(f"Missing env vars for DB connection: {missing}")

    # DefaultAzureCredential auth chain:
    #   Locally  → reads AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET → Service Principal
    #   On Azure → detects MI environment, uses AZURE_CLIENT_ID as user-assigned MI client id if set
    token_str = _get_db_token()
    token_bytes = token_str.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{_DB_SERVER}.database.windows.net,1433;"
        f"Database={_DB_NAME};"
        "Encrypt=yes;TrustServerCertificate=no;"
        "Connection Timeout=10;"
    )
    # timeout=10 is the pyodbc-level cap (covers AAD token exchange + TCP connect).
    # Connection Timeout in the DSN string only applies to the raw TCP layer;
    # the pyodbc timeout parameter is more reliable for Azure AD auth flows.
    return pyodbc.connect(conn_str, attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: token_struct}, timeout=10)


def _ensure_table_exists(conn) -> None:
    """Create both registration tables if they don't already exist."""
    cursor = conn.cursor()
    try:
        cursor.execute(_CREATE_TABLE_SQL)
        cursor.execute(_CREATE_SESSIONS_TABLE_SQL)
        conn.commit()
    finally:
        cursor.close()


# ── CSV download + parse ───────────────────────────────────────────────────────

def _download_and_parse_doppel_csv(
    doppel_group_name: str,
    doppel_model_name: str,
    num_devices: int,
    test_env: str = "default",
) -> list:
    """
    Call the Doppelio DownloadBulkDoppel endpoint and return the first
    `num_devices` rows as a list of dicts:
        {"device_id": ..., "doppel_id": ..., "doppel_model": ...}

    The response may be a raw CSV or a ZIP archive containing a CSV.
    """
    url = f"{_WEB_BASE}/Doppel/DownloadBulkDoppel"
    params = {
        "DoppelGroupName": doppel_group_name,
        "DoppelModelName": doppel_model_name,
        "ForWhichTestEnvironment": test_env,
    }
    # Mirror the curl: Bearer token in header + accessToken field in multipart body
    from agentic_utils.tools.doppelio_tools import _get_token
    resp = requests.post(
        url,
        params=params,
        headers=_headers(),
        files={"accessToken": (None, _get_token())},
        verify=_VERIFY_SSL,
        timeout=60,
    )
    resp.raise_for_status()

    content = resp.content

    # Detect ZIP by magic bytes PK\x03\x04
    if content[:2] == b'PK':
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise RuntimeError("Downloaded ZIP contains no .csv file")
            csv_bytes = zf.read(csv_names[0])
    else:
        csv_bytes = content

    # utf-8-sig strips BOM if present
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    rows = []
    for row in reader:
        if len(rows) >= num_devices:
            break
        device_id    = (row.get("Device Id")         or row.get("device_id")         or "").strip()
        doppel_id    = (row.get("Doppel Name")        or row.get("doppel_name")        or "").strip()
        doppel_model = (row.get("Doppel Model Name")  or row.get("doppel_model_name")  or "").strip()
        if device_id:
            rows.append({
                "device_id":    device_id,
                "doppel_id":    doppel_id,
                "doppel_model": doppel_model,
            })

    return rows


# ── Public entry point ─────────────────────────────────────────────────────────

def register_load_test_devices(
    doppel_name: str,
    model_name: str,
    suite_name: str,
    num_devices: int,
    test_run_name: str,
    test_env: str = "default",
    total_duration_secs: int = 0,
) -> dict:
    """
    Download the device list for `doppel_name` and insert `num_devices` rows
    into the Azure SQL LoadTestDevices table.

    Always returns a dict — never raises — so a DB failure cannot block a test.

    Returns:
        {"registered": N, "status": "ok"}             on success
        {"registered": 0, "status": "skipped", "error": "..."}  on any failure
    """
    try:
        logger.info(
            "DB registration: doppel='%s' model='%s' suite='%s' devices=%d run='%s'",
            doppel_name, model_name, suite_name, num_devices, test_run_name,
        )
        devices = _download_and_parse_doppel_csv(
            doppel_group_name=doppel_name,
            doppel_model_name=model_name,
            num_devices=num_devices,
            test_env=test_env,
        )
        if not devices:
            logger.warning(
                "DB registration: CSV returned 0 rows for doppel='%s'", doppel_name
            )
            return {"registered": 0, "status": "skipped", "error": "CSV returned no device rows"}

        rows = [
            (d["device_id"], d["doppel_id"], d["doppel_model"], test_run_name, suite_name)
            for d in devices
        ]

        conn = _get_db_connection()
        try:
            _ensure_table_exists(conn)
            cursor = conn.cursor()
            try:
                cursor.executemany(_INSERT_SQL, rows)
                conn.commit()
            finally:
                cursor.close()
            if total_duration_secs > 0:
                sess_cur = conn.cursor()
                try:
                    sess_cur.execute(
                        f"SELECT COUNT(*) FROM {_SESSIONS_TABLE} WHERE session_name = ?",
                        (test_run_name,),
                    )
                    if sess_cur.fetchone()[0] == 0:
                        sess_cur.execute(_INSERT_SESSION_SQL, (test_run_name, total_duration_secs))
                        conn.commit()
                finally:
                    sess_cur.close()
        finally:
            conn.close()

        logger.info(
            "DB registration: inserted %d rows for run='%s' suite='%s'",
            len(rows), test_run_name, suite_name,
        )
        return {"registered": len(rows), "status": "ok"}

    except Exception as exc:
        logger.warning("DB registration skipped (non-fatal): %s", exc)
        return {"registered": 0, "status": "skipped", "error": str(exc)}


def update_session_completed_duration(
    session_name: str,
    completed_duration_secs: int,
) -> dict:
    """Persist the actual completed duration for a session after it finishes.

    Safe to call multiple times — the UPDATE WHERE ... IS NULL guard ensures
    only the first call writes, so retries are idempotent.

    Always returns a dict — never raises — so a DB failure cannot block anything.

    Returns:
        {"updated": True, "status": "ok"}                         on success
        {"updated": False, "status": "skipped", "error": "..."}   on any failure
    """
    try:
        if not session_name or completed_duration_secs <= 0:
            return {"updated": False, "status": "skipped", "error": "Invalid args"}
        conn = _get_db_connection()
        try:
            cur = conn.cursor()
            try:
                cur.execute(_UPDATE_SESSION_SQL, (completed_duration_secs, session_name))
                conn.commit()
                affected = cur.rowcount
            finally:
                cur.close()
        finally:
            conn.close()
        logger.info(
            "update_session_completed_duration: session='%s' secs=%d rows=%d",
            session_name, completed_duration_secs, affected,
        )
        return {"updated": affected > 0, "status": "ok"}
    except Exception as exc:
        logger.warning("update_session_completed_duration skipped: %s", exc)
        return {"updated": False, "status": "skipped", "error": str(exc)}
