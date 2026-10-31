"""MCP Manager for loading and configuring MCP toolsets from portable-agent.yaml"""
import yaml
import os
import re
import json
import logging
from typing import List, Any

logger = logging.getLogger(__name__)


def _sanitize_variable_name(name: str) -> str:
    """Convert MCP name to valid Python variable name"""
    sanitized = re.sub(r'\W', '_', name.lower())
    sanitized = re.sub(r'_+', '_', sanitized)
    sanitized = sanitized.strip('_')
    if sanitized and sanitized[0].isdigit():
        sanitized = 'mcp_' + sanitized
    return sanitized


class McpManager:
    """Loads MCP toolsets from portable-agent.yaml"""

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

    def pick(self, mcp_names: List[str]) -> List[Any]:
        """
        Pick MCP toolsets by name from portable-agent.yaml.

        Args:
            mcp_names: List of MCP names (e.g., ['Azure Monitor MCP'])

        Returns:
            List of MCPToolset instances for Google ADK
        """
        mcp_entries = self.config.get('mCPS', [])
        if not mcp_entries:
            logger.warning("No mCPS entries found in portable-agent.yaml")
            return []

        try:
            from google.adk.tools.mcp_tool.mcp_toolset import (
                MCPToolset,
                StreamableHTTPConnectionParams,
            )
        except ImportError as e:
            logger.error(f"Could not import google.adk MCP modules: {e}")
            return []

        result = []
        for name in mcp_names:
            toolset = self._create_mcp_toolset(name, mcp_entries, MCPToolset, StreamableHTTPConnectionParams)
            if toolset:
                result.append(toolset)
        return result

    def _create_mcp_toolset(self, name: str, mcp_entries: list, mcp_toolset_cls, connection_params_cls):
        """Create a single MCP toolset from configuration"""
        entry = next((m for m in mcp_entries if m.get('name') == name), None)
        if not entry:
            logger.warning(f"MCP '{name}' not found in portable-agent.yaml")
            return None

        mcp_type = entry.get('type', 'STREAMABLE_HTTP')
        if mcp_type not in ('STREAMABLE_HTTP', 'EXTERNAL_STREAMABLE_HTTP'):
            logger.warning(f"MCP type '{mcp_type}' for '{name}' is not supported in this local manager")
            return None

        deploy_url = entry.get('deployUrl', '')
        mcp_url = self._build_mcp_url(deploy_url)

        try:
            toolset = mcp_toolset_cls(
                connection_params=connection_params_cls(
                    url=mcp_url,
                    headers={'Content-Type': 'application/json'},
                    timeout=30,
                    sse_read_timeout=120,
                )
            )
            logger.info(f"Loaded MCP toolset: {name} at {mcp_url}")
            return toolset
        except Exception as e:
            logger.error(f"Failed to initialize MCPToolset for '{name}': {e}")
            return None

    def _build_mcp_url(self, deploy_url: str) -> str:
        """Build the /mcp endpoint URL"""
        if deploy_url and not deploy_url.rstrip('/').endswith('/mcp'):
            return deploy_url.rstrip('/') + '/mcp'
        return deploy_url


# Global instance
mcpManager = McpManager()
