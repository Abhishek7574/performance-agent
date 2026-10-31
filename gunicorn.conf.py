import subprocess
import sys

# Pre-startup hook: install requirements before gunicorn loads workers
def on_starting(server):  # noqa: ARG001
    """Gunicorn pre-startup hook. Parameter required by hook signature but not used."""
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "/home/site/wwwroot/requirements.txt",
         "--root-user-action=ignore", "-q"],
        check=False,
    )
