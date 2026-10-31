from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import psutil
import subprocess
from datetime import datetime
import json
from typing import List, Dict

app = FastAPI(title="Advanced Process Monitor Tool")

class ToolRequest(BaseModel):
    query: str

class ToolResponse(BaseModel):
    result: str

def get_top_processes_by_cpu(limit=10):
    """Get top processes by CPU usage"""
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'create_time', 'status']):
        try:
            proc.cpu_percent()  # First call returns 0
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    
    # Wait a bit then get real CPU usage
    import time
    time.sleep(1)
    
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'create_time', 'status']):
        try:
            info = proc.info
            processes.append({
                'pid': info['pid'],
                'name': info['name'],
                'cpu_percent': info['cpu_percent'],
                'memory_percent': info['memory_percent'],
                'status': info['status'],
                'create_time': datetime.fromtimestamp(info['create_time']).strftime('%Y-%m-%d %H:%M:%S')
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    
    return sorted(processes, key=lambda x: x['cpu_percent'] or 0, reverse=True)[:limit]

def get_top_processes_by_memory(limit=10):
    """Get top processes by memory usage"""
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_percent', 'memory_info', 'cpu_percent', 'status']):
        try:
            info = proc.info
            memory_mb = info['memory_info'].rss / (1024 * 1024)  # Convert to MB
            processes.append({
                'pid': info['pid'],
                'name': info['name'],
                'memory_percent': info['memory_percent'],
                'memory_mb': f"{memory_mb:.2f} MB",
                'cpu_percent': info['cpu_percent'],
                'status': info['status']
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    
    return sorted(processes, key=lambda x: x['memory_percent'] or 0, reverse=True)[:limit]

def get_process_count_by_status():
    """Get process count by status"""
    status_count = {}
    for proc in psutil.process_iter(['status']):
        try:
            status = proc.info['status']
            status_count[status] = status_count.get(status, 0) + 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return status_count

def get_detailed_process_info(pid: int):
    """Get detailed information for a specific process"""
    try:
        proc = psutil.Process(pid)
        return {
            'pid': proc.pid,
            'name': proc.name(),
            'status': proc.status(),
            'cpu_percent': proc.cpu_percent(),
            'memory_percent': proc.memory_percent(),
            'memory_info': f"{proc.memory_info().rss / (1024*1024):.2f} MB",
            'create_time': datetime.fromtimestamp(proc.create_time()).strftime('%Y-%m-%d %H:%M:%S'),
            'num_threads': proc.num_threads(),
            'connections': len(proc.connections()) if hasattr(proc, 'connections') else 'N/A',
            'open_files': len(proc.open_files()) if hasattr(proc, 'open_files') else 'N/A'
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {'error': f'Process {pid} not found or access denied'}

def _format_top_cpu_processes(top_cpu: list) -> str:
    """Format top CPU processes for display."""
    result = "🔥 TOP PROCESSES BY CPU USAGE\n" + "="*60 + "\n\n"
    for i, proc in enumerate(top_cpu, 1):
        result += f"{i:2}. {proc['name']:25} | PID: {proc['pid']:8} | CPU: {proc['cpu_percent']:6.1f}% | MEM: {proc['memory_percent']:5.1f}% | Status: {proc['status']}\n"
    return result

def _format_top_memory_processes(top_mem: list) -> str:
    """Format top memory processes for display."""
    result = "🧠 TOP PROCESSES BY MEMORY USAGE\n" + "="*60 + "\n\n"
    for i, proc in enumerate(top_mem, 1):
        result += f"{i:2}. {proc['name']:25} | PID: {proc['pid']:8} | MEM: {proc['memory_percent']:6.1f}% | Size: {proc['memory_mb']:>12} | CPU: {proc['cpu_percent']:5.1f}%\n"
    return result

def _format_status_count(status_count: dict) -> str:
    """Format process status count for display."""
    total = sum(status_count.values())
    result = f"📊 PROCESS STATUS SUMMARY\n" + "="*40 + f"\nTotal Processes: {total}\n\n"
    for status, count in sorted(status_count.items()):
        result += f"{status:15}: {count:4} processes\n"
    return result

def _format_comprehensive_overview(status_count: dict, top_cpu: list, top_mem: list) -> str:
    """Format comprehensive process overview for display."""
    total = sum(status_count.values())
    result = "🖥️  COMPREHENSIVE PROCESS ANALYSIS\n" + "="*70 + "\n\n"
    
    result += f"📊 Total Processes: {total}\n"
    for status, count in status_count.items():
        result += f"   {status}: {count}\n"
    
    result += f"\n🔥 Top 10 CPU Intensive Processes:\n" + "-"*50 + "\n"
    for i, proc in enumerate(top_cpu, 1):
        result += f"{i:2}. {proc['name']:20} | {proc['cpu_percent']:5.1f}% CPU | PID: {proc['pid']}\n"
    
    result += f"\n🧠 Top 10 Memory Intensive Processes:\n" + "-"*50 + "\n"
    for i, proc in enumerate(top_mem, 1):
        result += f"{i:2}. {proc['name']:20} | {proc['memory_percent']:5.1f}% MEM | {proc['memory_mb']}\n"
    
    return result

def _format_quick_overview(status_count: dict) -> str:
    """Format quick process overview for display."""
    total = sum(status_count.values())
    result = "⚡ QUICK PROCESS OVERVIEW\n" + "="*40 + "\n"
    result += f"Total Active Processes: {total}\n"
    result += f"Running: {status_count.get('running', 0)}\n"
    result += f"Sleeping: {status_count.get('sleeping', 0)}\n"
    return result

@app.post("/", response_model=ToolResponse)
async def monitor_processes(request: ToolRequest):
    """Advanced process monitoring"""
    try:
        query = request.query.lower()
        
        if "top cpu" in query or "cpu processes" in query:
            top_cpu = get_top_processes_by_cpu(15)
            result = _format_top_cpu_processes(top_cpu)
            
        elif "top memory" in query or "memory processes" in query:
            top_mem = get_top_processes_by_memory(15)
            result = _format_top_memory_processes(top_mem)
            
        elif "process count" in query or "status count" in query:
            status_count = get_process_count_by_status()
            result = _format_status_count(status_count)
                
        elif "all processes" in query or "process overview" in query:
            status_count = get_process_count_by_status()
            top_cpu = get_top_processes_by_cpu(10)
            top_mem = get_top_processes_by_memory(10)
            result = _format_comprehensive_overview(status_count, top_cpu, top_mem)
                
        else:
            status_count = get_process_count_by_status()
            total = sum(status_count.values())
            result = _format_quick_overview(status_count)
            result += f"Other: {total - status_count.get('running', 0) - status_count.get('sleeping', 0)}\n\n"
            result += "💡 Try: 'top cpu processes', 'top memory processes', 'all processes'"
        
        return ToolResponse(result=result)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Process monitoring error: {str(e)}")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "Process Monitor Tool"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6104)