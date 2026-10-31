"""Tests for tools/dashboardtool/CODE_EXECUTION/main.py"""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from tests.helpers import load_tool

mod = load_tool("dashboardtool")
client = TestClient(mod.app)


def _psutil_patches():
    mem = MagicMock(percent=60.0, available=4 * 1024 ** 3)
    disk = MagicMock(percent=40.0, free=50 * 1024 ** 3)
    proc = MagicMock()
    proc.info = {"pid": 1, "name": "proc", "cpu_percent": 2.0, "memory_percent": 1.0}
    return [
        patch.object(mod.psutil, "cpu_percent", return_value=30.0),
        patch.object(mod.psutil, "virtual_memory", return_value=mem),
        patch.object(mod.psutil, "disk_usage", return_value=disk),
        patch.object(mod.psutil, "boot_time", return_value=1_000_000.0),
        patch.object(mod.psutil, "process_iter", return_value=iter([proc])),
    ]


def _start_patches(patches):
    for p in patches:
        p.start()


def _stop_patches(patches):
    for p in patches:
        p.stop()


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_create_ascii_chart_normal():
    result = mod.create_ascii_chart([10.0, 50.0, 100.0], width=10)
    assert "█" in result


def test_create_ascii_chart_empty():
    assert "No data" in mod.create_ascii_chart([])


def test_get_status_icon():
    assert mod._get_status_icon("online") == "🟢"
    assert mod._get_status_icon("offline") == "🔴"
    assert mod._get_status_icon("error") == "🟡"


def test_endpoint_live():
    ps = _psutil_patches()
    _start_patches(ps)
    try:
        resp = client.post("/", json={"query": "live dashboard"})
        assert resp.status_code == 200
    finally:
        _stop_patches(ps)


def test_endpoint_summary():
    ps = _psutil_patches()
    _start_patches(ps)
    try:
        resp = client.post("/", json={"query": "summary overview"})
        assert resp.status_code == 200
        assert "DASHBOARD" in resp.json()["result"]
    finally:
        _stop_patches(ps)


def test_endpoint_metrics():
    ps = _psutil_patches()
    _start_patches(ps)
    try:
        resp = client.post("/", json={"query": "metrics feed"})
        assert resp.status_code == 200
        assert "METRICS" in resp.json()["result"]
    finally:
        _stop_patches(ps)


def test_endpoint_health_status():
    mock_resp = MagicMock(status_code=200)
    mock_resp.elapsed.total_seconds.return_value = 0.05
    with patch.object(mod.requests, "get", return_value=mock_resp):
        resp = client.post("/", json={"query": "health status"})
    assert resp.status_code == 200
    assert "HEALTH CHECK" in resp.json()["result"]


def test_endpoint_default():
    ps = _psutil_patches()
    _start_patches(ps)
    try:
        resp = client.post("/", json={"query": "show dashboard"})
        assert resp.status_code == 200
        assert "DASHBOARD" in resp.json()["result"]
    finally:
        _stop_patches(ps)
