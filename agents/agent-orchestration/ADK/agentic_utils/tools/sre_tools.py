"""SRE Agent tools for environment validation and infrastructure monitoring."""
import os
import requests
from google.adk.tools import FunctionTool


PERFORMANCE_TOOL_URL = os.getenv("PERFORMANCE_TOOL_URL", "http://127.0.0.1:6103/")
SECURITY_MONITOR_URL = os.getenv("SECURITY_MONITOR_URL", "http://127.0.0.1:6105/")
APP_MONITOR_URL = os.getenv("APPLICATION_MONITOR_URL", "http://127.0.0.1:6107/")


def validate_environment_parity(baseline_description: str) -> str:
    """
    Validate current Azure infrastructure configuration against a production baseline.
    Checks TLS settings, IP restrictions, instance counts, and resource health.
    Produces a gap report describing differences between current state and the baseline.

    Args:
        baseline_description: Description of the expected production baseline configuration.

    Returns:
        Gap report as a formatted string.
    """
    try:
        security_resp = requests.post(
            SECURITY_MONITOR_URL,
            json={"query": f"security config check against baseline: {baseline_description}"},
            timeout=30,
        )
        security_text = (
            security_resp.json().get("result", "")
            if security_resp.status_code == 200
            else f"Error: HTTP {security_resp.status_code}"
        )
    except Exception as e:
        security_text = f"Exception: {str(e)}"

    try:
        health_resp = requests.post(
            PERFORMANCE_TOOL_URL, json={"query": "azure health status all services"}, timeout=30
        )
        health_text = (
            health_resp.json().get("result", "")
            if health_resp.status_code == 200
            else f"Error: HTTP {health_resp.status_code}"
        )
    except Exception as e:
        health_text = f"Exception: {str(e)}"

    return (
        "ENVIRONMENT PARITY REPORT\n"
        + "=" * 60
        + "\n"
        f"Baseline: {baseline_description}\n\n"
        f"Security & Config:\n{security_text}\n\n"
        f"Service Health:\n{health_text}"
    )


def monitor_infra_and_alert(context: str) -> str:
    """
    Monitor infrastructure metrics during test execution and raise alerts if anomalies detected.
    Checks CPU, memory, error rates, and service availability.

    Args:
        context: Current test execution context or description.

    Returns:
        Alert report: GREEN (normal), YELLOW (warning), or RED (alert) with details.
    """
    try:
        perf_resp = requests.post(
            PERFORMANCE_TOOL_URL,
            json={"query": f"performance metrics during: {context}"},
            timeout=30,
        )
        perf_text = (
            perf_resp.json().get("result", "")
            if perf_resp.status_code == 200
            else f"Error: HTTP {perf_resp.status_code}"
        )
    except Exception as e:
        perf_text = f"Exception: {str(e)}"

    try:
        app_resp = requests.post(
            APP_MONITOR_URL, json={"query": f"application health during: {context}"}, timeout=30
        )
        app_text = (
            app_resp.json().get("result", "")
            if app_resp.status_code == 200
            else f"Error: HTTP {app_resp.status_code}"
        )
    except Exception as e:
        app_text = f"Exception: {str(e)}"

    return (
        "INFRA MONITORING ALERT REPORT\n"
        + "=" * 60
        + "\n"
        f"Context: {context}\n\n"
        f"Performance:\n{perf_text}\n\n"
        f"Application:\n{app_text}"
    )


environment_validator_tool = FunctionTool(func=validate_environment_parity)
infra_alert_tool = FunctionTool(func=monitor_infra_and_alert)
