"""Guardrail utilities for agent safety and compliance"""
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class Guardrail:
    """Guardrail system for monitoring and controlling agent behavior"""

    def __init__(self):
        self.enabled = False
        self.rules = {}

    def patch_agent(self, agent, guardrails_dict: Dict[str, Any] = None):
        """
        Patch an agent with guardrails

        Args:
            agent: The agent instance to patch
            guardrails_dict: Dictionary of guardrail configurations
        """
        if guardrails_dict is None:
            guardrails_dict = {}

        self.enabled = len(guardrails_dict) > 0
        self.rules = guardrails_dict

        if self.enabled:
            logger.info(f"Guardrails enabled with {len(self.rules)} rules")
        else:
            logger.info("Guardrails disabled - no rules specified")

        # In a full implementation, this would add monitoring hooks to the agent
        # For now, we just log that patching occurred
        return agent

    def validate_input(self, input_text: str) -> bool:
        """Validate user input against guardrails"""
        if not self.enabled:
            return True

        # Validate input is not empty
        if not input_text or not input_text.strip():
            logger.warning("Empty input detected")
            return False

        # Apply custom rules if configured
        if "input_rules" in self.rules:
            for rule_name, rule_config in self.rules["input_rules"].items():
                if not self._apply_rule(input_text, rule_config):
                    logger.warning(f"Input failed validation rule: {rule_name}")
                    return False

        return True

    def validate_output(self, output_text: str) -> bool:
        """Validate agent output against guardrails"""
        if not self.enabled:
            return True

        # Validate output is not empty
        if not output_text or not output_text.strip():
            logger.warning("Empty output detected")
            return False

        # Apply custom rules if configured
        if "output_rules" in self.rules:
            for rule_name, rule_config in self.rules["output_rules"].items():
                if not self._apply_rule(output_text, rule_config):
                    logger.warning(f"Output failed validation rule: {rule_name}")
                    return False

        return True

    def _apply_rule(self, text: str, rule_config: Dict[str, Any]) -> bool:
        """
        Apply a specific validation rule to text

        Args:
            text: The text to validate
            rule_config: Configuration for the rule

        Returns:
            True if validation passes, False otherwise
        """
        if not rule_config:
            return True

        # Check for blocked patterns
        if "blocked_patterns" in rule_config:
            for pattern in rule_config["blocked_patterns"]:
                if pattern.lower() in text.lower():
                    return False

        # Check minimum length
        if "min_length" in rule_config and len(text) < rule_config["min_length"]:
            return False

        # Check maximum length
        if "max_length" in rule_config and len(text) > rule_config["max_length"]:
            return False

        return True
