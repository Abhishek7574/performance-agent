from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import psutil
import requests
from datetime import datetime, timedelta
import json
import asyncio
from typing import Dict, List

app = FastAPI(title="Real-time Dashboard Tool")

class ToolRequest(BaseModel):
    query: str

class ToolResponse(BaseModel):
    result: str

async def gather_all_metrics():
    """Gather metrics from all monitoring tools"""
    tools = [
        ('Performance', 'http://127.0.0.1:6103/', 'all stats'),
        ('Process Monitor', 'http://127.0.0.1:6104/', 'process overview'),  
        ('Security Monitor', 'http://127.0.0.1:6105/', 'security summary'),
        ('Log Analytics', 'http://127.0.0.1:6106/', 'statistics'),
        ('Application Monitor', 'http://127.0.0.1:6107/', 'overview')
    ]
    
    metrics = {}
    
    for tool_name, url, query in tools:
        try:
            response = requests.post(url, 
                json={"query": query}, 
                timeout=5,
                headers={"Content-Type": "application/json"}
            )
            if response.status_code == 200:
                data = response.json()
                metrics[tool_name] = {
                    'status': 'online',
                    'data': data.get('result', ''),
                    'response_time': f"{response.elapsed.total_seconds()*1000:.2f}ms"
                }
            else:
                metrics[tool_name] = {'status': 'error', 'code': response.status_code}
        except Exception as e:
            metrics[tool_name] = {'status': 'offline', 'error': str(e)}
    
    return metrics

def get_system_overview():
    """Get high-level system overview"""
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot_time
        
        return {
            'cpu_usage': f"{cpu_percent:.1f}%",
            'memory_usage': f"{memory.percent:.1f}%",
            'memory_available': f"{memory.available / (1024**3):.1f} GB",
            'disk_usage': f"{disk.percent:.1f}%",
            'disk_free': f"{disk.free / (1024**3):.1f} GB",
            'uptime': str(uptime).split('.')[0],
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    except Exception as e:
        return {'error': str(e)}

def create_ascii_chart(values, width=20):
    """Create simple ASCII bar chart"""
    if not values or max(values) == 0:
        return "No data available"
    
    max_val = max(values)
    chart = ""
    
    for i, val in enumerate(values):
        bar_length = int((val / max_val) * width)
        bar = "█" * bar_length + "░" * (width - bar_length)
        chart += f"{i+1:2}: {bar} {val:.1f}%\n"
    
    return chart

def get_top_processes_summary():
    """Get summary of top processes"""
    try:
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            processes.append(proc.info)
        
        # Sort by CPU usage
        top_cpu = sorted(processes, key=lambda x: x['cpu_percent'] or 0, reverse=True)[:5]
        # Sort by Memory usage  
        top_memory = sorted(processes, key=lambda x: x['memory_percent'] or 0, reverse=True)[:5]
        
        return {
            'top_cpu': top_cpu,
            'top_memory': top_memory
        }
    except Exception as e:
        return {'error': str(e)}

def create_live_dashboard():
    """Create comprehensive live dashboard"""
    system = get_system_overview()
    processes = get_top_processes_summary()
    
    dashboard = f"""
🖥️  REAL-TIME SYSTEM DASHBOARD
{'='*80}
📅 Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
⏱️  System Uptime: {system.get('uptime', 'N/A')}

🔥 RESOURCE UTILIZATION
{'─'*40}
CPU Usage    : {system.get('cpu_usage', 'N/A'):>10} 
Memory Usage : {system.get('memory_usage', 'N/A'):>10} ({system.get('memory_available', 'N/A')} available)
Disk Usage   : {system.get('disk_usage', 'N/A'):>10} ({system.get('disk_free', 'N/A')} free)

🏃 TOP PROCESSES (CPU)
{'─'*40}"""

    if 'error' not in processes:
        for i, proc in enumerate(processes['top_cpu'], 1):
            dashboard += f"\n{i}. {proc['name'][:20]:20} | CPU: {proc['cpu_percent']:5.1f}% | PID: {proc['pid']}"
        
        dashboard += f"\n\n🧠 TOP PROCESSES (MEMORY)\n{'─'*40}"
        for i, proc in enumerate(processes['top_memory'], 1):
            dashboard += f"\n{i}. {proc['name'][:20]:20} | MEM: {proc['memory_percent']:5.1f}% | PID: {proc['pid']}"
    
    return dashboard

def _build_live_dashboard() -> str:
    """Build live real-time dashboard view."""
    return create_live_dashboard()

def _build_summary_dashboard() -> str:
    """Build summary overview dashboard."""
    system = get_system_overview()
    
    return f"""
📊 MONITORING SUITE DASHBOARD
{'='*60}

🎯 System Health Status
{'─'*30}
🖥️  CPU: {system.get('cpu_usage', 'N/A')} | 🧠 Memory: {system.get('memory_usage', 'N/A')} | 💾 Disk: {system.get('disk_usage', 'N/A')}
⏰ Uptime: {system.get('uptime', 'N/A')}
🕐 Last Updated: {system.get('timestamp', 'N/A')}

🔧 Available Monitoring Tools
{'─'*30}
✅ Performance Tool      (Port 6103) - CPU, Memory, Disk, Network
✅ Process Monitor       (Port 6104) - Top Processes, Process Management  
✅ Security Monitor      (Port 6105) - Network Security, Connections
✅ Log Analytics         (Port 6106) - System Logs, Error Analysis
✅ Application Monitor   (Port 6107) - Service Health, API Status
✅ Dashboard Tool        (Port 6108) - This Dashboard
"""

async def _build_comprehensive_dashboard() -> str:
    """Build comprehensive dashboard with all tools data."""
    metrics = await gather_all_metrics()
    
    result = f"""
🎛️  COMPREHENSIVE MONITORING DASHBOARD
{'='*80}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

🛠️  MONITORING TOOLS STATUS
{'─'*50}"""
    
    for tool_name, data in metrics.items():
        status_icon = _get_status_icon(data['status'])
        result += f"\n{status_icon} {tool_name:18} | Status: {data['status']:8}"
        if 'response_time' in data:
            result += f" | Response: {data['response_time']}"
        elif 'error' in data:
            result += f" | Error: {data['error'][:30]}..."
    
    # Add system overview
    system = get_system_overview()
    result += f"\n\n💻 SYSTEM OVERVIEW\n{'─'*30}"
    result += f"\nCPU: {system.get('cpu_usage', 'N/A'):>8} | Memory: {system.get('memory_usage', 'N/A'):>8} | Disk: {system.get('disk_usage', 'N/A'):>8}"
    result += f"\nUptime: {system.get('uptime', 'N/A')}"
    
    return result

def _get_status_icon(status: str) -> str:
    """Get emoji icon for status."""
    if status == 'online':
        return "🟢"
    elif status == 'offline':
        return "🔴"
    else:
        return "🟡"

def _build_metrics_dashboard() -> str:
    """Build live metrics feed dashboard."""
    system = get_system_overview()
    processes = get_top_processes_summary()
    
    result = f"""
📈 LIVE METRICS FEED
{'='*50}
⏰ Timestamp: {datetime.now().strftime('%H:%M:%S')}

📊 Current Resource Usage:
   🔥 CPU: {system.get('cpu_usage', 'N/A')}
   🧠 Memory: {system.get('memory_usage', 'N/A')} 
   💾 Disk: {system.get('disk_usage', 'N/A')}

🏆 Performance Leaders:"""
    
    if 'error' not in processes:
        result += f"\n   Top CPU: {processes['top_cpu'][0]['name']} ({processes['top_cpu'][0]['cpu_percent']:.1f}%)"
        result += f"\n   Top Memory: {processes['top_memory'][0]['name']} ({processes['top_memory'][0]['memory_percent']:.1f}%)"
    
    result += f"\n\n🔄 Auto-refresh available - ask for 'live dashboard' for continuous monitoring"
    
    return result

def _build_health_dashboard() -> str:
    """Build health check dashboard for all tools."""
    tools_health = {
        'Performance Tool': 'http://127.0.0.1:6103/health',
        'Process Monitor': 'http://127.0.0.1:6104/health', 
        'Security Monitor': 'http://127.0.0.1:6105/health',
        'Log Analytics': 'http://127.0.0.1:6106/health',
        'Application Monitor': 'http://127.0.0.1:6107/health'
    }
    
    result = "🏥 MONITORING SUITE HEALTH CHECK\n" + "="*60 + "\n\n"
    
    healthy_tools = 0
    for tool_name, health_url in tools_health.items():
        try:
            response = requests.get(health_url, timeout=3)
            if response.status_code == 200:
                result += f"✅ {tool_name:20} | Healthy | Response: {response.elapsed.total_seconds()*1000:.0f}ms\n"
                healthy_tools += 1
            else:
                result += f"⚠️  {tool_name:20} | Warning | Status: {response.status_code}\n"
        except Exception:
            result += f"❌ {tool_name:20} | Offline | No response\n"
    
    result += f"\n📊 Overall Health: {healthy_tools}/{len(tools_health)} tools operational"
    health_percentage = (healthy_tools / len(tools_health)) * 100
    if health_percentage >= 80:
        result += " 🟢"
    elif health_percentage >= 60:
        result += " 🟡"
    else:
        result += " 🔴"
    
    return result

def _build_default_dashboard() -> str:
    """Build default dashboard view."""
    system = get_system_overview()
    
    return f"""
🎛️  MONITORING DASHBOARD
{'='*40}

⚡ Quick Status
CPU: {system.get('cpu_usage', 'N/A')} | Memory: {system.get('memory_usage', 'N/A')} | Disk: {system.get('disk_usage', 'N/A')}
Uptime: {system.get('uptime', 'N/A')}

🛠️  Available Views:
• 'live dashboard' - Real-time system view
• 'comprehensive dashboard' - All tools data
• 'health status' - Tool health checks  
• 'live metrics' - Current performance data
• 'summary overview' - System summary

🕐 Last Updated: {datetime.now().strftime('%H:%M:%S')}
"""

@app.post("/", response_model=ToolResponse)
async def dashboard_tool(request: ToolRequest):
    """Real-time monitoring dashboard with live metrics and visualizations"""
    try:
        query = request.query.lower()
        
        if "live" in query or "real-time" in query:
            result = _build_live_dashboard()
        elif "summary" in query or "overview" in query:
            result = _build_summary_dashboard()
        elif "all tools" in query or "comprehensive" in query:
            result = await _build_comprehensive_dashboard()
        elif "metrics" in query or "data" in query:
            result = _build_metrics_dashboard()
        elif "health" in query or "status" in query:
            result = _build_health_dashboard()
        else:
            result = _build_default_dashboard()
        
        return ToolResponse(result=result)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Dashboard error: {str(e)}")

@app.get("/health")
async def health_check():
    return {
        "status": "healthy", 
        "service": "Dashboard Tool",
        "timestamp": datetime.now().isoformat(),
        "features": ["live_dashboard", "metrics_aggregation", "health_monitoring"]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6108)