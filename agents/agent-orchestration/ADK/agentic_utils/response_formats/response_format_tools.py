"""Response Format Detection Tool"""
from google.adk.tools import FunctionTool


def detect_response_format(query: str) -> str:
    """
    Detect the desired response format from the user's query.

    Args:
        query: The user's question or request

    Returns:
        The detected format type (e.g., 'text', 'json', 'markdown')
    """
    query_lower = query.lower()

    # Check for specific format requests
    if 'json' in query_lower or 'structured' in query_lower:
        return 'json'
    elif 'markdown' in query_lower or 'formatted' in query_lower:
        return 'markdown'
    elif 'table' in query_lower or 'tabular' in query_lower:
        return 'table'
    else:
        return 'text'


# Create the FunctionTool instance
response_format_detection_tool = FunctionTool(func=detect_response_format)
