"""Additional integration checks on generated models and new test documents only."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from solidworks_mcp.actor import StaActor
from solidworks_mcp.backend import SolidWorks
from solidworks_mcp.config import Settings
from solidworks_mcp.lock import WorkspaceLock


async def run(settings):
    report = []
    actor = StaActor(lambda: SolidWorks(settings), settings.workspace / ".state" / "live-checks-audit.jsonl")

    async def call(operation, **args):
        result = await actor.invoke(operation, args, 120)
        report.append(result)
        print(json.dumps({"operation": operation, "ok": result["ok"], "error": result.get("error")}, ensure_ascii=False), flush=True)
        if not result["ok"]:
            raise RuntimeError(result)
        return result["result"]

    try:
        latest = json.loads((settings.workspace / "smoke-latest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        package = latest["result"]["result"]
        assert package["state"] == "completed"
        await call("connect")
        part = package["outputs"]["model"]
        await call("mass_properties", document=part)
        info = await call("sheet_info", document=part)
        item = info["sheet_metal"][0]
        await call("sheet_set_parameters", document=part, **item)
        await call("cut_list_update", document=part)
        tag = uuid4().hex[:8]
        await call("cut_list_csv", document=part, path=f"diagnostics/{tag}/sheet-cut-list.csv")
        await call("save_document", document=part, path=part, overwrite=True)
        # A structural member on a new document, using an explicit size from the installed profile.
        frame = (await call("new_document", kind="part"))["document"]
        sketch = await call("sketch_3d", document=frame, segments_mm=[((0, 0, 0), (500, 0, 0))], name="MCP_Frame_Path")
        profile = str(settings.library_roots[0] / "iso" / "square tube.sldlfp")
        sizes = (await call("library_configurations", path=profile))["configurations"]
        await call("weldment_create", document=frame, profile=profile, profile_configuration=sizes[0], groups=[sketch["segments"]])
        await call("cut_list_update", document=frame)
        await call("cut_list_csv", document=frame, path=f"diagnostics/{tag}/frame-cut-list.csv")
        await call("mass_properties", document=frame)
        frame = (await call("save_document", document=frame, path=f"diagnostics/{tag}/frame.sldprt"))["path"]
        assembly = (await call("new_document", kind="assembly"))["document"]
        await call("assembly_insert", document=assembly, path=frame, position_mm=(0, 0, 0))
        await call("assembly_insert", document=assembly, path=part, position_mm=(100, 100, 0))
        components = await call("assembly_components", document=assembly)
        assert len(components["components"]) == 2
        await call("save_document", document=assembly, path=f"diagnostics/{tag}/test-assembly.sldasm")
    finally:
        (settings.workspace / "live-checks.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        actor.close()


if __name__ == "__main__":
    settings = Settings.load()
    with WorkspaceLock(settings.workspace):
        asyncio.run(run(settings))
