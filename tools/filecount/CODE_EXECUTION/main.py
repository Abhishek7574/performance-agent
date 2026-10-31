from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
import json
from pathlib import Path
from datetime import datetime

app = FastAPI(title="File Count Tool")

class ToolRequest(BaseModel):
    query: str

class ToolResponse(BaseModel):
    result: str

def parse_folder_path(query: str) -> str:
    """Extract folder path from user query using flexible natural language parsing"""
    import re
    
    # Remove common prefixes and clean up the query
    query = query.strip()
    
    # Remove recursive keywords first to extract clean path
    recursive_keywords = ["recursive", "recursively", "all subdirectories", "including subdirectories"]
    cleaned_query = query
    for keyword in recursive_keywords:
        cleaned_query = cleaned_query.replace(keyword, "").strip()
    
    # Convert to lowercase for pattern matching but preserve original for extraction
    query_lower = cleaned_query.lower()
    
    # Define flexible patterns to match various ways users might ask
    patterns = [
        # Direct command patterns
        r"^count\s+files?\s+in\s+(.+)$",
        r"^count\s+files?\s+(.+)$", 
        r"^count\s+the\s+files?\s+in\s+(.+)$",
        r"^count\s+the\s+files?\s+(.+)$",
        
        # Question patterns  
        r"^how\s+many\s+files?\s+(?:are\s+)?(?:there\s+)?in\s+(.+)$",
        r"^how\s+many\s+files?\s+(?:are\s+)?(?:there\s+)?(.+)$",
        r"^how\s+many\s+files?\s+(?:in\s+)?(.+)$",
        
        # File count variants
        r"^file\s+count\s+(?:for\s+)?(?:in\s+)?(.+)$",
        r"^files?\s+count\s+(?:for\s+)?(?:in\s+)?(.+)$",
        
        # List/show variants
        r"^list\s+files?\s+in\s+(.+)$",
        r"^show\s+files?\s+in\s+(.+)$",
        r"^display\s+files?\s+in\s+(.+)$",
        
        # Find variants
        r"^find\s+files?\s+in\s+(.+)$",
        r"^search\s+files?\s+in\s+(.+)$",
        
        # Directory/folder variants
        r"^count\s+(?:items\s+)?(?:in\s+)?(?:directory\s+)?(?:folder\s+)?(.+)$",
        r"^how\s+many\s+(?:items\s+)?(?:are\s+)?(?:in\s+)?(?:directory\s+)?(?:folder\s+)?(.+)$",
        
        # What's in patterns
        r"^what'?s\s+in\s+(.+)$",
        r"^what\s+(?:files?\s+)?(?:are\s+)?in\s+(.+)$",
        
        # Check patterns
        r"^check\s+files?\s+in\s+(.+)$",
        r"^check\s+(.+)\s+(?:files?|directory|folder)$",
        
        # Get patterns
        r"^get\s+files?\s+(?:from\s+)?(?:in\s+)?(.+)$",
        r"^get\s+file\s+count\s+(?:for\s+)?(.+)$",
    ]
    
    # Try each pattern to extract the path
    for pattern in patterns:
        match = re.match(pattern, query_lower)
        if match:
            # Extract the path from the original query (preserving case)
            # Find the position in the original string
            start_pos = cleaned_query.lower().find(match.group(1).strip())
            if start_pos != -1:
                end_pos = start_pos + len(match.group(1).strip())
                path = cleaned_query[start_pos:end_pos].strip()
            else:
                path = match.group(1).strip()
            
            # Clean up the path
            path = path.strip('"\'`')  # Remove quotes
            path = path.strip()
            return path
    
    # If no patterns match, try some simple cleanup
    # Remove common command words from the beginning
    command_words = [
        "count", "files", "file", "how", "many", "are", "there", "in", "the",
        "list", "show", "display", "find", "search", "what", "whats", "check",
        "get", "items", "directory", "folder", "for", "from", "of"
    ]
    
    words = cleaned_query.split()
    # Remove command words from the beginning
    while words and words[0].lower().rstrip('?!.,') in command_words:
        words.pop(0)
    
    # Join remaining words as the path
    path = " ".join(words).strip('"\'`')
    
    # If still no path found, return the cleaned query as-is
    return path if path else cleaned_query.strip('"\'`')

def format_size(size_bytes: int) -> str:
    """Format size in bytes to human-readable format."""
    if size_bytes == 0:
        return "0 B"
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.1f} {size_names[i]}"

def _process_file_item(item: Path, items: list, total_size: int) -> int:
    """Process a file item and add to items list. Returns updated total_size."""
    try:
        size = item.stat().st_size
        total_size += size
        items.append({
            "name": item.name,
            "type": "file",
            "size": size
        })
    except OSError:
        items.append({
            "name": item.name,
            "type": "file",
            "size": "unknown"
        })
    return total_size

def _process_dir_item(item: Path, items: list) -> None:
    """Process a directory item and add to items list."""
    items.append({
        "name": item.name,
        "type": "directory",
        "size": None
    })

def count_files_in_directory(directory_path: str) -> dict:
    """Count files and directories in the given path"""
    try:
        # Convert to Path object for better handling
        path = Path(directory_path).expanduser().resolve()
        
        if not path.exists():
            raise FileNotFoundError(f"Path '{directory_path}' does not exist")
        
        if not path.is_dir():
            raise NotADirectoryError(f"Path '{directory_path}' is not a directory")
        
        file_count = 0
        dir_count = 0
        total_size = 0
        items = []
        
        # Count items in the directory
        for item in path.iterdir():
            if item.is_file():
                file_count += 1
                total_size = _process_file_item(item, items, total_size)
            elif item.is_dir():
                dir_count += 1
                _process_dir_item(item, items)
        
        result = {
            "directory_path": str(path),
            "file_count": file_count,
            "directory_count": dir_count,
            "total_items": file_count + dir_count,
            "total_size": format_size(total_size),
            "total_size_bytes": total_size,
            "timestamp": datetime.now().isoformat(),
            "items": sorted(items, key=lambda x: (x["type"], x["name"].lower()))
        }
        
        return result
        
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"Permission denied: {str(e)}")
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error counting files: {str(e)}")

def count_files_recursively(directory_path: str) -> dict:
    """Count files and directories recursively"""
    try:
        path = Path(directory_path).expanduser().resolve()
        
        if not path.exists():
            raise FileNotFoundError(f"Path '{directory_path}' does not exist")
        
        if not path.is_dir():
            raise NotADirectoryError(f"Path '{directory_path}' is not a directory")
        
        file_count = 0
        dir_count = 0
        total_size = 0
        
        # Walk through all directories recursively
        for item in path.rglob("*"):
            if item.is_file():
                file_count += 1
                try:
                    total_size += item.stat().st_size
                except OSError:
                    pass  # Skip files we can't access
            elif item.is_dir():
                dir_count += 1
        
        result = {
            "directory_path": str(path),
            "file_count": file_count,
            "directory_count": dir_count,
            "total_items": file_count + dir_count,
            "total_size": format_size(total_size),
            "total_size_bytes": total_size,
            "recursive": True,
            "timestamp": datetime.now().isoformat()
        }
        
        return result
        
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"Permission denied: {str(e)}")
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error counting files: {str(e)}")

def _format_recursive_result(result_data: dict) -> str:
    """Format recursive file count results as text."""
    return f"""📁 Recursive File Count Results for: {result_data['directory_path']}
            
📊 Summary:
• Files: {result_data['file_count']:,}
• Directories: {result_data['directory_count']:,}
• Total Items: {result_data['total_items']:,}
• Total Size: {result_data['total_size']}
• Scanned: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Note: This count includes ALL files and subdirectories recursively."""

def _format_standard_result(result_data: dict) -> str:
    """Format standard (non-recursive) file count results as text."""
    result_text = f"""📁 File Count Results for: {result_data['directory_path']}
            
📊 Summary:
• Files: {result_data['file_count']}
• Directories: {result_data['directory_count']}
• Total Items: {result_data['total_items']}
• Total Size: {result_data['total_size']}

📋 Detailed List:"""
    
    if result_data['items']:
        for item in result_data['items']:
            if item['type'] == 'file':
                size_info = f" ({item['size']} bytes)" if item['size'] != "unknown" else ""
                result_text += f"\n  📄 {item['name']}{size_info}"
            else:
                result_text += f"\n  📁 {item['name']}/"
    else:
        result_text += "\n  (empty directory)"
    
    result_text += f"\n\nScanned: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    result_text += "\n\n💡 Tip: Add 'recursive' to your query to count files in subdirectories too."
    
    return result_text

@app.post("/")
async def handle_request(request: ToolRequest) -> ToolResponse:
    """Handle file counting requests"""
    try:
        query = request.query.lower().strip()
        
        # Extract the folder path from the query
        folder_path = parse_folder_path(request.query)
        
        if not folder_path:
            raise HTTPException(
                status_code=400, 
                detail="Please provide a folder path. Example: 'count files in /path/to/folder' or '/path/to/folder'"
            )
        
        # Determine if user wants recursive count
        recursive = any(keyword in query for keyword in ["recursive", "recursively", "all subdirectories", "including subdirectories"])
        
        # Count files and format result
        if recursive:
            result_data = count_files_recursively(folder_path)
            result_text = _format_recursive_result(result_data)
        else:
            result_data = count_files_in_directory(folder_path)
            result_text = _format_standard_result(result_data)
        
        return ToolResponse(result=result_text)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "File Count Tool"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6110)