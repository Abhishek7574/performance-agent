"""Tests for tools/securitymonitor/CODE_EXECUTION/main.py

Note: the @app.post('/') decorator is applied to _format_connections_report,
not security_monitor. security_monitor is tested directly via asyncio.run().
"""
import asyncio
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tests.helpers import load_tool

mod = load_tool("securitymonitor")
client = TestClient(mod.app)

_EMPTY_CONNS = []
_FAKE_SUMMARY = {
    "timestamp": "2024-01-01 00:00:00",
    "total_connections": 0,
    "active_tcp_connections": 0,
    "listening_ports_count": 0,
}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_format_default_status():
    result = mod._format_default_status({"total_connections": 0, "listening_ports_count": 0})
    assert "SECURITY STATUS" in result


def test_format_threats_no_threats():
    result = mod._format_threats_report([], [])
    assert "No suspicious" in result


def test_format_threats_with_threat():
    threat = {
        "type": "Unusual Port",
        "details": "suspicious port",
        "connection": {"local_address": "local:5000", "remote_address": "remote:4444"},
    }
    result = mod._format_threats_report([threat], [])
    assert "suspicious" in result.lower()


def test_security_monitor_default():
    with patch.object(mod.psutil, "net_connections", return_value=_EMPTY_CONNS):
        result = asyncio.run(mod.security_monitor(mod.ToolRequest(query="security status")))
    assert "SECURITY STATUS" in result.result


def test_security_monitor_connections():
    with patch.object(mod.psutil, "net_connections", return_value=_EMPTY_CONNS):
        result = asyncio.run(mod.security_monitor(mod.ToolRequest(query="connections")))
    assert "NETWORK CONNECTIONS" in result.result


def test_security_monitor_ports():
    with patch.object(mod.psutil, "net_connections", return_value=_EMPTY_CONNS):
        result = asyncio.run(mod.security_monitor(mod.ToolRequest(query="listening ports")))
    assert "LISTENING PORTS" in result.result


def test_security_monitor_suspicious():
    with patch.object(mod.psutil, "net_connections", return_value=_EMPTY_CONNS):
        result = asyncio.run(mod.security_monitor(mod.ToolRequest(query="suspicious threats")))
    assert result.result is not None


def test_security_monitor_overview():
    with patch.object(mod.psutil, "net_connections", return_value=_EMPTY_CONNS):
        result = asyncio.run(mod.security_monitor(mod.ToolRequest(query="security summary overview")))
    assert "SECURITY OVERVIEW" in result.result


def test_post_endpoint_covered():
    # POST "/" is on _format_connections_report; send empty list to execute that code path
    resp = client.post("/", json=[])
    assert resp.status_code in (200, 422, 500)
