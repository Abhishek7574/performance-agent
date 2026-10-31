from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import psutil
import requests
import socket
import subprocess
from datetime import datetime
import json
import time
from typing import List, Dict, Optional

app = FastAPI(title="Application Monitor Tool")

# Constants for service names
_PROCESS_MONITOR = "Process Monitor"
_LOG_ANALYTICS = "Log Analytics"

class ToolRequest(BaseModel):
    query: str

class ToolResponse(BaseModel):
    result: str

def check_service_status(service_name):
    """Check if a service is running"""
    try:
        for proc in psutil.process_iter(['pid', 'name', 'status', 'cpu_percent', 'memory_percent']):
            if service_name.lower() in proc.info['name'].lower():
                return {
                    'name': proc.info['name'],
                    'pid': proc.info['pid'],
                    'status': proc.info['status'],
                    'cpu_percent': proc.info['cpu_percent'],
                    'memory_percent': proc.info['memory_percent'],
                    'running': True
                }
        return {'name': service_name, 'running': False}
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return {'name': service_name, 'running': False, 'error': 'Access denied'}

def check_url_health(url, timeout=5):
    """Check if a URL/API endpoint is healthy"""
    try:
        start_time = time.time()
        response = requests.get(url, timeout=timeout)
        response_time = (time.time() - start_time) * 1000  # Convert to ms
        
        return {
            'url': url,
            'status_code': response.status_code,
            'response_time_ms': f"{response_time:.2f}ms",
            'healthy': 200 <= response.status_code < 400,
            'content_length': len(response.content) if response.content else 0
        }
    except requests.exceptions.RequestException as e:
        return {
            'url': url,
            'healthy': False,
            'error': str(e),
            'response_time_ms': 'N/A'
        }

def check_database_connections():
    """Check for database connections"""
    db_ports = {
        3306: 'MySQL',
        5432: 'PostgreSQL', 
        1433: 'SQL Server',
        27017: 'MongoDB',
        6379: 'Redis',
        5984: 'CouchDB'
    }
    
    db_connections = []
    for conn in psutil.net_connections():
        if conn.laddr and conn.laddr.port in db_ports:
            try:
                proc = psutil.Process(conn.pid) if conn.pid else None
                db_connections.append({
                    'database': db_ports[conn.laddr.port],
                    'port': conn.laddr.port,
                    'status': conn.status,
                    'process': proc.name() if proc else 'Unknown',
                    'pid': conn.pid
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                continue
    
    return db_connections

def check_web_servers():
    """Check for web server processes"""
    web_servers = ['nginx', 'apache', 'httpd', 'lighttpd', 'caddy']
    running_servers = []
    
    for server in web_servers:
        status = check_service_status(server)
        if status['running']:
            running_servers.append(status)
    
    return running_servers

def get_application_ports():
    """Get applications listening on specific ports"""
    interesting_ports = {
        80: 'HTTP',
        443: 'HTTPS', 
        8080: 'HTTP Alt',
        3000: 'Node.js/React',
        8000: 'Django/Python',
        5000: 'Flask/Python',
        4000: 'Ruby/Rails',
        9000: 'PHP-FPM',
        6103: 'Performance Tool',
        6104: _PROCESS_MONITOR,
        6105: 'Security Monitor',
        6106: _LOG_ANALYTICS,
        6107: 'Application Monitor'
    }
    
    listening_apps = []
    for conn in psutil.net_connections():
        if conn.status == 'LISTEN' and conn.laddr and conn.laddr.port in interesting_ports:
            try:
                proc = psutil.Process(conn.pid) if conn.pid else None
                listening_apps.append({
                    'port': conn.laddr.port,
                    'service': interesting_ports[conn.laddr.port],
                    'process': proc.name() if proc else 'Unknown',
                    'pid': conn.pid,
                    'address': conn.laddr.ip
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                continue
    
    return sorted(listening_apps, key=lambda x: x['port'])

def perform_health_checks():
    """Perform comprehensive health checks"""
    health_urls = [
        'http://127.0.0.1:6103/health',  # Performance Tool
        'http://127.0.0.1:6104/health',  # Process Monitor
        'http://127.0.0.1:6105/health',  # Security Monitor
        'http://127.0.0.1:6106/health',  # Log Analytics
        'http://127.0.0.1:6107/health',  # Application Monitor
    ]
    
    results = []
    for url in health_urls:
        health = check_url_health(url, timeout=3)
        results.append(health)
    
    return results

def get_system_services():
    """Get critical system services status"""
    # Common system services to monitor
    services_to_check = [
        'sshd', 'ssh', 'systemd', 'launchd', 'cron', 'crond',
        'networkd', 'dnsmasq', 'dhcpcd', 'NetworkManager'
    ]
    
    running_services = []
    for service in services_to_check:
        status = check_service_status(service)
        if status['running']:
            running_services.append(status)
    
    return running_services

def _format_health_check_report(health_results: list) -> str:
    """Format health check results into a report."""
    result = "🏥 APPLICATION HEALTH CHECKS\n" + "="*60 + "\n\n"
    
    healthy_count = sum(1 for h in health_results if h['healthy'])
    result += f"Health Summary: {healthy_count}/{len(health_results)} services healthy\n\n"
    
    for health in health_results:
        status_icon = "✅" if health['healthy'] else "❌"
        port_key = health['url'].split(':')[2].split('/')[0]
        service_map = {'6103': 'Performance', '6104': _PROCESS_MONITOR,
                      '6105': 'Security', '6106': _LOG_ANALYTICS, '6107': 'Application Monitor'}
        service_name = service_map.get(port_key, f"Port {port_key}")
        
        result += f"{status_icon} {service_name:18} | "
        if health['healthy']:
            result += f"Status: {health['status_code']} | Response: {health['response_time_ms']}\n"
        else:
            result += f"Error: {health.get('error', 'Unknown error')}\n"
    
    return result

def _format_web_servers_report() -> str:
    """Format web servers and applications report."""
    web_servers = check_web_servers()
    listening_apps = get_application_ports()
    
    result = "🌐 WEB SERVERS & APPLICATIONS\n" + "="*60 + "\n\n"
    
    if web_servers:
        result += "🖥️  Running Web Servers:\n"
        for server in web_servers:
            result += f"   • {server['name']:15} | PID: {server['pid']:6} | CPU: {server['cpu_percent']:5.1f}% | Status: {server['status']}\n"
        result += "\n"
    
    result += "🔗 Applications by Port:\n"
    for app in listening_apps:
        result += f"   Port {app['port']:5} | {app['service']:18} | Process: {app['process']:15} | PID: {app['pid']}\n"
    
    return result

def _format_database_report() -> str:
    """Format database connections report."""
    db_connections = check_database_connections()
    result = "🗄️  DATABASE CONNECTIONS\n" + "="*50 + "\n\n"
    
    if db_connections:
        result += f"Found {len(db_connections)} database connections:\n\n"
        for db in db_connections:
            result += f"📊 {db['database']:12} | Port: {db['port']:5} | Status: {db['status']:12} | Process: {db['process']}\n"
    else:
        result += "No active database connections detected\n"
        result += "Monitored ports: MySQL(3306), PostgreSQL(5432), MongoDB(27017), Redis(6379)\n"
    
    return result

def _format_services_report() -> str:
    """Format system services report."""
    services = get_system_services()
    result = "⚙️  SYSTEM SERVICES STATUS\n" + "="*50 + "\n\n"
    
    if services:
        result += f"Running System Services ({len(services)} found):\n\n"
        for service in services:
            result += f"🔧 {service['name']:15} | PID: {service['pid']:6} | CPU: {service.get('cpu_percent', 0):5.1f}% | "
            result += f"Memory: {service.get('memory_percent', 0):5.1f}% | Status: {service['status']}\n"
    else:
        result += "No monitored system services detected\n"
    
    return result

def _format_monitoring_overview() -> str:
    """Format comprehensive monitoring overview."""
    health_results = perform_health_checks()
    web_servers = check_web_servers()
    db_connections = check_database_connections()
    listening_apps = get_application_ports()
    
    result = "🎯 COMPREHENSIVE APPLICATION MONITORING\n" + "="*70 + "\n\n"
    
    healthy_services = sum(1 for h in health_results if h['healthy'])
    result += f"📊 Overall Health: {healthy_services}/{len(health_results)} monitoring services healthy\n\n"
    
    result += "📈 Quick Statistics:\n"
    result += f"   Active Web Servers: {len(web_servers)}\n"
    result += f"   Database Connections: {len(db_connections)}\n"
    result += f"   Monitored Applications: {len(listening_apps)}\n\n"
    
    result += "🏥 Critical Services:\n"
    for health in health_results:
        port_key = health['url'].split(':')[2].split('/')[0]
        service_map = {'6103': 'Performance Tool', '6104': _PROCESS_MONITOR,
                      '6105': 'Security Monitor', '6106': _LOG_ANALYTICS}
        service_name = service_map.get(port_key, f"Port {port_key}")
        
        status = "🟢 Healthy" if health['healthy'] else "🔴 Down"
        result += f"   {service_name:20} | {status}\n"
    
    return result

def _format_performance_report() -> str:
    """Format performance monitoring status report."""
    perf_health = check_url_health('http://127.0.0.1:6103/health')
    result = "⚡ PERFORMANCE MONITORING STATUS\n" + "="*50 + "\n\n"
    
    if perf_health['healthy']:
        result += "✅ Performance Tool: Online and Healthy\n"
        result += f"   Response Time: {perf_health['response_time_ms']}\n"
        result += f"   Status Code: {perf_health['status_code']}\n"
        result += f"   Last Check: {datetime.now().strftime('%H:%M:%S')}\n"
    else:
        result += "❌ Performance Tool: Offline or Unreachable\n"
        result += f"   Error: {perf_health.get('error', 'Unknown')}\n"
    
    return result

def _format_default_overview() -> str:
    """Format default application overview."""
    listening_apps = get_application_ports()
    result = "🏗️  APPLICATION OVERVIEW\n" + "="*40 + "\n"
    result += f"Active Applications: {len(listening_apps)}\n"
    result += f"Monitoring Services: Running\n"
    result += f"Last Check: {datetime.now().strftime('%H:%M:%S')}\n\n"
    result += "💡 Try: 'health checks', 'web servers', 'database connections', 'monitoring overview'"
    return result

@app.post("/", response_model=ToolResponse)
async def monitor_applications(request: ToolRequest):
    """Application and service monitoring"""
    try:
        query = request.query.lower()
        
        if "health check" in query or "health" in query:
            result = _format_health_check_report(perform_health_checks())
        elif "web servers" in query or "servers" in query:
            result = _format_web_servers_report()
        elif "database" in query or "db" in query:
            result = _format_database_report()
        elif "services" in query or "system services" in query:
            result = _format_services_report()
        elif "monitoring" in query or "overview" in query:
            result = _format_monitoring_overview()
        elif "performance" in query:
            result = _format_performance_report()
        else:
            result = _format_default_overview()
        
        return ToolResponse(result=result)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Application monitoring error: {str(e)}")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "Application Monitor Tool", "timestamp": datetime.now().isoformat()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6107)