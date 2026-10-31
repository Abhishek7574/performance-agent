from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import psutil
import socket
import subprocess
from datetime import datetime
import json
import re
from typing import List, Dict

app = FastAPI(title="Security Monitor Tool")

class ToolRequest(BaseModel):
    query: str

class ToolResponse(BaseModel):
    result: str

def get_active_connections():
    """Get all active network connections"""
    connections = []
    for conn in psutil.net_connections(kind='inet'):
        try:
            proc = psutil.Process(conn.pid) if conn.pid else None
            connections.append({
                'local_address': f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else 'N/A',
                'remote_address': f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else 'N/A',
                'status': conn.status,
                'pid': conn.pid,
                'process': proc.name() if proc else 'Unknown',
                'family': 'IPv4' if conn.family == socket.AF_INET else 'IPv6',
                'type': 'TCP' if conn.type == socket.SOCK_STREAM else 'UDP'
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    return connections

def get_listening_ports():
    """Get ports that are listening for connections"""
    listening = []
    for conn in psutil.net_connections(kind='inet'):
        if conn.status == 'LISTEN':
            try:
                proc = psutil.Process(conn.pid) if conn.pid else None
                listening.append({
                    'port': conn.laddr.port,
                    'address': conn.laddr.ip,
                    'process': proc.name() if proc else 'Unknown',
                    'pid': conn.pid
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return sorted(listening, key=lambda x: x['port'])

def check_suspicious_connections():
    """Check for potentially suspicious connections"""
    suspicious = []
    connections = get_active_connections()
    
    for conn in connections:
        # Check for unusual ports
        if conn['remote_address'] != 'N/A':
            remote_port = int(conn['remote_address'].split(':')[1])
            if remote_port in [1337, 31337, 4444, 5555, 6666, 8080, 9999]:  # Common backdoor ports
                suspicious.append({
                    'type': 'Unusual Port',
                    'details': f"Connection to suspicious port {remote_port}",
                    'connection': conn
                })
    
    return suspicious

def get_failed_login_attempts():
    """Check for failed login attempts (simplified version)"""
    try:
        # This is a simplified version - in production you'd check /var/log/auth.log
        subprocess.run(['last', '-f', '/var/log/wtmp'], 
                      capture_output=True, text=True, timeout=5)
        failed_logins = []
        # Parse the output for failed attempts (simplified)
        return failed_logins
    except Exception:
        return ["Login monitoring requires elevated permissions"]

def get_security_summary():
    """Get comprehensive security summary"""
    connections = get_active_connections()
    listening_ports = get_listening_ports()
    suspicious = check_suspicious_connections()
    
    return {
        'total_connections': len(connections),
        'listening_ports_count': len(listening_ports),
        'suspicious_activity_count': len(suspicious),
        'active_tcp_connections': len([c for c in connections if c['type'] == 'TCP' and c['status'] == 'ESTABLISHED']),
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

@app.post("/", response_model=ToolResponse)
def _format_connections_report(connections: list) -> str:
    """Format network connections report."""
    result = "🌐 ACTIVE NETWORK CONNECTIONS\n" + "="*70 + "\n\n"
    result += f"{'Local Address':20} | {'Remote Address':20} | {'Status':12} | {'Process':15} | Type\n"
    result += "-" * 85 + "\n"
    
    for conn in connections[:20]:  # Show top 20
        local = conn['local_address'][:19]
        remote = conn['remote_address'][:19] if conn['remote_address'] != 'N/A' else 'N/A'
        result += f"{local:20} | {remote:20} | {conn['status']:12} | {conn['process']:15} | {conn['type']}\n"
    
    if len(connections) > 20:
        result += f"\n... and {len(connections) - 20} more connections"
    
    return result


def _format_listening_ports_report(ports: list) -> str:
    """Format listening ports report."""
    result = "🔓 LISTENING PORTS\n" + "="*50 + "\n\n"
    result += f"{'Port':8} | {'Address':15} | {'Process':20} | PID\n"
    result += "-" * 50 + "\n"
    
    for port in ports:
        result += f"{port['port']:8} | {port['address']:15} | {port['process']:20} | {port['pid']}\n"
    
    return result


def _format_threats_report(suspicious: list, ports: list) -> str:
    """Format security threats analysis report."""
    result = "⚠️  SECURITY THREAT ANALYSIS\n" + "="*50 + "\n\n"
    
    if suspicious:
        result += f"🚨 Found {len(suspicious)} suspicious activities:\n\n"
        for i, threat in enumerate(suspicious, 1):
            result += f"{i}. {threat['type']}: {threat['details']}\n"
            result += f"   Connection: {threat['connection']['local_address']} → {threat['connection']['remote_address']}\n\n"
    else:
        result += "✅ No suspicious network activity detected\n"
    
    # Add listening port analysis
    common_ports = {22: 'SSH', 80: 'HTTP', 443: 'HTTPS', 25: 'SMTP', 53: 'DNS', 21: 'FTP'}
    unusual_ports = [p for p in ports if p['port'] not in common_ports and p['port'] > 1024]
    
    if unusual_ports:
        result += f"\n🔍 Unusual listening ports detected:\n"
        for port in unusual_ports[:10]:
            result += f"   Port {port['port']} - Process: {port['process']}\n"
    
    return result


def _format_security_overview(summary: dict, connections: list, ports: list) -> str:
    """Format comprehensive security overview."""
    result = "🛡️  COMPREHENSIVE SECURITY OVERVIEW\n" + "="*60 + "\n\n"
    result += f"📊 Security Metrics (as of {summary['timestamp']}):\n"
    result += f"   Total Network Connections: {summary['total_connections']}\n"
    result += f"   Active TCP Connections: {summary['active_tcp_connections']}\n"
    result += f"   Listening Ports: {summary['listening_ports_count']}\n"
    
    # Top ports
    result += f"\n🔓 Key Listening Ports:\n"
    key_ports = {22: 'SSH', 80: 'HTTP', 443: 'HTTPS', 25: 'SMTP', 3389: 'RDP'}
    for port in ports[:10]:
        service = key_ports.get(port['port'], 'Custom')
        result += f"   {port['port']:5} ({service:8}) - {port['process']}\n"
    
    # Connection summary by status
    status_count = {}
    for conn in connections:
        status_count[conn['status']] = status_count.get(conn['status'], 0) + 1
    
    result += f"\n📡 Connection Status Summary:\n"
    for status, count in sorted(status_count.items()):
        result += f"   {status:15}: {count:3} connections\n"
    
    return result


def _format_default_status(summary: dict) -> str:
    """Format default security status."""
    result = "🛡️  SECURITY STATUS\n" + "="*30 + "\n"
    result += f"Active Connections: {summary['total_connections']}\n"
    result += f"Listening Ports: {summary['listening_ports_count']}\n"
    result += f"Status: Monitoring\n\n"
    result += "💡 Try: 'active connections', 'listening ports', 'suspicious activity', 'security summary'"
    return result


async def security_monitor(request: ToolRequest):
    """Security monitoring and threat detection"""
    try:
        query = request.query.lower()
        
        if "connections" in query or "network" in query:
            connections = get_active_connections()
            result = _format_connections_report(connections)
                
        elif "ports" in query or "listening" in query:
            ports = get_listening_ports()
            result = _format_listening_ports_report(ports)
                
        elif "suspicious" in query or "threats" in query:
            suspicious = check_suspicious_connections()
            ports = get_listening_ports()
            result = _format_threats_report(suspicious, ports)
                    
        elif "security summary" in query or "overview" in query:
            summary = get_security_summary()
            connections = get_active_connections()
            ports = get_listening_ports()
            result = _format_security_overview(summary, connections, ports)
                
        else:
            # Default security overview
            summary = get_security_summary()
            result = _format_default_status(summary)
        
        return ToolResponse(result=result)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Security monitoring error: {str(e)}")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "Security Monitor Tool"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6105)