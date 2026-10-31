"""Tests for tools/performancetool/CODE_EXECUTION/main.py"""
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from tests.helpers import load_tool

mod = load_tool("performancetool")
client = TestClient(mod.app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_resolve_cloud_explicit():
    assert mod.resolve_cloud("show azure metrics") == "azure"


def test_resolve_cloud_default():
    assert mod.resolve_cloud("show metrics") == mod.DEFAULT_CLOUD


def test_resolve_capability_dashboard():
    assert mod.resolve_capability("show all services") == "dashboard"


def test_resolve_capability_performance():
    assert mod.resolve_capability("cpu usage metrics") == "performance"


def test_resolve_capability_logs():
    assert mod.resolve_capability("show error logs") == "logs"


def test_extract_text_with_content():
    block = MagicMock()
    block.text = "response text"
    result_obj = MagicMock()
    result_obj.content = [block]
    assert mod.extract_text(result_obj) == "response text"


def test_extract_text_empty():
    result_obj = MagicMock()
    result_obj.content = []
    assert isinstance(mod.extract_text(result_obj), str)


def test_endpoint_empty_query():
    resp = client.post("/", json={"query": ""})
    assert resp.status_code == 200
    assert "Error" in resp.json()["result"]


def test_endpoint_query_too_long():
    resp = client.post("/", json={"query": "x" * 5001})
    assert resp.status_code == 200
    assert "Error" in resp.json()["result"]


def test_endpoint_dispatches_to_mcp():
    with patch.object(mod, "call_cloud_mcp", new_callable=AsyncMock, return_value="ok"):
        resp = client.post("/", json={"query": "azure cpu metrics"})
    assert resp.status_code == 200
    assert resp.json()["result"] == "ok"


def test_endpoint_mcp_unavailable():
    with patch.object(mod, "call_cloud_mcp", new_callable=AsyncMock, side_effect=Exception("down")):
        resp = client.post("/", json={"query": "azure cpu metrics"})
    assert resp.status_code == 200
    assert "unavailable" in resp.json()["result"].lower()
