"""Ingestion pipeline: .dat.gz → Parquet (partitioned by device/date)."""
import gzip
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

# Fixed schema for all CMEP CSV files
SCHEMA = pa.schema([
    pa.field("device",      pa.string()),
    pa.field("meter_id",    pa.string()),
    pa.field("status",      pa.string()),
    pa.field("qualifier",   pa.string()),
    pa.field("channel",     pa.string()),
    pa.field("timestamp",   pa.timestamp("s")),
    pa.field("value",       pa.float64()),
    pa.field("source_file", pa.string()),
])


def _parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse 12-char YYYYMMDDHHMI timestamp."""
    ts_str = ts_str.strip()
    if len(ts_str) >= 12:
        try:
            return datetime(
                int(ts_str[0:4]), int(ts_str[4:6]), int(ts_str[6:8]),
                int(ts_str[8:10]), int(ts_str[10:12]),
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return None


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except Exception:
                self._data = {}

    def is_ingested(self, filepath: str) -> bool:
        return str(filepath) in self._data

    def mark_ingested(self, filepath: str):
        self._data[str(filepath)] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)

    def count(self) -> int:
        return len(self._data)


class IngestionPipeline:
    def __init__(self, parquet_dir: Path, manifest: Manifest):
        self.parquet_dir = parquet_dir
        self.manifest = manifest

    def ingest(self, filepath: str):
        filepath = str(filepath)
        source_name = Path(filepath).name

        # Parse device/date from filename for partition path
        device, date = self._parse_filename_meta(source_name)

        # Parse CSV records from .dat.gz
        records = self._parse_file(filepath, source_name)
        if not records:
            logger.warning(f"No valid records in {source_name}, skipping.")
            self.manifest.mark_ingested(filepath)
            return

        # Write Parquet (atomic: write .tmp then rename)
        partition_dir = self.parquet_dir / f"device={device}" / f"date={date}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = partition_dir / source_name.replace(".dat.gz", ".parquet")
        tmp_path = parquet_path.with_suffix(".tmp")

        table = pa.table(records, schema=SCHEMA)
        pq.write_table(table, tmp_path, compression="snappy")
        tmp_path.rename(parquet_path)
        logger.info(f"Parquet written: {parquet_path} ({len(records['device'])} rows)")

        self.manifest.mark_ingested(filepath)
        logger.info(f"Ingestion complete: {source_name}")

    def _parse_filename_meta(self, filename: str):
        """Extract device and date from CMEP filename."""
        try:
            base = filename.replace(".dat.gz", "")
            parts = base.split("_")
            if len(parts) >= 3:
                return parts[1], parts[2]
        except Exception:
            pass
        return "UNKNOWN", "00000000"

    def _extract_value(self, fields: list) -> float | None:
        """Extract and parse value field from CSV fields."""
        try:
            return float(fields[16].strip())
        except (ValueError, IndexError):
            return None
    
    def _extract_field(self, fields: list, index: int, default: str = "") -> str:
        """Safely extract field from CSV fields list."""
        return fields[index].strip() if len(fields) > index else default
    
    def _append_record(self, cols: dict, fields: list, source_name: str) -> None:
        """Append a parsed record to columns dict."""
        value = self._extract_value(fields)
        ts = _parse_timestamp(fields[14]) if len(fields) > 14 else None
        
        cols["device"].append(fields[0].strip() or "UNKNOWN")
        cols["meter_id"].append(fields[7].strip())
        cols["status"].append(fields[8].strip())
        cols["qualifier"].append(self._extract_field(fields, 9))
        cols["channel"].append(self._extract_field(fields, 10))
        cols["timestamp"].append(ts)
        cols["value"].append(value)
        cols["source_file"].append(source_name)

    def _parse_file(self, filepath: str, source_name: str) -> dict:
        """Parse .dat.gz CSV into column-oriented dict matching SCHEMA."""
        cols = {f.name: [] for f in SCHEMA}

        try:
            with gzip.open(filepath, "rt", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("<") or not line:
                        continue
                    fields = line.split(",")
                    if len(fields) < 17:
                        continue
                    self._append_record(cols, fields, source_name)

        except gzip.BadGzipFile:
            logger.error(f"Corrupt gzip file: {filepath}")
            return {}
        except EOFError:
            logger.error(f"Truncated gzip file: {filepath}")
            return {}

        return cols
