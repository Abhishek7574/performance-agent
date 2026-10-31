import logging
import sys
import os
from os import getenv
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

logger = logging.getLogger(__name__)
parent_dir = os.path.join(os.path.dirname(__file__), '..', '..')
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from agentic_utils.llm.llm_picker import llmManager
from agentic_utils.prompts.prompt_manager import promptManager
from agentic_utils.guardrail import guardrail

# Import sub-agents
from agents.sre_agent.agent import sre_agent          # owns: CloudLens IaC + digital_twin_agent (load test gatekeeper)
from agents.platform_monitor_agent.agent import platform_monitor_agent

AGENT_MODEL = llmManager.pick(name="Azure GPT 4o")

# Use Master prompt for orchestration
instructions = promptManager.pick(prompts=['Master'])

APP_NAME = "honeywell_multi_agent"
USER_ID = "admin"
SESSION_ID = None
SESSION_DB_URL = getenv("SESSION_DB_URL", "sqlite+aiosqlite:///./sessions.db")

# Master Agent: pure router. No tools of its own — it only delegates to a sub-agent.
# IaC + lifecycle → sre_agent (owns CloudLens IaC Tool).
# Status / health / metrics → platform_monitor_agent (owns Performance Tool).
# Digital-twin / virtual-device readiness → digital_twin_agent.
root_agent = Agent(
    name="master_agent",
    model=AGENT_MODEL,
    description="Master orchestration agent. Pure router — delegates every request to exactly one sub-agent. SRE Agent owns infrastructure (discovery, IaC generation, lifecycle) AND all load testing (it gates via infra check then delegates to Digital Twin). Platform Monitor Agent owns live status/health/metrics.",
    instruction=instructions,
    tools=[],
    sub_agents=[sre_agent, platform_monitor_agent],
    output_key="agent_output",
)

guardrails_dict = {}

guard = guardrail.Guardrail()
guard.patch_agent(root_agent, guardrails_dict)

session_service = InMemorySessionService()

runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)
