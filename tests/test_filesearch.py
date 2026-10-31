"""Tests for tools/filesearch/CODE_EXECUTION/main.py — pure mock, no filesystem."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import load_tool

mod = load_tool("filesearch")
client = TestClient(mod.app)

_FAKE_PATH = "/fake"
_CONSOLIDATE_OK = {"result": "### Consolidation Complete\n\nFiles Processed: 2", "output_path": "/fake/out.dat"}
_SEARCH_OK = {"result": "### Search Results\n\nMatches Found: 1\n  Line 1: match"}
_SEARCH_NONE = {"result": "No records matching 'xyz' found in the consolidated file."}


def test_post_root():
    resp = client.post("/", json={"query": "some query"})
    assert resp.status_code == 200


def test_parse_query_consolidate():
    result = mod.parse_query("consolidate files in /data/folder")
    assert result["action"] == "consolidate"


def test_parse_query_search():
    result = mod.parse_query("search for error in /data/folder")
    assert result["action"] == "search"
    assert "error" in result["keyword"]


def test_parse_query_fallback():
    result = mod.parse_query("random keyword text")
    assert result["action"] == "search_last"


def test_format_search_results_with_matches():
    matches = [(1, "match line here")]
    result = mod._format_search_results("match", "/fake/file.dat", matches, 100, 50)
    assert "Search Results" in result


def test_format_search_results_empty():
    result = mod._format_search_results("xyz", "/fake/file.dat", [], 100, 50)
    assert "No records" in result


def test_endpoint_consolidate():
    with patch.object(mod, "consolidate_dat_gz_files", return_value=_CONSOLIDATE_OK):
        with patch.object(mod, "parse_query", return_value={"action": "consolidate", "folder_path": _FAKE_PATH}):
            resp = client.post("/", json={"query": f"consolidate {_FAKE_PATH}"})
    assert resp.status_code == 200
    assert "Consolidation Complete" in resp.json()["result"]


def test_endpoint_search():
    with patch.object(mod, "search_consolidated_file", return_value=_SEARCH_OK):
        with patch.object(mod, "parse_query", return_value={"action": "search", "keyword": "error", "folder_path": _FAKE_PATH}):
            resp = client.post("/", json={"query": f"search for error in {_FAKE_PATH}"})
    assert resp.status_code == 200
    assert "Search Results" in resp.json()["result"]


def test_endpoint_search_last():
    with patch.object(mod, "search_consolidated_file", return_value=_SEARCH_NONE):
        with patch.object(mod, "parse_query", return_value={"action": "search_last", "keyword": "xyz"}):
            resp = client.post("/", json={"query": "xyz"})
    assert resp.status_code == 200


def test_endpoint_error_result():
    with patch.object(mod, "parse_query", return_value={"action": "consolidate", "folder_path": "/fake"}):
        with patch.object(mod, "consolidate_dat_gz_files", return_value={"error": "not found"}):
            resp = client.post("/", json={"query": "consolidate /fake"})
    assert resp.status_code == 200
    assert "Error" in resp.json()["result"]
