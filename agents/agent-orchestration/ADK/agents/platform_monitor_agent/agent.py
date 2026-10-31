"""Platform Monitor Agent - monitors Azure application health and infrastructure."""
import os
import sys
from os import getenv

from google.adk.agents import Agent

# Add parent directory to path for imports
parent_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..')
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from agentic_utils.llm.llm_picker import llmManager
from agentic_utils.prompts.prompt_manager import promptManager
from agentic_utils.tools.tool_manager import toolManager
from agentic_utils.response_formats.response_format_tools import response_format_detection_tool

AGENT_MODEL = llmManager.pick(name="Azure GPT 4o")

# Use Base prompt for platform monitoring
instructions = promptManager.pick(prompts=['Base'])

monitoring_mode = getenv("MONITORING_MODE", "PROD")

if monitoring_mode == "local":
    # Local mode: register local psutil-based tools
    tools_list = toolManager.pick(
        tool_names=['Performance Tool', 'Process Monitor', 'Security Monitor', 'Log Analytics', 'Application Monitor', 'Dashboard Tool'],
    ) + [response_format_detection_tool]
else:
    # PROD mode: Performance Tool is the cloud gateway
    tools_list = toolManager.pick(
        tool_names=['Performance Tool'],
    ) + [response_format_detection_tool]

platform_monitor_agent = Agent(
    name="platform_monitor_agent",
    model=AGENT_MODEL,
    description="Monitors Azure application health, services, and data-layer metrics. Use for infrastructure performance queries, database status, logs, and real-time dashboards.",
    instruction=instructions,
    tools=tools_list,
    output_key="agent_output",
)
