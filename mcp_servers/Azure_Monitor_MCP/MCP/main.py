"""
Azure Monitor MCP Server
========================
FastMCP server exposing Azure cloud monitoring as MCP tools.

Port: 6200
Transport: streamable-http

Tools exposed:
  azure_performance  : App Service & Function App CPU / memory / HTTP metrics
  azure_processes    : Running processes via Kudu API
  azure_security     : IP restrictions, TLS config, exposed endpoints
  azure_logs         : Log Analytics workspace KQL queries
  azure_health       : Azure Resource Health for all configured services
  azure_eventhub     : Event Hub messaging metrics
  azure_database     : Database DTU / connection / storage metrics
  azure_dashboard    : Aggregated overview of all Azure services
"""

import sys
import os
import json
import glob
import asyncio
import re as _re
from datetime import datetime, timezone

# Allow importing azure_rest from the parent directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse

import azure_rest as az

load_dotenv()

# Create MCP server instance
mcp = FastMCP("AzureMonitorMCP", host="0.0.0.0", port=6200, stateless_http=True)

# ── Time window constants ─────────────────────────────────────────────────────
_HOURS_PER_DAY      = 24
_HOURS_PER_WEEK     = 168
_MAX_HOURS          = 720   # 30 days — upper bound for any time query
_DASHBOARD_WORKERS  = 24    # thread pool size for parallel metric/health fetches

# ── Helpers ───────────────────────────────────────────────────────────────────

def _cfg_check(keys: list) -> str | None:
    """Return error message if required config keys are missing."""
    cfg = az.get_cfg()
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        labels = {
            "subscription_id": "AZURE_SUBSCRIPTION_ID",
            "webapp_name":     "AZURE_WEBAPP_NAME",
            "webapp_rg":       "AZURE_WEBAPP_RESOURCE_GROUP",
            "function_name":   "AZURE_FUNCTION_APP_NAME",
            "function_rg":     "AZURE_FUNCTION_RESOURCE_GROUP",
            "eventhub_ns":     "AZURE_EVENTHUB_NAMESPACE",
            "eventhub_rg":     "AZURE_EVENTHUB_RESOURCE_GROUP",
            "db_server":       "AZURE_DATABASE_SERVER",
            "db_name":         "AZURE_DATABASE_NAME",
            "db_rg":           "AZURE_DATABASE_RESOURCE_GROUP",
            "workspace_id":    "AZURE_LOG_ANALYTICS_WORKSPACE_ID",
        }
        env_names = [labels.get(k, k.upper()) for k in missing]
        return f"Missing configuration: set {', '.join(env_names)} in .env"
    return None


def parse_hours(query: str) -> float:
    """Extract a time window in hours from a natural language query string.

    Returns a float so sub-hour windows (minutes) are supported.
    Examples:
        "last 5 minutes" → 0.0833   "last 30 min"  → 0.5
        "last 1 hour"    → 1        "last 6 hours" → 6
        "last day"       → 24       "last 2 days"  → 48
        "last week"      → 168      "last 2 weeks" → 336
        no match         → 3  (default)
    """
    q = query.lower()
    m = _re.search(r'(\d+)\s*month', q)
    if m:
        return min(int(m.group(1)) * 720, _MAX_HOURS)
    if _re.search(r'\b(last|past)\s+month\b', q):
        return _MAX_HOURS          # 720 h = 30 days
    m = _re.search(r'(\d+)\s*week', q)
    if m:
        return min(int(m.group(1)) * _HOURS_PER_WEEK, _MAX_HOURS)
    if _re.search(r'\b(last|past)\s+week\b', q):
        return _HOURS_PER_WEEK
    m = _re.search(r'(\d+)\s*day', q)
    if m:
        return min(int(m.group(1)) * _HOURS_PER_DAY, _MAX_HOURS)
    if _re.search(r'\b(last|past)\s+day\b', q):
        return _HOURS_PER_DAY
    m = _re.search(r'(\d+)\s*h(?:our)?s?\b', q)
    if m:
        return min(max(int(m.group(1)), 1), _MAX_HOURS)
    if _re.search(r'\b(last|past)\s+hour\b', q):
        return 1
    # Minutes — must come after hours to avoid partial matches
    m = _re.search(r'(\d+)\s*min(?:ute)?s?\b', q)
    if m:
        return round(int(m.group(1)) / 60, 6)   # fractional hours; timedelta(hours=X) accepts floats
    if _re.search(r'\b(last|past)\s+minute\b', q):
        return round(1 / 60, 6)
    return 3


def _fmt_window(hours: float) -> str:
    """Format a fractional-hours value as a human-readable window string."""
    if hours < 1:
        mins = round(hours * 60)
        return f"{mins}m"
    return f"{int(hours)}h"


def _webapp_metrics_for_query(q: str, webapp_name: str) -> tuple:
    """Returns (metric_names, label) for webapp based on query, or (None, None) to skip."""
    has_cpu = "cpu" in q
    has_mem = "memory" in q or "ram" in q
    has_http = "http" in q or "request" in q or "response" in q
    has_net = "network" in q or "storage" in q or "disk" in q or "io" in q
    if "function" in q or "func" in q:
        return None, None
    if has_cpu and has_mem:
        return "CpuTime,MemoryWorkingSet,AverageMemoryWorkingSet", f"Web App ({webapp_name}) — CPU & Memory"
    if has_mem:
        return "MemoryWorkingSet,AverageMemoryWorkingSet", f"Web App ({webapp_name}) — Memory"
    if has_http:
        return "Requests,HttpResponseTime,Http2xx,Http3xx,Http4xx,Http5xx,Http401,Http403,Http404", f"Web App ({webapp_name}) — HTTP"
    if has_net:
        return "BytesReceived,BytesSent,IoReadBytesPerSecond,IoWriteBytesPerSecond,FileSystemUsage", f"Web App ({webapp_name}) — Network & Storage"
    if has_cpu:
        return "CpuTime,Requests,Http5xx", f"Web App ({webapp_name}) — CPU"
    return "CpuTime,MemoryWorkingSet,Requests,Http5xx,InstanceCount", f"Web App ({webapp_name}) — Overview"


def _fmt_log_stats(hours: int, rows: list, rows2: list) -> str:
    level_map = {0: "Verbose", 1: "Information", 2: "Warning", 3: "Error", 4: "Critical"}
    result = f"AZURE LOG STATISTICS (last {_fmt_window(hours)})\n{'='*50}\n"
    if rows and "error" not in rows[0]:
        for row in rows:
            lvl = int(row.get("severityLevel", 0))
            result += f"  {level_map.get(lvl, str(lvl)):15}: {row.get('count_', 'N/A')}\n"
    else:
        result += az.fmt_log_rows(rows)
    if rows2 and "error" not in rows2[0]:
        result += f"\nTotal exceptions (last {_fmt_window(hours)}): {rows2[0].get('count_', 'N/A')}\n"
    return result


def _fmt_eventhub_entities(entities: list) -> str:
    result = "  [ Event Hubs ]\n"
    for e in entities:
        s = e["status"]
        state_label = "Active" if s == "Active" else f"[{s}]"
        line = f"    {e['name']}: {state_label}"
        if e["partition_count"]:
            line += f" | {e['partition_count']} partitions"
        result += line + "\n"
    return result


# ── Dashboard helpers ─────────────────────────────────────────────────────────

def _dashboard_section(title: str) -> str:
    return f"\n{'─'*70}\n{title}\n"


def _build_metric_tasks(cfg: dict) -> list:
    tasks = []
    if cfg.get("webapp_name"):
        tasks += [
            ("webapp_compute", "webapp", "CpuTime,AverageMemoryWorkingSet,MemoryWorkingSet,InstanceCount,HealthCheckStatus"),
            ("webapp_http",    "webapp", "Requests,HttpResponseTime,ResponseTime,Http2xx,Http3xx,Http4xx,Http5xx,Http401,Http403,Http404,Http406"),
            ("webapp_net",     "webapp", "BytesReceived,BytesSent,IoReadBytesPerSecond,IoWriteBytesPerSecond,IoReadOperationsPerSecond,IoWriteOperationsPerSecond,FileSystemUsage"),
        ]
    if cfg.get("function_name"):
        tasks += [
            ("func_connections", "function", "AppConnections,Handles"),
            ("func_executions",  "function", "FunctionExecutionCount,FunctionExecutionUnits"),
            ("func_compute",     "function", "CpuTime,MemoryWorkingSet"),
            ("func_http",        "function", "Requests,HttpResponseTime,ResponseTime,Http2xx,Http4xx,Http5xx"),
            ("func_net",         "function", "BytesReceived,BytesSent,IoReadBytesPerSecond,IoWriteBytesPerSecond"),
        ]
    if cfg.get("eventhub_ns"):
        tasks += [
            ("eh_messages",    "eventhub", "IncomingMessages,OutgoingMessages,IncomingBytes,OutgoingBytes,IncomingRequests,SuccessfulRequests,CapturedBytes,CapturedMessages,CaptureBacklog,Size"),
            ("eh_connections", "eventhub", "ActiveConnections,ConnectionsOpened,ConnectionsClosed,ServerErrors,UserErrors,ThrottledRequests,QuotaExceededErrors,ReplicationLagCount,ReplicationLagDuration"),
        ]
    if cfg.get("db_server") and cfg.get("db_name"):
        tasks += [
            ("db_perf",        "database", "cpu_percent,sql_instance_cpu_percent,sqlserver_process_core_percent,workers_percent,sessions_percent,sessions_count,availability"),
            ("db_memory",      "database", "sql_instance_memory_percent,sqlserver_process_memory_percent"),
            ("db_storage",     "database", "storage,storage_percent,physical_data_read_percent,log_write_percent,xtp_storage_percent"),
            ("db_connections", "database", "connection_successful,connection_failed,connection_failed_user_error,blocked_by_firewall,deadlock"),
        ]
    return tasks


def _safe_future(fut, default):
    try:
        return fut.result()
    except Exception:
        return default


_DASHBOARD_COUNT_KEYS = {
    "incomingmessages", "outgoingmessages", "incomingrequests", "successfulrequests",
    "throttledrequests", "capturebacklog", "connectionsopened", "connectionsclosed",
    "servererrors", "usererrors", "quotaexceedederrors",
}


def _dashboard_metric_vals(met: dict) -> list:
    vals = []
    is_count = met.get("metric_key", "").lower() in _DASHBOARD_COUNT_KEYS
    if is_count:
        if met["window_total"] != "N/A": vals.append(f"total (window)={met['window_total']}")
        if met["latest_total"] != "N/A": vals.append(f"latest={met['latest_total']}")
    else:
        if met["latest_avg"]   != "N/A": vals.append(f"avg={met['latest_avg']}")
        if met["latest_max"]   != "N/A": vals.append(f"max={met['latest_max']}")
        if met["latest_total"] != "N/A": vals.append(f"total={met['latest_total']}")
    return vals


_TOTAL_CHART_KEYS = {
    "requests",
    "http2xx", "http3xx", "http4xx", "http5xx", "http401", "http403", "http404",
    "functionexecutioncount", "functionexecutionunits",
    "bytesreceived", "bytessent",
    "incomingmessages", "outgoingmessages", "incomingrequests", "successfulrequests",
    "throttledrequests", "connectionsopened", "connectionsclosed",
    "servererrors", "usererrors",
}


def _dashboard_chart_tag(met: dict, layer: str = "") -> str:
    ts = met.get("timeseries") or []
    key = met.get("metric_key", "").lower().replace("_", "").replace(" ", "")
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
        "name":  met.get("metric_key") or met.get("name", ""),
        "label": met.get("name", ""),
        "unit":  met.get("unit", "").strip(),
        "layer": layer,
        "t":     [t for t, _ in pts],
        "v":     [v for _, v in pts],
    }
    if met.get("baseline_30d"):
        chart["baseline_30d"] = met["baseline_30d"]
    return f"<chart>{json.dumps(chart, separators=(',', ':'))}</chart>\n"


def _fmt_metric_rows(met_list: list, label_width: int = 45, layer: str = "") -> str:
    out = ""
    for met in met_list:
        vals = _dashboard_metric_vals(met)
        out += f"    {met['name']:{label_width}}: {', '.join(vals) or 'No data'}\n"
        out += _dashboard_chart_tag(met, layer)
    return out


def _build_health_section(configured_svcs: dict, health: dict) -> str:
    result = _dashboard_section("SERVICE HEALTH")
    healthy_count = 0
    for svc, (label, name) in configured_svcs.items():
        h = health[svc]
        if h["healthy"]:
            healthy_count += 1
            result += f"✅ {label:15} {name:35} Online\n"
        else:
            result += f"❌ {label:15} {name:35} {h['state']}\n"
    result += f"\nOverall: {healthy_count}/{len(configured_svcs)} services healthy\n"
    return result


def _build_webapp_section(cfg: dict, metrics: dict, webapp_status: dict, kudu_procs: list, hours: int) -> str:
    result = _dashboard_section(f"WEB APP — {cfg['webapp_name']}")
    if webapp_status:
        state = webapp_status.get("state", "Unknown")
        state_icon = "RUNNING" if state.lower() == "running" else state.upper()
        result += "  [ App Status ]\n"
        result += f"  State        : [{state_icon}] {state}\n"
        result += f"  Availability : {webapp_status.get('availability','Unknown')}\n"
        result += f"  OS / Stack   : {webapp_status.get('os','?')} / {webapp_status.get('stack','N/A')}\n"
        result += f"  Hostname     : {webapp_status.get('default_hostname','')}\n"
        result += f"  Last Modified: {webapp_status.get('last_modified_utc','N/A')}\n"
        result += f"  Outbound IPs : {webapp_status.get('outbound_ips','N/A')[:80]}\n"
    else:
        result += "  App status error\n"
    result += f"  [ Compute ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("webapp_compute", []), layer="application")
    result += f"  [ HTTP Requests & Latency ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("webapp_http", []), layer="application")
    result += f"  [ Network & Storage ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("webapp_net", []), layer="application")
    result += "  [ Running Processes ]\n"
    if kudu_procs:
        top = sorted(kudu_procs, key=lambda x: x.get("working_set", 0), reverse=True)[:5]
        result += f"    {'PID':>6} | {'Name':22} | {'Memory':>10} | CPU Time\n"
        result += "    " + "-"*60 + "\n"
        for p in top:
            ws_mb = round(p.get("working_set", 0) / (1024 * 1024))
            result += f"    {str(p.get('id','?')):>6} | {str(p.get('name','?'))[:22]:22} | {ws_mb:>8.1f}MB | {p.get('total_cpu_time','N/A')}\n"
        if len(kudu_procs) > 5:
            result += f"    ... and {len(kudu_procs)-5} more\n"
    else:
        result += "    No processes found (app may be idle/stopped or Kudu access denied)\n"
    return result


def _build_function_section(cfg: dict, metrics: dict, func_status: dict, health: dict, hours: int) -> str:
    result = _dashboard_section(f"FUNCTION APP — {cfg['function_name']}")
    if func_status:
        state = func_status.get("state", "Unknown")
        state_icon = "RUNNING" if state.lower() == "running" else state.upper()
        result += "  [ App Status ]\n"
        result += f"  State        : [{state_icon}] {state}\n"
        result += f"  OS / Stack   : {func_status.get('os','?')} / {func_status.get('stack','N/A')}\n"
        result += f"  Hostname     : {func_status.get('default_hostname','')}\n"
    result += f"  [ Connections ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("func_connections", []), layer="communication")
    result += f"  [ Executions ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("func_executions", []), layer="communication")
    result += f"  [ Compute ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("func_compute", []), layer="communication")
    result += f"  [ HTTP ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("func_http", []), layer="communication")
    result += f"  [ Network & IO ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("func_net", []), layer="communication")
    h = health.get("function", {})
    icon = "✅" if h.get("healthy") else "❌"
    result += f"  [ Health ] {icon} {h.get('state','Unknown')}\n"
    return result


def _build_eventhub_section(cfg: dict, metrics: dict, health: dict, hours: int) -> str:
    result = _dashboard_section(f"EVENT HUB — {cfg['eventhub_ns']}")
    h = health.get("eventhub", {})
    icon = "✅" if h.get("healthy") else "❌"
    result += f"  [ Namespace Health ] {icon} {h.get('state','Unknown')}\n"
    result += f"  [ Messages & Bandwidth ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("eh_messages", []), layer="communication")
    result += f"  [ Connections & Errors ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("eh_connections", []), layer="communication")
    return result


def _build_database_section(cfg: dict, metrics: dict, health: dict, hours: int) -> str:
    db_type = cfg.get("db_type", "sqlserver")
    result = _dashboard_section(f"DATABASE — {cfg['db_server']} / {cfg['db_name']} ({db_type})")
    h = health.get("database", {})
    icon = "✅" if h.get("healthy") else "❌"
    result += f"  [ Availability ] {icon} {h.get('state','Unknown')}\n"
    result += f"  [ CPU & Performance ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("db_perf", []), layer="application")
    result += f"  [ Memory ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("db_memory", []), layer="application")
    result += f"  [ Storage & IO ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("db_storage", []), layer="application")
    result += f"  [ Connections ] (last {_fmt_window(hours)})\n" + _fmt_metric_rows(metrics.get("db_connections", []), layer="application")
    return result


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def azure_performance(query: str) -> str:
    """
    Get Azure App Service and Function App performance metrics from Azure Monitor.
    Returns CPU percentage, memory working set, HTTP response time, request count,
    HTTP 5xx/4xx errors, bytes received/sent, and file system usage.
    Use for any Azure cloud performance, CPU, memory, HTTP, network, or storage queries.
    """
    err = _cfg_check(["subscription_id", "webapp_rg", "webapp_name"])
    if err:
        return err

    cfg = az.get_cfg()
    hours = parse_hours(query)
    result = f"AZURE PERFORMANCE METRICS\n{'='*60}\n"
    result += f"Checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Time window: last {_fmt_window(hours)}\n"

    q = query.lower()
    has_func = "function" in q or "func" in q
    has_specific = any(k in q for k in ("cpu", "memory", "ram", "http", "request", "response", "network", "storage", "disk", "io"))

    try:
        metric_names, label = _webapp_metrics_for_query(q, cfg["webapp_name"])
        if metric_names:
            result += az.format_metrics(az.get_metrics("webapp", metric_names, hours), label)
    except Exception as e:
        result += f"\nWeb App metrics error: {e}\n"

    if cfg.get("function_name") and (has_func or not has_specific):
        try:
            func_metrics = az.get_metrics("function", "FunctionExecutionCount,FunctionExecutionUnits,AppConnections,Handles", hours)
            result += az.format_metrics(func_metrics, f"Function App ({cfg['function_name']})")
        except Exception as e:
            result += f"\nFunction App metrics error: {e}\n"

    result += "\nTip: Ask for 'memory', 'http requests', 'network', or 'all performance' for specific metrics."
    return result


@mcp.tool()
def azure_processes(query: str) -> str:
    """
    Get running processes inside Azure App Service container via the Kudu REST API.
    Returns process ID, name, working set memory, private memory, and CPU time.
    Use for any Azure cloud process list or running application queries.
    """
    err = _cfg_check(["webapp_name"])
    if err:
        return err

    cfg = az.get_cfg()
    webapp = cfg["webapp_name"]
    result = f"AZURE APP SERVICE — PROCESS LIST\n{'='*60}\n"
    result += f"App: {webapp}\n\n"

    q = query.lower()

    try:
        procs = az.get_kudu_processes(webapp)
        if not procs:
            return result + "No processes found (App Service may be in a stopped state).\n"

        if "cpu" in q:
            sorted_procs = sorted(procs, key=lambda x: str(x.get("total_cpu_time", "")), reverse=True)
            result += "Sorted by CPU time:\n"
        else:
            sorted_procs = sorted(procs, key=lambda x: x.get("working_set", 0), reverse=True)
            result += "Sorted by memory (working set):\n"

        result += f"\n{'PID':>6} | {'Name':25} | {'Working Set':>12} | {'Private Mem':>12} | CPU Time\n"
        result += "-"*80 + "\n"
        for p in sorted_procs[:20]:
            ws_mb  = round(p.get("working_set",   0) / (1024*1024))
            prv_mb = round(p.get("private_memory", 0) / (1024*1024))
            result += f"{str(p.get('id','N/A')):>6} | {str(p.get('name','Unknown'))[:25]:25} | {ws_mb:>10.1f}MB | {prv_mb:>10.1f}MB | {p.get('total_cpu_time','N/A')}\n"

        if len(procs) > 20:
            result += f"\n... and {len(procs) - 20} more processes\n"

    except PermissionError as e:
        result += f"Access denied: {e}\n"
    except Exception as e:
        result += f"Error fetching process list: {e}\n"

    return result


@mcp.tool()
def azure_security(query: str) -> str:
    """
    Get Azure App Service security configuration including IP access restrictions,
    TLS/HTTPS settings, inbound/outbound rules, and exposed service endpoints.
    Use for any Azure cloud security, firewall, TLS, or network access queries.
    """
    err = _cfg_check(["webapp_name"])
    if err:
        return err

    cfg = az.get_cfg()
    result = f"AZURE SECURITY CONFIGURATION\n{'='*60}\n"
    result += f"Checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    result += "NOTE: Network security in Azure is enforced at the platform level\n"
    result += "(NSG, App Service access restrictions, Azure Firewall).\n\n"

    def _fmt_ip_rules(rules: list, label: str) -> str:
        if not rules:
            return f"  {label}: No restrictions (all traffic allowed)\n"
        out = f"  {label}:\n"
        for r in rules:
            out += f"    [{r.get('priority','?'):4}] {r.get('action','Allow'):6} | {r.get('ipAddress', r.get('vnetSubnetResourceId','N/A')):30} | {r.get('name','Unnamed')}\n"
        return out

    if cfg.get("webapp_name"):
        try:
            props = az.get_webapp_config("webapp")
            result += f"Web App: {cfg['webapp_name']}\n"
            result += f"  HTTPS Only    : {props.get('httpsOnly', 'N/A')}\n"
            result += f"  Min TLS       : {props.get('minTlsVersion', 'N/A')}\n"
            result += f"  HTTP/2        : {props.get('http20Enabled', 'N/A')}\n"
            result += f"  Client Cert   : {props.get('clientCertMode', 'N/A')}\n"
            result += _fmt_ip_rules(props.get("ipSecurityRestrictions", []),    "Inbound IP Rules")
            result += _fmt_ip_rules(props.get("scmIpSecurityRestrictions", []), "SCM/Kudu Rules")
            result += "\n"
        except Exception as e:
            result += f"  Web App config error: {e}\n\n"

    if cfg.get("function_name"):
        try:
            props = az.get_webapp_config("function")
            result += f"Function App: {cfg['function_name']}\n"
            result += f"  HTTPS Only    : {props.get('httpsOnly', 'N/A')}\n"
            result += f"  Min TLS       : {props.get('minTlsVersion', 'N/A')}\n"
            result += _fmt_ip_rules(props.get("ipSecurityRestrictions", []), "Inbound IP Rules")
            result += "\n"
        except Exception as e:
            result += f"  Function App config error: {e}\n\n"

    result += "SERVICE ENDPOINTS\n" + "-"*40 + "\n"
    if cfg.get("webapp_name"):
        result += f"  Web App   HTTPS 443  https://{cfg['webapp_name']}.azurewebsites.net\n"
        result += f"  Kudu      HTTPS 443  https://{cfg['webapp_name']}.scm.azurewebsites.net\n"
    if cfg.get("function_name"):
        result += f"  Functions HTTPS 443  https://{cfg['function_name']}.azurewebsites.net\n"
    if cfg.get("eventhub_ns"):
        result += f"  EventHub  AMQP  5671 {cfg['eventhub_ns']}.servicebus.windows.net:5671\n"
        result += f"  EventHub  AMQPS 443  {cfg['eventhub_ns']}.servicebus.windows.net:443 (websocket)\n"
    if cfg.get("db_server"):
        ports = {"sqlserver": 1433, "postgresql": 5432, "mysql": 3306}
        port = ports.get(cfg["db_type"], 1433)
        result += f"  Database  TCP {port:4}  {cfg['db_server']}.database.windows.net:{port}\n"

    return result


@mcp.tool()
def azure_logs(query: str) -> str:
    """
    Query Azure Monitor Log Analytics workspace using KQL for application logs,
    errors, exceptions, traces, and custom log searches.
    Use for any Azure cloud log, error, exception, or trace queries.
    """
    err = _cfg_check(["workspace_id"])
    if err:
        return err + "\n\nNote: AZURE_LOG_ANALYTICS_WORKSPACE_ID must be set for log queries."

    hours = parse_hours(query)
    q = query.lower()

    if "error" in q or "exception" in q:
        kql = """
AppExceptions
| order by TimeGenerated desc
| take 30
| project TimeGenerated, outerMessage, type, severityLevel
"""
        rows = az.query_logs(kql, hours=hours)
        result = f"AZURE ERROR / EXCEPTION LOGS (last {_fmt_window(hours)})\n{'='*60}\n"
        result += f"Total found: {len(rows) if rows and 'error' not in rows[0] else 0}\n\n"
        result += az.fmt_log_rows(rows)

    elif "statistics" in q or "stats" in q:
        kql = """
AppTraces
| summarize count() by severityLevel
| order by severityLevel asc
"""
        rows = az.query_logs(kql, hours=hours)
        rows2 = az.query_logs("AppExceptions | summarize count()", hours=hours)
        result = _fmt_log_stats(hours, rows, rows2)

    elif "search" in q:
        term = query.replace("search", "").replace("for", "").strip()
        if not term:
            return "Please specify a search term. Example: 'search timeout'"
        kql = f"""
AppTraces
| where Message contains "{term}"
| order by TimeGenerated desc
| take 30
"""
        rows = az.query_logs(kql, hours=hours)
        result = f"AZURE LOG SEARCH: '{term}' (last {_fmt_window(hours)})\n{'='*60}\n"
        result += az.fmt_log_rows(rows)

    elif any(k in q for k in ("monitoring", "real-time", "overview")):
        kql = """
AppTraces
| where severityLevel >= 2
| order by TimeGenerated desc
| take 20
| project TimeGenerated, Message, severityLevel
"""
        rows = az.query_logs(kql, hours=hours)
        result = f"AZURE LOG MONITORING (last {_fmt_window(hours)})\n{'='*60}\n"
        result += az.fmt_log_rows(rows)

    else:
        kql = """
AppTraces
| order by TimeGenerated desc
| take 30
"""
        rows = az.query_logs(kql, hours=hours)
        result = f"AZURE RECENT LOGS (last {_fmt_window(hours)})\n{'='*60}\n"
        result += az.fmt_log_rows(rows)
        result += "\nTip: Ask for 'error logs', 'log statistics', or 'search <term>'"

    return result


@mcp.tool()
def azure_health(query: str) -> str:
    """
    Get Azure service health by checking connectivity to each configured service.
    Returns Online/Offline status for Web App, Function App, Event Hub, and Database.
    Use for any Azure cloud health, status, or availability queries.
    """
    err = _cfg_check(["subscription_id"])
    if err:
        return err

    cfg = az.get_cfg()
    result = f"AZURE SERVICE HEALTH CHECK\n{'='*60}\n"
    result += f"Checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    service_labels = {
        "webapp":   ("Web App",    cfg.get("webapp_name",   "")),
        "function": ("Function App", cfg.get("function_name", "")),
        "eventhub": ("Event Hub",  cfg.get("eventhub_ns",   "")),
        "database": ("Database",   f"{cfg.get('db_server','')}/{cfg.get('db_name','')}"),
    }
    configured = {k: v for k, v in service_labels.items() if v[1].strip("/")}
    if not configured:
        return result + "No Azure services configured. Set AZURE_WEBAPP_NAME, etc. in .env\n"

    from concurrent.futures import ThreadPoolExecutor
    def _check(svc):
        return svc, az.check_service_connectivity(svc)

    with ThreadPoolExecutor(max_workers=4) as executor:
        checks = dict(executor.map(lambda s: _check(s), configured.keys()))

    healthy = 0
    for svc, (label, name) in configured.items():
        h = checks.get(svc, {"healthy": False, "state": "Unknown"})
        if h["healthy"]:
            healthy += 1
            result += f"✅ {label:20} | Online\n"
        else:
            result += f"❌ {label:20} | {h['state']}\n"

    result += f"\nOverall: {healthy}/{len(configured)} services healthy\n"
    return result


@mcp.tool()
def azure_eventhub(query: str) -> str:
    """
    Get Azure Event Hub namespace metrics: incoming/outgoing messages, bytes,
    throttled requests, active connections, and server errors.
    Use for any Azure Event Hub, messaging, or broker queries.
    """
    err = _cfg_check(["subscription_id", "eventhub_rg", "eventhub_ns"])
    if err:
        return err

    cfg = az.get_cfg()
    hours = parse_hours(query)
    _sep = "─" * 70

    q = query.lower()

    result = f"AZURE EVENT HUB METRICS\n{_sep}\nTime window: last {_fmt_window(hours)}\n"
    result += f"\n{_sep}\nEVENT HUB — {cfg['eventhub_ns']}\n"

    try:
        h = az.check_service_connectivity("eventhub")
        icon = "✅" if h["healthy"] else "❌"
        ns_label = "Running" if h["healthy"] else h["state"]
        result += f"  [ Health ] {icon} {ns_label}\n"

        if "connection" in q:
            metrics = az.get_metrics("eventhub", "ActiveConnections,ConnectionsOpened,ConnectionsClosed,ServerErrors,UserErrors,ThrottledRequests,QuotaExceededErrors", hours)
            result += f"  [ Connections & Errors ] (last {_fmt_window(hours)})\n"
            result += az.format_metrics(metrics, "")
        elif any(k in q for k in ("byte", "network", "bandwidth")):
            metrics = az.get_metrics("eventhub", "IncomingBytes,OutgoingBytes,CapturedBytes,CaptureBacklog", hours)
            result += f"  [ Bandwidth ] (last {_fmt_window(hours)})\n"
            result += az.format_metrics(metrics, "")
        else:
            msg_metrics = az.get_metrics("eventhub", "IncomingMessages,OutgoingMessages,IncomingRequests,SuccessfulRequests,ThrottledRequests", hours)
            result += f"  [ Messages & Requests ] (last {_fmt_window(hours)})\n"
            result += az.format_metrics(metrics=msg_metrics, title="")

            conn_metrics = az.get_metrics("eventhub", "ActiveConnections,ConnectionsOpened,ConnectionsClosed,IncomingBytes,OutgoingBytes,ServerErrors,UserErrors", hours)
            result += f"  [ Connections & Bandwidth ] (last {_fmt_window(hours)})\n"
            result += az.format_metrics(metrics=conn_metrics, title="")

        entities = az.get_eventhub_entities()
        if entities:
            result += _fmt_eventhub_entities(entities)
    except Exception as e:
        result += f"Error: {e}\n"

    result += f"\n{_sep}\n"
    return result


@mcp.tool()
def azure_database(query: str) -> str:
    """
    Get Azure Database metrics: DTU/CPU consumption, connection counts, deadlocks,
    storage usage, and resource health. Supports SQL Server, PostgreSQL, and MySQL.
    Use for any Azure database, SQL, DTU, connection, or storage queries.
    """
    err = _cfg_check(["subscription_id", "db_rg", "db_server", "db_name"])
    if err:
        return err

    cfg = az.get_cfg()
    hours = parse_hours(query)
    result = f"AZURE DATABASE METRICS\n{'='*60}\n"
    result += f"Server: {cfg['db_server']} / DB: {cfg['db_name']} ({cfg['db_type']}) | Time window: last {_fmt_window(hours)}\n\n"

    q = query.lower()

    try:
        if "connection" in q:
            metrics = az.get_metrics("database", "connection_successful,connection_failed,connection_failed_user_error,blocked_by_firewall,deadlock", hours)
            result += az.format_metrics(metrics, "Database — Connections")
        elif any(k in q for k in ("storage", "space", "disk", "io")):
            metrics = az.get_metrics("database", "storage,storage_percent,physical_data_read_percent,log_write_percent,xtp_storage_percent", hours)
            result += az.format_metrics(metrics, "Database — Storage & IO")
        elif "memory" in q or "ram" in q:
            metrics = az.get_metrics("database", "sql_instance_memory_percent,sqlserver_process_memory_percent", hours)
            result += az.format_metrics(metrics, "Database — Memory")
        else:
            metrics = az.get_metrics("database", "cpu_percent,sql_instance_cpu_percent,sqlserver_process_core_percent,workers_percent,sessions_percent,sessions_count,availability,connection_successful,connection_failed,deadlock,storage_percent", hours)
            result += az.format_metrics(metrics, "Database — Overview")

        h = az.check_service_connectivity("database")
        icon = "✅" if h["healthy"] else "❌"
        result += f"\nDatabase Server: {icon} {h['state']}\n"
    except Exception as e:
        result += f"Error: {e}\n"

    return result


@mcp.tool()
def azure_dashboard(query: str) -> str:
    """
    Get a comprehensive Azure monitoring dashboard for ALL configured services.
    Covers: Web App CPU/memory/HTTP/connections/storage, Function App executions/CPU/memory,
    Event Hub messages/bandwidth/connections/errors, Database DTU/CPU/connections/storage/deadlocks,
    Resource Health for every service, and running processes via Kudu.
    Use for any Azure overview, summary, all-services, current status, or full metrics queries.
    """
    from concurrent.futures import ThreadPoolExecutor

    cfg = az.get_cfg()
    hours = parse_hours(query)
    metric_tasks = _build_metric_tasks(cfg)

    health_map = {
        "webapp":   ("Web App",      cfg.get("webapp_name",   "")),
        "function": ("Function App", cfg.get("function_name", "")),
        "eventhub": ("Event Hub",    cfg.get("eventhub_ns",   "")),
        "database": ("Database",     cfg.get("db_server",     "")),
    }
    configured_svcs = {k: v for k, v in health_map.items() if v[1]}

    # Primary metrics whose 30-day baseline we want to fetch (cached after first call)
    _BASELINE_TASKS = []
    if cfg.get("webapp_name"):
        _BASELINE_TASKS += [("webapp",   "CpuTime"),   ("webapp",   "Requests")]
    if cfg.get("function_name"):
        _BASELINE_TASKS += [("function", "FunctionExecutionCount"), ("function", "FunctionExecutionUnits")]
    if cfg.get("db_server"):
        _BASELINE_TASKS += [("database", "cpu_percent"), ("database", "storage_percent")]

    with ThreadPoolExecutor(max_workers=_DASHBOARD_WORKERS) as executor:
        metric_futures = {k: executor.submit(az.get_metrics, svc, names, hours)
                          for k, svc, names in metric_tasks}
        health_futures = {svc: executor.submit(az.check_service_connectivity, svc)
                          for svc in configured_svcs}
        webapp_status_fut = executor.submit(az.get_webapp_status, "webapp") if cfg.get("webapp_name") else None
        func_status_fut   = executor.submit(az.get_webapp_status, "function") if cfg.get("function_name") else None
        kudu_fut          = executor.submit(az.get_kudu_processes, cfg["webapp_name"]) if cfg.get("webapp_name") else None
        baseline_futures  = {(svc, mk): executor.submit(az.get_metric_baseline, svc, mk)
                              for svc, mk in _BASELINE_TASKS}

        metrics       = {k: _safe_future(f, [])                              for k, f in metric_futures.items()}
        health        = {s: _safe_future(f, {"healthy": False, "state": "Unknown"}) for s, f in health_futures.items()}
        webapp_status = _safe_future(webapp_status_fut, {}) if webapp_status_fut else {}
        func_status   = _safe_future(func_status_fut,   {}) if func_status_fut   else {}
        kudu_procs    = _safe_future(kudu_fut,          []) if kudu_fut           else []
        baselines     = {k: _safe_future(f, {}) for k, f in baseline_futures.items()}

    # Attach baseline_30d to matching metric entries so _dashboard_chart_tag can include them
    for met_list in metrics.values():
        for met in met_list:
            key = met.get("metric_key", "")
            # Try both service-specific and service-agnostic lookup
            for svc_key in ("webapp", "function", "database", "eventhub"):
                bl = baselines.get((svc_key, key))
                if bl:
                    met["baseline_30d"] = bl
                    break

    result = f"AZURE MONITORING DASHBOARD\n{'='*70}\n"
    result += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Time window: last {_fmt_window(hours)}\n\n"
    result += _build_health_section(configured_svcs, health)

    if cfg.get("webapp_name"):
        result += _build_webapp_section(cfg, metrics, webapp_status, kudu_procs, hours)
    if cfg.get("function_name"):
        result += _build_function_section(cfg, metrics, func_status, health, hours)
    if cfg.get("eventhub_ns"):
        result += _build_eventhub_section(cfg, metrics, health, hours)
    if cfg.get("db_server") and cfg.get("db_name"):
        result += _build_database_section(cfg, metrics, health, hours)

    result += f"\n{'='*70}\n"
    return result

# ── HTTP Endpoints ─────────────────────────────────────────────────────────────


_METRICS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


@mcp.custom_route("/collect-metrics", methods=["GET"])
async def collect_metrics_endpoint(request: Request) -> JSONResponse:
    """
    GET /collect-metrics[?hours=N][&path=...]
    Discovers all available metrics per service and writes a JSON file.
    """
    params = request.query_params
    hours = 3
    try:
        hours = max(1, min(int(params.get("hours", 3)), _HOURS_PER_WEEK))
    except (ValueError, TypeError):
        pass

    if not az.get_cfg().get("subscription_id"):
        return JSONResponse({"error": "Missing AZURE_SUBSCRIPTION_ID"}, status_code=500)

    from concurrent.futures import ThreadPoolExecutor
    cfg = az.get_cfg()
    services = {
        "webapp":   {"name": cfg.get("webapp_name",   ""), "metrics": "CpuTime,MemoryWorkingSet,Requests,Http5xx"},
        "function": {"name": cfg.get("function_name", ""), "metrics": "FunctionExecutionCount,CpuTime,MemoryWorkingSet"},
        "eventhub": {"name": cfg.get("eventhub_ns",   ""), "metrics": "IncomingMessages,OutgoingMessages,ThrottledRequests"},
        "database": {"name": cfg.get("db_server",     ""), "metrics": "cpu_percent,connection_successful,deadlock,storage_percent"},
    }
    configured = {k: v for k, v in services.items() if v["name"]}

    def _fetch_svc(svc_key):
        v = configured[svc_key]
        try:
            health  = az.check_service_connectivity(svc_key)
            metrics = az.get_metrics(svc_key, v["metrics"], hours)
            return svc_key, {"status": "ok", "health": health["state"], "metrics_fetched": len(metrics)}
        except Exception as e:
            return svc_key, {"status": "error", "error": str(e)}

    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            summary = dict(ex.map(_fetch_svc, configured.keys()))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours_window": hours,
        "services":     summary,
    })


@mcp.custom_route("/metrics-data", methods=["GET"])
async def metrics_data_endpoint(request: Request) -> JSONResponse:
    """
    GET /metrics-data
    Returns the latest collected JSON from disk (no API calls).
    """
    pattern = os.path.join(_METRICS_DIR, "azure_metrics_*.json")
    files = glob.glob(pattern)
    if not files:
        return JSONResponse(
            {"error": "No metrics file found. Call /collect-metrics first."},
            status_code=404,
        )
    latest = max(files, key=os.path.getmtime)
    try:
        def _read():
            with open(latest, encoding="utf-8") as f:
                return json.load(f)
        data = await asyncio.to_thread(_read)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Azure Monitor Dashboard</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#21262d;--text:#c9d1d9;--muted:#8b949e;--green:#3fb950;--yellow:#d29922;--red:#f85149;--blue:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);font-size:12px}
header{background:var(--card);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:10}
h1{font-size:15px;font-weight:600;color:#fff}
.meta{font-size:11px;color:var(--muted);margin-top:3px}
.btn{background:#238636;border:1px solid #2ea043;color:#fff;padding:5px 12px;border-radius:5px;cursor:pointer;font-size:11px}
.btn:hover{background:#2ea043}.btn:disabled{opacity:.5;cursor:default}.btn.busy{background:#1f6feb;border-color:#388bfd}
main{padding:16px;display:flex;flex-direction:column;gap:14px}
.svc{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.svc-head{padding:10px 14px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border);background:#1c2128;flex-wrap:wrap}
.svc-name{font-size:13px;font-weight:600;color:#fff}
.badge{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600}
.ok{background:#0d4429;color:var(--green)}.warn{background:#3d2b05;color:var(--yellow)}.down{background:#3d0c0c;color:var(--red)}.unk{background:#21262d;color:var(--muted)}
.svc-info{margin-left:auto;font-size:10px;color:var(--muted)}
.tiles{padding:12px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}
.tile{background:var(--bg);border:1px solid var(--border);border-radius:7px;flex:1;min-width:240px;overflow:hidden}
.tile-head{background:#1c2128;padding:7px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);border-bottom:1px solid var(--border)}
table{width:100%;border-collapse:collapse}
thead tr{background:#0d1117}
th{font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600;padding:5px 8px;text-align:right;border-bottom:1px solid var(--border)}
th:first-child{text-align:left}
td{padding:5px 8px;border-bottom:1px solid #1c2128;text-align:right;color:var(--text)}
td:first-child{text-align:left;color:var(--muted);font-size:11px}
tr:last-child td{border-bottom:none}
tr:hover td{background:#1c2128}
.v{font-weight:700;color:#fff}.na{color:#30363d}
.rt{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;padding:10px;font-size:11px}
.rk{color:var(--muted)}.rv{color:#fff;font-weight:500}
#msg{text-align:center;padding:50px;color:var(--muted);font-size:13px}
</style>
</head>
<body>
<header>
  <div><h1>&#9729; Azure Monitor Dashboard</h1><div class="meta" id="meta">—</div></div>
  <button class="btn" id="btn" onclick="doRefresh()">&#x21BB; Refresh</button>
</header>
<main id="main"><div id="msg">Loading&hellip;</div></main>
<script>
const GROUPS={
  eventhub:[
    {t:'Status & Requests',  k:['SuccessfulRequests','IncomingRequests','ServerErrors','UserErrors','ThrottledRequests','QuotaExceededErrors']},
    {t:'Size & Throughput',  k:['Size','IncomingBytes','OutgoingBytes','CapturedBytes']},
    {t:'Messages',           k:['IncomingMessages','OutgoingMessages','CapturedMessages']},
    {t:'Connections',        k:['ActiveConnections','ConnectionsOpened','ConnectionsClosed','CaptureBacklog']},
    {t:'Replication',        k:['ReplicationLagDuration','ReplicationLagCount']},
  ],
  database:[
    {t:'Health & Availability',k:['availability','cpu_percent','workers_percent','sessions_percent','sessions_count']},
    {t:'Storage',              k:['storage','storage_percent','allocated_data_storage','full_backup_size_bytes','diff_backup_size_bytes','log_backup_size_bytes']},
    {t:'Performance & IO',     k:['physical_data_read_percent','log_write_percent','xtp_storage_percent','deadlock','connection_successful','connection_failed','connection_failed_user_error','replication_lag_seconds']},
  ],
  webapp:[
    {t:'Compute',k:['CpuTime','MemoryWorkingSet','InstanceCount','AppConnections','Handles','HealthCheckStatus']},
    {t:'HTTP',   k:['Requests','HttpResponseTime','ResponseTime','Http2xx','Http3xx','Http4xx','Http5xx','Http401','Http403','Http404','Http406']},
    {t:'IO',     k:['BytesReceived','BytesSent','IoReadBytesPerSecond','IoWriteBytesPerSecond','IoReadOperationsPerSecond','IoWriteOperationsPerSecond','FileSystemUsage']},
  ],
  function:[
    {t:'Executions',k:['FunctionExecutionCount','FunctionExecutionUnits']},
    {t:'Compute',   k:['CpuTime','MemoryWorkingSet','AppConnections','Handles']},
    {t:'HTTP',      k:['Requests','HttpResponseTime','ResponseTime','Http2xx','Http4xx','Http5xx']},
    {t:'IO',        k:['BytesReceived','BytesSent','IoReadBytesPerSecond','IoWriteBytesPerSecond']},
  ],
};
const LABELS={eventhub:'Event Hub',database:'Database',webapp:'Web App',function:'Function App'};
const ORDER=['eventhub','database','webapp','function'];

function badge(h){
  const s=(h&&h.availability_state||'').toLowerCase();
  if(s==='available')return'<span class="badge ok">Available</span>';
  if(s.includes('degraded'))return'<span class="badge warn">Degraded</span>';
  if(s.includes('409'))return'<span class="badge warn">API 409</span>';
  if(s.includes('unavailable'))return'<span class="badge down">Unavailable</span>';
  return`<span class="badge unk">${(h&&h.availability_state)||'Unknown'}</span>`;
}

function idx(metrics){
  const m={};
  for(const x of(metrics||[])) if(x.metric_key) m[x.metric_key]=x;
  return m;
}

function v(val){return val&&val!=='N/A'?`<span class="v">${val}</span>`:'<span class="na">—</span>';}

const TILE_DEFAULT_SHOW={database:2,webapp:2,function:3,eventhub:3};

function toggleTileMore(btn){
  const tbody=btn.closest('table').querySelector('.tile-more');
  if(!tbody) return;
  const show=tbody.style.display==='none';
  tbody.style.display=show?'':'none';
  btn.textContent=show?'View less':'View more';
}

function tile(title,keys,ix,svcType){
  const rows=keys.flatMap(k=>{
    const m=ix[k]; if(!m) return[];
    const cur=m.latest_avg!=='N/A'?m.latest_avg:(m.latest_total!=='N/A'?m.latest_total:'N/A');
    if(cur==='N/A'&&m.window_max==='N/A'&&m.window_min==='N/A') return[];
    const name=m.name.length>28?m.name.slice(0,26)+'\u2026':m.name;
    return[`<tr>
      <td title="${m.name}">${name}</td>
      <td>${v(cur)}</td>
      <td>${v(m.window_min)}</td>
      <td>${v(m.window_mean)}</td>
      <td>${v(m.window_max)}</td>
      <td>${m.window_count!=null?`<span class="v">${m.window_count}</span>`:'<span class="na">—</span>'}</td>
    </tr>`];
  });
  if(!rows.length) return'';
  const maxShow=TILE_DEFAULT_SHOW[svcType]||rows.length;
  const visible=rows.slice(0,maxShow);
  const hidden=rows.slice(maxShow);
  const moreRow=hidden.length?`<tr><td colspan="6" style="text-align:center;padding:5px 0">
    <button onclick="toggleTileMore(this)" style="background:none;border:1px solid var(--border);color:var(--blue);padding:3px 10px;border-radius:4px;cursor:pointer;font-size:10px">View more</button>
  </td></tr>`:'';
  const moreTbody=hidden.length?`<tbody class="tile-more" style="display:none">${hidden.join('')}</tbody>`:'';
  return`<div class="tile">
    <div class="tile-head">${title}</div>
    <table>
      <thead><tr><th>Metric</th><th>Current</th><th>Min</th><th>Mean</th><th>Max</th><th>Count</th></tr></thead>
      <tbody>${visible.join('')}${moreRow}</tbody>
      ${moreTbody}
    </table>
  </div>`;
}

function rtTile(rt){
  if(!rt) return'';
  return`<div class="tile" style="min-width:160px;max-width:200px;flex:0 0 auto">
    <div class="tile-head">Runtime</div>
    <div class="rt">
      <span class="rk">State</span><span class="rv">${rt.state||'?'}</span>
      <span class="rk">Stack</span><span class="rv">${rt.stack||rt.os||'?'}</span>
      <span class="rk">Availability</span><span class="rv">${rt.availability||'?'}</span>
      <span class="rk">Hostname</span><span class="rv" style="font-size:10px;word-break:break-all">${rt.default_hostname||'?'}</span>
    </div>
  </div>`;
}

function svc(key,entry){
  const ix=idx(entry.metrics);
  const grps=GROUPS[key]||[];
  let tiles=rtTile(entry.runtime);
  for(const g of grps) tiles+=tile(g.t,g.k,ix,key);
  if(!tiles) tiles='<div style="padding:14px;color:var(--muted)">No data for this window</div>';
  return`<div class="svc">
    <div class="svc-head">
      <span class="svc-name">${LABELS[key]||key}</span>
      ${badge(entry.health)}
    </div>
    <div class="tiles">${tiles}</div>
  </div>`;
}

async function render(){
  let data;
  try{
    const r=await fetch('/metrics-data');
    if(!r.ok){const e=await r.json().catch(()=>({error:r.statusText}));document.getElementById('main').innerHTML=`<div id="msg">${e.error}</div>`;return;}
    data=await r.json();
  }catch(e){document.getElementById('main').innerHTML=`<div id="msg">Error: ${e.message}</div>`;return;}
  document.getElementById('meta').textContent=
    `Collected: ${data.collected_at||'—'} \u00b7 Window: ${data.hours_window||'?'}h \u00b7 ${(data.output_file||'').replace(/.*[\\\\/]/,'')}`;
  const svcs=data.services||{};
  let html='';
  for(const k of ORDER){const e=svcs[k];if(!e||e.status==='skipped')continue;html+=svc(k,e);}
  document.getElementById('main').innerHTML=html||'<div id="msg">No services configured. Call /collect-metrics first.</div>';
}

async function doRefresh(){
  const btn=document.getElementById('btn');
  btn.textContent='Collecting\u2026';btn.className='btn busy';btn.disabled=true;
  try{await fetch('/collect-metrics');await render();}
  catch(e){alert('Failed: '+e.message);}
  finally{btn.textContent='\u21BB Refresh';btn.className='btn';btn.disabled=false;}
}

render();
setInterval(render,60000);
</script>
</body>
</html>"""


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_endpoint(request: Request) -> HTMLResponse:
    """GET /dashboard — tile-based monitoring UI"""
    return HTMLResponse(_DASHBOARD_HTML)


@mcp.custom_route("/resource-config", methods=["GET"])
async def resource_config_endpoint(request: Request) -> JSONResponse:
    """GET /resource-config — returns SKU/tier for all configured Azure resources."""
    cfg = az.get_cfg()
    result: dict = {}

    async def _fetch(svc_key: str, out_prefix: str) -> None:
        try:
            status = await asyncio.to_thread(az.get_webapp_status, svc_key)
            result[f"{out_prefix}_sku"]  = status.get("sku", "")
            result[f"{out_prefix}_kind"] = status.get("kind", "")
            result[f"{out_prefix}_os"]   = status.get("os", "")
        except Exception:
            pass

    tasks = []
    if cfg.get("webapp_name"):
        tasks.append(_fetch("webapp", "webapp"))
    if cfg.get("function_name"):
        tasks.append(_fetch("function", "function"))

    if tasks:
        await asyncio.gather(*tasks)

    # Add DB type for frontend reference
    result["db_type"] = cfg.get("db_type", "")
    result["db_name"] = cfg.get("db_name", "")
    return JSONResponse(result)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
