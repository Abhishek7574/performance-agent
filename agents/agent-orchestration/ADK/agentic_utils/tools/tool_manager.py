"""Tool Manager for loading and configuring tools from portable-agent.yaml"""
import yaml
import os
import requests
from typing import List, Dict, Any
from google.adk.tools import FunctionTool
import logging

logger = logging.getLogger(__name__)


class ToolManager:
    """Manages tools from portable-agent.yaml"""

    def __init__(self):
        self.config = self._load_config()

    def _load_config(self):
        """Load configuration from portable-agent.yaml"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        agent_dir = os.path.dirname(os.path.dirname(current_dir))
        config_path = os.path.join(agent_dir, '..', 'portable-agent.yaml')

        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return yaml.safe_load(f)
        return {}

    def _create_tool_function(self, tool_name: str, deploy_url: str, description: str):
        """Create a function that calls the tool API"""
        def tool_function(query: str) -> str:
            """
            Execute the tool with the given query.

            Args:
                query: The user's question or request

            Returns:
                The tool's response as a string
            """
            try:
                logger.info(f"Calling {tool_name} at {deploy_url} with query: {query}")
                response = requests.post(
                    deploy_url,
                    json={"query": query},
                    timeout=120
                )

                if response.status_code == 200:
                    result = response.json()
                    return result.get("result", str(result))
                else:
                    return f"Error calling {tool_name}: HTTP {response.status_code}"

            except Exception as e:
                logger.error(f"Error calling {tool_name}: {e}")
                return f"Error calling {tool_name}: {str(e)}"

        # Set function metadata
        tool_function.__name__ = tool_name.lower().replace(' ', '_').replace('-', '_')
        tool_function.__doc__ = description

        return tool_function

    def pick(self, tool_names: List[str]) -> List[FunctionTool]:
        """
        Pick tools by name from the configuration

        Args:
            tool_names: List of tool names to load

        Returns:
            List of configured FunctionTool instances
        """
        if not self.config or 'tools' not in self.config:
            logger.warning("No tools configuration found in portable-agent.yaml")
            return []

        tools = []
        tool_configs = self.config['tools']

        for tool_name in tool_names:
            # Find the tool configuration
            tool_config = None
            for config in tool_configs:
                if config.get('name') == tool_name:
                    tool_config = config
                    break

            if not tool_config:
                logger.warning(f"Tool '{tool_name}' not found in configuration")
                continue

            # Create the tool function
            deploy_url = tool_config.get('deployUrl', '')
            description = tool_config.get('description', f'{tool_name} function')

            tool_func = self._create_tool_function(tool_name, deploy_url, description)

            # Wrap in FunctionTool for Google ADK
            function_tool = FunctionTool(func=tool_func)
            tools.append(function_tool)

            logger.info(f"Loaded tool: {tool_name} from {deploy_url}")

        return tools


# Global instance
toolManager = ToolManager()
