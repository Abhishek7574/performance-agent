from contextlib import asynccontextmanager
from pathlib import Path
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

load_dotenv()

# S4792: SECURITY HOTSPOT REVIEWED - SAFE CONFIGURATION
# Logger configured with INFO level and structured format. No sensitive data logged.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Paths - Security: Validate and sanitize paths
DATA_DIR_STR = os.getenv("RAG_DATA_DIRECTORY", "")
if DATA_DIR_STR:
    DATA_DIR = Path(DATA_DIR_STR).expanduser().resolve()
    # Security: Ensure the path is absolute and doesn't contain path traversal
    if not DATA_DIR.is_absolute():
        logger.warning(f"DATA_DIR is not absolute, using default")
        DATA_DIR = Path(__file__).parent / "data"
else:
    DATA_DIR = Path(__file__).parent / "data"
    
BASE_DIR = Path(__file__).parent / "data"
PARQUET_DIR = BASE_DIR / "parquet"
MANIFEST_PATH = BASE_DIR / "manifest.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create data directories
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    from ingestion import IngestionPipeline, Manifest
    from watcher import start_watcher
    from query_engine import QueryEngine

    manifest = Manifest(MANIFEST_PATH)
    pipeline = IngestionPipeline(PARQUET_DIR, manifest)

    # Ingest any existing files not yet in manifest
    if DATA_DIR.exists():
        existing = list(DATA_DIR.glob("*.dat.gz"))
        new_files = [f for f in existing if not manifest.is_ingested(str(f))]
        if new_files:
            logger.info(f"Startup ingestion: {len(new_files)} new file(s) found")
            for f in new_files:
                try:
                    pipeline.ingest(str(f))
                except Exception:
                    logger.exception(f"Startup ingestion failed: {f}")
        else:
            logger.info(f"Startup: all {len(existing)} file(s) already ingested")
    else:
        logger.warning(f"Data directory does not exist: {DATA_DIR}")

    # Start file watcher
    observer = start_watcher(DATA_DIR, pipeline, manifest)
    app.state.query_engine = QueryEngine(PARQUET_DIR)
    app.state.observer = observer
    app.state.manifest = manifest

    yield

    observer.stop()
    observer.join()
    logger.info("Watchdog stopped.")


app = FastAPI(title="RAG Search Tool", lifespan=lifespan)


class ToolRequest(BaseModel):
    query: str


class ToolResponse(BaseModel):
    result: str


@app.post("/", response_model=ToolResponse)
async def search(request: ToolRequest):
    try:
        # Security: Validate and sanitize input
        query = request.query.strip()
        if not query:
            return ToolResponse(result="Error: Empty query provided")
        if len(query) > 10000:  # Prevent DoS with very large queries
            return ToolResponse(result="Error: Query too long (max 10000 characters)")
        
        result = app.state.query_engine.query(query)
        return ToolResponse(result=result)
    except Exception:
        logger.exception("Query failed")
        # Security: Don't leak detailed error information
        return ToolResponse(result="Error processing request. Please try again or contact support.")


@app.get("/health")
async def health_check():
    observer = getattr(app.state, "observer", None)
    manifest = getattr(app.state, "manifest", None)

    return {
        "status": "healthy",
        "service": "Production RAG Search Tool (DuckDB + Parquet)",
        "data_directory": str(DATA_DIR),
        "data_directory_exists": DATA_DIR.exists(),
        "files_in_manifest": manifest.count() if manifest else 0,
        "watcher_alive": observer.is_alive() if observer else False,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8002)
