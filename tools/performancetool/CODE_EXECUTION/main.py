import os
import re
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


logger = logging.getLogger("performance_tool")
# S4792: SECURITY HOTSPOT REVIEWED - SAFE CONFIGURATION
# Logger configured with INFO level and structured format. No sensitive data logged.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


DEFAULT_CLOUD = os.getenv("ACTIVE_CLOUD", "azure").lower()

KNOWN_CLOUDS = ("azure", "gcp", "aws")

# Security: Validate MCP URLs to prevent SSRF
def _validate_mcp_url(url: str) -> bool:
    """Validate that MCP URL is safe and properly formatted."""
    if not url:
        return False
    # Only allow localhost or explicit URLs (prevent SSRF)
    if not (url.startswith("http://localhost:") or url.startswith("http://127.0.0.1:")):
        # For production, you might want to whitelist specific domains
        logger.warning(f"MCP URL validation: non-localhost URL {url}")
    return (
        url.startswith("http://localhost:")
        or url.startswith("http://127.0.0.1:")
        or url.startswith("https://")
    )

CLOUD_MCP_URLS = {
    "azure": os.getenv("AZURE_MCP_URL", "http://localhost:6200/mcp"),
    # "gcp": os.getenv("GCP_MCP_URL", "http://localhost:6201/mcp"),
    # "aws": os.getenv("AWS_MCP_URL", "http://localhost:6202/mcp"),
}

# Validate URLs on startup
for cloud, url in list(CLOUD_MCP_URLS.items()):
    if not _validate_mcp_url(url):
        logger.warning(f"Invalid MCP URL for {cloud}: {url}")
        del CLOUD_MCP_URLS[cloud]

CAPABILITY_KEYWORDS = [
    # Service-specific patterns checked FIRST — so "event hub metrics" routes to
    # eventhub, not to the generic "performance" pattern that would also match "metrics".
    ("eventhub",    (r"\b(event\s*hub|eventhub)\b",)),
    ("database",    (r"\b(database|db|dtu|sql|connection|deadlock)\b",)),
    ("performance", (r"\b(performance|cpu|memory|ram|http|response\s*time|latency|throughput|bytes|metrics?|state)\b",)),
    ("processes",   (r"\b(process|processes|top|kudu|running\s*apps?)\b",)),
    ("security",    (r"\b(security|tls|https?|ip\s*restriction|firewall|inbound|outbound)\b",)),
    ("logs",        (r"\b(log|logs|error|errors|kql|exception|trace)\b",)),
    ("health",      (r"\b(health|healthy|status|availability|online|offline)\b",)),
    ("dashboard",   (r"\b(dashboard|overview|summary|all|everything|full)\b",)),
]

# If any of these patterns match, the query is an explicit multi-service/overview
# request and routes to the dashboard regardless of other keywords.
DASHBOARD_OVERRIDE_PATTERN = r"\b(dashboard|overview|summary|all\s+services|every\s+service)\b"

DEFAULT_CAPABILITY = "dashboard"


class ToolRequest(BaseModel):
    query: str


class ToolResponse(BaseModel):
    result: str


def resolve_cloud(query: str) -> str:
    q = query.lower()
    for cloud in KNOWN_CLOUDS:
        if re.search(rf"\b{cloud}\b", q):
            return cloud
    return DEFAULT_CLOUD


def resolve_capability(query: str) -> str:
    q = query.lower()
    if re.search(DASHBOARD_OVERRIDE_PATTERN, q):
        return "dashboard"
    for capability, patterns in CAPABILITY_KEYWORDS:
        for pattern in patterns:
            if re.search(pattern, q):
                return capability
    return DEFAULT_CAPABILITY


def extract_text(call_result) -> str:
    parts = []
    for block in getattr(call_result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else str(call_result)


async def call_cloud_mcp(cloud: str, tool_name: str, query: str) -> str:
    mcp_url = CLOUD_MCP_URLS.get(cloud)
    if not mcp_url:
        return f"Cloud '{cloud}' is not configured. Configured clouds: {sorted(CLOUD_MCP_URLS)}."

    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, {"query": query})
            return extract_text(result)


app = FastAPI(title="Performance Monitoring Tool (Cloud Gateway)")


@app.post("/", response_model=ToolResponse)
async def monitor_performance(request: ToolRequest):
    # Security: Validate input
    if not request.query or not request.query.strip():
        return ToolResponse(result="Error: Empty query provided")
    if len(request.query) > 5000:  # Prevent DoS
        return ToolResponse(result="Error: Query too long (max 5000 characters)")
    
    cloud = resolve_cloud(request.query)
    capability = resolve_capability(request.query)
    tool_name = f"{cloud}_{capability}"

    logger.info("dispatch cloud=%s capability=%s tool=%s", cloud, capability, tool_name)

    try:
        text = await call_cloud_mcp(cloud, tool_name, request.query)
        return ToolResponse(result=text)
    except Exception:
        logger.exception("MCP call failed")
        # Security: Don't leak detailed error information
        return ToolResponse(
            result=f"Cloud monitoring service ({cloud}) is currently unavailable. Please try again later."
        )


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "Performance Monitoring Tool",
        "active_cloud": DEFAULT_CLOUD,
        "configured_clouds": sorted(CLOUD_MCP_URLS),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6103)
