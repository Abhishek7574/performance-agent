# Azure App Service Entry Point
# Delegates to app_startup.py which sets up paths, starts MCP, and imports the agent app.
# Used by Azure as: gunicorn app_azure:app --bind 0.0.0.0:8000 --worker-class uvicorn.workers.UvicornWorker

from app_startup import app
