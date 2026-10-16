"""Validate a real normal hole by its removed volume, plus dimension editing and DXF."""
import asyncio
import json
import math
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from solidworks_mcp.actor import StaActor
from solidworks_mcp.backend import SolidWorks
from solidworks_mcp.config import Settings
from solidworks_mcp.lock import WorkspaceLock


async def run(settings):
    actor = StaActor(lambda: SolidWorks(settings), settings.workspace / ".state" / "sheet-checks-audit.jsonl")
    report = []

    async def call(operation, **args):
        result = await actor.invoke(operation, args, 120)
        report.append(result)
        print(json.dumps({"operation": operation, "ok": result["ok"], "result": result.get("result"),
                          "error": result.get("error")}, ensure_ascii=False), flush=True)
        if not result["ok"]:
            raise RuntimeError(result)
        return result["result"]

    try:
        await call("connect")
        part = (await call("new_document", kind="part"))["document"]
        plane = next(f["name"] for f in (await call("features", document=part))["features"] if f["type"] == "RefPlane")
        sketch = (await call("sketch_polyline", document=part, plane=plane,
                            points_mm=[(0, 0), (60, 0), (60, 40), (0, 40)], closed=True))["sketch"]
        flange = await call("sheet_base_flange", document=part, sketch=sketch, thickness_mm=2, bend_radius_mm=2)
        dims = (await call("feature_dimensions", document=part, feature=flange["feature"]))["dimensions"]
        for dimension in dims:
            if dimension["unit"] in ("mm", "deg") and dimension["values"]:
                await call("set_dimension", document=part, name=dimension["name"], value=dimension["values"][0], unit=dimension["unit"])
                break
        before = await call("mass_properties", document=part)
        sketch = (await call("sketch_circles", document=part, plane=plane, circles_mm=[(30, 20, 3)], name="MCP_Hole"))["sketch"]
        await call("cut_extrude", document=part, sketch=sketch, normal_cut=True, both_directions=True)
        after = await call("mass_properties", document=part)
        removed = before["volume_m3"] - after["volume_m3"]
        assert math.isclose(removed, math.pi * 3**2 * 2 / 1e9, rel_tol=1e-4), removed
        print(json.dumps({"removed_volume_m3": removed, "expected_m3": math.pi * 3**2 * 2 / 1e9}), flush=True)
        tag = uuid4().hex[:8]
        part = (await call("save_document", document=part, path=f"diagnostics/{tag}/plate-with-hole-{tag}.sldprt"))["path"]
        await call("sheet_export_dxf", document=part, path=f"diagnostics/{tag}/plate-with-hole-{tag}.dxf")
        flat = (await call("sheet_info", document=part))["flat_patterns"][0]
        await call("set_suppression", document=part, feature=flat, suppressed=False)
        await call("set_suppression", document=part, feature=flat, suppressed=True)
        refused = await actor.invoke("close_document", {"document": part}, 30)
        assert not refused["ok"] and refused["error"]["code"] == "UNSAVED_CHANGES"
        report.append(refused)
        await call("save_document", document=part, path=part, overwrite=True)
        await call("close_document", document=part)
        await call("open_document", path=part, read_only=True)
        refused = await actor.invoke("set_properties", {"document": part, "values": {"Test": "blocked"}}, 30)
        assert not refused["ok"] and refused["error"]["code"] == "READ_ONLY"
        report.append(refused)
        await call("close_document", document=part)
    finally:
        (settings.workspace / "sheet-checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        actor.close()


if __name__ == "__main__":
    settings = Settings.load()
    with WorkspaceLock(settings.workspace):
        asyncio.run(run(settings))
