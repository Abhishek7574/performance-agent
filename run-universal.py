#!/usr/bin/env python3
"""
Launcher for Honeywell Agent and all tool services.
Usage:
  python run-universal.py           # Start everything
  python run-universal.py --tools   # Start tools only
  python run-universal.py --agent   # Start agent only
"""

import sys
import os
import subprocess
import time
import signal
from pathlib import Path

ROOT = Path(__file__).parent

TOOLS = [
    {"name": "Performance Tool",    "path": ROOT / "tools/performancetool/CODE_EXECUTION",   "port": 6103},
    {"name": "Process Monitor",     "path": ROOT / "tools/processmonitor/CODE_EXECUTION",    "port": 6104},
    {"name": "Security Monitor",    "path": ROOT / "tools/securitymonitor/CODE_EXECUTION",   "port": 6105},
    {"name": "Log Analytics",       "path": ROOT / "tools/loganalytics/CODE_EXECUTION",      "port": 6106},
    {"name": "Application Monitor", "path": ROOT / "tools/applicationmonitor/CODE_EXECUTION","port": 6107},
    {"name": "Dashboard Tool",      "path": ROOT / "tools/dashboardtool/CODE_EXECUTION",     "port": 6108},
    {"name": "File Count Tool",     "path": ROOT / "tools/filecount/CODE_EXECUTION",         "port": 6110},
    {"name": "File Search Tool",    "path": ROOT / "tools/filesearch/CODE_EXECUTION",        "port": 6111},
    {"name": "CloudLens IaC Tool",  "path": ROOT / "tools/cloudlens_iac/CODE_EXECUTION",    "port": 6212},
    {"name": "RAG Search",          "path": ROOT / "tools/ragsearch/CODE_EXECUTION",         "port": 8002},
    {"name": "Blob Log Reader",     "path": ROOT / "tools/blobreader/CODE_EXECUTION",        "port": 6115},
]

MCP_SERVER = {"name": "Azure Monitor MCP", "path": ROOT / "mcp_servers/Azure_Monitor_MCP/MCP", "port": 6200}

AGENT = {
    "name": "Honeywell Agent",
    "path": ROOT / "agents/agent-orchestration/ADK",
    "port": 8000,
}

processes = []

def start_tool(tool):
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(tool["port"])],
        cwd=tool["path"],
    )
    print(f"  Started {tool['name']} on port {tool['port']} (pid {proc.pid})")
    return proc

def start_mcp():
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=MCP_SERVER["path"],
    )
    print(f"  Started {MCP_SERVER['name']} on port {MCP_SERVER['port']} (pid {proc.pid})")
    return proc

def start_agent():
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=AGENT["path"],
    )
    print(f"  Started {AGENT['name']} on port {AGENT['port']} (pid {proc.pid})")
    return proc

def shutdown(sig, frame):
    print("\nShutting down all services...")
    for p in processes:
        p.terminate()
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

args = sys.argv[1:]
run_tools = "--agent" not in args
run_agent = "--tools" not in args

print("=" * 50)
print("  Honeywell Agentic AI Platform")
print("=" * 50)

if run_tools:
    print("\nStarting tools...")
    for tool in TOOLS:
        processes.append(start_tool(tool))
    print("  Starting MCP server...")
    processes.append(start_mcp())
    print("Waiting for tools to be ready...")
    time.sleep(5)

if run_agent:
    print("\nStarting agent...")
    processes.append(start_agent())
    print(f"\nAgent UI: http://127.0.0.1:{AGENT['port']}")
    print(f"Chat API: http://127.0.0.1:{AGENT['port']}/api/chat")

print("\nAll services running. Press Ctrl+C to stop.\n")

for p in processes:
    p.wait()
