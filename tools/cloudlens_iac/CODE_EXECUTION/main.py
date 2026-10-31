"""CloudLens IaC Tool - Preview Mode: Show Azure services needed for production (no provisioning)."""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import difflib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from pathlib import Path

# Make the project root importable so we can use the shared agentic package.
# Path: tools/cloudlens_iac/CODE_EXECUTION/main.py → project root is 4 levels up.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# IaC generation cache — 30-min TTL. The same Azure resource set doesn't
# change faster than this, so LLM calls for Terraform/Bicep are skipped on
# repeated requests with identical resources.
_ADK_DIR = _PROJECT_ROOT / "agents" / "agent-orchestration" / "ADK"
if str(_ADK_DIR) not in sys.path:
    sys.path.insert(0, str(_ADK_DIR))
try:
    from agentic_utils.cache.response_cache import ResponseCache as _ResponseCache
    _iac_cache = _ResponseCache(default_ttl=1800)
except Exception as _cache_err:
    _iac_cache = None
    print(f"Warning: ResponseCache unavailable ({_cache_err}); IaC LLM results won't be cached")

try:
    from agentic import action_executor as _ae
except Exception as _e:
    _ae = None
    print(f"Warning: agentic.action_executor not importable ({_e}); actions will be no-ops")

# Make azure_rest importable so cloudlens can read real Azure state directly.
_AZURE_REST_DIR = _PROJECT_ROOT / "mcp_servers" / "Azure_Monitor_MCP"
if str(_AZURE_REST_DIR) not in sys.path:
    sys.path.insert(0, str(_AZURE_REST_DIR))
try:
    import azure_rest as az
except Exception as _e:
    az = None
    print(f"Warning: azure_rest not importable ({_e}); cloudlens cannot read real Azure state")

# Load .env from project root (4 levels up from this file)
try:
    from dotenv import load_dotenv
    env_file = _PROJECT_ROOT / '.env'
    if env_file.exists():
        load_dotenv(env_file)
except ImportError:
    pass

import requests

app = FastAPI(title="CloudLens IaC Tool")

class ToolRequest(BaseModel):
    query: str

class ToolResponse(BaseModel):
    result: str

# Constants for duplicated strings
_CONTENT_TYPE_JSON = "application/json"
_AZURE_COMPUTE_VMS = "Microsoft.Compute/virtualMachines"
_AZURE_STORAGE_ACCOUNTS = "Microsoft.Storage/storageAccounts"
_AZURE_NETWORK_VNETS = "Microsoft.Network/virtualNetworks"
_AZURE_SQL_SERVERS = "Microsoft.Sql/servers"
_AZURE_WEB_SITES = "Microsoft.Web/sites"
_AZURE_EVENTHUB_NAMESPACES = "Microsoft.EventHub/namespaces"
_DISPLAY_APP_SERVICE = "App Service"
_DISPLAY_EVENT_HUB = "Event Hub"
_DISPLAY_VIRTUAL_MACHINE = "Virtual Machine"
_DISPLAY_SQL_SERVER = "SQL Server"
_DISPLAY_FUNCTION_APP = "Function App"
_AGENT_RECOMMENDATION_HEADER = "### 🤖 Agent Recommendation"
_ALL_SERVICES = "All services"

# Query keywords
_KEYWORD_EVENT_HUB = 'event hub'

# Code fence markers
_FENCE_HCL = "```hcl"

# Terraform repeated strings
_TF_RG_NAME = "resource_group_name = azurerm_resource_group.main.name"
_TF_LOCATION = "location            = azurerm_resource_group.main.location"
_BICEP_LOCATION = "location: location"
_WHAT_NEXT_HEADER = "### What would you like to do next?"

_last_resources = None


def _resource_graph_query(query: str, subscription_id: str, timeout: int = 30) -> List[Dict[str, Any]]:
    """POST a KQL query to Azure Resource Graph REST and return rows. Uses azure_rest's
    Service Principal token and session — same auth surface as the read path. Raises on failure."""
    if az is None:
        raise RuntimeError("azure_rest not importable; Azure auth unavailable")
    token = az._get_token("https://management.azure.com/.default")
    resp = az._session.post(
        "https://management.azure.com/providers/Microsoft.ResourceGraph/resources",
        params={"api-version": "2021-03-01"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": _CONTENT_TYPE_JSON,
        },
        json={"subscriptions": [subscription_id], "query": query},
        timeout=timeout,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Resource Graph query failed: HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json().get("data", []) or []


class CloudLensService:
    """Real Azure resource discovery + IaC generation via raw Resource Graph REST.
    No Azure SDK install needed — reuses azure_rest's Service Principal auth."""

    def __init__(self, subscription_id: Optional[str] = None):
        sub = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID")
        if not sub:
            raise ValueError("AZURE_SUBSCRIPTION_ID not set in environment")
        if az is None:
            raise RuntimeError("azure_rest not importable; cannot reach Azure")
        self.subscription_id: str = sub
        # Optional Azure OpenAI config — only required by the LLM-assisted Terraform/Bicep paths.
        self._azure_openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        self._azure_openai_key = os.getenv("AZURE_OPENAI_API_KEY")
        self._deployment_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

    def get_all_resources(self) -> List[Dict[str, Any]]:
        """Query every resource in the configured subscription."""
        rows = _resource_graph_query(
            query=(
                "resources "
                "| project id, name, type, resourceGroup, location, "
                "subscriptionId, kind, properties, tags, sku "
                "| order by tolower(type) asc, tolower(resourceGroup) asc, tolower(id) asc"
            ),
            subscription_id=self.subscription_id,
        )
        return [{
            'id': r.get('id'),
            'name': r.get('name'),
            'type': r.get('type'),
            'resourceGroup': r.get('resourceGroup'),
            'location': r.get('location'),
            'subscriptionId': r.get('subscriptionId') or self.subscription_id,
            'properties': r.get('properties') or {},
            'tags': r.get('tags') or {},
            'sku': r.get('sku') or {},
        } for r in rows]

    def get_resource_dependencies(self, resource_id: str) -> Dict[str, Any]:
        """Best-effort dependency extraction from a single resource's properties.
        Failures degrade to an empty list (cosmetic feature)."""
        try:
            rows = _resource_graph_query(
                query=f"resources | where id == '{resource_id}'",
                subscription_id=self.subscription_id,
                timeout=20,
            )
            if not rows:
                return {'resource_id': resource_id, 'dependencies': []}
            properties = rows[0].get('properties') or {}
            deps = (self._extract_dependencies(properties, resource_id)
                    if isinstance(properties, dict) else [])
            return {'resource_id': resource_id, 'dependencies': deps}
        except Exception:
            return {'resource_id': resource_id, 'dependencies': []}

    def _extract_dependencies(self, obj: Any, parent_id: str = "") -> List[Dict[str, Any]]:
        """Recursively extract dependency resource IDs from properties."""
        dependencies = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and '/subscriptions/' in value and '/resourceGroups/' in value:
                    if value != parent_id:
                        dependencies.append({'id': value, 'context': key})
                elif isinstance(value, (dict, list)):
                    dependencies.extend(self._extract_dependencies(value, parent_id))
        elif isinstance(obj, list):
            for item in obj:
                dependencies.extend(self._extract_dependencies(item, parent_id))
        return dependencies

    def generate_terraform_code(self, resources: List[Dict[str, Any]]) -> str:
        """Generate Terraform HCL for the given resources."""
        if not resources:
            return ""

        tf_code = """# Terraform configuration for Azure resources
# Generated using AI-powered LLM (GPT-4o)
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

"""
        for resource in resources:
            resource_tf = self._generate_terraform_resource(resource)
            tf_code += resource_tf + "\n"

        return tf_code

    def _generate_terraform_resource(self, resource: Dict[str, Any]) -> str:
        """Generate Terraform for a single resource."""
        try:
            if not self._azure_openai_endpoint or not self._azure_openai_key:
                return self._generate_terraform_resource_fallback(resource)
            return self._generate_terraform_with_llm(resource)
        except Exception:
            return self._generate_terraform_resource_fallback(resource)

    def _generate_terraform_with_llm(self, resource: Dict[str, Any]) -> str:
        """Generate Terraform using Azure OpenAI."""
        # Check cache before making an LLM call — same resource JSON = same output.
        if _iac_cache is not None:
            _tf_key = _iac_cache.make_key("tf:" + json.dumps(resource, sort_keys=True))
            _cached_tf = _iac_cache.get(_tf_key)
            if _cached_tf is not None:
                return _cached_tf

        try:
            prompt = f"""You are an expert Azure and Terraform engineer. Generate a Terraform resource block for this Azure resource.

Resource JSON:
{json.dumps(resource, indent=2)}

Requirements:
1. Use exact values from the JSON (no placeholders)
2. Resource name must match the original name
3. Use azurerm provider (version 3.0+)
4. Include all relevant properties
5. Add depends_on for any dependencies
6. Sanitize all identifiers to valid Terraform names
7. Include tags if present

Output ONLY the resource block, no markdown or extra text."""

            headers = {
                "api-key": self._azure_openai_key,
                "Content-Type": _CONTENT_TYPE_JSON
            }

            data = {
                "messages": [
                    {"role": "system", "content": "You are a Terraform expert."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 2000
            }

            url = f"{self._azure_openai_endpoint}/openai/deployments/{self._deployment_name}/chat/completions?api-version=2024-02-01"
            response = requests.post(url, headers=headers, json=data, timeout=30)

            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    content = result['choices'][0]['message']['content']
                    if 'resource "' in content:
                        if _iac_cache is not None:
                            _iac_cache.set(_tf_key, content)
                        return content

            return self._generate_terraform_resource_fallback(resource)
        except Exception:
            return self._generate_terraform_resource_fallback(resource)

    def _generate_terraform_resource_fallback(self, resource: Dict[str, Any]) -> str:
        """Generate basic Terraform template as fallback."""
        name = resource.get('name', 'resource').replace('-', '_').lower()
        resource_type = resource.get('type', '')
        location = resource.get('location', 'eastus')
        rg = resource.get('resourceGroup', 'resource_group')

        type_mapping = {
            _AZURE_COMPUTE_VMS: 'azurerm_linux_virtual_machine',
            _AZURE_STORAGE_ACCOUNTS: 'azurerm_storage_account',
            _AZURE_NETWORK_VNETS: 'azurerm_virtual_network',
            'Microsoft.Network/networkInterfaces': 'azurerm_network_interface',
            _AZURE_SQL_SERVERS: 'azurerm_mssql_server',
            _AZURE_WEB_SITES: 'azurerm_app_service',
            _AZURE_EVENTHUB_NAMESPACES: 'azurerm_eventhub_namespace',
            'Microsoft.KeyVault/vaults': 'azurerm_key_vault',
        }

        tf_type = type_mapping.get(resource_type, 'azurerm_resource_group')
        tags = resource.get('tags', {})
        tags_str = json.dumps(tags) if tags else "{}"

        tf_block = f'''resource "{tf_type}" "{name}" {{
  name                = "{name}"
  location            = "{location}"
  resource_group_name = "{rg}"

  tags = {tags_str}

  # Note: Additional properties require manual configuration
}}
'''
        return tf_block

    def generate_bicep_code(self, resources: List[Dict[str, Any]]) -> str:
        """Generate Bicep for the given resources."""
        if not resources:
            return ""

        bicep_code = """// Bicep template for Azure resources
// Generated using AI-powered LLM (GPT-4o)

param location string = resourceGroup().location
param environment string = 'dev'
@allowed(['dev', 'staging', 'prod'])

var commonTags = {
  'deployedBy': 'bicep'
  'generatedFrom': 'azure-resource-cart'
  'environment': environment
}

"""
        for resource in resources:
            resource_bicep = self._generate_bicep_resource(resource)
            bicep_code += resource_bicep + "\n"

        return bicep_code

    def _generate_bicep_resource(self, resource: Dict[str, Any]) -> str:
        """Generate Bicep for a single resource."""
        try:
            if not self._azure_openai_endpoint or not self._azure_openai_key:
                return self._generate_bicep_resource_fallback(resource)
            return self._generate_bicep_with_llm(resource)
        except Exception:
            return self._generate_bicep_resource_fallback(resource)

    def _generate_bicep_with_llm(self, resource: Dict[str, Any]) -> str:
        """Generate Bicep using Azure OpenAI."""
        # Check cache before making an LLM call — same resource JSON = same output.
        if _iac_cache is not None:
            _bicep_key = _iac_cache.make_key("bicep:" + json.dumps(resource, sort_keys=True))
            _cached_bicep = _iac_cache.get(_bicep_key)
            if _cached_bicep is not None:
                return _cached_bicep

        try:
            prompt = f"""You are an expert Azure and Bicep engineer. Generate a Bicep resource block for this Azure resource.

Resource JSON:
{json.dumps(resource, indent=2)}

Requirements:
1. Use exact values from the JSON (no placeholders)
2. Use camelCase identifiers
3. Include @secure() for sensitive parameters
4. Use managed identities over connection strings
5. Use correct Azure API versions
6. Include dependsOn for dependencies
7. Add tags from the resource

Output ONLY the resource block in Bicep syntax, no markdown or extra text."""

            headers = {
                "api-key": self._azure_openai_key,
                "Content-Type": _CONTENT_TYPE_JSON
            }

            data = {
                "messages": [
                    {"role": "system", "content": "You are a Bicep expert."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 2000
            }

            url = f"{self._azure_openai_endpoint}/openai/deployments/{self._deployment_name}/chat/completions?api-version=2024-02-01"
            response = requests.post(url, headers=headers, json=data, timeout=30)

            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    content = result['choices'][0]['message']['content']
                    if 'resource ' in content:
                        if _iac_cache is not None:
                            _iac_cache.set(_bicep_key, content)
                        return content

            return self._generate_bicep_resource_fallback(resource)
        except Exception:
            return self._generate_bicep_resource_fallback(resource)

    def _generate_bicep_resource_fallback(self, resource: Dict[str, Any]) -> str:
        """Generate basic Bicep template as fallback."""
        name = resource.get('name', 'resource').replace('-', '_').lower()
        resource_type = resource.get('type', '')

        api_version_mapping = {
            _AZURE_STORAGE_ACCOUNTS: '2021-09-01',
            _AZURE_COMPUTE_VMS: '2021-07-01',
            _AZURE_NETWORK_VNETS: '2021-05-01',
            _AZURE_SQL_SERVERS: '2019-06-01-preview',
            'Microsoft.Web/sites': '2021-02-01',
            'Microsoft.EventHub/namespaces': '2021-11-01',
        }

        api_version = api_version_mapping.get(resource_type, '2021-04-01')

        bicep_block = f'''resource '{name}' '{resource_type}@{api_version}' = {{
  name: '{name}'
  location: location
  tags: commonTags

  // Note: Additional properties require manual configuration
}}
'''
        return bicep_block


def _parse_query_intent(query: str) -> dict:
    """Parse query and determine user intent. Returns dict with intent flags."""
    START_VERBS = ('start', 'activate', 'fix', 'restart', 'turn on', 'bring up')
    STOP_VERBS  = ('stop', 'deactivate', 'shut down', 'turn off', 'take down', 'kill', 'pause', 'disable')
    SERVICE_TOKENS = (
        'function app', 'function-10112', _KEYWORD_EVENT_HUB, 'dataingestionnamespace',
        'app service', 'web app', 'infrainsight', 'sql server', 'database', 'sqlsrv10112',
        'storage account', 'virtual machine', 'vm-01', 'services', 'service', 
        'all stopped', 'all', 'stopped', 'everything',
    )
    VALIDATE_KEYWORDS = ('validate', 'verify', 'drift', 'compare against', 'check against',
                          'environment validation', 'compare current')
    PRE_LOAD_TEST_KEYWORDS = ('pre-load-test', 'pre load test', 'infra check',
                              'load test infra', 'load test pre-check', 'precheck')
    SERVICE_HEALTH_KEYWORDS = ('environment status', 'service status', 'service health', 'show environment')
    ENV_CONFIG_KEYWORDS = ('environment configuration', 'current environment')
    
    IDLE_KEYWORDS = ('idle', 'unused', 'underutilized', 'not used', 'not being used',
                     'infrequently used', 'scan idle', 'wasted', 'cost optimization',
                     'cost saving', 'cost-saving', 'save cost', 'save money',
                     'cost usage', 'show cost', 'billing', 'cost breakdown', 'spending')

    has_start_verb = any(v in query for v in START_VERBS)
    has_stop_verb  = any(v in query for v in STOP_VERBS)
    wants_validate = any(k in query for k in VALIDATE_KEYWORDS)
    is_event_hub_query = _KEYWORD_EVENT_HUB in query or 'eventhub' in query
    has_idle_kw = any(k in query for k in IDLE_KEYWORDS)

    return {
        # Idle/cost: a plain idle query scans + suggests; pairing it with a stop verb is
        # the approval of that suggestion → stop ONLY the idle subset.
        'is_confirm_stop_idle': has_idle_kw and has_stop_verb,
        'wants_idle_scan': has_idle_kw and not has_stop_verb,
        'is_yes_confirmation': 'yes' in query and has_start_verb,
        'is_direct_start': has_start_verb and any(s in query for s in SERVICE_TOKENS),
        'is_direct_stop': has_stop_verb and any(s in query for s in SERVICE_TOKENS),
        'wants_validate': wants_validate,
        'is_pre_load_test': any(k in query for k in PRE_LOAD_TEST_KEYWORDS),
        'is_service_health': any(k in query for k in SERVICE_HEALTH_KEYWORDS),
        'is_env_config': any(k in query for k in ENV_CONFIG_KEYWORDS),
        'is_event_hub_query': is_event_hub_query,
        'wants_terraform': any(w in query for w in ['terraform', 'replicate', 'tf script', 'generate script']),
        'wants_bicep': any(w in query for w in ['bicep', 'arm template']),
        'wants_execute': any(p in query for p in ['execute terraform', 'execute bicep', 'execute this', 
                                                    'run terraform', 'run bicep', 'apply terraform', 
                                                    'apply bicep', 'deploy this']),
        'wants_discover': any(w in query for w in ['environment', 'details', 'configuration', 
                                                     'resources', 'infrastructure', 'what', 'discover']),
    }


def _route_cloudlens_request(intent: dict, service, query: str = "") -> str:
    """Route request based on parsed intent."""
    # Approval of an idle-service cost suggestion → stop ONLY the idle subset.
    # Checked before the generic stop so "stop the idle services" doesn't over-stop.
    if intent['is_confirm_stop_idle']:
        return _stop_idle_services("idle-service cost optimization (approved)")

    # Direct stop command — pass original query so only named service(s) are stopped
    if intent['is_direct_stop']:
        return _simulate_stop_service(query)

    # Yes confirmation with validation
    if intent['is_yes_confirmation'] and intent['wants_validate']:
        return _simulate_start_and_validate()

    # Direct start for a specific service — only start the named service(s)
    if intent['is_direct_start'] and not intent['is_yes_confirmation']:
        targeted = _parse_start_query(query)
        return _simulate_activate_and_apply(targeted_services=targeted if targeted else None)

    # Yes confirmation or direct start (fallback) — start all stopped services
    if intent['is_yes_confirmation'] or intent['is_direct_start']:
        return _simulate_activate_and_apply()
    
    # Pre-load-test infra check
    if intent['is_pre_load_test']:
        return _infra_ready_check()

    # Idle-service cost scan → suggestion card
    if intent['wants_idle_scan']:
        return _scan_idle_for_savings()

    # Validate environment
    if intent['wants_validate']:
        return _validate_environment()
    
    # Execute terraform/bicep
    if intent['wants_execute']:
        return _simulate_execution_check()
    
    # Generate Terraform scripts
    if intent['wants_terraform']:
        return _generate_terraform_scripts(service)
    
    # Generate Bicep scripts
    if intent['wants_bicep']:
        return _generate_bicep_scripts(service)
    
    # Service health status
    if intent['is_service_health']:
        return _infra_ready_check()
    
    # Environment configuration
    if intent['is_env_config']:
        return _generate_terraform_preview(service)
    
    # Discovery query (but not event hub)
    if not intent['is_event_hub_query'] and intent['wants_discover']:
        return _generate_terraform_preview(service)
    
    return "CloudLens IaC Tool (Preview Mode): Ask about environment details or generate terraform/bicep scripts. Note: This is preview mode - resources are not created."


@app.post("/", response_model=ToolResponse)
async def cloudlens_iac_tool(request: ToolRequest):
    """CloudLens IaC tool - Preview mode: Show what Azure services are needed (no actual provisioning)."""
    global _last_resources

    try:
        query = request.query.lower()

        # Try to create service; fall back to demo mode if Azure SDK unavailable
        service = None
        try:
            service = CloudLensService()
        except Exception as e:
            # Azure SDK or credentials not available; use demo mode
            print(f"Azure not available ({str(e)}), using demo mode with sample resources")
            service = None

        # Parse intent and route request
        intent = _parse_query_intent(query)
        result = _route_cloudlens_request(intent, service, query)
        
        return ToolResponse(result=result)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CloudLens IaC error: {str(e)}")


def _get_demo_resources() -> List[Dict[str, Any]]:
    """Return real Azure resources via Resource Graph. Name is preserved for backwards
    compatibility with the call sites; the body is no longer demo. Returns [] on failure
    so callers degrade to 'no resources found' messaging instead of fake rows."""
    sub_id = os.getenv("AZURE_SUBSCRIPTION_ID")
    if not sub_id or az is None:
        return []
    try:
        rows = _resource_graph_query(
            query=(
                "resources "
                "| project id, name, type, resourceGroup, location, "
                "subscriptionId, kind, properties, tags, sku "
                "| order by tolower(type) asc, tolower(resourceGroup) asc, tolower(id) asc"
            ),
            subscription_id=sub_id,
        )
    except Exception as e:
        print(f"Azure resource discovery failed: {e}")
        return []
    return [{
        'id': r.get('id'),
        'name': r.get('name'),
        'type': r.get('type'),
        'resourceGroup': r.get('resourceGroup'),
        'location': r.get('location'),
        'subscriptionId': r.get('subscriptionId') or sub_id,
        'properties': r.get('properties') or {},
        'tags': r.get('tags') or {},
        'sku': r.get('sku') or {},
    } for r in rows]


def _extract_sku_tier(resource: Dict[str, Any]) -> str:
    """Extract SKU and tier information from resource."""
    sku = resource.get('sku', {})
    if isinstance(sku, dict):
        name = sku.get('name', '')
        tier = sku.get('tier', '')
        if name and tier and name != tier:
            return f"{tier} {name}"
        elif name:
            return name
        elif tier:
            return tier
    return "Standard"


def _get_resource_display_name(resource_type: str) -> str:
    """Convert Azure resource type to friendly display name."""
    type_map = {
        'virtualMachines': _DISPLAY_VIRTUAL_MACHINE,
        'storageAccounts': 'Storage Account',
        'virtualNetworks': 'Virtual Network',
        'networkInterfaces': 'Network Interface',
        'servers': _DISPLAY_SQL_SERVER,
        'sites': _DISPLAY_APP_SERVICE,
        'namespaces': _DISPLAY_EVENT_HUB,
        'vaults': 'Key Vault',
        'databases': 'Database',
        'publicIPAddresses': 'Public IP',
        'networkSecurityGroups': 'Network Security Group',
        'subnets': 'Subnet',
    }

    resource_name = resource_type.split('/')[-1] if '/' in resource_type else resource_type
    return type_map.get(resource_name, resource_name)


# Resource types that Azure auto-creates as side-effects of declaring something else.
# Don't include them in the Terraform replica — they get re-created automatically.
_AUTO_MANAGED_TYPES = {
    'microsoft.network/networkinterfaces',                    # auto-attached to private endpoints / VMs
    'microsoft.network/networkwatchers',                      # subscription-level, Azure-managed
    'microsoft.network/networkwatchers/flowlogs',
}


# Modern azurerm provider (>= 3.x) resource-type map. For Microsoft.Web/sites the real
# resource type depends on the resource's `kind` field (function vs web, linux vs windows)
# so callers resolve that separately via _resolve_web_site_tf_type.
_TF_TYPE_MAP = {
    'Microsoft.Web/serverfarms':                       'azurerm_service_plan',
    _AZURE_COMPUTE_VMS:               'azurerm_linux_virtual_machine',
    _AZURE_STORAGE_ACCOUNTS:               'azurerm_storage_account',
    _AZURE_EVENTHUB_NAMESPACES:                   'azurerm_eventhub_namespace',
    _AZURE_SQL_SERVERS:                           'azurerm_mssql_server',
    'Microsoft.Sql/servers/databases':                 'azurerm_mssql_database',
    'Microsoft.ContainerRegistry/registries':          'azurerm_container_registry',
    'Microsoft.ManagedIdentity/userAssignedIdentities':'azurerm_user_assigned_identity',
    'Microsoft.KeyVault/vaults':                       'azurerm_key_vault',
    'Microsoft.Network/virtualNetworks':               'azurerm_virtual_network',
    'Microsoft.Network/networkSecurityGroups':         'azurerm_network_security_group',
    'Microsoft.Network/privateEndpoints':              'azurerm_private_endpoint',
    'Microsoft.Network/privateDnsZones':               'azurerm_private_dns_zone',
    'Microsoft.Network/privateDnsZones/virtualNetworkLinks': 'azurerm_private_dns_zone_virtual_network_link',
    'Microsoft.Network/publicIPAddresses':             'azurerm_public_ip',
    'Microsoft.Insights/actionGroups':                 'azurerm_monitor_action_group',
    'Microsoft.Insights/activityLogAlerts':            'azurerm_monitor_activity_log_alert',
}


def _resolve_web_site_tf_type(kind: str) -> str:
    """Microsoft.Web/sites splits across four azurerm resources by `kind`."""
    k = (kind or '').lower()
    is_function = 'functionapp' in k
    is_windows = 'windows' in k and 'linux' not in k
    if is_function and is_windows:
        return 'azurerm_windows_function_app'
    if is_function:
        return 'azurerm_linux_function_app'
    if is_windows:
        return 'azurerm_windows_web_app'
    return 'azurerm_linux_web_app'


_TF_TYPE_MAP_LC = {k.lower(): v for k, v in _TF_TYPE_MAP.items()}


def _get_terraform_resource_type(resource_type: str, kind: str = '') -> str:
    """Map Azure resource type to the matching azurerm Terraform type name.
    Case-insensitive — ARM Resource Graph returns lowercase types."""
    rt_lc = (resource_type or '').lower()
    if rt_lc == 'microsoft.web/sites':
        return _resolve_web_site_tf_type(kind)
    return _TF_TYPE_MAP_LC.get(rt_lc, 'azurerm_resource')


def _filter_essential_resources(resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop resource types that Azure auto-creates (NICs attached to private endpoints,
    subscription-level network watchers, flow logs). What's left is what you actually
    declare to replicate the environment."""
    return [r for r in resources if (r.get('type') or '').lower() not in _AUTO_MANAGED_TYPES]


def _safe_tf_name(name: str) -> str:
    """Make a Terraform-safe identifier from an Azure resource name."""
    return re.sub(r'\W+', '_', (name or 'res')).strip('_').lower() or 'res'


def _parent_name_from_id(resource_id: Optional[str], child_segment: str) -> Optional[str]:
    """Extract the parent resource name from an ARM resource id. Handles ids of the form
    .../{parentSegment}/{parentName}/{childSegment}/{childName}. Case-insensitive."""
    if not resource_id or not child_segment:
        return None
    m = re.search(
        rf'/providers/[^/]+/[^/]+/([^/]+)/{re.escape(child_segment)}/[^/]+$',
        resource_id,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


def _to_chip(resource: Dict[str, Any]) -> Dict[str, Any]:
    """Build the chip dict consumed by _build_terraform_script / _build_chips_display.
    Includes the fields needed for HCL emission: kind (for Web/sites variant resolution),
    parent_name (for child resources whose ARM name is 'parent/child' OR whose name is
    only the leaf), parent_plan_name (for App Service sites that reference a serverFarmId)."""
    rtype = resource.get('type', 'Unknown')
    rtype_lc = (rtype or '').lower()
    rid = resource.get('id')
    name = resource.get('name') or ''
    # ARM Resource Graph sometimes returns child resource names as "parent/child"
    # (e.g. SQL databases) and sometimes as just the leaf (e.g. private DNS vnet links).
    # Try the "parent/child" form first, then fall back to parsing the ARM id.
    parent_name: Optional[str] = name.split('/', 1)[0] if '/' in name else None
    if parent_name is None:
        # Map child resource types to their segment in the ARM id.
        child_segment_map = {
            'microsoft.network/privatednszones/virtualnetworklinks': 'virtualNetworkLinks',
            'microsoft.sql/servers/databases':                       'databases',
            'microsoft.eventhub/namespaces/eventhubs':               'eventhubs',
            'microsoft.web/sites/slots':                             'slots',
        }
        seg = child_segment_map.get(rtype_lc)
        if seg:
            parent_name = _parent_name_from_id(rid, seg)
    # For Microsoft.Web/sites, properties.serverFarmId points at the App Service Plan.
    props = resource.get('properties') or {}
    plan_id = props.get('serverFarmId') if isinstance(props, dict) else None
    parent_plan_name = plan_id.split('/')[-1] if isinstance(plan_id, str) and '/' in plan_id else None
    return {
        'id': resource.get('id'),
        'name': name,
        'display_name': _get_resource_display_name(rtype),
        'sku_tier': _extract_sku_tier(resource),
        'location': resource.get('location', 'Unknown'),
        'type': rtype,
        'kind': resource.get('kind') or '',
        'parent_name': parent_name,
        'parent_plan_name': parent_plan_name,
    }


def _resources_for_replica(resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop Azure-auto-managed resources (NICs, network watchers, flow logs) before
    handing the list to the Terraform/Bicep generator."""
    return _filter_essential_resources(resources)


# (service, display, resource_type, rg_cfg_key, name_cfg_key)
_REAL_SERVICE_DESC = [
    ("webapp",   _DISPLAY_APP_SERVICE,   _AZURE_WEB_SITES,          "webapp_rg",   "webapp_name"),
    ("function", _DISPLAY_FUNCTION_APP,  _AZURE_WEB_SITES,          "function_rg", "function_name"),
    ("eventhub", _DISPLAY_EVENT_HUB,     _AZURE_EVENTHUB_NAMESPACES, "eventhub_rg", "eventhub_ns"),
    ("database", _DISPLAY_SQL_SERVER,    _AZURE_SQL_SERVERS,         "db_rg",       "db_server"),
]


def _load_service_states() -> Dict[str, Dict[str, str]]:
    """Read live service states from Azure ARM. Returns dict keyed by resource name.
    Each entry carries name, display, status, health, resource_type, resource_id —
    enough for the decision gate AND for the executor to perform a real start."""
    if az is None:
        return {}
    cfg = az.get_cfg()
    sub_id = cfg.get("subscription_id")
    if not sub_id:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for svc_key, display, rtype, rg_key, name_key in _REAL_SERVICE_DESC:
        name = cfg.get(name_key)
        rg = cfg.get(rg_key)
        if not (name and rg):
            continue
        try:
            h = az.check_service_connectivity(svc_key)
        except Exception as e:
            h = {"healthy": False, "state": f"Probe error: {type(e).__name__}"}
        running = bool(h.get("healthy"))
        status = h.get("state", "Unknown") or "Unknown"
        health = "Healthy" if running else "Disabled"
        provider, kind = rtype.split("/", 1)
        resource_id = (
            f"/subscriptions/{sub_id}/resourceGroups/{rg}"
            f"/providers/{provider}/{kind}/{name}"
        )
        out[name] = {
            "name": name,
            "display": display,
            "status": status,
            "health": health,
            "resource_type": rtype,
            "resource_id": resource_id,
        }
    return out


def _format_status_emoji(status: str) -> str:
    return "✅" if status == "Running" else "⚠️"


# Production-impact classification per service. Drives the "Impact" column in the
# validation table so users can tell at a glance whether a stoppage matters for the
# platform. Defaults to "Critical" for anything not listed.
_SERVICE_IMPACT = {
    _DISPLAY_APP_SERVICE:  "Non-critical",
    _DISPLAY_FUNCTION_APP: "Critical",
    _DISPLAY_EVENT_HUB:    "Critical",
    _DISPLAY_SQL_SERVER:   "Critical",
}


def _service_impact(display: str) -> str:
    return _SERVICE_IMPACT.get(display, "Critical")


def _build_status_table(states: Dict[str, Dict[str, str]]) -> List[str]:
    rows = ["| Service | Instance | Status | Health | Impact |",
            "|---------|----------|--------|--------|--------|"]
    for svc in states.values():
        emoji = _format_status_emoji(svc['status'])
        status_text = f"**{svc['status']}**" if svc['status'] != "Running" else svc['status']
        health_text = f"**{svc['health']}**" if svc['health'] != "Healthy" else svc['health']
        impact = _service_impact(svc['display'])
        impact_text = f"**{impact}**" if impact == "Critical" else impact
        rows.append(f"| {svc['display']} | `{svc['name']}` | {emoji} {status_text} | {health_text} | {impact_text} |")
    return rows


# Plain-English purpose for each service in this environment. The validate / gate flows
# embed this in the rationale so the user sees "what + why this matters" instead of
# generic infrastructure jargon.
_SERVICE_PURPOSE = {
    _DISPLAY_APP_SERVICE:
        "hosts the InfraInsight web app — the user-facing entry point. "
        "With it stopped, users can't reach the platform.",
    _DISPLAY_FUNCTION_APP:
        "runs the event handlers that pull telemetry from the Event Hub and write it "
        "into SQL. With it stopped, new data isn't being ingested and the data pipeline stalls.",
    _DISPLAY_EVENT_HUB:
        "is the streaming ingestion point for incoming telemetry. With it stopped, "
        "upstream producers can't deliver data and the Function App has nothing to process.",
    _DISPLAY_SQL_SERVER:
        "stores all application data. With it stopped, every read/write the app makes "
        "will fail and validation can't exercise the data layer.",
    "Storage Account":
        "backs the Function App runtime and stores deployment artifacts. With it "
        "stopped, the Function App can't start.",
    _DISPLAY_VIRTUAL_MACHINE:
        "is the compute host for a dependent workload. With it stopped, anything "
        "running on it goes offline.",
}


def _build_rationale(stopped_services: List[Dict[str, Any]]) -> str:
    """One concise paragraph per stopped service: what it does in THIS environment and
    the concrete consequence of leaving it stopped. Grounded in real state — no LLM
    rephrasing, no Terraform-jargon boilerplate."""
    if not stopped_services:
        return ""
    sentences: List[str] = []
    for s in stopped_services:
        display = s.get('display', 'Service')
        name = s.get('name', '?')
        purpose = _SERVICE_PURPOSE.get(display,
                                       "is part of the platform and other services depend on it being available.")
        sentences.append(f"**{display}** (`{name}`) {purpose}")
    return " ".join(sentences)


def _infra_ready_check() -> str:
    """Pre-load-test infra gate: reads live ARM service states and returns a clear
    READY / NOT READY signal that the Master agent can parse without ambiguity.
    CRITICAL services = Function App, Event Hub, SQL Server.
    App Service (infrainsight) is Non-critical and does NOT block the load test."""
    CRITICAL_DISPLAY = {_DISPLAY_FUNCTION_APP, _DISPLAY_EVENT_HUB, _DISPLAY_SQL_SERVER}
    states = _load_service_states()
    critical_stopped = [
        svc for svc in states.values()
        if svc['display'] in CRITICAL_DISPLAY and svc['status'] != "Running"
    ]
    out = ["## Pre-Load-Test Infrastructure Check", ""]
    out.extend(_build_status_table(states))
    out.append("")
    if not critical_stopped:
        out.append("✅ **INFRA READY** — All critical services (Function App, Event Hub, SQL Server) are Running.")
        out.append("")
        out.append("The load test may proceed.")
    else:
        names_md = ", ".join(f"**{s['display']}** (`{s['name']}`)" for s in critical_stopped)
        out.append(f"❌ **INFRA NOT READY** — {len(critical_stopped)} critical service(s) are Stopped: {names_md}")
        out.append("")
        out.append("The load test **cannot start** until these services are Running.")
        out.append("")
        out.append("> " + _build_rationale(critical_stopped))
    return "\n".join(out)


def _render_health_gate(action_summary: str, suggested_message: str) -> str:
    """If any sim service is Stopped, render a top-of-message gate + PENDING_ACTION marker.
    Returns '' when everything is Running so callers can prepend unconditionally."""
    states = _load_service_states()
    stopped = [svc for svc in states.values() if svc['status'] != "Running"]
    if not stopped:
        return ""
    out = ["## ⚠️ Pre-Action Health Check — Issue Detected", ""]
    out.extend(_build_status_table(states))
    out.append("")
    names_md = ", ".join(f"**{s['display']}** (`{s['name']}`)" for s in stopped)
    out.append(f"**{len(stopped)} service(s) not Running:** {names_md}")
    out.append("")
    out.append(f"> {_build_rationale(stopped)}")
    out.append("")
    out.append(_AGENT_RECOMMENDATION_HEADER)
    out.append("")
    if len(stopped) == 1:
        svc = stopped[0]
        out.append(f"I recommend starting the **{svc['display']}** before proceeding. Would you like me to start it now?")
        primary_service = svc['display']
    else:
        out.append("I recommend starting all stopped services before proceeding. Would you like me to start them now?")
        primary_service = ", ".join(s['display'] for s in stopped)
    out.append("")
    pending = {
        "type": "start_and_apply",
        "service": primary_service,
        "summary": f"Start {len(stopped)} stopped service(s) and {action_summary}",
        "rationale": _build_rationale(stopped),
        "suggested_user_message": suggested_message,
        "stopped_services": [s['name'] for s in stopped],
    }
    out.append(f"<!--PENDING_ACTION:{json.dumps(pending)}-->")
    out.append("")
    out.append("---")
    out.append("")
    return "\n".join(out)


# ── Idle-service detection → cost-saving stop suggestion ───────────────────────
# Only the known, metrics-backed services can be assessed (azure_rest.get_metrics
# resolves a fixed set of service keys). Map their display names to those keys.
_DISPLAY_TO_METRIC_KEY = {
    _DISPLAY_APP_SERVICE:  "webapp",
    _DISPLAY_FUNCTION_APP: "function",
    _DISPLAY_EVENT_HUB:    "eventhub",
    _DISPLAY_SQL_SERVER:   "database",
}

# Per service key: (metric_names, activity_counter_keys, cpu_metric_key_or_None).
# activity = summed traffic counters; cpu = average compute signal (percent).
_IDLE_METRIC_SPEC = {
    "webapp":   ("Requests,CpuTime",                        ("requests",),                            None),
    "function": ("Requests,FunctionExecutionCount,CpuTime", ("requests", "functionexecutioncount"),   None),
    "eventhub": ("IncomingMessages,OutgoingMessages",       ("incomingmessages", "outgoingmessages"), None),
    "database": ("connection_successful,cpu_percent",       ("connection_successful",),               "cpu_percent"),
}


def _idle_thresholds() -> tuple:
    """(window_days, max_activity, max_cpu_pct) from env with safe defaults."""
    window_days = int(os.getenv("FINOPS_IDLE_WINDOW_DAYS", "15"))
    max_activity = float(os.getenv("FINOPS_IDLE_ACTIVITY_MAX", "1"))
    max_cpu_pct = float(os.getenv("FINOPS_IDLE_CPU_PCT", "5"))
    return window_days, max_activity, max_cpu_pct


def _sum_metric_total(entries: List[Dict[str, Any]], metric_key: str) -> Optional[float]:
    """Sum raw 'total' (fallback 'average') points across the window for a metric.
    Returns None when the metric/timeseries is absent — data unavailable, not idle."""
    for m in entries:
        if m.get("metric_key", "").lower() != metric_key:
            continue
        ts = m.get("timeseries") or []
        vals = [p.get("total") if p.get("total") is not None else p.get("average") for p in ts]
        vals = [v for v in vals if v is not None]
        return float(sum(vals)) if vals else None
    return None


def _avg_metric_average(entries: List[Dict[str, Any]], metric_key: str) -> Optional[float]:
    """Mean of raw 'average' points across the window for a metric, or None if absent."""
    for m in entries:
        if m.get("metric_key", "").lower() != metric_key:
            continue
        ts = m.get("timeseries") or []
        vals = [p.get("average") for p in ts if p.get("average") is not None]
        return float(sum(vals) / len(vals)) if vals else None
    return None


def _assess_service_idle(svc_key: str, hours: int, max_activity: float, max_cpu_pct: float) -> tuple:
    """Return (is_idle, detail). is_idle is None when there's no usable data — we do NOT
    suggest stopping something we cannot measure."""
    spec = _IDLE_METRIC_SPEC.get(svc_key)
    if not spec or az is None:
        return None, "no metric spec"
    metric_names, activity_keys, cpu_key = spec
    try:
        entries = az.get_metrics(svc_key, metric_names, hours=hours)
    except Exception as e:
        return None, f"metric fetch error: {type(e).__name__}"
    if not entries:
        return None, "no metric data available"

    activity_total = 0.0
    saw_activity_data = False
    for key in activity_keys:
        total = _sum_metric_total(entries, key)
        if total is not None:
            saw_activity_data = True
            activity_total += total

    cpu_avg = _avg_metric_average(entries, cpu_key) if cpu_key else None

    if not saw_activity_data and cpu_avg is None:
        return None, "no usable metric data"

    activity_idle = (not saw_activity_data) or (activity_total <= max_activity)
    cpu_idle = (cpu_avg is None) or (cpu_avg <= max_cpu_pct)

    detail_parts = []
    if saw_activity_data:
        detail_parts.append(f"{int(activity_total)} requests/messages")
    if cpu_avg is not None:
        detail_parts.append(f"avg CPU {cpu_avg:.1f}%")
    detail = "; ".join(detail_parts) if detail_parts else "no measurable activity"
    return (activity_idle and cpu_idle), detail


def _find_idle_resources(window_days: Optional[int] = None) -> List[Dict[str, Any]]:
    """Detect Running services that have been effectively unused over the window.
    Only the known, metrics-backed services are considered. Returns [] on no-data."""
    wd, max_activity, max_cpu = _idle_thresholds()
    if window_days:
        wd = window_days
    hours = wd * 24
    idle: List[Dict[str, Any]] = []
    for svc in _load_service_states().values():
        if svc.get("status") != "Running":
            continue  # only Running services can be stopped
        svc_key = _DISPLAY_TO_METRIC_KEY.get(svc.get("display", ""))
        if not svc_key:
            continue
        is_idle, detail = _assess_service_idle(svc_key, hours, max_activity, max_cpu)
        if is_idle:
            idle.append({
                "name": svc.get("name"),
                "display": svc.get("display"),
                "resource_id": svc.get("resource_id"),
                "resource_type": svc.get("resource_type"),
                "reason": f"idle over last {wd}d ({detail})",
                "window_days": wd,
            })
    return idle


def _get_idle_cost_data(idle: List[Dict[str, Any]], window_days: int) -> dict:
    """Fetch actual Azure billing cost for idle resources over the detection window.
    Returns {resource_id_lower: cost_info} or {} when unavailable."""
    if az is None or not idle:
        return {}
    resource_ids = [r["resource_id"] for r in idle if r.get("resource_id")]
    if not resource_ids:
        return {}
    try:
        return az.get_resource_costs(resource_ids, days=window_days)
    except Exception:
        return {}


def _prescriptions_for_service(display: str, name: str) -> List[str]:
    """Return actionable remediation bullets for an idle service."""
    if display == _DISPLAY_APP_SERVICE:
        return [
            f"`az webapp stop --name {name} --resource-group <rg>`",
            "Portal: **App Services** → select instance → **Stop**",
            "Alternative (keep warm): Scale down to **Free (F1)** tier to eliminate compute cost",
        ]
    if display == _DISPLAY_FUNCTION_APP:
        return [
            f"`az functionapp stop --name {name} --resource-group <rg>`",
            "Portal: **Function Apps** → select instance → **Stop**",
            "Alternative: Switch to **Consumption plan** so you only pay per execution",
        ]
    if display == _DISPLAY_EVENT_HUB:
        return [
            f"`az eventhubs namespace update --name {name} --resource-group <rg> --sku Basic`",
            "Portal: **Event Hubs** → namespace → **Pricing tier** → downgrade to Basic",
            "Delete unused Event Hub entities inside the namespace to cut retention costs",
        ]
    if display == _DISPLAY_SQL_SERVER:
        return [
            f"`az sql db update --name <db> --server {name} --resource-group <rg> --edition Basic`",
            "For Serverless databases: enable **Auto-pause** (pauses after N minutes idle, billing stops)",
            "Portal: **SQL databases** → select DB → **Configure** → lower DTU / vCore tier",
        ]
    return [f"`az resource delete --ids <resource-id-for-{name}> --yes`"]


_DEMO_SUB = "00000000-0000-0000-0000-000000000000"
_DEMO_RG  = "demo-unused-rg"

def _demo_idle_resources(wd: int) -> List[Dict[str, Any]]:
    """Return clearly fictional idle services for demo/test mode (FINOPS_DEMO_MODE=true).
    Names use a 'demo-unused-*' prefix so they can never be confused with real resources."""
    return [
        {
            "name": "demo-unused-webapp-01",
            "display": _DISPLAY_APP_SERVICE,
            "resource_id": f"/subscriptions/{_DEMO_SUB}/resourceGroups/{_DEMO_RG}/providers/Microsoft.Web/sites/demo-unused-webapp-01",
            "resource_type": "Microsoft.Web/sites",
            "reason": f"idle over last {wd}d (0 requests; avg CPU 0.3%)",
            "window_days": wd,
        },
        {
            "name": "demo-unused-func-01",
            "display": _DISPLAY_FUNCTION_APP,
            "resource_id": f"/subscriptions/{_DEMO_SUB}/resourceGroups/{_DEMO_RG}/providers/Microsoft.Web/sites/demo-unused-func-01",
            "resource_type": "Microsoft.Web/sites",
            "reason": f"idle over last {wd}d (0 requests; 0 executions)",
            "window_days": wd,
        },
        {
            "name": "demo-unused-eventhub-01",
            "display": _DISPLAY_EVENT_HUB,
            "resource_id": f"/subscriptions/{_DEMO_SUB}/resourceGroups/{_DEMO_RG}/providers/Microsoft.EventHub/namespaces/demo-unused-eventhub-01",
            "resource_type": "Microsoft.EventHub/namespaces",
            "reason": f"idle over last {wd}d (0 incoming/outgoing messages)",
            "window_days": wd,
        },
    ]


def _demo_cost_data(idle: List[Dict[str, Any]], wd: int) -> dict:
    """Return mock cost data keyed by resource_id for demo mode."""
    mock_costs = {
        _DISPLAY_APP_SERVICE:  (18.50,  "USD"),
        _DISPLAY_FUNCTION_APP: (6.20,   "USD"),
        _DISPLAY_EVENT_HUB:    (22.80,  "USD"),
        _DISPLAY_SQL_SERVER:   (43.00,  "USD"),
    }
    result = {}
    for r in idle:
        rid = (r.get("resource_id") or "").lower()
        if not rid:
            continue
        total, cur = mock_costs.get(r["display"], (10.0, "USD"))
        daily_avg = total / wd if wd > 0 else 0.0
        result[rid] = {
            "total_cost": round(total, 2),
            "currency": cur,
            "daily_avg": round(daily_avg, 4),
            "monthly_projected": round(daily_avg * 30, 2),
        }
    return result


def _scan_idle_for_savings() -> str:
    """Scan for idle services and emit a cost-saving stop suggestion (PENDING_ACTION card)."""
    wd, _, _ = _idle_thresholds()
    demo_mode = os.getenv("FINOPS_DEMO_MODE", "").lower() in ("1", "true", "yes")

    if demo_mode:
        idle = _demo_idle_resources(wd)
        cost_data = _demo_cost_data(idle, wd)
    else:
        idle = _find_idle_resources()

    out = ["## 💸 Idle-Service Cost Scan", ""]
    if demo_mode:
        out.append("> ⚠️ **DEMO MODE** — results below are simulated with fictional service names.")
        out.append("> Set `FINOPS_DEMO_MODE=false` (or unset it) and provide Azure credentials to scan real resources.")
        out.append("")
    if not idle:
        out.append(f"No idle services detected over the last {wd} days — nothing to optimize right now. ✅")
        return "\n".join(out)

    if not demo_mode:
        cost_data = _get_idle_cost_data(idle, wd)

    def _fmt_cost(resource_id: Optional[str]) -> tuple:
        """Return (cost_last_window_str, projected_monthly_str, monthly_float)."""
        if not resource_id:
            return "N/A", "N/A", 0.0
        info = cost_data.get(resource_id.lower())
        if not info:
            return "N/A", "N/A", 0.0
        cur = info.get("currency", "USD")
        total = info.get("total_cost", 0.0)
        proj = info.get("monthly_projected", 0.0)
        return f"{cur} {total:.2f}", f"{cur} {proj:.2f}", proj

    out.append(f"Found **{len(idle)} service(s)** with no meaningful activity over the last **{wd} days**:")
    out.append("")
    out.append(f"| Service | Instance | Idle Reason | Cost (last {wd}d) | Projected 30-day Cost |")
    out.append("|---------|----------|-------------|-------------------|----------------------|")
    total_monthly = 0.0
    for r in idle:
        cost_str, proj_str, proj_float = _fmt_cost(r.get("resource_id"))
        total_monthly += proj_float
        out.append(f"| {r['display']} | `{r['name']}` | {r['reason']} | {cost_str} | {proj_str} |")
    out.append("")
    if total_monthly > 0:
        out.append(f"> **Total projected waste if unchanged: ~${total_monthly:.2f}/month**")
        out.append("")

    out.append(_AGENT_RECOMMENDATION_HEADER)
    out.append("")
    displays = ", ".join(r["display"] for r in idle)
    out.append(
        f"These services appear unused and are still incurring cost. Would you like me to stop "
        f"**{displays}** to save money? You can restart them anytime."
    )
    out.append("")

    out.append("### 🛠 Prescriptions")
    out.append("")
    out.append("Run any of the commands below to stop or right-size each idle service:")
    out.append("")
    for r in idle:
        out.append(f"**{r['display']} — `{r['name']}`**")
        for bullet in _prescriptions_for_service(r["display"], r["name"]):
            out.append(f"- {bullet}")
        out.append("")

    # The suggested message intentionally keeps the words 'idle' + 'stop' together so the
    # approval routes to the dedicated idle-stop handler (which stops ONLY the idle subset),
    # not the generic stop parser (which would over-stop everything running).
    pending = {
        "type": "stop_idle_for_savings",
        "service": displays,
        "summary": f"Stop {len(idle)} idle service(s) for cost savings",
        "rationale": "; ".join(f"{r['display']} ({r['reason']})" for r in idle),
        "suggested_user_message": "Yes, stop the idle services to save cost",
        "idle_services": [r["name"] for r in idle],
        "projected_monthly_cost": round(total_monthly, 2),
        "window_days": wd,
    }
    out.append(f"<!--PENDING_ACTION:{json.dumps(pending)}-->")
    return "\n".join(out)


def _stop_idle_services(query: str) -> str:
    """Approval handler for the idle-service suggestion. Re-derives the idle set and stops
    EXACTLY those services (still Running) through the gated executor. Audit-logged."""
    idle = _find_idle_resources()
    states = _load_service_states()
    to_stop = [
        states[r["name"]] for r in idle
        if r["name"] in states and states[r["name"]].get("status") == "Running"
    ]

    output = ["## Stopping Idle Services (Cost Optimization)", ""]
    stopped_summaries: List[Dict[str, Any]] = []
    if not to_stop:
        output.append("No idle services are currently running — nothing to stop.")
    else:
        output.append("```")
        for svc in to_stop:
            _stop_single_service(svc, query, output, stopped_summaries)
        output.append("```")
        output.append("")
        output.append("### Updated Service Status")
        output.append("")
        output.extend(_build_status_table(_load_service_states()))
        output.append("")

    has_real = any(s.get("real_action_taken") for s in stopped_summaries)
    output.append("### 🔒 Action provenance")
    output.append("")
    output.append("- All actions written to the audit log (visible in the **Agent Actions** sidebar tab).")
    if has_real:
        output.append("- *Real Azure stop operations were performed on allowlisted resources.*")
    else:
        output.append("- *Sim-mode: set AGENTIC_STOP_ALLOWLIST to enable real Azure stops.*")
    output.append("")

    executed = {
        "type": "stop_service",
        "service": ", ".join(s['display'] for s in to_stop) if to_stop else "none",
        "summary": (f"Stopped {len(to_stop)} idle service(s) for cost savings" if to_stop
                    else "No idle running services matched"),
        "rationale": f"User approved idle-service cost optimization: {query}",
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "real_action_taken": has_real,
    }
    output.append(f"<!--AGENTIC_ACTION:{json.dumps(executed)}-->")
    return "\n".join(output)


def _simulate_execution_check() -> str:
    """Pre-execution health check — reads ACTUAL service states from disk.
    Emits a PENDING_ACTION marker so the UI can render an Accept/Decline action card."""
    states = _load_service_states()
    stopped_services = [svc for svc in states.values() if svc['status'] != "Running"]

    output = []
    output.append("## Pre-Execution Health Check")
    output.append("")
    output.append("Before applying the infrastructure scripts, I'm validating the current state of all Azure services...")
    output.append("")
    output.append("### Service Status Report")
    output.append("")
    output.extend(_build_status_table(states))
    output.append("")
    output.append("---")
    output.append("")

    if not stopped_services:
        output.append("### ✅ All Services Healthy")
        output.append("")
        output.append("All services are running. The deployment can proceed safely.")
        output.append("")
        output.append(_AGENT_RECOMMENDATION_HEADER)
        output.append("")
        output.append("No issues detected. Would you like me to proceed with the Terraform apply?")
        output.append("")
        output.append("*Click \"Apply Terraform now\" to proceed with deployment.*")
        # Pending action: apply only (no service start needed)
        pending = {
            "type": "iac_apply",
            "service": _ALL_SERVICES,
            "summary": "Apply Terraform deployment to current environment",
            "rationale": "All services are Running. Terraform apply can proceed safely without prior remediation.",
            "suggested_user_message": "Yes, apply the Terraform now",
        }
        output.append(f"<!--PENDING_ACTION:{json.dumps(pending)}-->")
        return "\n".join(output)

    output.append(f"### ⚠️ Issue Detected — {len(stopped_services)} Service(s) Not Running")
    output.append("")
    for svc in stopped_services:
        output.append(f"**{svc['display']} `{svc['name']}` is currently in `{svc['status']}` state.**")
    output.append("")
    rationale = _build_rationale(stopped_services)
    output.append("### 🧠 Why this action is needed")
    output.append("")
    output.append(f"> {rationale}")
    output.append("")
    output.append(_AGENT_RECOMMENDATION_HEADER)
    output.append("")
    if len(stopped_services) == 1:
        svc = stopped_services[0]
        output.append(f"I recommend starting the **{svc['display']}** *before* applying the deployment scripts. Would you like me to start it now?")
        output.append("")
        output.append(f"*Click \"Yes, start the {svc['display']}\" to proceed with activation, or \"No, skip\" to apply scripts anyway.*")
        suggested_msg = f"Yes, start the {svc['display']} and apply the Terraform"
        primary_service = svc['display']
    else:
        output.append("I recommend starting all stopped services *before* applying the deployment scripts. Would you like me to start them now?")
        output.append("")
        output.append("*Click \"Yes, start all stopped services\" to proceed with activation.*")
        suggested_msg = "Yes, start all stopped services and apply the Terraform"
        primary_service = ", ".join(s['display'] for s in stopped_services)

    pending = {
        "type": "start_and_apply",
        "service": primary_service,
        "summary": f"Start {len(stopped_services)} stopped service(s) and apply Terraform",
        "rationale": rationale,
        "suggested_user_message": suggested_msg,
        "stopped_services": [s['name'] for s in stopped_services],
    }
    output.append(f"<!--PENDING_ACTION:{json.dumps(pending)}-->")
    return "\n".join(output)


def _parse_start_query(query: str) -> set:
    """Parse start query and return set of targeted display names. Empty set means no specific target."""
    _QUERY_TO_DISPLAY = {
        'function app':   _DISPLAY_FUNCTION_APP,
        'function-10112': _DISPLAY_FUNCTION_APP,
        _KEYWORD_EVENT_HUB: _DISPLAY_EVENT_HUB,
        'dataingestionnamespace': _DISPLAY_EVENT_HUB,
        'app service':    _DISPLAY_APP_SERVICE,
        'web app':        _DISPLAY_APP_SERVICE,
        'infrainsight':   _DISPLAY_APP_SERVICE,
        'sql server':     _DISPLAY_SQL_SERVER,
        'database':       _DISPLAY_SQL_SERVER,
        'sqlsrv10112':    _DISPLAY_SQL_SERVER,
    }
    q = query.lower()
    return {display for token, display in _QUERY_TO_DISPLAY.items() if token in q}


def _parse_stop_query(query: str) -> tuple[set, bool]:
    """Parse stop query and return (targeted_display_names, generic_all_flag)."""
    _QUERY_TO_DISPLAY = {
        'function app':   _DISPLAY_FUNCTION_APP,
        'function-10112': _DISPLAY_FUNCTION_APP,
        _KEYWORD_EVENT_HUB: _DISPLAY_EVENT_HUB,
        'dataingestionnamespace': _DISPLAY_EVENT_HUB,
        'app service':    _DISPLAY_APP_SERVICE,
        'web app':        _DISPLAY_APP_SERVICE,
        'infrainsight':   _DISPLAY_APP_SERVICE,
        'sql server':     _DISPLAY_SQL_SERVER,
        'database':       _DISPLAY_SQL_SERVER,
        'sqlsrv10112':    _DISPLAY_SQL_SERVER,
        'storage account':'Storage Account',
        'virtual machine':_DISPLAY_VIRTUAL_MACHINE,
        'vm-01':          _DISPLAY_VIRTUAL_MACHINE,
    }
    targeted: set = set()
    q = query.lower()
    for token, display in _QUERY_TO_DISPLAY.items():
        if token in q:
            targeted.add(display)
    generic_all = any(t in q for t in ('services', 'service', 'all', 'everything'))
    return targeted, generic_all


def _stop_single_service(svc: Dict[str, str], query: str, output: List[str], stopped_summaries: List[Dict[str, Any]]) -> None:
    """Stop a single service and append results to output."""
    output.append(f"[INFO] Sending stop request to {svc['display']}: {svc['name']}")
    if _ae is not None:
        res = _ae.stop_service(
            service_name=svc['name'],
            rationale=f"User requested: {query}",
            resource_type=_guess_resource_type(svc),
            resource_id=_guess_resource_id(svc),
            before_state=svc.get('status', 'Unknown'),
        )
        stopped_summaries.append({"service": svc['display'], **res})
        real_tag = "real" if res.get("real_action_taken") else "sim"
        output.append(f"[INFO] Status: {res.get('before_state')} → {res.get('after_state')} ({real_tag})")
        output.append(f"[{'SUCCESS' if res.get('success') else 'INFO'}] {svc['display']} `{svc['name']}` — {res.get('summary')} {'✅' if res.get('success') else '⚠️'}")
    else:
        output.append(f"[ERROR] action_executor unavailable; cannot stop {svc['display']} `{svc['name']}`")


def _simulate_stop_service(query: str) -> str:
    """Stop the service(s) named in the query. Calls action_executor.stop_service() for each
    matched Running service, audit-logs, and emits an AGENTIC_ACTION marker."""
    states = _load_service_states()
    targeted, generic_all = _parse_stop_query(query)

    to_stop = [
        svc for svc in states.values()
        if svc['status'] == 'Running'
        and (generic_all or not targeted or svc['display'] in targeted)
    ]

    output = []
    output.append("## Service Stop")
    output.append("")

    stopped_summaries: List[Dict[str, Any]] = []

    if not to_stop:
        targeted_str = ', '.join(sorted(targeted)) if targeted else 'requested services'
        output.append(f"No running instances of **{targeted_str}** found — nothing to stop.")
    else:
        output.append("### Stopping Services")
        output.append("")
        output.append("```")
        for svc in to_stop:
            _stop_single_service(svc, query, output, stopped_summaries)
        output.append("```")
        output.append("")

        output.append("### Updated Service Status")
        output.append("")
        output.extend(_build_status_table(_load_service_states()))
        output.append("")

    has_real = any(s.get("real_action_taken") for s in stopped_summaries)
    output.append("### 🔒 Action provenance")
    output.append("")
    output.append("- All actions written to the audit log (visible in the **Agent Actions** sidebar tab).")
    if has_real:
        output.append("- *Real Azure stop operations were performed on allowlisted resources.*")
    else:
        output.append("- *Sim-mode: state tracked in-memory; set AGENTIC_STOP_ALLOWLIST to enable real Azure stops.*")
    output.append("")

    executed = {
        "type": "stop_service",
        "service": ", ".join(s['display'] for s in to_stop) if to_stop else "none",
        "summary": (f"Stopped {len(to_stop)} service(s)" if to_stop else "No running services matched"),
        "rationale": f"User requested: {query}",
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "real_action_taken": has_real,
    }
    output.append(f"<!--AGENTIC_ACTION:{json.dumps(executed)}-->")
    return "\n".join(output)


def _start_stopped_services(stopped_services_before: List[Dict[str, Any]], rationale: str):
    """Start each stopped service; return (output_lines, started_summaries)."""
    out = ["### Step 1: Starting Stopped Services", "", "```"]
    started_summaries: List[Dict[str, Any]] = []
    for svc in stopped_services_before:
        out.append(f"[INFO] Sending start request to {svc['display']}: {svc['name']}")
        if _ae is not None:
            res = _ae.start_service(
                service_name=svc['name'],
                rationale=rationale,
                resource_type=_guess_resource_type(svc),
                resource_id=_guess_resource_id(svc),
                before_state=svc.get('status', 'Unknown'),
            )
            started_summaries.append({"service": svc['display'], **res})
            real_tag = "real" if res.get("real_action_taken") else "sim"
            out.append(f"[INFO] Status: {res.get('before_state')} → {res.get('after_state')} ({real_tag})")
            out.append(f"[SUCCESS] {svc['display']} `{svc['name']}` — {res.get('summary')} ✅")
        else:
            # In product mode the executor must be importable — refuse to fake success.
            out.append(f"[ERROR] action_executor unavailable; cannot start {svc['display']} `{svc['name']}`")
    out.extend(["```", ""])
    return out, started_summaries


def _save_tf_artifact(tf_content: str, rationale: str) -> Dict[str, Any]:
    """Persist the Terraform content as an artifact and audit-log the action."""
    artifact_info: Dict[str, Any] = {"success": False}
    if _ae is None:
        return artifact_info
    try:
        artifact_info = _ae.save_iac_artifact(content=tf_content, service_name="environment", fmt="tf")
        if artifact_info.get("success"):
            _ae.append_audit_log({
                "type": "save_iac_artifact",
                "service": "environment",
                "rationale": rationale,
                "result": {
                    "success": True,
                    "summary": f"Saved Terraform to {artifact_info.get('filename')}",
                    "evidence": artifact_info.get("download_url"),
                },
            })
    except Exception as e:
        artifact_info = {"success": False, "error": f"{type(e).__name__}: {e}"}
    return artifact_info


def _build_step3_output(tf_content: str, artifact_info: Dict[str, Any]) -> List[str]:
    out = ["### Step 3: Generated Terraform Script", ""]
    if artifact_info.get("success"):
        out.append(f"📄 Saved to `{artifact_info['filename']}` — "
                   f"[Download Terraform]({artifact_info['download_url']})")
        out.append("")
    out += [_FENCE_HCL, tf_content, "```", ""]
    return out


def _build_completion_summary(stopped_services_before: List[Dict[str, Any]]) -> List[str]:
    out = ["### ✅ Generation Complete", ""]
    for svc in stopped_services_before:
        out.append(f"- {svc['display']} `{svc['name']}` was started before generation")
    out.append("- Terraform script generated from the live Azure environment")
    out.append("- Save as `main.tf` + create a `variables.tf` for `sql_admin_password`, then run `terraform init` && `terraform plan`")
    out.append("")
    return out


def _build_provenance_lines(started_summaries: List[Dict[str, Any]], artifact_info: Dict[str, Any]) -> List[str]:
    out = ["### 🔒 Action provenance", ""]
    if artifact_info.get("success"):
        out.append(f"- Terraform artifact saved at `{artifact_info['path']}` ({artifact_info.get('size', 0)} bytes) — proof of action.")
    has_real = any(s.get("real_action_taken") for s in started_summaries)
    if started_summaries:
        svc_list = ", ".join(
            f"{s['service']} ({'real Azure' if s.get('real_action_taken') else 'sim'})"
            for s in started_summaries
        )
        out.append(f"- Service start actions executed for: {svc_list}")
    out.append("- All actions written to the audit log (visible in the **Agent Actions** sidebar tab).")
    out.append("")
    if has_real:
        out.append("*Real Azure operations were performed on allowlisted resources.*")
    else:
        out.append("*Sim-mode for state changes; Terraform artifact and audit log are real on-disk artifacts.*")
    return out


def _build_executed_marker(
    stopped_services_before: List[Dict[str, Any]],
    started_summaries: List[Dict[str, Any]],
    rationale: str,
    artifact_info: Dict[str, Any],
) -> Dict[str, Any]:
    has_real = any(s.get("real_action_taken") for s in started_summaries)
    svc_names = ", ".join(s['display'] for s in stopped_services_before)
    return {
        "type": "iac_apply",
        "service": svc_names if stopped_services_before else _ALL_SERVICES,
        "summary": (f"Activated {len(stopped_services_before)} service(s) and saved Terraform"
                    if stopped_services_before else "Saved Terraform for current environment"),
        "rationale": rationale,
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evidence_url": artifact_info.get("download_url") if artifact_info.get("success") else None,
        "evidence_path": artifact_info.get("path") if artifact_info.get("success") else None,
        "real_action_taken": has_real,
    }


def _simulate_activate_and_apply(targeted_services: set = None) -> str:
    """Activate the stopped services AND perform real persistent actions:
       - For each stopped service, call action_executor.start_service() (sim-state flip; real Azure
         when in AGENTIC_START_ALLOWLIST). Each call audit-logs.
       - Save the generated Terraform to a real file via save_iac_artifact(); audit-log.
       - Emit an AGENTIC_ACTION marker so the UI can render the executed-action confirmation strip.
       - targeted_services: if provided, only start services whose display name is in this set.
    """
    states = _load_service_states()
    stopped_services_before = [
        dict(svc) for svc in states.values()
        if svc['status'] != "Running"
        and (not targeted_services or svc['display'] in targeted_services)
    ]
    rationale = _build_rationale(stopped_services_before) or "All services are Running. Proceeding with Terraform apply."

    output = [
        "## Service Activation & Deployment", "",
        "### 🧠 Why this action is being taken", "",
        f"> {rationale}", "",
    ]

    if not stopped_services_before:
        output += ["### Step 1: Service Health Check", "",
                   "All services are already running. Skipping activation step.", ""]
        started_summaries: List[Dict[str, Any]] = []
    else:
        step1_lines, started_summaries = _start_stopped_services(stopped_services_before, rationale)
        output += step1_lines

    output += ["### Step 2: Validating Service Health", ""]
    output.extend(_build_status_table(_load_service_states()))
    output += ["", "All services are now healthy and ready for deployment.", ""]

    tf_content = _build_terraform_script_for_demo()
    artifact_info = _save_tf_artifact(tf_content, rationale)

    output += _build_step3_output(tf_content, artifact_info)
    output += _build_completion_summary(stopped_services_before)
    output += _build_provenance_lines(started_summaries, artifact_info)

    executed = _build_executed_marker(stopped_services_before, started_summaries, rationale, artifact_info)
    output.append(f"<!--AGENTIC_ACTION:{json.dumps(executed)}-->")
    return "\n".join(output)


# ── Helpers used by the agentic action flow ─────────────────────────────────
_RESOURCE_TYPE_GUESS = {
    "infrainsight": "Microsoft.Web/sites",
    "vm-01": "Microsoft.Compute/virtualMachines",
    "dataingestionnamespace": "Microsoft.EventHub/namespaces",
    "sqlsrv10112": "Microsoft.Sql/servers",
    "stgacct": "Microsoft.Storage/storageAccounts",
}


def _guess_resource_type(svc: Dict[str, Any]) -> Optional[str]:
    # Real state already carries resource_type; fall back to the legacy name map only
    # for entries that didn't come from azure_rest.
    rtype = svc.get("resource_type")
    if rtype:
        return rtype
    name = svc.get("name")
    return _RESOURCE_TYPE_GUESS.get(name) if name else None


def _guess_resource_id(svc: Dict[str, Any]) -> Optional[str]:
    """Return the ARM resource id. Real state already carries it; fall back to matching
    against demo resources only if absent."""
    if svc.get("resource_id"):
        return svc["resource_id"]
    name = svc.get("name")
    if not name:
        return None
    for res in _get_demo_resources():
        if res.get("name") == name:
            return res.get("id")
    return None


def _build_terraform_script_for_demo() -> str:
    """Produce a complete Terraform script for the current environment, used when persisting
    an artifact at apply-time. Uses the same chip factory + auto-managed filter as the
    user-facing 'generate terraform' path so the artifact matches what the user sees."""
    global _last_resources
    raw = _last_resources if _last_resources else _get_demo_resources()
    resources = _resources_for_replica(raw)
    chips = [_to_chip(r) for r in resources]
    return _build_terraform_script(chips)


# Fixed, version-controlled baseline. Lives alongside this module so it ships with the code
# and can be diffed in git. Override via CLOUDLENS_BASELINE_PATH for tests or per-env runs.
_BASELINE_PATH = Path(
    os.getenv(
        "CLOUDLENS_BASELINE_PATH",
        str(Path(__file__).parent / "configuration" / "baseline.tf"),
    )
)


def _find_latest_baseline() -> Optional[Path]:
    """Return the fixed baseline Terraform path (configuration/baseline.tf next to this
    module). Returns None if the file is missing — in which case the validate flow tells
    the user to regenerate it. Override the path with the CLOUDLENS_BASELINE_PATH env var."""
    return _BASELINE_PATH if _BASELINE_PATH.exists() and _BASELINE_PATH.is_file() else None


def _validate_environment() -> str:
    """Compare the live Azure environment against the most recent Terraform baseline.
    Surfaces:
      • a stopped-service gate (PENDING_ACTION) if any major service is not Running
      • a unified diff of baseline.tf vs the freshly generated TF
    A clean validation = no stopped services AND no drift."""
    baseline = _find_latest_baseline()
    out: List[str] = []
    out.append("## Environment Validation")
    out.append("")

    if baseline is None:
        out.append("❌ **Expected configuration not available.**")
        out.append("")
        out.append("The reference Terraform for this environment hasn't been captured yet, so there's nothing to validate against.")
        out.append("")
        out.append("Ask me to *'generate terraform scripts to replicate this environment'* first, then re-run validation.")
        return "\n".join(out)

    # Step 1 — live service health
    states = _load_service_states()
    stopped = [svc for svc in states.values() if svc.get('status') != "Running"]

    out.append("### Step 1: Service Health (live ARM read)")
    out.append("")
    out.extend(_build_status_table(states))
    out.append("")

    if stopped:
        rationale = _build_rationale(stopped)
        names_md = ", ".join(f"**{s['display']}** (`{s['name']}`)" for s in stopped)
        out.append(f"### ⚠️ {len(stopped)} service(s) not Running")
        out.append("")
        out.append(f"**Stopped:** {names_md}")
        out.append("")
        out.append(f"> {rationale}")
        out.append("")
        out.append("Validation is **inconclusive** while these are stopped — start them and I'll re-run the comparison automatically.")
        out.append("")
        if len(stopped) == 1:
            primary_service = stopped[0]['display']
            out.append(f"### 🤖 Recommendation")
            out.append("")
            out.append(f"Start the **{primary_service}** and re-validate.")
        else:
            primary_service = ", ".join(s['display'] for s in stopped)
            out.append("### 🤖 Recommendation")
            out.append("")
            out.append("Start all stopped services and re-validate.")
        out.append("")
        pending = {
            "type": "start_and_validate",
            "service": primary_service,
            "summary": f"Start {len(stopped)} stopped service(s) and re-validate the environment",
            "rationale": rationale,
            "suggested_user_message": "Yes, start all stopped services and re-validate the environment",
            "stopped_services": [s['name'] for s in stopped],
        }
        out.append(f"<!--PENDING_ACTION:{json.dumps(pending)}-->")
        out.append("")
        out.append("---")
        out.append("")

    # Step 2 — Terraform drift check
    try:
        baseline_tf = baseline.read_text(encoding="utf-8")
    except Exception as e:
        out.append(f"❌ Could not read the expected configuration: {type(e).__name__}: {e}")
        return "\n".join(out)
    current_tf = _build_terraform_script_for_demo()

    out.append("### Step 2: Terraform Drift Check")
    out.append("")

    if current_tf.strip() == baseline_tf.strip():
        out.append("✅ **No drift detected.** Current environment matches the expected configuration.")
        if not stopped:
            out.append("")
            out.append("✅ All services are Running. **Validation passed.**")
        return "\n".join(out)

    diff_lines = list(difflib.unified_diff(
        baseline_tf.splitlines(keepends=True),
        current_tf.splitlines(keepends=True),
        fromfile="expected.tf",
        tofile="current.tf",
        n=3,
    ))
    added = sum(1 for ln in diff_lines if ln.startswith('+') and not ln.startswith('+++'))
    removed = sum(1 for ln in diff_lines if ln.startswith('-') and not ln.startswith('---'))
    out.append(f"⚠️ **Drift detected:** {added} line(s) added, {removed} line(s) removed compared to the expected configuration.")
    out.append("")
    out.append("```diff")
    out.append("".join(diff_lines).rstrip())
    out.append("```")
    return "\n".join(out)


def _simulate_start_and_validate() -> str:
    """Confirmation handler: start stopped services (real ARM) then re-run validation.
    Emits an AGENTIC_ACTION marker so the UI can render the confirmation strip."""
    states = _load_service_states()
    stopped_before = [dict(svc) for svc in states.values() if svc.get('status') != "Running"]
    rationale = _build_rationale(stopped_before) or "All services are Running. Proceeding with validation."
    started_summaries: List[Dict[str, Any]] = []

    out: List[str] = []
    out.append("## Start & Re-validate")
    out.append("")
    out.append("### 🧠 Why")
    out.append("")
    out.append(f"> {rationale}")
    out.append("")

    if stopped_before and _ae is not None:
        out.append("### Step 1: Starting Stopped Services")
        out.append("")
        out.append("```")
        for svc in stopped_before:
            res = _ae.start_service(
                service_name=svc['name'],
                rationale=rationale,
                resource_type=_guess_resource_type(svc),
                resource_id=_guess_resource_id(svc),
                before_state=svc.get('status', 'Unknown'),
            )
            started_summaries.append({"service": svc['display'], **res})
            real_tag = "real" if res.get("real_action_taken") else "sim"
            out.append(f"[INFO] {svc['display']} `{svc['name']}`: {res.get('before_state')} → {res.get('after_state')} ({real_tag})")
            out.append(f"[SUCCESS] {res.get('summary')}")
        out.append("```")
        out.append("")
    elif not stopped_before:
        out.append("### Step 1: Service Health Check")
        out.append("")
        out.append("All services are already Running. Skipping activation step.")
        out.append("")
    else:
        out.append(f"[ERROR] action_executor unavailable; cannot start services.")
        out.append("")

    out.append("---")
    out.append("")
    # Inline the validation output (Step 2: live read + Step 3: drift check)
    out.append(_validate_environment())

    executed = {
        "type": "start_and_validate",
        "service": ", ".join(s['display'] for s in stopped_before) if stopped_before else _ALL_SERVICES,
        "summary": (f"Started {len(stopped_before)} service(s) and re-validated environment"
                    if stopped_before else "Re-validated environment"),
        "rationale": rationale,
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "real_action_taken": any(s.get("real_action_taken") for s in started_summaries),
    }
    out.append("")
    out.append(f"<!--AGENTIC_ACTION:{json.dumps(executed)}-->")
    return "\n".join(out)


def _tf_emit_web_serverfarm(name: str, tf_name: str, sku: str, kind: str) -> List[str]:
    """Generate Terraform for azurerm_service_plan."""
    os_type = 'Windows' if 'windows' in kind.lower() else 'Linux'
    sku_name = sku.split()[-1] if sku else 'B2'
    return [
        f'resource "azurerm_service_plan" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        f'  os_type             = "{os_type}"',
        f'  sku_name            = "{sku_name}"',
        '}',
    ]


def _tf_emit_web_sites(name: str, tf_name: str, kind: str, plan_expr: str) -> List[str]:
    """Generate Terraform for azurerm_*_web_app or azurerm_*_function_app."""
    tf_kind = _resolve_web_site_tf_type(kind)
    block = [
        f'resource "{tf_kind}" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        f'  service_plan_id     = {plan_expr}',
    ]
    if 'function_app' in tf_kind:
        block += [
            '  storage_account_name       = "<TODO: storage account name>"',
            '  storage_account_access_key = "<TODO: storage account access key>"',
            '  site_config {}',
        ]
    else:
        block += ['  site_config {}']
    block += ['}']
    return block


def _tf_emit_storage_account(name: str, tf_name: str, sku: str) -> List[str]:
    """Generate Terraform for azurerm_storage_account."""
    parts = sku.split()
    tier = parts[0] if len(parts) > 1 else 'Standard'
    replication = parts[-1].split('_')[-1] if '_' in parts[-1] else 'LRS'
    return [
        f'resource "azurerm_storage_account" "{tf_name}" {{',
        f'  name                     = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        f'  account_tier             = "{tier}"',
        f'  account_replication_type = "{replication}"',
        '}',
    ]


def _tf_emit_eventhub_namespace(name: str, tf_name: str, sku: str) -> List[str]:
    """Generate Terraform for azurerm_eventhub_namespace."""
    sku_word = sku.split()[0] if sku else 'Standard'
    return [
        f'resource "azurerm_eventhub_namespace" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        f'  sku                 = "{sku_word}"',
        '  capacity            = 1',
        '}',
    ]


def _tf_emit_sql_server(name: str, tf_name: str) -> List[str]:
    """Generate Terraform for azurerm_mssql_server."""
    return [
        f'resource "azurerm_mssql_server" "{tf_name}" {{',
        f'  name                         = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        '  version                      = "12.0"',
        '  administrator_login          = "sqladmin"',
        '  administrator_login_password = var.sql_admin_password',
        '}',
    ]


def _tf_emit_sql_database(db_name: str, db_tf_name: str, srv_expr: str, sku: str) -> List[str]:
    """Generate Terraform for azurerm_mssql_database."""
    sku_name = sku.split()[-1] if sku else 'GP_S_Gen5_2'
    return [
        f'resource "azurerm_mssql_database" "{db_tf_name}" {{',
        f'  name      = "{db_name}"',
        f'  server_id = {srv_expr}',
        f'  sku_name  = "{sku_name}"',
        '}',
    ]


def _tf_emit_container_registry(name: str, tf_name: str, sku: str) -> List[str]:
    """Generate Terraform for azurerm_container_registry."""
    sku_word = sku.split()[0] if sku else 'Basic'
    return [
        f'resource "azurerm_container_registry" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        f'  sku                 = "{sku_word}"',
        '  admin_enabled       = false',
        '}',
    ]


def _tf_emit_user_assigned_identity(name: str, tf_name: str) -> List[str]:
    """Generate Terraform for azurerm_user_assigned_identity."""
    return [
        f'resource "azurerm_user_assigned_identity" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        '}',
    ]


def _tf_emit_virtual_network(name: str, tf_name: str) -> List[str]:
    """Generate Terraform for azurerm_virtual_network."""
    return [
        f'resource "azurerm_virtual_network" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        '  address_space       = ["10.0.0.0/16"]   # TODO: replace with real address space',
        '}',
    ]


def _tf_emit_network_security_group(name: str, tf_name: str) -> List[str]:
    """Generate Terraform for azurerm_network_security_group."""
    return [
        f'resource "azurerm_network_security_group" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        '  # TODO: add security_rule blocks from the real NSG',
        '}',
    ]


def _tf_emit_private_endpoint(name: str, tf_name: str) -> List[str]:
    """Generate Terraform for azurerm_private_endpoint."""
    return [
        f'resource "azurerm_private_endpoint" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        '  subnet_id           = "<TODO: subnet id from the VNet>"',
        '  private_service_connection {',
        f'    name                           = "{tf_name}_psc"',
        '    is_manual_connection           = false',
        '    private_connection_resource_id = "<TODO: target resource id>"',
        '    subresource_names              = ["<TODO: e.g. sqlServer, sites, blob>"]',
        '  }',
        '}',
    ]


def _tf_emit_private_dns_zone(name: str, tf_name: str) -> List[str]:
    """Generate Terraform for azurerm_private_dns_zone."""
    return [
        f'resource "azurerm_private_dns_zone" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        '}',
    ]


def _tf_emit_private_dns_vnet_link(link_name: str, link_tf_name: str, zone_expr: str) -> List[str]:
    """Generate Terraform for azurerm_private_dns_zone_virtual_network_link."""
    return [
        f'resource "azurerm_private_dns_zone_virtual_network_link" "{link_tf_name}" {{',
        f'  name                  = "{link_name}"',
        f'  {_TF_RG_NAME}',
        f'  private_dns_zone_name = {zone_expr}',
        '  virtual_network_id    = "<TODO: vnet id>"',
        '}',
    ]


def _tf_emit_action_group(name: str, tf_name: str) -> List[str]:
    """Generate Terraform for azurerm_monitor_action_group."""
    return [
        f'resource "azurerm_monitor_action_group" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  short_name          = "{tf_name[:12]}"',
        '  # TODO: add email_receiver / webhook_receiver blocks',
        '}',
    ]


def _tf_emit_activity_log_alert(name: str, tf_name: str) -> List[str]:
    """Generate Terraform for azurerm_monitor_activity_log_alert."""
    return [
        f'resource "azurerm_monitor_activity_log_alert" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        '  scopes              = [data.azurerm_subscription.current.id]',
        '  criteria { category = "ServiceHealth" }',
        '  action {',
        '    action_group_id = "<TODO: action_group_id>"',
        '  }',
        '}',
    ]


def _tf_emit_virtual_machine(name: str, tf_name: str, sku: str) -> List[str]:
    """Generate Terraform for azurerm_linux_virtual_machine."""
    return [
        f'resource "azurerm_linux_virtual_machine" "{tf_name}" {{',
        f'  name                = "{name}"',
        f'  {_TF_RG_NAME}',
        f'  {_TF_LOCATION}',
        f'  size                = "{sku or "Standard_D2s_v3"}"',
        '  admin_username      = "azureuser"',
        '  network_interface_ids = ["<TODO: nic id>"]',
        '  os_disk {',
        '    caching              = "ReadWrite"',
        '    storage_account_type = "Standard_LRS"',
        '  }',
        '  source_image_reference {',
        '    publisher = "Canonical"',
        '    offer     = "UbuntuServer"',
        '    sku       = "18.04-LTS"',
        '    version   = "latest"',
        '  }',
        '}',
    ]


def _tf_emit_unsupported(rtype: str, name: str) -> List[str]:
    """Generate comment for unsupported resource type."""
    return [
        f'# Unsupported resource type for replication: {rtype}',
        f'# Resource name: {name}',
        f'# Declare manually if needed.',
    ]


# ── Terraform dispatch helpers ────────────────────────────────────────────────

_SIMPLE_TF_EMITTERS: Dict[str, Any] = {
    'microsoft.web/serverfarms':                        lambda n, tf, sku, kind: _tf_emit_web_serverfarm(n, tf, sku, kind),
    'microsoft.storage/storageaccounts':                lambda n, tf, sku, kind: _tf_emit_storage_account(n, tf, sku),
    'microsoft.eventhub/namespaces':                    lambda n, tf, sku, kind: _tf_emit_eventhub_namespace(n, tf, sku),
    'microsoft.sql/servers':                            lambda n, tf, sku, kind: _tf_emit_sql_server(n, tf),
    'microsoft.containerregistry/registries':           lambda n, tf, sku, kind: _tf_emit_container_registry(n, tf, sku),
    'microsoft.managedidentity/userassignedidentities': lambda n, tf, sku, kind: _tf_emit_user_assigned_identity(n, tf),
    'microsoft.network/virtualnetworks':                lambda n, tf, sku, kind: _tf_emit_virtual_network(n, tf),
    'microsoft.network/networksecuritygroups':          lambda n, tf, sku, kind: _tf_emit_network_security_group(n, tf),
    'microsoft.network/privateendpoints':               lambda n, tf, sku, kind: _tf_emit_private_endpoint(n, tf),
    'microsoft.network/privatednszones':                lambda n, tf, sku, kind: _tf_emit_private_dns_zone(n, tf),
    'microsoft.insights/actiongroups':                  lambda n, tf, sku, kind: _tf_emit_action_group(n, tf),
    'microsoft.insights/activitylogalerts':             lambda n, tf, sku, kind: _tf_emit_activity_log_alert(n, tf),
    'microsoft.compute/virtualmachines':                lambda n, tf, sku, kind: _tf_emit_virtual_machine(n, tf, sku),
}


def _tf_web_sites_chip(chip: Dict[str, Any], name: str, tf_name: str, kind: str,
                       name_to_tf: Dict[str, str]) -> List[str]:
    plan_ref = chip.get('parent_plan_name')
    plan_tf = name_to_tf.get(plan_ref) if plan_ref else None
    plan_expr = f'azurerm_service_plan.{plan_tf}.id' if plan_tf else '"<TODO: set service_plan_id>"'
    return _tf_emit_web_sites(name, tf_name, kind, plan_expr)


def _tf_sql_database_chip(chip: Dict[str, Any], name: str, tf_name: str, sku: str,
                           name_to_tf: Dict[str, str]) -> List[str]:
    parent_server = chip.get('parent_name') or (name.split('/')[0] if '/' in name else '')
    srv_tf = name_to_tf.get(parent_server)
    srv_expr = f'azurerm_mssql_server.{srv_tf}.id' if srv_tf else '"<TODO: set server_id>"'
    db_name = name.split('/')[-1] if '/' in name else name
    db_tf_name = _safe_tf_name(f'{parent_server}_{db_name}') if parent_server else tf_name
    return _tf_emit_sql_database(db_name, db_tf_name, srv_expr, sku)


def _tf_dns_vnet_link_chip(chip: Dict[str, Any], name: str, tf_name: str,
                            name_to_tf: Dict[str, str]) -> List[str]:
    parent_zone = chip.get('parent_name') or (name.split('/')[0] if '/' in name else '')
    zone_tf = name_to_tf.get(parent_zone)
    zone_expr = f'azurerm_private_dns_zone.{zone_tf}.name' if zone_tf else '"<TODO: dns zone name>"'
    link_name = name.split('/')[-1] if '/' in name else name
    link_tf_name = _safe_tf_name(f'{parent_zone}_{link_name}') if parent_zone else tf_name
    return _tf_emit_private_dns_vnet_link(link_name, link_tf_name, zone_expr)


def _dispatch_tf_resource(chip: Dict[str, Any], name_to_tf: Dict[str, str]) -> List[str]:
    """Dispatch a resource chip to the appropriate Terraform emitter."""
    rtype = (chip['type'] or '').lower()
    name = chip['name']
    tf_name = _safe_tf_name(name)
    sku = chip.get('sku_tier') or ''
    kind = chip.get('kind') or ''
    if rtype == 'microsoft.web/sites':
        return _tf_web_sites_chip(chip, name, tf_name, kind, name_to_tf)
    if rtype == 'microsoft.sql/servers/databases':
        return _tf_sql_database_chip(chip, name, tf_name, sku, name_to_tf)
    if rtype == 'microsoft.network/privatednszones/virtualnetworklinks':
        return _tf_dns_vnet_link_chip(chip, name, tf_name, name_to_tf)
    emitter = _SIMPLE_TF_EMITTERS.get(rtype)
    if emitter:
        return emitter(name, tf_name, sku, kind)
    return _tf_emit_unsupported(rtype, name)


def _build_terraform_script(resource_chips: List[Dict[str, Any]], resource_group: str = None, region: str = "eastus") -> str:
    """Build a complete Terraform script for the discovered resources. Emits modern
    azurerm provider (>= 3.x) syntax. Cross-resource references are made by Terraform
    name where the parent is in the same script; complex links (private-endpoint
    target_resource_id, App Service serverFarmId for sites whose plan lives elsewhere)
    are flagged with a TODO comment so a human can wire them after `terraform init`."""
    if resource_group is None:
        resource_group = os.getenv("AZURE_RESOURCE_GROUP", "prod")
    out: List[str] = [
        'terraform {',
        '  required_providers {',
        '    azurerm = {',
        '      source  = "hashicorp/azurerm"',
        '      version = "~> 3.0"',
        '    }',
        '  }',
        '}',
        '',
        'provider "azurerm" {',
        '  features {}',
        '}',
        '',
        'resource "azurerm_resource_group" "main" {',
        f'  name     = "{resource_group}"',
        f'  location = "{region}"',
        '}',
        '',
    ]
    name_to_tf = {chip['name']: _safe_tf_name(chip['name']) for chip in resource_chips}
    for chip in resource_chips:
        out += _dispatch_tf_resource(chip, name_to_tf)
        out.append('')
    if any(c['type'] == 'Microsoft.Insights/activityLogAlerts' for c in resource_chips):
        out.append('data "azurerm_subscription" "current" {}')
        out.append('')
    return "\n".join(out)


def _bicep_emit_web_sites(bp_name: str, name: str, sku: str) -> List[str]:
    """Generate Bicep for Microsoft.Web/sites with its serverFarm."""
    lines = []
    lines.append(f"resource {bp_name}Plan 'Microsoft.Web/serverfarms@2022-03-01' = {{")
    lines.append(f"  name: '{name}-plan'")
    lines.append(f"  {_BICEP_LOCATION}")
    lines.append(f"  sku: {{ name: '{sku.split()[-1] if sku else 'B2'}', tier: '{sku.split()[0] if sku else 'Basic'}' }}")
    lines.append("  kind: 'linux'")
    lines.append("  properties: { reserved: true }")
    lines.append("}")
    lines.append("")
    lines.append(f"resource {bp_name} 'Microsoft.Web/sites@2022-03-01' = {{")
    lines.append(f"  name: '{name}'")
    lines.append(f"  {_BICEP_LOCATION}")
    lines.append(f"  properties: {{ serverFarmId: {bp_name}Plan.id }}")
    lines.append("}")
    return lines


def _bicep_emit_virtual_machine(bp_name: str, name: str, sku: str) -> List[str]:
    """Generate Bicep for Microsoft.Compute/virtualMachines."""
    return [
        f"resource {bp_name} 'Microsoft.Compute/virtualMachines@2023-03-01' = {{",
        f"  name: '{name}'",
        f"  {_BICEP_LOCATION}",
        f"  properties: {{ hardwareProfile: {{ vmSize: '{sku}' }} }}",
        "}",
    ]


def _bicep_emit_eventhub_namespace(bp_name: str, name: str, sku: str) -> List[str]:
    """Generate Bicep for Microsoft.EventHub/namespaces."""
    return [
        f"resource {bp_name} 'Microsoft.EventHub/namespaces@2022-10-01-preview' = {{",
        f"  name: '{name}'",
        f"  {_BICEP_LOCATION}",
        f"  sku: {{ name: '{sku.split()[0] if sku else 'Standard'}', tier: '{sku.split()[0] if sku else 'Standard'}', capacity: 1 }}",
        "}",
    ]


def _bicep_emit_sql_server(bp_name: str, name: str) -> List[str]:
    """Generate Bicep for Microsoft.Sql/servers."""
    return [
        f"resource {bp_name} 'Microsoft.Sql/servers@2022-05-01-preview' = {{",
        f"  name: '{name}'",
        f"  {_BICEP_LOCATION}",
        "  properties: {",
        "    administratorLogin: 'sqladmin'",
        "    administratorLoginPassword: sqlAdminPassword",
        "    version: '12.0'",
        "  }",
        "}",
    ]


def _bicep_emit_storage_account(bp_name: str, name: str, sku: str) -> List[str]:
    """Generate Bicep for Microsoft.Storage/storageAccounts."""
    tier_parts = sku.split()
    sku_name = tier_parts[-1] if tier_parts else 'Standard_LRS'
    return [
        f"resource {bp_name} 'Microsoft.Storage/storageAccounts@2023-01-01' = {{",
        f"  name: '{name}'",
        f"  {_BICEP_LOCATION}",
        f"  sku: {{ name: '{sku_name}' }}",
        "  kind: 'StorageV2'",
        "}",
    ]


def _build_bicep_script(resource_chips: List[Dict[str, Any]], resource_group: str = None, region: str = "eastus") -> str:
    """Build a complete Bicep script for the discovered resources."""
    if resource_group is None:
        resource_group = os.getenv("AZURE_RESOURCE_GROUP", "prod")
    lines = []
    lines.append(f"// Bicep template generated by CloudLens IaC")
    lines.append(f"// Resource Group: {resource_group} | Region: {region}")
    lines.append("")
    lines.append("targetScope = 'resourceGroup'")
    lines.append("")
    lines.append(f"param location string = '{region}'")
    lines.append("@secure()")
    lines.append("param sqlAdminPassword string")
    lines.append("")

    for chip in resource_chips:
        rtype = chip['type']
        name = chip['name']
        bp_name = name.replace('-', '').lower()
        sku = chip['sku_tier']

        if rtype == _AZURE_WEB_SITES:
            lines.extend(_bicep_emit_web_sites(bp_name, name, sku))

        elif rtype == _AZURE_COMPUTE_VMS:
            lines.extend(_bicep_emit_virtual_machine(bp_name, name, sku))

        elif rtype == _AZURE_EVENTHUB_NAMESPACES:
            lines.extend(_bicep_emit_eventhub_namespace(bp_name, name, sku))

        elif rtype == _AZURE_SQL_SERVERS:
            lines.extend(_bicep_emit_sql_server(bp_name, name))

        elif rtype == _AZURE_STORAGE_ACCOUNTS:
            lines.extend(_bicep_emit_storage_account(bp_name, name, sku))

        lines.append("")

    return "\n".join(lines)


def _generate_terraform_scripts(service: Optional[CloudLensService]) -> str:
    """Generate actual Terraform scripts for the discovered resources."""
    global _last_resources
    try:
        if _last_resources is None:
            resources = _resources_for_replica(service.get_all_resources() if service else _get_demo_resources())
            _last_resources = resources
        else:
            resources = _last_resources

        if not resources:
            return "No Azure resources found to generate Terraform for."

        resource_chips = [_to_chip(r) for r in resources]

        # Gate-first: if any major service is Stopped, show the action card and DO NOT
        # emit the Terraform yet. The user accepts → _simulate_activate_and_apply starts
        # the service(s) AND emits the full Terraform inline in the same response.
        gate = _render_health_gate(
            action_summary="generate the Terraform script",
            suggested_message="Yes, start all stopped services and generate the Terraform",
        )
        if gate:
            return (
                gate
                + "*I'll generate the Terraform script as soon as the stopped service(s) "
                  "are started — click **Accept** above to start them, then I'll emit the "
                  "full HCL in the same reply.*\n"
            )

        # All healthy — emit the Terraform inline immediately AND persist it as the new
        # baseline. Saving on every generation means 'validate' always has something to
        # compare against, and the user can diff future runs against the most recent save.
        tf_script = _build_terraform_script(resource_chips)
        baseline_note = ""
        if _ae is not None:
            try:
                ai = _ae.save_iac_artifact(content=tf_script, service_name="environment", fmt="tf")
                if ai.get("success"):
                    _ae.append_audit_log({
                        "type": "save_iac_artifact",
                        "service": "environment",
                        "result": {
                            "success": True,
                            "summary": f"Saved baseline Terraform to {ai.get('filename')}",
                            "evidence": ai.get("download_url"),
                        },
                    })
                    baseline_note = (f" Saved as baseline `{ai.get('filename')}` — "
                                     f"[Download]({ai.get('download_url')})")
            except Exception as e:
                baseline_note = f" (baseline save failed: {type(e).__name__})"

        output = []
        output.append("## Terraform Scripts Generated")
        output.append("")
        output.append(f"Generated Terraform configuration for **{len(resource_chips)} resources** to replicate the current Azure environment.{baseline_note}")
        output.append("")
        output.append("### main.tf")
        output.append("")
        output.append(_FENCE_HCL)
        output.append(tf_script)
        output.append("```")
        output.append("")
        output.append("### variables.tf")
        output.append("")
        output.append(_FENCE_HCL)
        output.append('variable "sql_admin_password" {')
        output.append('  type      = string')
        output.append('  sensitive = true')
        output.append('  description = "SQL Server admin password"')
        output.append('}')
        output.append("```")
        output.append("")
        output.append("### Usage")
        output.append("")
        output.append("```bash")
        output.append("terraform init")
        output.append("terraform plan")
        output.append("terraform apply")
        output.append("```")
        output.append("")
        output.append("---")
        output.append("")
        output.append(_WHAT_NEXT_HEADER)
        output.append("")
        output.append("- **Execute this Terraform script** (with pre-deployment health check)")
        output.append("- **Generate Bicep templates instead**")
        output.append("- **Show me the terraform plan output**")
        output.append("")
        output.append("*Preview mode — these scripts are generated but not executed.*")
        return "\n".join(output)
    except Exception as e:
        return f"Error generating Terraform scripts: {str(e)}"


def _generate_bicep_scripts(service: Optional[CloudLensService]) -> str:
    """Generate actual Bicep scripts for the discovered resources."""
    global _last_resources
    try:
        if _last_resources is None:
            resources = _resources_for_replica(service.get_all_resources() if service else _get_demo_resources())
            _last_resources = resources
        else:
            resources = _last_resources

        if not resources:
            return "No Azure resources found to generate Bicep for."

        resource_chips = [_to_chip(r) for r in resources]

        bicep_script = _build_bicep_script(resource_chips)

        output = []
        output.append("## Bicep Templates Generated")
        output.append("")
        output.append(f"Generated Bicep template for **{len(resource_chips)} resources** to replicate the current Azure environment.")
        output.append("")
        output.append("### main.bicep")
        output.append("")
        output.append("```bicep")
        output.append(bicep_script)
        output.append("```")
        output.append("")
        output.append("### Usage")
        output.append("")
        output.append("```bash")
        output.append("az deployment group create \\")
        output.append("  --resource-group prod \\")
        output.append("  --template-file main.bicep \\")
        output.append("  --parameters sqlAdminPassword=<password>")
        output.append("```")
        output.append("")
        output.append("---")
        output.append("")
        output.append(_WHAT_NEXT_HEADER)
        output.append("")
        output.append("- **Execute this Bicep template** (with pre-deployment health check)")
        output.append("- **Generate Terraform scripts instead**")
        output.append("- **Validate this environment against production baseline**")
        output.append("")
        output.append("*Preview mode — these scripts are generated but not executed.*")
        return "\n".join(output)
    except Exception as e:
        return f"Error generating Bicep scripts: {str(e)}"


def _build_chips_display(resource_chips: List[Dict[str, Any]], resource_group: str = None, region: str = "eastus") -> str:
    """Build a clean, symmetric, Terraform-ready response format."""
    if resource_group is None:
        resource_group = os.getenv("AZURE_RESOURCE_GROUP", "prod")
    output = []
    output.append("## Azure Environment Configuration")
    output.append("")
    output.append(f"**Resource Group:** `{resource_group}` | **Region:** `{region}` | **Mode:** Preview (no provisioning)")
    output.append(f"**Total Resources:** {len(resource_chips)}")
    output.append("")
    output.append("### Service Configuration Table")
    output.append("")
    output.append("| # | Service | Instance Name | SKU/Tier | Region |")
    output.append("|---|---------|---------------|----------|--------|")
    for idx, chip in enumerate(resource_chips, start=1):
        output.append(f"| {idx} | {chip['display_name']} | `{chip['name']}` | **{chip['sku_tier']}** | {chip['location']} |")
    output.append("")
    output.append("### Terraform Resource Mapping")
    output.append("")
    for idx, chip in enumerate(resource_chips, start=1):
        tf_type = _get_terraform_resource_type(chip['type'])
        output.append(f"{idx}. **{chip['display_name']}** — `{chip['name']}` — **{chip['sku_tier']}**")
        output.append(f"   - Terraform: `{tf_type}`")
        output.append(f"   - Azure Type: `{chip['type']}`")
    output.append("")
    output.append("---")
    output.append("")
    output.append(_WHAT_NEXT_HEADER)
    output.append("")
    output.append("- **Generate Terraform scripts to replicate this environment**")
    output.append("- **Generate Bicep templates for these resources**")
    output.append("- **Validate this environment against a production baseline**")
    output.append("- **Show detailed configuration for a specific service**")
    output.append("")
    output.append("*Click any service chip to enable provisioning — feature not available in this version.*")
    return "\n".join(output)


def _generate_terraform_preview(service: Optional[CloudLensService]) -> str:
    """Generate preview of resources needed for Terraform (no execution)."""
    global _last_resources

    try:
        if _last_resources is None:
            resources = _resources_for_replica(service.get_all_resources() if service else _get_demo_resources())
            _last_resources = resources
        else:
            resources = _last_resources

        if not resources:
            return json.dumps({
                "status": "no_resources",
                "message": "No Azure resources found in the subscription.",
                "resources": []
            })

        # Extract resource details for chips
        resource_chips = []
        for resource in resources:
            resource_chips.append(_to_chip(resource))

        # Build clean, symmetric, Terraform-ready chip display
        chips_display = _build_chips_display(resource_chips)

        gate = _render_health_gate(
            action_summary="apply Terraform",
            suggested_message="Yes, start all stopped services and apply the Terraform",
        )
        if gate:
            return gate + chips_display
        return chips_display

    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Error generating Terraform preview: {str(e)}",
            "resources": []
        })


def _generate_bicep_preview(service: Optional[CloudLensService]) -> str:
    """Generate preview of resources needed for Bicep (no execution)."""
    global _last_resources

    try:
        if _last_resources is None:
            resources = _resources_for_replica(service.get_all_resources() if service else _get_demo_resources())
            _last_resources = resources
        else:
            resources = _last_resources

        if not resources:
            return json.dumps({
                "status": "no_resources",
                "message": "No Azure resources found in the subscription.",
                "resources": []
            })

        # Extract resource details for chips
        resource_chips = []
        for resource in resources:
            resource_chips.append(_to_chip(resource))

        # Build clean, symmetric chip display
        chips_display = _build_chips_display(resource_chips)
        return chips_display

    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Error generating Bicep preview: {str(e)}",
            "resources": []
        })


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "CloudLens IaC Tool", "timestamp": datetime.now().isoformat()}


@app.get("/admin/status")
async def admin_get_status():
    """Read-only probe of the real Azure resources cloudlens monitors. No sim — values come
    from ARM via azure_rest.check_service_connectivity."""
    return {"source": "azure_arm", "states": _load_service_states()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6212)
