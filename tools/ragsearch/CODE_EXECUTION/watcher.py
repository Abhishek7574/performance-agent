"""File system watcher for automatic .dat.gz ingestion."""
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)


class CMEPFileHandler(FileSystemEventHandler):
    def __init__(self, ingestion_pipeline, manifest):
        super().__init__()
        self.pipeline = ingestion_pipeline
        self.manifest = manifest
        self.executor = ThreadPoolExecutor(max_workers=2)

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.dat.gz'):
            self.executor.submit(self._handle_new_file, event.src_path)

    def on_moved(self, event):
        if not event.is_directory and event.dest_path.endswith('.dat.gz'):
            self.executor.submit(self._handle_new_file, event.dest_path)

    def _handle_new_file(self, filepath: str):
        # Wait for file to finish writing (size stable for 2 consecutive polls)
        prev_size = -1
        for _attempt in range(10):
            try:
                curr_size = os.stat(filepath).st_size
            except FileNotFoundError:
                logger.warning(f"File disappeared: {filepath}")
                return
            if curr_size == prev_size and curr_size > 0:
                break
            prev_size = curr_size
            time.sleep(1)
        else:
            logger.warning(f"File stability check failed after 10s: {filepath}")
            return

        if self.manifest.is_ingested(filepath):
            logger.debug(f"Already ingested, skipping: {filepath}")
            return

        logger.info(f"New file detected, ingesting: {filepath}")
        try:
            self.pipeline.ingest(filepath)
        except Exception:
            logger.exception(f"Ingestion failed for {filepath}")


def start_watcher(data_dir, ingestion_pipeline, manifest):
    handler = CMEPFileHandler(ingestion_pipeline, manifest)
    observer = Observer()
    observer.schedule(handler, str(data_dir), recursive=False)
    observer.daemon = True
    observer.start()
    logger.info(f"Watchdog started on {data_dir}")
    return observer
