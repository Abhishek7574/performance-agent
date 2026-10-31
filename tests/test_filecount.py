"""Tests for tools/filecount/CODE_EXECUTION/main.py — pure mock, no filesystem."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import load_tool

mod = load_tool("filecount")
client = TestClient(mod.app)

_FAKE_DIR_PATH = "/fake/path"
_FAKE_RESULT = {
    "directory_path": _FAKE_DIR_PATH,
    "file_count": 3,
    "directory_count": 1,
    "total_items": 4,
    "total_size": "1.0 KB",
    "total_size_bytes": 1024,
    "timestamp": "2024-01-01T00:00:00",
    "items": [
        {"name": "a.txt", "type": "file", "size": 512},
        {"name": "b.txt", "type": "file", "size": 512},
        {"name": "sub", "type": "directory", "size": None},
    ],
}

_FAKE_RECURSIVE = {**_FAKE_RESULT, "recursive": True}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_format_size_zero():
    assert mod.format_size(0) == "0 B"


def test_format_size_kb():
    assert "KB" in mod.format_size(2048)


def test_format_size_mb():
    assert "MB" in mod.format_size(5 * 1024 * 1024)


def test_format_standard_result():
    result = mod._format_standard_result(_FAKE_RESULT)
    assert "File Count Results" in result
    assert "a.txt" in result


def test_format_recursive_result():
    result = mod._format_recursive_result(_FAKE_RECURSIVE)
    assert "Recursive File Count" in result


def test_endpoint_non_recursive():
    with patch.object(mod, "parse_folder_path", return_value=_FAKE_DIR_PATH):
        with patch.object(mod, "count_files_in_directory", return_value=_FAKE_RESULT):
            resp = client.post("/", json={"query": f"count files in {_FAKE_DIR_PATH}"})
    assert resp.status_code == 200
    assert "File Count Results" in resp.json()["result"]


def test_endpoint_recursive():
    with patch.object(mod, "parse_folder_path", return_value=_FAKE_DIR_PATH):
        with patch.object(mod, "count_files_recursively", return_value=_FAKE_RECURSIVE):
            resp = client.post("/", json={"query": f"count files in {_FAKE_DIR_PATH} recursively"})
    assert resp.status_code == 200
    assert "Recursive" in resp.json()["result"]


def test_endpoint_no_path():
    with patch.object(mod, "parse_folder_path", return_value=""):
        resp = client.post("/", json={"query": ""})
    assert resp.status_code in (400, 422)
