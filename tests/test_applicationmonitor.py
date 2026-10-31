"""Tests for tools/applicationmonitor/CODE_EXECUTION/main.py"""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tests.helpers import load_tool

mod = load_tool("applicationmonitor")
client = TestClient(mod.app)

_HEALTHY = [{"url": "svc://host:6103/health", "healthy": True, "status_code": 200, "response_time_ms": "5ms"}]
_UNHEALTHY = [{"url": "svc://host:6103/health", "healthy": False, "error": "refused"}]


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_format_health_check_report():
    result = mod._format_health_check_report(_HEALTHY)
    assert "HEALTH CHECKS" in result


def test_format_database_report_empty():
    with patch.object(mod.psutil, "net_connections", return_value=[]):
        result = mod._format_database_report()
    assert "DATABASE" in result


def test_format_default_overview():
    with patch.object(mod.psutil, "net_connections", return_value=[]):
        result = mod._format_default_overview()
    assert "APPLICATION OVERVIEW" in result


def test_endpoint_health_check():
    with patch.object(mod, "perform_health_checks", return_value=_HEALTHY):
        resp = client.post("/", json={"query": "health check"})
    assert resp.status_code == 200
    assert "HEALTH CHECKS" in resp.json()["result"]


def test_endpoint_web_servers():
    with patch.object(mod, "check_web_servers", return_value=[]):
        with patch.object(mod, "get_application_ports", return_value=[]):
            resp = client.post("/", json={"query": "web servers"})
    assert resp.status_code == 200
    assert "WEB SERVERS" in resp.json()["result"]


def test_endpoint_database():
    with patch.object(mod.psutil, "net_connections", return_value=[]):
        resp = client.post("/", json={"query": "database connections"})
    assert resp.status_code == 200
    assert "DATABASE" in resp.json()["result"]


def test_endpoint_services():
    with patch.object(mod.psutil, "process_iter", return_value=iter([])):
        resp = client.post("/", json={"query": "system services"})
    assert resp.status_code == 200
    assert "SYSTEM SERVICES" in resp.json()["result"]


def test_endpoint_monitoring():
    with patch.object(mod, "perform_health_checks", return_value=[]):
        with patch.object(mod, "check_web_servers", return_value=[]):
            with patch.object(mod, "check_database_connections", return_value=[]):
                with patch.object(mod, "get_application_ports", return_value=[]):
                    resp = client.post("/", json={"query": "monitoring overview"})
    assert resp.status_code == 200
    assert "MONITORING" in resp.json()["result"]


def test_endpoint_default():
    with patch.object(mod.psutil, "net_connections", return_value=[]):
        resp = client.post("/", json={"query": "status"})
    assert resp.status_code == 200
    assert "APPLICATION OVERVIEW" in resp.json()["result"]
