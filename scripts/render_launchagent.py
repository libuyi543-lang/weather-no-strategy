"""Render a macOS launchd template for this checkout; does not load it."""
import os
from pathlib import Path
import plistlib
import sys
root = Path(__file__).resolve().parents[1]
text = Path(sys.argv[1]).read_text().replace("__PROJECT_ROOT__", str(root)).replace("__USER_HOME__", str(Path.home())).replace("__PYTHON__", os.environ.get("WEATHER_PYTHON", sys.executable))
payload = plistlib.loads(text.encode())
Path(sys.argv[2]).write_bytes(plistlib.dumps(payload))
