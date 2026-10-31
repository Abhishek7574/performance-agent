"""SRE Agent - validates environment parity and monitors infrastructure."""
import os
import sys

from google.adk.agents import Agent

# Add parent directory to path for imports
parent_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..')
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from agentic_utils.llm.llm_picker import llmManager
from agentic_utils.prompts.prompt_manager import promptManager
from agentic_utils.tools.tool_manager import toolManager
from agentic_utils.tools.log_remediation_tools import (
    fetch_and_analyze_logs_tool,
    remediate_log_errors_tool,
    diagnose_and_fix_service_tool,
)
from agents.digital_twin_agent.agent import digital_twin_agent

AGENT_MODEL = llmManager.pick(name="Azure GPT 4o")

# Use SRE prompt
instructions = promptManager.pick(prompts=['SRE'])

# Get CloudLens IaC Tool from YAML registration
cloudlens_tool = toolManager.pick(['CloudLens IaC Tool'])

sre_agent = Agent(
    name="sre_agent",
    model=AGENT_MODEL,
    description="SRE (Site Reliability Engineering) agent. Discovers Azure infrastructure and generates production-ready Terraform/Bicep scripts via the CloudLens IaC tool. Also acts as infra gatekeeper for load tests — checks service health before delegating to the Digital Twin agent.",
    instruction=instructions,
    tools=cloudlens_tool + [
        fetch_and_analyze_logs_tool,
        remediate_log_errors_tool,
        diagnose_and_fix_service_tool,
    ],
    sub_agents=[digital_twin_agent],
    output_key="agent_output",
)
