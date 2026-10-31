"""Tests for tools/loganalytics/CODE_EXECUTION/main.py"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import load_tool

mod = load_tool("loganalytics")
client = TestClient(mod.app)

_DEMO_LOGS = mod.generate_demo_logs()
_DEMO_STUB = patch.object(mod, "get_system_logs", return_value=_DEMO_LOGS)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_generate_demo_logs():
    logs = mod.generate_demo_logs()
    assert len(logs) == 50


def test_get_log_statistics():
    stats = mod.get_log_statistics(_DEMO_LOGS)
    assert "total_entries" in stats
    assert stats["total_entries"] == 50


def test_health_status_good():
    assert "Good" in mod._get_health_status(0)


def test_health_status_warning():
    assert "Warning" in mod._get_health_status(10)


def test_health_status_critical():
    assert "Critical" in mod._get_health_status(25)


def test_endpoint_recent_logs():
    with _DEMO_STUB:
        resp = client.post("/", json={"query": "recent logs"})
    assert resp.status_code == 200
    assert "RECENT SYSTEM LOGS" in resp.json()["result"]


def test_endpoint_errors():
    with _DEMO_STUB:
        resp = client.post("/", json={"query": "errors"})
    assert resp.status_code == 200
    assert "ERROR PATTERN" in resp.json()["result"]


def test_endpoint_statistics():
    with _DEMO_STUB:
        resp = client.post("/", json={"query": "statistics"})
    assert resp.status_code == 200
    assert "STATISTICS" in resp.json()["result"]


def test_endpoint_monitoring():
    with _DEMO_STUB:
        resp = client.post("/", json={"query": "real-time monitoring"})
    assert resp.status_code == 200
    assert "MONITORING" in resp.json()["result"]


def test_endpoint_search_with_term():
    with _DEMO_STUB:
        resp = client.post("/", json={"query": "search for authentication"})
    assert resp.status_code == 200
    assert "SEARCH RESULTS" in resp.json()["result"]


def test_endpoint_search_no_term():
    with _DEMO_STUB:
        resp = client.post("/", json={"query": "search"})
    assert resp.status_code == 200
    assert "specify a search term" in resp.json()["result"]


def test_endpoint_default():
    with _DEMO_STUB:
        resp = client.post("/", json={"query": "what is in the logs"})
    assert resp.status_code == 200
    assert "LOG ANALYTICS OVERVIEW" in resp.json()["result"]
