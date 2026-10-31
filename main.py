#!/usr/bin/env python3
import os
import sys

def install_dependencies():
    """Install required packages if not available"""
    try:
        import uvicorn
        print("✅ uvicorn is available")
        return True
    except ImportError:
        print("❌ uvicorn not found, attempting to install...")
        try:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "fastapi", "uvicorn[standard]", "pydantic", "aiofiles"],
                          capture_output=True, text=True, check=True)
            print("✅ Dependencies installed successfully")
            return True
        except Exception as e:
            print(f"❌ Failed to install dependencies: {e}")
            return False

if __name__ == "__main__":
    try:
        # Print startup information for Azure logs
        print("🚀 Starting Honeywell Multi-Service Infrastructure")
        print(f"📍 Python version: {sys.version}")
        print(f"📁 Working directory: {os.getcwd()}")
        print(f"📋 Environment PORT: {os.environ.get('WEBSITES_PORT', 'not set')}")
        
        # Check and install dependencies
        if not install_dependencies():
            sys.exit(1)
            
        # Now import uvicorn after ensuring it's installed
        import uvicorn
        
        # Get port from Azure environment
        port = int(os.environ.get("WEBSITES_PORT", 8000))
        print(f"🌐 Starting on port: {port}")
        
        # Start the app — app_startup sets up paths, starts MCP, then serves the agent
        host = os.environ.get("HOST", "127.0.0.1")
        uvicorn.run(
            "app_startup:app",
            host=host,
            port=port,
            reload=False,
            access_log=True,
            workers=1,
            loop="asyncio"
        )
    except Exception as e:
        print(f"❌ Startup error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)