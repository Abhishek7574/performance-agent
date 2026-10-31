"""Digital Twin Agent - placeholder for virtual device management and Doppelio load-testing integration."""
import os
import sys

from google.adk.agents import Agent

# Add parent directory to path for imports
parent_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..')
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from agentic_utils.llm.llm_picker import llmManager
from agentic_utils.prompts.prompt_manager import promptManager
from agentic_utils.tools.doppelio_tools import ALL_DOPPELIO_TOOLS
from agentic_utils.tools.doppelio_orchestrator import run_load_test_auto_tool, attach_to_session_tool, stop_active_test_tool, stop_all_tests_tool
from agentic_utils.tools.telemetry_validation_tools import ALL_VALIDATION_TOOLS
from agentic_utils.tools.db_insights import ALL_INSIGHTS_TOOLS

AGENT_MODEL = llmManager.pick(name="Azure GPT 4o")

instructions = promptManager.pick(prompts=['DigitalTwin'])

digital_twin_agent = Agent(
    name="digital_twin_agent",
    model=AGENT_MODEL,
    description=(
        "Digital Twin Agent for Doppelio load testing. "
        "Handles the full load-test lifecycle via NLP: upload device CSV, create doppels, "
        "verify doppels, fetch test suites, run tests, check status and progress, "
        "stream telemetry metadata to Azure Function App, and stop tests. "
        "Can list currently running tests and attach live monitoring to any selected session."
    ),
    instruction=instructions,
    tools=[run_load_test_auto_tool, attach_to_session_tool, stop_active_test_tool, stop_all_tests_tool, *ALL_DOPPELIO_TOOLS, *ALL_VALIDATION_TOOLS, *ALL_INSIGHTS_TOOLS],
    output_key="agent_output",
)
