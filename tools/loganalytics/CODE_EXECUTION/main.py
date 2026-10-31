from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import os
import re
import logging
from datetime import datetime, timedelta
import json
from typing import List, Dict
import glob

logger = logging.getLogger(__name__)

app = FastAPI(title="Log Analytics Tool")

class ToolRequest(BaseModel):
    query: str

class ToolResponse(BaseModel):
    result: str

def get_system_logs(lines=100):
    """Get recent system logs"""
    try:
        # Try different log locations based on system
        log_commands = [
            ['tail', '-n', str(lines), '/var/log/system.log'],  # macOS
            ['dmesg', '|', 'tail', '-50'],  # Linux kernel messages
            ['journalctl', '--no-pager', '-n', str(lines)],  # systemd systems
        ]
        
        for cmd in log_commands:
            try:
                if cmd[0] == 'journalctl':
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                else:
                    result = subprocess.run(' '.join(cmd), shell=True, capture_output=True, text=True, timeout=10)
                
                if result.returncode == 0 and result.stdout:
                    return parse_log_entries(result.stdout)
            except Exception as exc:
                logger.debug("Log command unavailable, trying next: %s", exc)
                continue

        # Fallback to available logs
        return get_available_logs()
    except Exception as e:
        return [f"Error accessing system logs: {str(e)}"]

def _read_log_file(log_file: str, file_path: str) -> list:
    """Read last 50 lines from a log file."""
    try:
        with open(file_path, 'r') as f:
            content = f.readlines()[-50:]
            return [f"[{log_file}] {line.strip()}" for line in content if line.strip()]
    except Exception as exc:
        logger.debug("Could not read log file %s: %s", log_file, exc)
        return []


def _scan_log_directory(log_dir: str) -> list:
    """Scan a directory for log files and read their contents."""
    logs = []
    if not os.path.exists(log_dir):
        return logs
    
    try:
        for log_file in os.listdir(log_dir):
            if log_file.endswith('.log'):
                file_path = os.path.join(log_dir, log_file)
                logs.extend(_read_log_file(log_file, file_path))
    except Exception as exc:
        logger.debug("Could not list log directory %s: %s", log_dir, exc)
    
    return logs


def get_available_logs():
    """Get logs from available sources"""
    logs = []
    
    # Check common log directories
    log_dirs = ['/var/log', '/usr/local/var/log', './logs']
    
    for log_dir in log_dirs:
        logs.extend(_scan_log_directory(log_dir))
    
    if not logs:
        # Generate synthetic log data for demo
        logs = generate_demo_logs()
    
    return logs[:100]  # Return last 100 entries

def generate_demo_logs():
    """Generate realistic demo log entries"""
    now = datetime.now()
    demo_logs = []
    
    events = [
        "User authentication successful for admin",
        "Database connection established",
        "HTTP request processed: GET /api/status - 200 OK",
        "Cache miss for key: user_session_12345",
        "Background job completed: data_backup",
        "Warning: High memory usage detected (85%)",
        "Error: Failed to connect to external API - timeout",
        "Info: System health check passed",
        "Security alert: Unusual login pattern detected",
        "Performance: Query execution time 2.5s",
        "Info: Automatic system update completed",
        "Warning: Disk space running low on /var partition",
    ]
    
    def _get_log_level(event_text: str) -> str:
        """Extract log level from event text."""
        if "Info" in event_text:
            return "INFO"
        elif "Warning" in event_text:
            return "WARN"
        elif "Error" in event_text:
            return "ERROR"
        else:
            return "INFO"
    
    for i in range(50):
        timestamp = now - timedelta(minutes=i*5)
        event = events[i % len(events)]
        level = _get_log_level(event)
        demo_logs.append(f"{timestamp.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {event}")
    
    return demo_logs

def parse_log_entries(log_text):
    """Parse log entries and extract relevant information"""
    lines = log_text.split('\n')
    entries = []
    
    for line in lines:
        if line.strip():
            entries.append(line.strip())
    
    return entries

def analyze_error_patterns(logs):
    """Analyze logs for error patterns"""
    error_patterns = {
        'authentication_failures': r'(auth|login|authentication).*(fail|error|denied)',
        'connection_errors': r'(connect|connection).*(error|fail|timeout|refused)',
        'database_errors': r'(database|db|sql).*(error|fail|timeout)',
        'memory_issues': r'(memory|ram|oom).*(error|warning|high|low)',
        'disk_issues': r'(disk|storage|space).*(full|error|warning|low)',
        'network_issues': r'(network|tcp|udp|http).*(error|timeout|fail)'
    }
    
    error_analysis = {}
    
    for pattern_name, pattern in error_patterns.items():
        matches = []
        for log in logs:
            if re.search(pattern, log.lower()):
                matches.append(log)
        error_analysis[pattern_name] = matches
    
    return error_analysis

def get_log_statistics(logs):
    """Get statistics from log entries"""
    total_logs = len(logs)
    error_count = len([log for log in logs if re.search(r'error|fail', log.lower())])
    warning_count = len([log for log in logs if re.search(r'warn|warning', log.lower())])
    info_count = total_logs - error_count - warning_count
    
    return {
        'total_entries': total_logs,
        'errors': error_count,
        'warnings': warning_count,
        'info': info_count,
        'error_rate': f"{(error_count/total_logs*100):.1f}%" if total_logs > 0 else "0%"
    }

def _get_health_status(error_count: int) -> str:
    """Determine health status based on error count."""
    if error_count < 5:
        return '🟢 Good'
    elif error_count < 20:
        return '🟡 Warning'
    else:
        return '🔴 Critical'

def _format_recent_logs(logs: list) -> str:
    """Format recent logs for display."""
    result = "📋 RECENT SYSTEM LOGS\n" + "="*80 + "\n\n"
    for i, log in enumerate(logs[-20:], 1):
        result += f"{i:2}. {log}\n"
    
    if len(logs) > 20:
        result += f"\n... and {len(logs) - 20} more entries"
    
    return result

def _format_error_analysis(error_analysis: dict) -> str:
    """Format error pattern analysis for display."""
    result = "🚨 ERROR PATTERN ANALYSIS\n" + "="*60 + "\n\n"
    
    total_errors = sum(len(errors) for errors in error_analysis.values())
    result += f"Total Error Patterns Found: {total_errors}\n\n"
    
    for pattern_name, matches in error_analysis.items():
        if matches:
            pattern_display = pattern_name.replace('_', ' ').title()
            result += f"🔍 {pattern_display} ({len(matches)} found):\n"
            for match in matches[:3]:
                result += f"   • {match[:80]}...\n"
            if len(matches) > 3:
                result += f"   ... and {len(matches) - 3} more similar entries\n"
            result += "\n"
    
    return result

def _format_statistics(stats: dict) -> str:
    """Format log statistics for display."""
    now = datetime.now()
    result = "📊 LOG ANALYSIS STATISTICS\n" + "="*50 + "\n\n"
    result += f"📈 Log Summary:\n"
    result += f"   Total Entries Analyzed: {stats['total_entries']}\n"
    result += f"   Info Messages: {stats['info']}\n"
    result += f"   Warnings: {stats['warnings']}\n"
    result += f"   Errors: {stats['errors']}\n"
    result += f"   Error Rate: {stats['error_rate']}\n\n"
    result += f"🕐 Recent Issues (Last analyzed): {now.strftime('%H:%M:%S')}\n"
    
    return result

def _format_monitoring(logs: list, stats: dict) -> str:
    """Format real-time monitoring display."""
    result = "🎯 REAL-TIME LOG MONITORING\n" + "="*60 + "\n\n"
    result += f"⚡ Live Status (Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}):\n"
    result += f"   System Health: {_get_health_status(stats['errors'])}\n"
    result += f"   Error Rate: {stats['error_rate']}\n"
    result += f"   Recent Entries: {stats['total_entries']}\n\n"
    
    critical_events = [log for log in logs if any(word in log.lower() for word in ['error', 'critical', 'fail', 'exception'])]
    if critical_events:
        result += "🚨 Recent Critical Events:\n"
        for event in critical_events[-5:]:
            result += f"   • {event[:70]}...\n"
    else:
        result += "✅ No critical events in recent logs\n"
    
    return result

def _format_search_results(search_term: str, matching_logs: list) -> str:
    """Format search results for display."""
    result = f"🔍 SEARCH RESULTS FOR '{search_term}'\n" + "="*60 + "\n\n"
    result += f"Found {len(matching_logs)} matching entries:\n\n"
    
    for i, log in enumerate(matching_logs[:15], 1):
        result += f"{i:2}. {log}\n"
        
    if len(matching_logs) > 15:
        result += f"\n... and {len(matching_logs) - 15} more matches"
    
    return result

def _format_overview(stats: dict) -> str:
    """Format log overview display."""
    result = "📋 LOG ANALYTICS OVERVIEW\n" + "="*40 + "\n"
    result += f"Recent Entries: {stats['total_entries']}\n"
    result += f"Errors: {stats['errors']}\n"
    result += f"Warnings: {stats['warnings']}\n"
    result += f"Error Rate: {stats['error_rate']}\n\n"
    
    return result

@app.post("/", response_model=ToolResponse)
async def analyze_logs(request: ToolRequest):
    """Real-time log analysis and monitoring"""
    try:
        query = request.query.lower()
        
        if "recent logs" in query or "latest logs" in query:
            logs = get_system_logs(50)
            result = _format_recent_logs(logs)
                
        elif "error" in query or "errors" in query:
            logs = get_system_logs(200)
            error_analysis = analyze_error_patterns(logs)
            result = _format_error_analysis(error_analysis)
                    
        elif "statistics" in query or "stats" in query:
            logs = get_system_logs(500)
            stats = get_log_statistics(logs)
            result = _format_statistics(stats)
            
        elif "monitoring" in query or "real-time" in query:
            logs = get_system_logs(100)
            stats = get_log_statistics(logs)
            result = _format_monitoring(logs, stats)
                
        elif "search" in query:
            search_term = query.replace("search", "").replace("for", "").strip()
            if search_term:
                logs = get_system_logs(200)
                matching_logs = [log for log in logs if search_term in log.lower()]
                result = _format_search_results(search_term, matching_logs)
            else:
                result = "Please specify a search term. Example: 'search for authentication errors'"
                
        else:
            logs = get_system_logs(100)
            stats = get_log_statistics(logs)
            result = _format_overview(stats)
            result += "💡 Try: 'recent logs', 'error analysis', 'log statistics', 'real-time monitoring'"
        
        return ToolResponse(result=result)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Log analytics error: {str(e)}")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "Log Analytics Tool"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6106)