"""Digital Twin Agent placeholder tools."""
from google.adk.tools import FunctionTool


def digital_twin_stub(action: str, device_config: str = "") -> str:
    """
    Placeholder for Digital Twin operations: creating virtual devices and running test scenarios.
    This tool will be connected to external Digital Twin APIs in a future sprint.

    Args:
        action: The requested action (e.g., 'create_twin', 'run_test', 'check_readiness').
        device_config: Optional device configuration or test scenario description.

    Returns:
        Placeholder acknowledgement message.
    """
    return (
        "DIGITAL TWIN — PLACEHOLDER RESPONSE\n"
        f"Action requested : {action}\n"
        f"Device config    : {device_config or '(none provided)'}\n\n"
        "Digital Twin API integration is pending. "
        "This agent will be wired to the external device simulation APIs in a future release. "
        "Proceeding with test execution using available infrastructure."
    )


digital_twin_stub_tool = FunctionTool(func=digital_twin_stub)
