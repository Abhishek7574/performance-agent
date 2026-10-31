from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
import gzip
import glob
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

app = FastAPI(title="File Search Tool")

# Directory to store consolidated files
CONSOLIDATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consolidated")
os.makedirs(CONSOLIDATED_DIR, exist_ok=True)


class ToolRequest(BaseModel):
    query: str


class ToolResponse(BaseModel):
    result: str


def parse_query(query: str) -> dict:
    """Parse user query to determine action: consolidate or search."""
    query_lower = query.strip().lower()

    # --- SEARCH patterns (check first since they may reference a path too) ---
    search_patterns = [
        r"(?:search|find|look\s+for|grep|filter|get)\s+(?:for\s+)?['\"]?(.+?)['\"]?\s+(?:in|from|inside)\s+(.+)",
        r"(?:search|find|look\s+for|grep|filter|get)\s+(?:record|records|lines?|entries?|data)?\s*['\"]?(.+?)['\"]?\s+(?:in|from)\s+(.+)",
    ]
    for pattern in search_patterns:
        match = re.match(pattern, query_lower)
        if match:
            keyword = match.group(1).strip().strip("'\"")
            folder = match.group(2).strip().strip("'\"")
            return {"action": "search", "keyword": keyword, "folder_path": expand_path(folder)}

    # Search with keyword after path
    search_after_patterns = [
        r"(?:in|from)\s+(.+?)\s+(?:search|find|look\s+for|grep|filter)\s+(?:for\s+)?['\"]?(.+?)['\"]?$",
    ]
    for pattern in search_after_patterns:
        match = re.match(pattern, query_lower)
        if match:
            folder = match.group(1).strip().strip("'\"")
            keyword = match.group(2).strip().strip("'\"")
            return {"action": "search", "keyword": keyword, "folder_path": expand_path(folder)}

    # --- CONSOLIDATE patterns ---
    consolidate_patterns = [
        r"(?:consolidate|merge|combine|concat|join)\s+(?:all\s+)?(?:files?\s+)?(?:in|from|under)?\s*(.+)",
        r"(?:create|make|build|generate)\s+(?:a\s+)?(?:consolidated|merged|combined|single)\s+file\s+(?:from|for|in|of)\s+(.+)",
        r"(?:read|extract|decompress|unzip|gunzip)\s+(?:all\s+)?(?:\.?dat\.?gz|gzip|gz)\s+(?:files?\s+)?(?:in|from)\s+(.+)",
        r"(?:process|load)\s+(?:all\s+)?(?:files?\s+)?(?:in|from)\s+(.+)",
    ]
    for pattern in consolidate_patterns:
        match = re.match(pattern, query_lower)
        if match:
            folder = match.group(1).strip().strip("'\"")
            return {"action": "consolidate", "folder_path": expand_path(folder)}

    # --- If it looks like just a path, default to consolidate ---
    potential_path = query.strip().strip("'\"")
    expanded = expand_path(potential_path)
    if os.path.isdir(expanded):
        return {"action": "consolidate", "folder_path": expanded}

    # --- Fallback: treat whole query as a search keyword on last consolidated file ---
    return {"action": "search_last", "keyword": query.strip()}


def expand_path(p: str) -> str:
    """Expand ~ and environment vars in path."""
    return os.path.expanduser(os.path.expandvars(p.strip()))


def consolidate_dat_gz_files(folder_path: str) -> dict:
    """Read all .dat.gz files in folder, decompress and consolidate into one file."""
    if not os.path.isdir(folder_path):
        return {"error": f"Directory not found: {folder_path}"}

    # Find all .dat.gz files
    gz_files = sorted(glob.glob(os.path.join(folder_path, "*.dat.gz")))

    if not gz_files:
        # Also check for .gz files in case pattern is slightly different
        gz_files = sorted(glob.glob(os.path.join(folder_path, "*.gz")))

    if not gz_files:
        return {"error": f"No .dat.gz or .gz files found in {folder_path}"}

    # Create consolidated output file
    folder_name = os.path.basename(folder_path.rstrip("/"))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"consolidated_{folder_name}_{timestamp}.dat"
    output_path = os.path.join(CONSOLIDATED_DIR, output_filename)

    total_lines = 0
    files_processed = 0
    files_failed = []
    file_details = []

    with open(output_path, "w", encoding="utf-8", errors="replace") as outfile:
        for gz_file in gz_files:
            file_basename = os.path.basename(gz_file)
            try:
                line_count = 0
                with gzip.open(gz_file, "rt", encoding="utf-8", errors="replace") as infile:
                    for line in infile:
                        outfile.write(line)
                        line_count += 1
                        if not line.endswith("\n"):
                            outfile.write("\n")

                total_lines += line_count
                files_processed += 1
                file_size = os.path.getsize(gz_file)
                file_details.append(f"  - {file_basename} ({file_size:,} bytes, {line_count:,} lines)")
            except Exception as e:
                files_failed.append(f"  - {file_basename}: {str(e)}")

    output_size = os.path.getsize(output_path)

    result_lines = [
        f"### Consolidation Complete",
        f"",
        f"**Source:** {folder_path}",
        f"**Output:** {output_path}",
        f"**Output Size:** {output_size:,} bytes ({output_size / (1024*1024):.2f} MB)",
        f"",
        f"**Files Processed:** {files_processed}/{len(gz_files)}",
        f"**Total Lines:** {total_lines:,}",
        f"",
        f"### Files Included:",
    ]
    result_lines.extend(file_details)

    if files_failed:
        result_lines.append(f"\n### Failed Files:")
        result_lines.extend(files_failed)

    result_lines.append(f"\nConsolidated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    result_lines.append(f"\n💡 Tip: You can now search records using: 'search <keyword> in {folder_path}'")

    return {"result": "\n".join(result_lines), "output_path": output_path}


def _find_consolidated_file(folder_path: Optional[str]) -> Optional[str]:
    """Find or create a consolidated file for searching."""
    if folder_path:
        folder_name = os.path.basename(folder_path.rstrip("/"))
        pattern = os.path.join(CONSOLIDATED_DIR, f"consolidated_{folder_name}_*.dat")
        existing = sorted(glob.glob(pattern))
        if existing:
            return existing[-1]  # Most recent
        
        # Need to consolidate first
        result = consolidate_dat_gz_files(folder_path)
        if "error" in result:
            return None
        return result["output_path"]
    else:
        # Search the most recent consolidated file
        all_consolidated = sorted(glob.glob(os.path.join(CONSOLIDATED_DIR, "consolidated_*.dat")))
        if all_consolidated:
            return all_consolidated[-1]
    
    return None


def _search_file_for_keyword(search_file: str, keyword: str, max_results: int) -> tuple:
    """Search a file for keyword matches. Returns (matches, total_lines, error)."""
    matches = []
    total_lines = 0
    try:
        with open(search_file, "r", encoding="utf-8", errors="replace") as f:
            for line_num, line in enumerate(f, 1):
                total_lines += 1
                if keyword.lower() in line.lower():
                    matches.append((line_num, line.rstrip()))
                    if len(matches) >= max_results:
                        break
        return matches, total_lines, None
    except Exception as e:
        return [], 0, f"Error reading file: {str(e)}"


def _format_search_results(keyword: str, search_file: str, matches: list, total_lines: int, max_results: int) -> str:
    """Format search results as text."""
    result_lines = [
        f"### Search Results",
        f"",
        f"**Keyword:** `{keyword}`",
        f"**File:** {search_file}",
        f"**Total Lines Scanned:** {total_lines:,}",
        f"**Matches Found:** {len(matches)}" + (f" (showing first {max_results})" if len(matches) >= max_results else ""),
        f"",
    ]

    if matches:
        result_lines.append("### Matching Records:")
        for line_num, line_text in matches:
            display_text = line_text[:500] + "..." if len(line_text) > 500 else line_text
            result_lines.append(f"  **Line {line_num}:** {display_text}")
    else:
        result_lines.append(f"No records matching '{keyword}' found in the consolidated file.")

    result_lines.append(f"\nSearched on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    return "\n".join(result_lines)


def search_consolidated_file(keyword: str, folder_path: Optional[str] = None, max_results: int = 100) -> dict:
    """Search for records in consolidated file(s)."""
    search_file = _find_consolidated_file(folder_path)

    if not search_file or not os.path.isfile(search_file):
        return {"error": "No consolidated file found. Please consolidate files first by providing a folder path."}

    matches, total_lines, error = _search_file_for_keyword(search_file, keyword, max_results)
    
    if error:
        return {"error": error}

    result_text = _format_search_results(keyword, search_file, matches, total_lines, max_results)
    return {"result": result_text}


@app.post("/", response_model=ToolResponse)
async def handle_query(request: ToolRequest):
    """Handle file consolidation and search queries."""
    try:
        parsed = parse_query(request.query)
        action = parsed.get("action")

        if action == "consolidate":
            result = consolidate_dat_gz_files(parsed["folder_path"])
        elif action == "search":
            result = search_consolidated_file(parsed["keyword"], parsed.get("folder_path"))
        elif action == "search_last":
            result = search_consolidated_file(parsed["keyword"])
        else:
            result = {"error": f"Unknown action: {action}"}

        if "error" in result:
            return ToolResponse(result=f"Error: {result['error']}")

        return ToolResponse(result=result["result"])

    except Exception as e:
        return ToolResponse(result=f"Error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6111)
