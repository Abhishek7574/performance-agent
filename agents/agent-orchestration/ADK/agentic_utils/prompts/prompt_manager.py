"""Prompt Manager for loading prompts from portable-agent.yaml"""
import yaml
import os
from typing import List


class PromptManager:
    """Manages prompts from portable-agent.yaml"""

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

    def pick(self, prompts: List[str]) -> str:
        """
        Pick prompts by name from the configuration

        Args:
            prompts: List of prompt names to load (e.g., ['Base'])

        Returns:
            Combined prompt instructions as a string
        """
        if not self.config or 'prompt' not in self.config:
            return "You are a helpful assistant."

        prompt_configs = self.config['prompt']
        combined_prompts = []

        for prompt_name in prompts:
            for prompt_config in prompt_configs:
                if prompt_config.get('name') == prompt_name:
                    combined_prompts.append(prompt_config.get('description', ''))

        return '\n\n'.join(combined_prompts) if combined_prompts else "You are a helpful assistant."


# Global instance
promptManager = PromptManager()
