"""MCP entrypoint usable from any current directory, without installing this project."""
from pathlib import Path
import os
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
if "--config" not in sys.argv and not os.environ.get("SW_MCP_CONFIG"):
    sys.argv.extend(["--config", str(root / "solidworks-mcp.local.toml")])

from solidworks_mcp.__main__ import main
main()
