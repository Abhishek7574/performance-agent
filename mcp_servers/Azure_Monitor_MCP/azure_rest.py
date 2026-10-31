"""
Azure REST API helpers
"""

import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

_retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=_retry)
_session = requests.Session()
_session.mount("https://", _adapter)
# S5332: SECURITY HOTSPOT REVIEWED - SAFE CONFIGURATION
# HTTP mount provided for requests library compatibility only.
# All Azure REST API calls use HTTPS exclusively (https://management.azure.com).
# Certificate verification enforced below. No actual HTTP traffic in production.
_session.mount("http://",  _adapter)

# Security: Enforce certificate verification
_session.verify = True

# ── Scope constants ───────────────────────────────────────────────────────────
_AZURE_MGMT_SCOPE = "https://management.azure.com/.default"

# ── Numeric constants ─────────────────────────────────────────────────────────
_TOKEN_EXPIRY_BUFFER_SECONDS = 60       # refresh token this many seconds before expiry
_DEFAULT_TOKEN_EXPIRY_SECONDS = 3600    # fallback if expires_in is absent from token response
_BYTES_PER_KB = 1024
_KB_PER_MB = 1024
_HOURS_PER_WEEK = 168

# ── Token cache (per scope) ───────────────────────────────────────────────────
_token_cache: dict = {}   # scope -> {"access_token": ..., "expires_at": ...}

# ── Metrics result cache (60-second TTL) ─────────────────────────────────────
_metrics_cache: dict = {}  # (service, metric_names, hours) -> (result, expires_at)
METRICS_CACHE_TTL = 60     # seconds


def _get_token(scope: str = _AZURE_MGMT_SCOPE) -> str:
    """
    Get a bearer token using Service Principal client-credentials flow.
    Tokens are cached until 60 seconds before expiry.
    """
    cached = _token_cache.get(scope)
    if cached and cached["expires_at"] > time.time() + _TOKEN_EXPIRY_BUFFER_SECONDS:
        return cached["access_token"]

    token, expires_in = _acquire_token(scope)
    _token_cache[scope] = {
        "access_token": token,
        "expires_at": time.time() + expires_in,
    }
    return token


def _acquire_token(scope: str):
    """Acquire token via Service Principal client-credentials flow."""
    tenant_id = os.getenv("AZURE_TENANT_ID", "")
    client_id = os.getenv("AZURE_CLIENT_ID", "")
    client_secret = os.getenv("AZURE_CLIENT_SECRET", "")

    if not (tenant_id and client_id and client_secret):
        raise ValueError(
            "Azure authentication failed. Set AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET in .env"
        )

    try:
        resp = _session.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", _DEFAULT_TOKEN_EXPIRY_SECONDS))
    except requests.RequestException:
        # Security: Don't leak detailed error information
        raise ValueError("Service Principal authentication failed. Check credentials and network connectivity.")


# ── Config helpers ────────────────────────────────────────────────────────────

def get_cfg() -> dict:
    default_rg = os.getenv("AZURE_RESOURCE_GROUP", "")
    return {
        "subscription_id": os.getenv("AZURE_SUBSCRIPTION_ID", ""),
        "resource_group":  default_rg,
        "webapp_name":     os.getenv("AZURE_WEBAPP_NAME", ""),
        "webapp_rg":       os.getenv("AZURE_WEBAPP_RESOURCE_GROUP", default_rg),
        "function_name":   os.getenv("AZURE_FUNCTION_APP_NAME", ""),
        "function_rg":     os.getenv("AZURE_FUNCTION_RESOURCE_GROUP", default_rg),
        "eventhub_ns":     os.getenv("AZURE_EVENTHUB_NAMESPACE", ""),
        "eventhub_rg":     os.getenv("AZURE_EVENTHUB_RESOURCE_GROUP", default_rg),
        "db_server":       os.getenv("AZURE_DATABASE_SERVER", ""),
        "db_name":         os.getenv("AZURE_DATABASE_NAME", ""),
        "db_type":         os.getenv("AZURE_DATABASE_TYPE", "sqlserver").lower(),
        "db_rg":           os.getenv("AZURE_DATABASE_RESOURCE_GROUP", default_rg),
        "workspace_id":    os.getenv("AZURE_LOG_ANALYTICS_WORKSPACE_ID", ""),
    }


def _resource_uri(service: str) -> str:
    cfg = get_cfg()
    sub = cfg["subscription_id"]
    if service == "webapp":
        return f"/subscriptions/{sub}/resourceGroups/{cfg['webapp_rg']}/providers/Microsoft.Web/sites/{cfg['webapp_name']}"
    if service == "function":
        return f"/subscriptions/{sub}/resourceGroups/{cfg['function_rg']}/providers/Microsoft.Web/sites/{cfg['function_name']}"
    if service == "eventhub":
        return f"/subscriptions/{sub}/resourceGroups/{cfg['eventhub_rg']}/providers/Microsoft.EventHub/namespaces/{cfg['eventhub_ns']}"
    if service == "database":
        db_type_map = {
            "sqlserver":  "Microsoft.Sql/servers/{srv}/databases/{db}",
            "postgresql": "Microsoft.DBforPostgreSQL/flexibleServers/{srv}/databases/{db}",
            "mysql":      "Microsoft.DBforMySQL/flexibleServers/{srv}/databases/{db}",
        }
        template = db_type_map.get(cfg["db_type"], db_type_map["sqlserver"])
        path = template.format(srv=cfg["db_server"], db=cfg["db_name"])
        return f"/subscriptions/{sub}/resourceGroups/{cfg['db_rg']}/providers/{path}"
    raise ValueError(f"Unknown service: {service}")


# ── Azure Monitor Metrics REST API ────────────────────────────────────────────

# Azure SQL storage/space metrics only emit Maximum aggregation.
_STORAGE_METRIC_KEYS = {"storage", "storage_percent", "allocated_data_storage"}


def _derive_unit_display(unit: str) -> tuple:
    """Returns (unit_str, is_bytes, is_bytes_per_sec)."""
    is_bytes = unit == "Bytes"
    is_bytes_per_sec = unit == "BytesPerSecond"
    if "Percent" in unit:
        unit_str = "%"
    elif is_bytes:
        unit_str = " MB"
    elif is_bytes_per_sec:
        unit_str = " KB/s"
    else:
        unit_str = f" {unit}"
    return unit_str, is_bytes, is_bytes_per_sec


def _fmt_val(val, is_bytes: bool, is_bytes_per_sec: bool, unit_str: str) -> str:
    if val is None:
        return "N/A"
    if is_bytes:
        mb = val / (_BYTES_PER_KB * _KB_PER_MB)
        return f"{mb / _KB_PER_MB:.2f} GB" if mb >= _KB_PER_MB else f"{mb:.1f} MB"
    if is_bytes_per_sec:
        kb = val / _BYTES_PER_KB
        return f"{kb / _KB_PER_MB:.2f} MB/s" if kb >= _KB_PER_MB else f"{kb:.1f} KB/s"
    return f"{val:.1f}{unit_str}"


def _round_if_present(value, decimals: int = 2):
    """Round value if not None, otherwise return None."""
    return round(value, decimals) if value is not None else None


def _has_metric_data(point: dict) -> bool:
    """Check if a data point has any metric values."""
    return any(point.get(k) is not None for k in ("average", "total", "maximum", "minimum", "count"))


def _create_data_point(point: dict) -> dict:
    """Create a formatted data point from raw metric data."""
    return {
        "timestamp": point.get("timeStamp", "N/A"),
        "average":   _round_if_present(point.get("average")),
        "minimum":   _round_if_present(point.get("minimum")),
        "maximum":   _round_if_present(point.get("maximum")),
        "total":     _round_if_present(point.get("total")),
        "count":     _round_if_present(point.get("count")),
    }


def _extract_series_data(timeseries: list) -> list:
    """Extract and format time series data from Azure Monitor response."""
    series_data = []
    for ts in timeseries:
        for pt in ts.get("data", []):
            if _has_metric_data(pt):
                series_data.append(_create_data_point(pt))
    return series_data


def _build_metric_entry(m: dict, series_data: list) -> dict:
    unit = m.get("unit", "")
    unit_str, is_bytes, is_bytes_per_sec = _derive_unit_display(unit)
    latest = series_data[-1] if series_data else {}

    def fmt(val):
        return _fmt_val(val, is_bytes, is_bytes_per_sec, unit_str)

    avgs   = [pt["average"] for pt in series_data if pt.get("average") is not None]
    mins_l = [pt["minimum"] for pt in series_data if pt.get("minimum") is not None]
    maxs_l = [pt["maximum"] for pt in series_data if pt.get("maximum") is not None]
    tots   = [pt["total"]   for pt in series_data if pt.get("total")   is not None]
    counts = [pt["count"]   for pt in series_data if pt.get("count")   is not None]

    return {
        "metric_key":   m.get("name", {}).get("value", ""),
        "name":         m.get("name", {}).get("localizedValue", "Unknown"),
        "unit":         unit_str,
        "latest_avg":   fmt(latest.get("average")),
        "latest_min":   fmt(latest.get("minimum")),
        "latest_max":   fmt(latest.get("maximum")),
        "latest_total": fmt(latest.get("total")),
        "latest_count": latest.get("count"),
        "window_mean":  fmt(sum(avgs) / len(avgs)) if avgs   else "N/A",
        "window_min":   fmt(min(mins_l))            if mins_l else "N/A",
        "window_max":   fmt(max(maxs_l))            if maxs_l else "N/A",
        "window_total": fmt(sum(tots))              if tots   else "N/A",
        "window_count": int(sum(counts))            if counts else None,
        "timeseries":   series_data,
    }


def _parse_metrics(data: dict) -> list:
    """Convert a raw Azure Monitor metrics response dict into structured results."""
    return [
        _build_metric_entry(m, _extract_series_data(m.get("timeseries", [])))
        for m in data.get("value", [])
    ]


def _has_data(metric: dict) -> bool:
    return any(metric[k] != "N/A" for k in ("latest_avg", "latest_max", "latest_total"))


def _fetch_metrics(service: str, metric_names: str, hours: int, aggregation: str) -> tuple:
    token = _get_token(_AZURE_MGMT_SCOPE)
    resource_uri = _resource_uri(service)
    now   = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    timespan = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    url = f"https://management.azure.com{resource_uri}/providers/microsoft.insights/metrics"
    if hours < 1:          # < 1 hour (e.g. 5 min, 30 min) → 1-min granularity for enough data points
        interval = "PT1M"
    elif hours <= 6:
        interval = "PT5M"
    elif hours <= 48:
        interval = "PT15M"
    elif hours <= _HOURS_PER_WEEK:
        interval = "PT1H"
    else:
        interval = "PT6H"
    params = {
        "api-version": "2021-05-01",
        "timespan":    timespan,
        "interval":    interval,
        "metricnames": metric_names,
        "aggregation": aggregation,
    }
    resp = _session.get(url, headers={"Authorization": f"Bearer {token}"},
                        params=params, timeout=8)
    return resp.status_code, resp.json() if resp.ok else {}


def _apply_storage_retry(results: list, service: str, metric_names: str) -> list:
    """Retry storage metrics with 24h + Maximum-only if initial fetch returned no data."""
    storage_results = [r for r in results if r["metric_key"].lower() in _STORAGE_METRIC_KEYS]
    if not (storage_results and all(not _has_data(r) for r in storage_results)):
        return results
    status2, data2 = _fetch_metrics(service, metric_names, 24, "Maximum")
    if status2 >= 400:
        return results
    retry = _parse_metrics(data2)
    by_key = {r["metric_key"]: r for r in results}
    for r2 in retry:
        if _has_data(r2):
            by_key[r2["metric_key"]] = r2
    return list(by_key.values())


def get_metrics(service: str, metric_names: str, hours: int = 3) -> list:
    """
    Fetch Azure Monitor metrics for a given service.

    Results are cached for METRICS_CACHE_TTL seconds to avoid redundant
    round-trips when multiple tools or the dashboard request the same data.
    """
    _cache_key = (service, metric_names, hours)
    cached = _metrics_cache.get(_cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    try:
        status, data = _fetch_metrics(service, metric_names, hours, "Average,Total,Maximum,Minimum,Count")
        if status >= 400:
            return []
        results = _parse_metrics(data)
        results = _apply_storage_retry(results, service, metric_names)
        _metrics_cache[_cache_key] = (results, time.time() + METRICS_CACHE_TTL)
        return results
    except Exception as e:
        print(f"Warning: Failed to get metrics: {str(e)[:100]}")
        return []


_COUNT_METRIC_KEYS = {
    "incomingmessages", "outgoingmessages", "incomingrequests", "successfulrequests",
    "throttledrequests", "capturebacklog", "connectionsopened", "connectionsclosed",
    "servererrors", "usererrors", "quotaexceedederrors",
}

# For chart rendering: use Total aggregation for these cumulative/count metrics
# (Average is more meaningful for gauge metrics like CPU %, Memory %)
_TOTAL_CHART_KEYS = {
    "cputime", "requests",
    "http2xx", "http3xx", "http4xx", "http5xx", "http401", "http403", "http404",
    "functionexecutioncount", "functionexecutionunits",
    "bytesreceived", "bytessent",
    "incomingmessages", "outgoingmessages", "incomingrequests", "successfulrequests",
    "throttledrequests", "connectionsopened", "connectionsclosed",
    "servererrors", "usererrors",
}

# 30-day baseline cache (service, metric_key) → ({"mean", "min", "max"}, expires_at)
_BASELINE_CACHE_TTL = 3600   # 1 hour — baselines change slowly
_baseline_cache: dict = {}


def _metric_display_values(m: dict) -> str:
    vals = []
    is_count_metric = m["metric_key"].lower() in _COUNT_METRIC_KEYS
    if is_count_metric:
        if m["window_total"] != "N/A":
            vals.append(f"total (window)={m['window_total']}")
        if m["latest_total"] != "N/A":
            vals.append(f"latest={m['latest_total']}")
    else:
        if m["latest_avg"]   != "N/A": vals.append(f"avg={m['latest_avg']}")
        if m["latest_max"]   != "N/A": vals.append(f"max={m['latest_max']}")
        if m["latest_total"] != "N/A": vals.append(f"total={m['latest_total']}")
    if not vals and m["metric_key"].lower() in _STORAGE_METRIC_KEYS:
        return "~0% (below Azure Monitor reporting threshold — DB idle or very low usage)"
    return ", ".join(vals) if vals else "No data"


def _metric_chart_tag(m: dict) -> str:
    import json as _json
    ts = m.get("timeseries") or []
    key = m.get("metric_key", "").lower().replace("_", "").replace(" ", "")
    use_total = key in _TOTAL_CHART_KEYS
    pts = []
    for p in ts:
        val = (p.get("total") if use_total and p.get("total") is not None
               else p.get("average") if p.get("average") is not None
               else p.get("total"))
        if p.get("timestamp") and val is not None:
            pts.append((p["timestamp"], val))
    if len(pts) < 2:
        return ""
    chart = {
        "name":  m.get("metric_key") or m.get("name", ""),
        "label": m.get("name", ""),
        "unit":  m.get("unit", "").strip(),
        "t":     [t for t, _ in pts],
        "v":     [v for _, v in pts],
    }
    if m.get("baseline_30d"):
        chart["baseline_30d"] = m["baseline_30d"]
    return f"<chart>{_json.dumps(chart, separators=(',', ':'))}</chart>\n"


def get_metric_baseline(service: str, metric_key: str) -> dict:
    """
    Fetch 30-day (720h) avg/min/max for a metric.
    Results are cached for 1 hour.  Returns {} on failure.
    """
    cache_key = (service, metric_key)
    cached = _baseline_cache.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    try:
        status, data = _fetch_metrics(service, metric_key, 720, "Average,Maximum,Minimum")
        if status >= 400 or not data:
            return {}
        entries = _parse_metrics(data)
        if not entries:
            return {}
        m = entries[0]
        ts = m.get("timeseries") or []
        avgs = [pt["average"] for pt in ts if pt.get("average") is not None]
        maxs = [pt["maximum"] for pt in ts if pt.get("maximum") is not None]
        mins = [pt["minimum"] for pt in ts if pt.get("minimum") is not None]
        if not avgs:
            return {}
        result = {
            "mean": round(sum(avgs) / len(avgs), 2),
            "max":  round(max(maxs), 2) if maxs else None,
            "min":  round(min(mins), 2) if mins else None,
        }
        _baseline_cache[cache_key] = (result, time.time() + _BASELINE_CACHE_TTL)
        return result
    except Exception as e:
        print(f"Warning: Failed to get 30d baseline for {metric_key}: {str(e)[:80]}")
        return {}


def format_metrics(metrics: list, title: str) -> str:
    import json as _json  # noqa: F401 — kept for backward compat if imported elsewhere
    result = f"\n{'='*50}\n{title}\n{'='*50}\n" if title else ""
    for m in metrics:
        result += f"{m['name']:40}: {_metric_display_values(m)}\n"
        result += _metric_chart_tag(m)
    return result


# ── Azure Resource Health REST API ───────────────────────────────────────────

_ARM_API_VERSIONS = {
    "webapp":    "2022-03-01",
    "function":  "2022-03-01",
    "eventhub":  "2021-11-01",
    "database":  "2022-05-01-preview",
}


def get_eventhub_entities(timeout: int = 10) -> list:
    """
    List individual Event Hub entities within the configured namespace.
    Returns a list of dicts: {"name": str, "status": str, "partition_count": int,
                               "message_retention": int}.
    status values from ARM: Active | Disabled | SendDisabled | ReceiveDisabled | Unknown
    """
    cfg = get_cfg()
    sub = cfg.get("subscription_id", "")
    rg  = cfg.get("eventhub_rg", "")
    ns  = cfg.get("eventhub_ns", "")
    if not (sub and rg and ns):
        return []
    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}/providers/Microsoft.EventHub/namespaces/{ns}"
        f"/eventhubs?api-version=2021-11-01"
    )
    try:
        token = _get_token(_AZURE_MGMT_SCOPE)
        resp = _session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return []
        entities = []
        for item in resp.json().get("value", []):
            p = item.get("properties", {})
            entities.append({
                "name":              item.get("name", "unknown"),
                "status":            p.get("status", "Unknown"),
                "partition_count":   p.get("partitionCount", 0),
                "message_retention": p.get("messageRetentionInDays", 0),
            })
        return entities
    except Exception:
        return []


def check_service_connectivity(service: str, timeout: int = 5) -> dict:
    """
    Fast health check: GET the ARM resource endpoint (no Resource Health API).
    Returns {"healthy": bool, "state": str}.
    200  → Healthy (state from resource properties if available)
    404  → Not Found
    else → Unhealthy with HTTP status
    """
    try:
        token = _get_token(_AZURE_MGMT_SCOPE)
        resource_uri = _resource_uri(service)
        url = f"https://management.azure.com{resource_uri}"
        api_version = _ARM_API_VERSIONS.get(service, "2021-04-01")
        resp = _session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"api-version": api_version},
            timeout=timeout,
        )
        if resp.status_code == 200:
            props = resp.json().get("properties", {})
            state = props.get("state", "Running") or "Running"
            # App Service / Function App can be HTTP-200-reachable (the ARM record exists)
            # while the runtime is Stopped. Reflect runtime state in `healthy` so callers
            # don't report a stopped app as Online.
            if service in ("webapp", "function"):
                healthy = state.lower() == "running"
            else:
                healthy = True
            return {"healthy": healthy, "state": state}
        elif resp.status_code == 404:
            return {"healthy": False, "state": "Not Found"}
        else:
            return {"healthy": False, "state": f"HTTP {resp.status_code}"}
    except Exception:
        return {"healthy": False, "state": "Unreachable"}


# ── App Service Runtime Status ───────────────────────────────────────────────

def get_webapp_status(service: str = "webapp") -> dict:
    """
    Get App Service / Function App runtime status from ARM:
    state, platform OS/stack, uptime (last restart), hostname, outbound IPs.
    """
    try:
        token = _get_token(_AZURE_MGMT_SCOPE)
        resource_uri = _resource_uri(service)
        url = f"https://management.azure.com{resource_uri}"
        resp = _session.get(url, headers={"Authorization": f"Bearer {token}"},
                            params={"api-version": "2022-03-01"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        props = data.get("properties", {})
        sc = props.get("siteConfig", {})

        linux_fx  = sc.get("linuxFxVersion", "")
        windows_fx = sc.get("windowsFxVersion", "")
        stack   = linux_fx or windows_fx or ("Windows" if not linux_fx else "Linux")
        os_type = "Linux" if linux_fx else "Windows"

        return {
            "state":             props.get("state", "Unknown"),
            "availability":      props.get("availabilityState", "Unknown"),
            "enabled":           props.get("enabled", True),
            "os":                os_type,
            "stack":             stack or "N/A",
            "last_modified_utc": props.get("lastModifiedTimeUtc", "N/A"),
            "default_hostname":  props.get("defaultHostName", ""),
            "outbound_ips":      props.get("outboundIpAddresses", ""),
            "kind":              data.get("kind", ""),
            "sku":               data.get("sku", {}).get("name", "") if data.get("sku") else "",
        }
    except Exception as e:
        return {"state": "Error", "availability": str(e)}


# ── Azure App Service Config (for security info) ──────────────────────────────

def get_webapp_config(service: str = "webapp") -> dict:
    """Fetch App Service web config (IP restrictions, TLS, etc.)"""
    try:
        token = _get_token(_AZURE_MGMT_SCOPE)
        resource_uri = _resource_uri(service)
        url = f"https://management.azure.com{resource_uri}/config/web"
        resp = _session.get(url, headers={"Authorization": f"Bearer {token}"},
                            params={"api-version": "2022-03-01"}, timeout=10)
        if resp.status_code >= 400:
            return {}
        return resp.json().get("properties", {})
    except Exception as e:
        print(f"Warning: Failed to get webapp config: {str(e)[:100]}")
        return {}


# ── Kudu API (App Service process list) ──────────────────────────────────────

def get_kudu_processes(webapp_name: str) -> list:
    """Fetch running processes from App Service Kudu REST API."""
    try:
        token = _get_token(_AZURE_MGMT_SCOPE)
        url = f"https://{webapp_name}.scm.azurewebsites.net/api/processes"
        resp = _session.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if resp.status_code == 401:
            raise PermissionError("Kudu access denied. Ensure the identity has 'Website Contributor' role.")
        if resp.status_code >= 400:
            return []
        return resp.json()
    except PermissionError:
        raise
    except Exception as e:
        print(f"Warning: Failed to get Kudu processes: {str(e)[:100]}")
        return []


# ── Log Analytics REST API ────────────────────────────────────────────────────

def query_logs(kql: str, hours: int = 1) -> list:
    """Run a KQL query against an Azure Monitor Log Analytics workspace."""
    cfg = get_cfg()
    workspace_id = cfg["workspace_id"]
    if not workspace_id:
        return [{"error": "AZURE_LOG_ANALYTICS_WORKSPACE_ID not set in .env"}]

    token = _get_token("https://api.loganalytics.io/.default")
    now   = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    url   = f"https://api.loganalytics.io/v1/workspaces/{workspace_id}/query"
    payload = {
        "query":    kql,
        "timespan": f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    }
    try:
        resp = _session.post(url, headers={"Authorization": f"Bearer {token}"},
                             json=payload, timeout=8)
        resp.raise_for_status()
        tables = resp.json().get("tables", [])
        if not tables:
            return []
        cols = [c["name"] for c in tables[0].get("columns", [])]
        return [dict(zip(cols, row)) for row in tables[0].get("rows", [])]
    except Exception as e:
        return [{"error": str(e)}]


def fmt_log_rows(rows: list, max_rows: int = 20) -> str:
    if not rows:
        return "  No records found.\n"
    if rows and "error" in rows[0]:
        return f"  Query error: {rows[0]['error']}\n"
    out = ""
    for i, row in enumerate(rows[:max_rows], 1):
        ts  = row.get("TimeGenerated", row.get("timestamp", ""))
        lvl = row.get("SeverityLevel", row.get("severityLevel", ""))
        msg = row.get("Message", row.get("message", row.get("outerMessage", str(row))))
        out += f"  {i:2}. [{ts}] {lvl}  {str(msg)[:100]}\n"
    if len(rows) > max_rows:
        out += f"  ... and {len(rows) - max_rows} more\n"
    return out


def get_resource_costs(resource_ids: list, days: int = 15) -> dict:
    """Fetch actual Azure billing cost per resource over the last N days via Cost Management API.

    Returns a dict keyed by lowercase resource_id:
        {resource_id_lower: {"total_cost": float, "currency": str, "daily_avg": float, "monthly_projected": float}}

    Returns {} gracefully on any error (e.g. Cost Management Reader role not assigned).
    """
    if not resource_ids:
        return {}

    cfg = get_cfg()
    sub = cfg.get("subscription_id", "")
    if not sub:
        return {}

    try:
        token = _get_token(_AZURE_MGMT_SCOPE)
    except Exception:
        return {}

    now = datetime.now(timezone.utc)
    date_to = now.strftime("%Y-%m-%dT00:00:00Z")
    date_from = (now - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")

    # Normalise resource IDs to lowercase for consistent matching
    normalised_ids = [r.lower() for r in resource_ids if r]

    body = {
        "type": "Usage",
        "timeframe": "Custom",
        "timePeriod": {"from": date_from, "to": date_to},
        "dataset": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {"name": "PreTaxCost", "function": "Sum"}
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "Currency"},
            ],
            "filter": {
                "dimensions": {
                    "name": "ResourceId",
                    "operator": "In",
                    "values": normalised_ids,
                }
            },
        },
    }

    try:
        resp = _session.post(
            f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.CostManagement/query",
            params={"api-version": "2023-11-01"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if not resp.ok:
            return {}
        data = resp.json()
    except Exception:
        return {}

    # Response columns: [ResourceId, Currency, PreTaxCost]
    columns = [c.get("name", "").lower() for c in data.get("properties", {}).get("columns", [])]
    rows = data.get("properties", {}).get("rows", []) or []

    result: dict = {}
    for row in rows:
        if len(row) < len(columns):
            continue
        row_dict = dict(zip(columns, row))
        rid = str(row_dict.get("resourceid", "")).lower()
        if not rid:
            continue
        try:
            total = float(row_dict.get("pretaxcost", 0) or 0)
        except (TypeError, ValueError):
            total = 0.0
        currency = str(row_dict.get("currency", "USD"))
        daily_avg = total / days if days > 0 else 0.0
        monthly_projected = daily_avg * 30
        result[rid] = {
            "total_cost": round(total, 4),
            "currency": currency,
            "daily_avg": round(daily_avg, 4),
            "monthly_projected": round(monthly_projected, 4),
        }

    return result
