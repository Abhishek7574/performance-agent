"""Query engine: pure DuckDB — structured + statistical queries."""
import logging
import re
from pathlib import Path
from typing import List

import duckdb

logger = logging.getLogger(__name__)

# --- Constants for repeated patterns and SQL fragments ---
DEVICE_PATTERN = r'\b(MEPMD\d+|MLA\d+|SAAS\d+)\b'
DATE_8DIGIT_PATTERN = r'\b(\d{8})\b'
DEVICE_CONDITION = "device = ?"
TIMESTAMP_LIKE_CONDITION = "CAST(timestamp AS VARCHAR) LIKE ?"
AND_SEPARATOR = " AND "
ERROR_MESSAGE_TEMPLATE = "❌ Query error: {0}"

# --- Structured query signals (exact/range lookups) ---
STRUCTURED_PATTERNS = [
    r'\b\d{7,}\b',                           # meter_id (7+ digits)
    r'\b\d{4}\.\d{2,}\b',                    # value like 1002.0133
    DEVICE_PATTERN,                          # device name
    r'\b\d{8}\b',                             # date like 20260317
    r'\b(from|between|after|before)\s+\d',   # date range
    r'\bvalue\s*(above|below|>|<|=|greater|less)',
    r'\b(meter|serial)\s*(no|id|number)?\s*\d',
]

# --- Statistical query signals (anomaly/trend/pattern detection) ---
STATISTICAL_PATTERNS = [
    (r'\b(anomal|unusual|spike|outlier)\b',                          "anomaly"),
    (r'\b(high|excessive|above.{0,10}(normal|average|baseline))\b',  "high"),
    (r'\b(low|below.{0,10}(normal|average|baseline))\b',             "low"),
    (r'\b(trend|over time|hourly|daily|weekly)\b',                    "trend"),
    (r'\b(peak|maximum|highest|top)\b',                               "peak"),
    (r'\b(fault|failure|error|alert|issue|problem|bad status)\b',     "fault"),
    (r'\b(compare|rank|which channel|channel breakdown)\b',           "compare"),
]


def _parse_multi_pairs(query: str):
    """Parse multi-value pair queries like '1235000161 with 1002.0133 / ...'"""
    pairs = []
    segments = re.split(r'\s*/\s*', query)
    for seg in segments:
        seg = seg.strip()
        with_match = re.search(r'(\d[\d.]+)\s+with\s+(\d[\d.]+)', seg)
        if with_match:
            pairs.append([with_match.group(1), with_match.group(2)])
            continue
        all_nums = re.findall(r'\d+\.\d+|\d{7,}', seg)
        seen, unique_nums = set(), []
        for n in all_nums:
            if n not in seen:
                seen.add(n)
                unique_nums.append(n)
        if len(unique_nums) == 2:
            pairs.append(unique_nums)
    return pairs


class QueryEngine:
    def __init__(self, parquet_dir: Path):
        self.parquet_dir = parquet_dir
        self.parquet_glob = str(parquet_dir / "**" / "*.parquet")

    def _has_parquet(self) -> bool:
        return any(self.parquet_dir.rglob("*.parquet"))

    def query(self, query_text: str) -> str:
        q = query_text.strip()
        ql = q.lower()

        if re.search(r'\b(stats|status|overview|summary)\b', ql):
            return self._stats_query()

        if re.search(r'\b(scan|index|reindex)\b', ql):
            return "ℹ️ Indexing is automatic — files are ingested when detected by the file watcher."

        if not self._has_parquet():
            return "⏳ No data indexed yet. Ingestion begins automatically when .dat.gz files are present."

        # Count/aggregate queries — "how many", "count", "total records"
        if re.search(r'\b(how many|count|total)\b', ql):
            return self._count_query(q)

        # Multi-pair search
        pairs = _parse_multi_pairs(q)
        if pairs:
            return self._multi_pair_query(pairs)

        # Check statistical patterns first (more specific)
        for pattern, mode in STATISTICAL_PATTERNS:
            if re.search(pattern, ql):
                return self._statistical_query(q, mode)

        # Then structured patterns
        structural_score = sum(1 for p in STRUCTURED_PATTERNS if re.search(p, q, re.IGNORECASE))
        if structural_score > 0:
            return self._structured_query(q)

        # Fallback: treat as structured search
        return self._structured_query(q)

    # ------------------------------------------------------------------ #
    #  STRUCTURED: exact / range lookups                                   #
    # ------------------------------------------------------------------ #

    def _structured_query(self, query: str) -> str:
        conditions, params = ["1=1"], []

        dev = re.search(DEVICE_PATTERN, query, re.IGNORECASE)
        if dev:
            conditions.append(DEVICE_CONDITION)
            params.append(dev.group(1).upper())

        meter = re.search(r'\b(\d{7,})\b', query)
        if meter:
            conditions.append("meter_id = ?")
            params.append(meter.group(1))

        dates = re.findall(DATE_8DIGIT_PATTERN, query)
        if len(dates) == 1:
            conditions.append(TIMESTAMP_LIKE_CONDITION)
            params.append("{0}%".format(dates[0]))
        elif len(dates) >= 2:
            conditions.append("CAST(timestamp AS VARCHAR) >= ?")
            params.append(dates[0])
            conditions.append("CAST(timestamp AS VARCHAR) <= ?")
            params.append(dates[1] + "235959")

        val = re.search(r'\b(\d+\.\d{2,})\b', query)
        if val:
            conditions.append("CAST(value AS VARCHAR) LIKE ?")
            params.append("%{0}%".format(val.group(1)))

        sql = """
            SELECT device, meter_id, status, channel,
                   CAST(timestamp AS VARCHAR) AS timestamp, value, source_file
            FROM read_parquet('{0}', hive_partitioning=true)
            WHERE {1}
            ORDER BY timestamp DESC
            LIMIT 1000
        """.format(self.parquet_glob, AND_SEPARATOR.join(conditions))
        try:
            con = duckdb.connect()
            df = con.execute(sql, params).df()
            con.close()
        except Exception as e:
            logger.exception("Structured query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

        if df.empty:
            return "\ud83d\udd0d No results found for: '{0}'\nTry adjusting the meter ID, date, or device name.".format(query)
        return self._format_structured(df, query)

    # ------------------------------------------------------------------ #
    #  COUNT: aggregated summary queries                                   #
    # ------------------------------------------------------------------ #

    def _count_query(self, query: str) -> str:
        conditions, params = ["1=1"], []

        dev = re.search(DEVICE_PATTERN, query, re.IGNORECASE)
        if dev:
            conditions.append(DEVICE_CONDITION)
            params.append(dev.group(1).upper())

        meter = re.search(r'\b(\d{7,})\b', query)
        if meter:
            conditions.append("meter_id = ?")
            params.append(meter.group(1))

        dates = re.findall(DATE_8DIGIT_PATTERN, query)
        if len(dates) == 1:
            conditions.append(TIMESTAMP_LIKE_CONDITION)
            params.append("{0}%".format(dates[0]))
        elif len(dates) >= 2:
            conditions.append("CAST(timestamp AS VARCHAR) >= ?")
            params.append(dates[0])
            conditions.append("CAST(timestamp AS VARCHAR) <= ?")
            params.append(dates[1] + "235959")

        val = re.search(r'\b(\d+\.\d{2,})\b', query)
        if val:
            conditions.append("CAST(value AS VARCHAR) LIKE ?")
            params.append("%{0}%".format(val.group(1)))

        where = AND_SEPARATOR.join(conditions)

        sql = """
            SELECT
                COUNT(*)                          AS total_records,
                COUNT(DISTINCT device)            AS devices,
                COUNT(DISTINCT meter_id)          AS unique_meters,
                COUNT(DISTINCT channel)           AS channels,
                COUNT(DISTINCT source_file)       AS source_files,
                COUNT(DISTINCT status)            AS status_types,
                ROUND(MIN(value), 4)              AS min_value,
                ROUND(MAX(value), 4)              AS max_value,
                ROUND(AVG(value), 4)              AS avg_value,
                CAST(MIN(timestamp) AS VARCHAR)   AS earliest,
                CAST(MAX(timestamp) AS VARCHAR)   AS latest
            FROM read_parquet('{0}', hive_partitioning=true)
            WHERE {1}
        """.format(self.parquet_glob, where)
        try:
            con = duckdb.connect()
            row = con.execute(sql, params).df().iloc[0]

            # Get breakdown by device and source file
            breakdown_sql = """
                SELECT device, source_file,
                       COUNT(*) AS records,
                       COUNT(DISTINCT meter_id) AS meters,
                       CAST(MIN(timestamp) AS VARCHAR) AS earliest,
                       CAST(MAX(timestamp) AS VARCHAR) AS latest
                FROM read_parquet('{0}', hive_partitioning=true)
                WHERE {1}
                GROUP BY device, source_file
                ORDER BY records DESC
            """.format(self.parquet_glob, where)
            breakdown = con.execute(breakdown_sql, params).df()
            con.close()
        except Exception as e:
            logger.exception("Count query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

        total = int(row["total_records"])
        if total == 0:
            return "🔍 No records found for: '{0}'".format(query)

        lines = [
            "📊 Aggregated Results for: '{0}'".format(query),
            "",
            "  Total Records  : {0:,}".format(total),
            "  Devices        : {0}".format(int(row['devices'])),
            "  Unique Meters  : {0:,}".format(int(row['unique_meters'])),
            "  Channels       : {0}".format(int(row['channels'])),
            "  Source Files   : {0}".format(int(row['source_files'])),
            "  Value Range    : {0} — {1}".format(row['min_value'], row['max_value']),
            "  Avg Value      : {0}".format(row['avg_value']),
            "  Date Range     : {0} → {1}".format(row['earliest'], row['latest']),
        ]

        if not breakdown.empty:
            lines.append("\n  Breakdown by source file:")
            lines.append("  {0:<55} {1:>8} {2:>8}  {3}".format('File', 'Records', 'Meters', 'Date Range'))
            lines.append("  {0}".format('-'*105))
            for _, r in breakdown.iterrows():
                date_range = "{0} → {1}".format(r['earliest'], r['latest'])
                lines.append("  {0:<55} {1:>8,} {2:>8,}  {3}".format(
                    str(r['source_file']), int(r['records']), int(r['meters']), date_range))

        return "\n".join(lines)

    def _multi_pair_query(self, pairs: List[List[str]]) -> str:
        all_results = []
        con = duckdb.connect()
        for pair in pairs:
            sql = """
                SELECT device, meter_id, status, channel,
                       CAST(timestamp AS VARCHAR) AS timestamp, value, source_file
                FROM read_parquet('{0}', hive_partitioning=true)
                WHERE meter_id = ? AND CAST(value AS VARCHAR) LIKE ?
                ORDER BY timestamp DESC
                LIMIT 500
            """.format(self.parquet_glob)
            try:
                df = con.execute(sql, [pair[0], "%{0}%".format(pair[1])]).df()
                all_results.append((pair, df))
            except Exception:
                logger.exception("Multi-pair query failed for pair: %s", pair)
                all_results.append((pair, None))
        con.close()
        return self._format_multi_pair(all_results)

    # ------------------------------------------------------------------ #
    #  STATISTICAL: anomaly / trend / fault detection via DuckDB          #
    # ------------------------------------------------------------------ #

    def _build_filters(self, query: str):
        """Build parameterized WHERE conditions from user query."""
        conditions, params = [], []

        dev = re.search(DEVICE_PATTERN, query, re.IGNORECASE)
        if dev:
            conditions.append(DEVICE_CONDITION)
            params.append(dev.group(1).upper())

        dates = re.findall(DATE_8DIGIT_PATTERN, query)
        if dates:
            conditions.append(TIMESTAMP_LIKE_CONDITION)
            params.append("{0}%".format(dates[0]))

        return conditions, params

    def _statistical_query(self, query: str, mode: str) -> str:
        """Dispatch to specific statistical query handler based on mode."""
        handlers = {
            "anomaly": self._query_anomaly,
            "high": self._query_high,
            "low": self._query_low,
            "trend": self._query_trend,
            "peak": self._query_peak,
            "fault": self._query_fault,
            "compare": self._query_compare,
        }
        handler = handlers.get(mode)
        if handler:
            return handler(query)
        return "🔍 No results found."

    def _query_anomaly(self, query: str) -> str:
        """Find anomalies using 3-sigma detection."""
        conditions, params = self._build_filters(query)
        base_filter = (AND_SEPARATOR + AND_SEPARATOR.join(conditions)) if conditions else ""
        try:
            con = duckdb.connect()
            sql = """
                WITH stats AS (
                    SELECT channel,
                           AVG(value)    AS avg_val,
                           STDDEV(value) AS std_val
                    FROM read_parquet('{0}', hive_partitioning=true)
                    WHERE value IS NOT NULL {1}
                    GROUP BY channel
                )
                SELECT r.device, r.meter_id, r.status, r.channel,
                       CAST(r.timestamp AS VARCHAR) AS timestamp,
                       r.value,
                       ROUND(s.avg_val, 4) AS channel_avg,
                       ROUND(s.std_val, 4) AS channel_stddev,
                       ROUND(ABS(r.value - s.avg_val) / NULLIF(s.std_val, 0), 2) AS sigma,
                       r.source_file
                FROM read_parquet('{0}', hive_partitioning=true) r
                JOIN stats s ON r.channel = s.channel
                WHERE r.value IS NOT NULL
                  AND s.std_val > 0
                  AND ABS(r.value - s.avg_val) > 3 * s.std_val
                  {1}
                ORDER BY sigma DESC
                LIMIT 200
            """.format(self.parquet_glob, base_filter)
            df = con.execute(sql, params + params).df()
            con.close()
            if df.empty:
                return "✅ No anomalies detected — all values are within 3 standard deviations of their channel average."
            return self._format_anomaly(df, query)
        except Exception as e:
            logger.exception("Anomaly query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

    def _query_high(self, query: str) -> str:
        """Find high consumption (top 10%)."""
        conditions, params = self._build_filters(query)
        base_filter = (AND_SEPARATOR + AND_SEPARATOR.join(conditions)) if conditions else ""
        try:
            con = duckdb.connect()
            sql = """
                WITH thresholds AS (
                    SELECT channel,
                           PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY value) AS p90
                    FROM read_parquet('{0}', hive_partitioning=true)
                    WHERE value IS NOT NULL {1}
                    GROUP BY channel
                )
                SELECT r.device, r.meter_id, r.status, r.channel,
                       CAST(r.timestamp AS VARCHAR) AS timestamp,
                       r.value,
                       ROUND(t.p90, 4) AS p90_threshold,
                       r.source_file
                FROM read_parquet('{0}', hive_partitioning=true) r
                JOIN thresholds t ON r.channel = t.channel
                WHERE r.value > t.p90
                  {1}
                ORDER BY r.value DESC
                LIMIT 200
            """.format(self.parquet_glob, base_filter)
            df = con.execute(sql, params + params).df()
            con.close()
            if df.empty:
                return "🔍 No high consumption records found."
            return self._format_statistical(df, query, "📈 High Consumption (top 10%)", "p90_threshold", "P90 threshold")
        except Exception as e:
            logger.exception("High consumption query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

    def _query_low(self, query: str) -> str:
        """Find low consumption (bottom 10%)."""
        conditions, params = self._build_filters(query)
        base_filter = (AND_SEPARATOR + AND_SEPARATOR.join(conditions)) if conditions else ""
        try:
            con = duckdb.connect()
            sql = """
                WITH thresholds AS (
                    SELECT channel,
                           PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY value) AS p10
                    FROM read_parquet('{0}', hive_partitioning=true)
                    WHERE value IS NOT NULL {1}
                    GROUP BY channel
                )
                SELECT r.device, r.meter_id, r.status, r.channel,
                       CAST(r.timestamp AS VARCHAR) AS timestamp,
                       r.value,
                       ROUND(t.p10, 4) AS p10_threshold,
                       r.source_file
                FROM read_parquet('{0}', hive_partitioning=true) r
                JOIN thresholds t ON r.channel = t.channel
                WHERE r.value < t.p10
                  {1}
                ORDER BY r.value ASC
                LIMIT 200
            """.format(self.parquet_glob, base_filter)
            df = con.execute(sql, params + params).df()
            con.close()
            if df.empty:
                return "🔍 No low consumption records found."
            return self._format_statistical(df, query, "📉 Low Consumption (bottom 10%)", "p10_threshold", "P10 threshold")
        except Exception as e:
            logger.exception("Low consumption query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

    def _query_trend(self, query: str) -> str:
        """Find hourly trends."""
        conditions, params = self._build_filters(query)
        base_filter = (AND_SEPARATOR + AND_SEPARATOR.join(conditions)) if conditions else ""
        try:
            con = duckdb.connect()
            sql = """
                SELECT device, channel,
                       DATE_TRUNC('hour', timestamp) AS hour,
                       COUNT(*)        AS readings,
                       ROUND(AVG(value), 4)  AS avg_value,
                       ROUND(MIN(value), 4)  AS min_value,
                       ROUND(MAX(value), 4)  AS max_value
                FROM read_parquet('{0}', hive_partitioning=true)
                WHERE value IS NOT NULL {1}
                GROUP BY device, channel, DATE_TRUNC('hour', timestamp)
                ORDER BY hour DESC, channel
                LIMIT 200
            """.format(self.parquet_glob, base_filter)
            df = con.execute(sql, params).df()
            con.close()
            if df.empty:
                return "🔍 No trend data found."
            return self._format_trend(df, query)
        except Exception as e:
            logger.exception("Trend query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

    def _query_peak(self, query: str) -> str:
        """Find peak values."""
        conditions, params = self._build_filters(query)
        base_filter = (AND_SEPARATOR + AND_SEPARATOR.join(conditions)) if conditions else ""
        try:
            con = duckdb.connect()
            sql = """
                SELECT device, meter_id, status, channel,
                       CAST(timestamp AS VARCHAR) AS timestamp,
                       value, source_file
                FROM read_parquet('{0}', hive_partitioning=true)
                WHERE value IS NOT NULL {1}
                ORDER BY value DESC
                LIMIT 50
            """.format(self.parquet_glob, base_filter)
            df = con.execute(sql, params).df()
            con.close()
            if df.empty:
                return "🔍 No records found."
            return self._format_structured(df, "Peak values — {0}".format(query))
        except Exception as e:
            logger.exception("Peak query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

    def _query_fault(self, query: str) -> str:
        """Find fault/error status records."""
        conditions, params = self._build_filters(query)
        base_filter = (AND_SEPARATOR + AND_SEPARATOR.join(conditions)) if conditions else ""
        try:
            con = duckdb.connect()
            sql = """
                SELECT device, meter_id, status, channel,
                       CAST(timestamp AS VARCHAR) AS timestamp,
                       value, source_file,
                       COUNT(*) OVER (PARTITION BY status) AS status_count
                FROM read_parquet('{0}', hive_partitioning=true)
                WHERE status NOT IN ('OK', 'ok', '')
                  {1}
                ORDER BY timestamp DESC
                LIMIT 500
            """.format(self.parquet_glob, base_filter)
            df = con.execute(sql, params).df()
            con.close()
            if df.empty:
                return "✅ No fault or error status records found — all records have OK status."
            return self._format_fault(df, query)
        except Exception as e:
            logger.exception("Fault query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

    def _query_compare(self, query: str) -> str:
        """Compare channel statistics."""
        conditions, params = self._build_filters(query)
        base_filter = (AND_SEPARATOR + AND_SEPARATOR.join(conditions)) if conditions else ""
        try:
            con = duckdb.connect()
            sql = """
                SELECT channel,
                       COUNT(*)               AS readings,
                       ROUND(AVG(value), 4)   AS avg_value,
                       ROUND(MIN(value), 4)   AS min_value,
                       ROUND(MAX(value), 4)   AS max_value,
                       ROUND(STDDEV(value), 4) AS stddev_value
                FROM read_parquet('{0}', hive_partitioning=true)
                WHERE value IS NOT NULL {1}
                GROUP BY channel
                ORDER BY avg_value DESC
                LIMIT 50
            """.format(self.parquet_glob, base_filter)
            df = con.execute(sql, params).df()
            con.close()
            if df.empty:
                return "🔍 No channel data found."
            return self._format_compare(df, query)
        except Exception as e:
            logger.exception("Compare query failed")
            return ERROR_MESSAGE_TEMPLATE.format(e)

    # ------------------------------------------------------------------ #
    #  STATS                                                               #
    # ------------------------------------------------------------------ #

    def _stats_query(self) -> str:
        lines = ["📊 Data Store Stats"]
        if self._has_parquet():
            try:
                con = duckdb.connect()
                df = con.execute("""
                    SELECT device, COUNT(*) AS records,
                           COUNT(DISTINCT channel) AS channels,
                           COUNT(DISTINCT meter_id) AS meters,
                           CAST(MIN(timestamp) AS VARCHAR) AS earliest,
                           CAST(MAX(timestamp) AS VARCHAR) AS latest
                    FROM read_parquet('{0}', hive_partitioning=true)
                    GROUP BY device
                """.format(self.parquet_glob)).df()
                con.close()
                total = df["records"].sum()
                lines.append(f"• Total records: {total:,}")
                for _, row in df.iterrows():
                    lines.append(f"\n  Device: {row['device']}")
                    lines.append(f"    Records : {row['records']:,}")
                    lines.append(f"    Channels: {row['channels']}")
                    lines.append(f"    Meters  : {row['meters']}")
                    lines.append(f"    Range   : {row['earliest']} → {row['latest']}")
            except Exception as e:
                lines.append(f"• Error reading data: {e}")
        else:
            lines.append("• No data indexed yet")
        lines.append(f"\n• Parquet store: {self.parquet_dir}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  FORMATTERS                                                          #
    # ------------------------------------------------------------------ #

    def _format_structured(self, df, query: str) -> str:
        total = len(df)
        file_groups = {}
        for _, row in df.iterrows():
            fname = row["source_file"]
            if fname not in file_groups:
                file_groups[fname] = {"device": row["device"], "records": []}
            file_groups[fname]["records"].append(row)

        lines = [f"🔍 Found {total} matches for: '{query}'",
                 f"📁 Across {len(file_groups)} file(s)", ""]
        for fname, group in file_groups.items():
            lines.append(f"📄 {fname} (Device: {group['device']})")
            lines.append(f"   {'Meter ID':<15} {'Status':<6} {'Timestamp':<20} {'Value':<15}")
            lines.append(f"   {'-'*60}")
            for row in group["records"][:50]:
                lines.append(f"   {str(row['meter_id']):<15} {str(row['status']):<6} {str(row['timestamp']):<20} {str(row['value']):<15}")
            if len(group["records"]) > 50:
                lines.append(f"   ... and {len(group['records']) - 50} more")
            lines.append("")
        return "\n".join(lines)

    def _format_anomaly(self, df, query: str) -> str:
        lines = [f"⚠️  Anomaly Detection Results for: '{query}'",
                 f"Found {len(df)} anomalous records (value > 3σ from channel average)", ""]
        lines.append(f"   {'Channel':<30} {'Value':<12} {'Avg':<12} {'StdDev':<12} {'Sigma':<8} {'Timestamp'}")
        lines.append(f"   {'-'*90}")
        for _, row in df.iterrows():
            lines.append(
                f"   {str(row['channel']):<30} {str(row['value']):<12} "
                f"{str(row['channel_avg']):<12} {str(row['channel_stddev']):<12} "
                f"{str(row['sigma']):<8} {str(row['timestamp'])}"
            )
        lines.append(f"\n📊 Total anomalies: {len(df)}")
        return "\n".join(lines)

    def _format_statistical(self, df, query: str, title: str, threshold_col: str, threshold_label: str) -> str:
        lines = [f"{title} for: '{query}'",
                 f"Found {len(df)} records", ""]
        lines.append(f"   {'Channel':<30} {'Value':<12} {threshold_label:<15} {'Timestamp'}")
        lines.append(f"   {'-'*75}")
        for _, row in df.iterrows():
            lines.append(
                f"   {str(row['channel']):<30} {str(row['value']):<12} "
                f"{str(row[threshold_col]):<15} {str(row['timestamp'])}"
            )
        return "\n".join(lines)

    def _format_trend(self, df, query: str) -> str:
        lines = [f"📈 Hourly Trend for: '{query}'",
                 f"{len(df)} hourly data points", ""]
        lines.append(f"   {'Hour':<22} {'Channel':<30} {'Readings':<10} {'Avg':<12} {'Min':<12} {'Max'}")
        lines.append(f"   {'-'*95}")
        for _, row in df.iterrows():
            lines.append(
                f"   {str(row['hour']):<22} {str(row['channel']):<30} "
                f"{str(row['readings']):<10} {str(row['avg_value']):<12} "
                f"{str(row['min_value']):<12} {str(row['max_value'])}"
            )
        return "\n".join(lines)

    def _format_fault(self, df, query: str) -> str:
        lines = [f"🔴 Fault / Error Records for: '{query}'",
                 f"Found {len(df)} non-OK status records", ""]
        status_counts = df.groupby("status").size().to_dict()
        lines.append("  Status breakdown:")
        for status, count in status_counts.items():
            lines.append(f"    {status}: {count} records")
        lines.append("")
        lines.append(f"   {'Meter ID':<15} {'Status':<12} {'Channel':<30} {'Timestamp':<20} {'Value'}")
        lines.append(f"   {'-'*90}")
        for _, row in df.head(100).iterrows():
            lines.append(
                f"   {str(row['meter_id']):<15} {str(row['status']):<12} "
                f"{str(row['channel']):<30} {str(row['timestamp']):<20} {str(row['value'])}"
            )
        if len(df) > 100:
            lines.append(f"   ... and {len(df) - 100} more")
        return "\n".join(lines)

    def _format_compare(self, df, query: str) -> str:
        lines = [f"📊 Channel Comparison for: '{query}'",
                 f"{len(df)} channels ranked by average value", ""]
        lines.append(f"   {'Channel':<35} {'Readings':<10} {'Avg':<12} {'Min':<12} {'Max':<12} {'StdDev'}")
        lines.append(f"   {'-'*95}")
        for _, row in df.iterrows():
            lines.append(
                f"   {str(row['channel']):<35} {str(row['readings']):<10} "
                f"{str(row['avg_value']):<12} {str(row['min_value']):<12} "
                f"{str(row['max_value']):<12} {str(row['stddev_value'])}"
            )
        return "\n".join(lines)

    def _format_multi_pair(self, all_results) -> str:
        total = sum(len(df) for _, df in all_results if df is not None)
        lines = ["🔍 Multi-Value Pair Search Results",
                 "🎯 Total matches: {0} (across {1} pair(s))".format(total, len(all_results)), ""]
        for pair, df in all_results:
            pair_str = " + ".join(pair)
            lines.append("━━━ Pair: {0} ━━━".format(pair_str))
            if df is None or df.empty:
                lines.append("  No matches found")
            else:
                lines.append("  {0} match(es)".format(len(df)))
                for _, row in df.iterrows():
                    lines.append("  📄 {0}".format(row['source_file']))
                    lines.append("     Meter: {0} | Status: {1} | {2} | Value: {3}".format(
                        row['meter_id'], row['status'], row['timestamp'], row['value']))
            lines.append("")
        lines.append("📊 TOTAL COUNT: {0} matching record(s)".format(total))
        return "\n".join(lines)
