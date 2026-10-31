"""LLM Model Picker for Google ADK"""
import yaml
import os
from google.adk.models.lite_llm import LiteLlm


class LLMPicker:
    """Picks and configures LLM models from portable-agent.yaml"""

    def __init__(self):
        self.config = self._load_config()

    def _load_config(self):
        """Load configuration from portable-agent.yaml"""
        # Look for portable-agent.yaml in the agent directory
        current_dir = os.path.dirname(os.path.abspath(__file__))
        agent_dir = os.path.dirname(os.path.dirname(current_dir))
        config_path = os.path.join(agent_dir, '..', 'portable-agent.yaml')

        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return yaml.safe_load(f)
        return {}

    def pick(self, name: str):
        """
        Pick an LLM model by name from the configuration

        Args:
            name: The name of the LLM model (e.g., "Azure GPT 4o")

        Returns:
            Configured LiteLlm model instance
        """
        if not self.config or 'llm' not in self.config:
            raise ValueError("No LLM configuration found in portable-agent.yaml")

        llm_config = self.config['llm'].get(name)
        if not llm_config:
            raise ValueError(f"LLM '{name}' not found in configuration")

        # Create LiteLlm instance with the configuration
        return LiteLlm(
            model=llm_config.get('model', 'azure/iCETS-MSCoE-GPT-4o'),
            api_base=llm_config.get('api_base'),
            api_key=llm_config.get('api_key'),
            api_version=llm_config.get('api_version'),
            temperature=llm_config.get('temperature', 0.3),
            timeout=llm_config.get('timeout', 120),
        )


# Global instance
llmManager = LLMPicker()
