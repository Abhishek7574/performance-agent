"""Tests for tools/processmonitor/CODE_EXECUTION/main.py"""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tests.helpers import load_tool

mod = load_tool("processmonitor")
client = TestClient(mod.app)

_FAKE_CPU_PROCS = [
    {"name": "proc_a", "pid": 1, "cpu_percent": 5.0, "memory_percent": 1.0, "status": "running"}
]
_FAKE_MEM_PROCS = [
    {"name": "proc_a", "pid": 1, "memory_percent": 5.0, "memory_mb": "10.00 MB", "cpu_percent": 1.0}
]
_FAKE_STATUS = {"running": 3, "sleeping": 10}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_format_top_cpu_processes():
    result = mod._format_top_cpu_processes(_FAKE_CPU_PROCS)
    assert "CPU" in result


def test_format_status_count():
    result = mod._format_status_count(_FAKE_STATUS)
    assert "Total Processes" in result


def test_endpoint_top_cpu():
    with patch.object(mod, "get_top_processes_by_cpu", return_value=_FAKE_CPU_PROCS):
        resp = client.post("/", json={"query": "top cpu processes"})
    assert resp.status_code == 200
    assert "CPU" in resp.json()["result"]


def test_endpoint_top_memory():
    with patch.object(mod, "get_top_processes_by_memory", return_value=_FAKE_MEM_PROCS):
        resp = client.post("/", json={"query": "top memory processes"})
    assert resp.status_code == 200
    assert "MEMORY" in resp.json()["result"]


def test_endpoint_process_count():
    with patch.object(mod, "get_process_count_by_status", return_value=_FAKE_STATUS):
        resp = client.post("/", json={"query": "process count"})
    assert resp.status_code == 200
    assert "PROCESS STATUS" in resp.json()["result"]


def test_endpoint_all_processes():
    with patch.object(mod, "get_process_count_by_status", return_value=_FAKE_STATUS):
        with patch.object(mod, "get_top_processes_by_cpu", return_value=_FAKE_CPU_PROCS):
            with patch.object(mod, "get_top_processes_by_memory", return_value=_FAKE_MEM_PROCS):
                resp = client.post("/", json={"query": "all processes"})
    assert resp.status_code == 200
    assert "COMPREHENSIVE" in resp.json()["result"]


def test_endpoint_default():
    with patch.object(mod, "get_process_count_by_status", return_value=_FAKE_STATUS):
        resp = client.post("/", json={"query": "what is running"})
    assert resp.status_code == 200
    assert "QUICK" in resp.json()["result"]
