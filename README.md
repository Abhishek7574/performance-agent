# Agentic AI Performance Validation Framework

> An LLM-powered multi-agent system that automates end-to-end performance validation for distributed IoT platforms — combining infrastructure alignment, digital twin simulation, and real-time observability.

---

## Problem Statement

In today's digital IoT landscape, organisations operate highly distributed, high-throughput platforms that process millions of transactions and device interactions in real time.

However, validating performance for such complex systems remains a significant challenge. Test environments often lack production parity, real-world conditions are difficult to simulate, and observability is fragmented.

As a result, organisations face unreliable performance benchmarks, delayed identification of critical issues, and repeated, high-cost validation cycles — increasing the risk of production failures, operational disruptions, and business impact.

---

## Solution Overview

To address these challenges, we introduce an **Agentic AI-driven performance validation framework** — a multi-agent system powered by LLM intelligence that automates and orchestrates end-to-end performance validation.

At the core, a **Master Agent** intelligently coordinates specialised agents to ensure:

### Production-Aligned Environments
The system validates and aligns test infrastructure with production configurations, ensuring reliable and realistic benchmarking.

### Realistic and Scalable Load Simulation
Digital Twin-based simulation replicates real-world device behaviour and traffic patterns at scale, eliminating assumptions and guesswork.

### Continuous, End-to-End Observability
Infrastructure, simulation, and application layers are monitored together in real time, enabling early detection of performance issues.

### Autonomous Insights and Decision Support
The framework correlates signals across the system, identifies bottlenecks, and provides actionable insights through a unified dashboard.

This transforms performance validation from a manual, reactive process into an **intelligent, autonomous, and production-relevant capability** — enabling faster, reliable, and confident go-live decisions.

---

## Architecture

### Agent Hierarchy

```
┌─────────────────────────────────────────────────────┐
│                   Master Agent                      │
│         (Intent Router — no tools of its own)       │
└──────────────┬──────────────┬──────────────┬────────┘
               │              │              │
       ┌───────▼──────┐ ┌─────▼──────┐ ┌────▼──────────┐
       │  SRE Agent   │ │  Platform  │ │ Digital Twin  │
       │              │ │  Monitor   │ │    Agent      │
       │ • Infra      │ │  Agent     │ │               │
       │   discovery  │ │            │ │ • Virtual     │
       │ • Terraform/ │ │ • Azure    │ │   device sim  │
       │   Bicep gen  │ │   Monitor  │ │ • Load test   │
       │ • Service    │ │ • Metrics  │ │   lifecycle   │
       │   lifecycle  │ │ • Dashbd   │ │ • Telemetry   │
       └──────┬───────┘ └─────┬──────┘ └────┬──────────┘
              │               │              │
       CloudLens IaC    Performance    Doppelio APIs
          Tool          Tool + MCP
```

### Tool Services

| Service | Port | Purpose |
|---|---|---|
| Performance Tool | 6103 | Azure Monitor gateway — CPU, memory, disk, network metrics |
| Process Monitor | 6104 | Local process & system monitoring (psutil) |
| Security Monitor | 6105 | Connections, threat detection, login attempts |
| Log Analytics | 6106 | System logs, error pattern detection |
| Application Monitor | 6107 | Service health, API status |
| Dashboard Tool | 6108 | Real-time metrics visualisation |
| File Count Tool | 6110 | File analysis and directory scanning |
| File Search Tool | 6111 | Consolidate `.dat.gz` files, search records |
| CloudLens IaC Tool | 6212 | Infrastructure discovery + Terraform/Bicep generation |
| RAG Search | 8002 | Vector-based semantic search |
| Azure Monitor MCP | 6200 | Model Context Protocol server for Azure Monitor |

### Data Flow

```
User Query
    │
    ▼
POST /api/chat
    │
    ▼
Master Agent (routes by intent)
    │
    ├─► SRE Agent ──────────► CloudLens IaC Tool ──► Azure Resource Graph
    │
    ├─► Platform Monitor ───► Performance Tool ──────► Azure Monitor APIs
    │         Agent            Azure Monitor MCP
    │
    └─► Digital Twin ────────► Doppelio APIs ──────── Virtual Devices
            Agent                                      Load Tests
                                                       Telemetry Stream
    │
    ▼
Markdown response + charts + suggested follow-up questions
```

---

## Prerequisites

- **Python 3.11+**
- **Azure subscription** with an Azure OpenAI resource (GPT-4o deployment)
- **Azure service principal** with read access to your target resource group
- **Doppelio account** and API token (for load testing features)
- Git

---

## Setup Instructions

### 1. Clone the Repository

```bash
git clone <repository-url>
cd performance-agent
```

### 2. Create a Virtual Environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Copy the template below into a `.env` file at the project root and fill in your values:

```dotenv
# ── Azure Authentication (Service Principal) ───────────────────────────────
AZURE_TENANT_ID=<your-tenant-id>
AZURE_CLIENT_ID=<your-client-id>
AZURE_CLIENT_SECRET=<your-client-secret>

# ── Azure OpenAI ────────────────────────────────────────────────────────────
AZURE_OPENAI_API_KEY=<your-api-key>
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT_NAME=<your-gpt4o-deployment-name>
AZURE_OPENAI_API_VERSION=2024-02-01

# ── Azure Resources ─────────────────────────────────────────────────────────
AZURE_SUBSCRIPTION_ID=<your-subscription-id>
AZURE_RESOURCE_GROUP=<your-resource-group>
AZURE_WEBAPP_NAME=<your-webapp-name>          # optional — for app monitoring
AZURE_DATABASE_SERVER=<your-sql-server-name>  # optional — for DB monitoring

# ── Monitoring Mode ─────────────────────────────────────────────────────────
# "PROD"  → queries Azure Monitor APIs (requires Azure credentials above)
# "local" → uses psutil for local process metrics (no Azure required)
MONITORING_MODE=PROD

# ── Doppelio Load Testing ────────────────────────────────────────────────────
DOPPELIO_TOKEN=<your-doppelio-api-token>
DOPPELIO_WEB_API_BASE=https://api.doppelio.com/doppelioWebAPI/api
DOPPELIO_CORE_BASE=https://api.doppelio.com/doppelio/api
DOPPELIO_FUNCTION_APP_ENDPOINT=http://localhost:7071/ingest
DOPPELIO_VERIFY_SSL=false                     # Set to 'true' in production
DOPPELIO_WORKSPACE=AiTemp                     # Doppelio workspace name
DOPPELIO_POLL_INTERVAL_SECS=30               # Telemetry polling interval
DOPPELIO_POST_TEST_DELAY_SECS=180            # Wait time after test completion
DOPPELIO_DOWNLOAD_MAX_RETRIES=10             # Max retries for downloads
DOPPELIO_DOWNLOAD_RETRY_INTERVAL_SECS=120    # Retry interval for downloads
DOPPELIO_SAMPLE_CSV_PATH=doppel_sample.csv   # Template CSV for device data
DOPPELIO_CSV_ID_COLUMN=device_id             # CSV column for device IDs
DOPPELIO_CSV_UPLOAD_DIR=/tmp/doppelio_uploads # Directory for CSV uploads
```

> **Tip:** Set `MONITORING_MODE=local` for a quick local demo without Azure credentials. The Platform Monitor Agent will use `psutil` instead of Azure Monitor.

### 5. Run Locally

```bash
python run-universal.py
```

This starts all tool microservices and the agent API concurrently. Selective startup:

```bash
python run-universal.py --tools    # start tool services only
python run-universal.py --agent    # start agent API only
```

Open the chat UI at **http://127.0.0.1:8000**

### 6. Azure Deployment

The project is Azure App Service-ready. The `app_startup.py` orchestrator starts all services before the gunicorn worker accepts traffic.

```bash
# Using gunicorn (production)
gunicorn app_startup:app --config gunicorn.conf.py

# Using uvicorn (alternative)
uvicorn app_startup:app --host 0.0.0.0 --port 8000
```

---

## Usage

### Chat UI

Navigate to `http://127.0.0.1:8000` for the built-in chat interface.

### REST API

**Send a query:**

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Show me the current CPU and memory usage"}'
```

**Health check:**

```bash
curl http://127.0.0.1:8000/health
```

### Example Queries

| Intent | Example Query |
|---|---|
| Infrastructure | `"Discover all resources in my resource group and generate a Terraform plan"` |
| Performance | `"Show me CPU, memory, and disk metrics for the last hour"` |
| Load Testing | `"Run a load test with 500 virtual devices for 10 minutes"` |
| Dashboards | `"Give me a real-time performance dashboard"` |
| Service Health | `"Is the application server healthy? Check all endpoints"` |

---

## Project Structure

```
performance-agent/
├── agents/
│   └── agent-orchestration/
│       └── ADK/
│           ├── agents/
│           │   ├── sre_agent/              # Infrastructure & IaC agent
│           │   ├── platform_monitor_agent/ # Azure monitoring agent
│           │   └── digital_twin_agent/     # Load testing agent
│           ├── master_agent/               # Intent router
│           ├── agentic_utils/              # Shared LLM, tools, prompts
│           └── _static/                    # Chat UI (chat.html)
├── agentic/
│   └── action_executor.py                  # IaC artifact storage + audit log
├── mcp_servers/                            # Azure Monitor MCP server
├── tools/                                  # FastAPI tool microservices
├── app_startup.py                          # Azure startup orchestrator
├── run-universal.py                        # Local multi-service launcher
├── main.py                                 # Azure entry point
├── gunicorn.conf.py                        # Production server config
├── requirements.txt                        # Python dependencies
└── .env                                    # Environment configuration
```

---

## Key Design Decisions

- **Google ADK 1.14+** — agent orchestration framework with built-in session management and streaming support
- **Azure OpenAI (GPT-4o)** via `litellm` — LLM backbone with token-efficient routing
- **Dual monitoring modes** — `PROD` (Azure Monitor APIs) and `local` (psutil) switchable via `MONITORING_MODE`
- **Agentic Action Layer** — all infrastructure operations go through `action_executor.py` which maintains an audit log and allowlists dangerous Azure operations
- **MCP protocol** — Azure Monitor exposed as a Model Context Protocol server, making it composable with other MCP-compatible agents

## Testing

The repository includes a `tests/` suite for the monitoring and tool components.

```bash
pytest
```

Tests that require cloud credentials or external services should be configured with safe development credentials and isolated test resources.

## Limitations and operational considerations

- Azure-dependent workflows require correctly scoped Azure credentials and configured resource identifiers.
- Doppelio-based load testing requires an available Doppelio account and token.
- The local monitoring mode is intended for development and does not replace production observability.
- Generated infrastructure plans and operational recommendations should be reviewed before being applied.

## Architecture documentation

See [docs/architecture.md](docs/architecture.md) for the high-level data flow, component responsibilities, and security considerations.
