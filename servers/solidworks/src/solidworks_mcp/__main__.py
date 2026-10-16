import argparse
import importlib.metadata
import json
import logging
import platform
import sys

from .config import Settings
from . import __version__


def main():
    parser = argparse.ArgumentParser(description="SOLIDWORKS engineering MCP (local stdio)")
    parser.add_argument("--config", help="Path to TOML config")
    parser.add_argument("--doctor", action="store_true", help="Check environment without launching SOLIDWORKS")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.load(args.config)
    if args.doctor:
        packages = {}
        for name in ("mcp", "pydantic", "pywin32"):
            try:
                packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                packages[name] = None
        print(json.dumps({"server_version": __version__, "platform": platform.platform(), "python": sys.version, "packages": packages,
                          "workspace": str(settings.workspace), "prog_id": settings.prog_id,
                          "templates": {kind: {"path": getattr(settings, f"{kind}_template"),
                                                "exists": __import__("pathlib").Path(getattr(settings, f"{kind}_template")).is_file()}
                                        for kind in ("part", "assembly", "drawing")},
                          "libraries": [{"path": str(p), "exists": p.is_dir()} for p in settings.library_roots]},
                         ensure_ascii=False, indent=2))
        return
    from .server import create_server
    # Claude may start separate Chat and Cowork/Code processes. Lock individual
    # operations inside the STA worker so every process can finish MCP discovery.
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
