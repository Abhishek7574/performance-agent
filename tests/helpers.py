"""Shared test utilities — loaded once per session via sys.modules cache."""
import importlib.util
import os
import sys


def load_tool(name: str):
    """Import a tool's main.py under a unique module name to avoid cache conflicts."""
    module_name = f"{name}_main"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "tools", name, "CODE_EXECUTION", "main.py")
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod
