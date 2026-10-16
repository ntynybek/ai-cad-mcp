"""Engineering workflows; partial outputs are retained and reported, never rolled back blindly."""
import json
import logging
from uuid import uuid4

from .errors import EngineeringError


def bracket_package(backend, **args):
    request_id = uuid4().hex[:10]
    stem = f"{args['name']}-{request_id}"
    folder = f"packages/{stem}"
    steps, outputs = [], {}
    part = drawing = None
    backend._connected()
    backend._template("part")
    if args["drawing"]:
        backend._template("drawing")
        if backend.settings.drawing_sheet_format:
            backend.paths.read(backend.settings.drawing_sheet_format, {".slddrt"})
    manifest_path = backend.paths.output(f"{folder}/manifest.json", {".json"})

    def manifest(state, error=None):
        content = {"state": state, "request": args, "steps": steps, "outputs": outputs,
                   "error": error, "standard": backend.settings.standard,
                   "review_required": ["Dimensions, tolerances, technical requirements and title block",
                                       "Bend allowance against the actual material/tooling bend table"],
                   "open_documents": {"part": part, "drawing": drawing}}
        manifest_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        return content

    def step(name, fn):
        result = fn()
        steps.append({"step": name, "result": result})
        manifest("running")
        return result

    manifest("running")
    try:
        part = step("new_part", lambda: backend.new_document("part"))["document"]
        planes = [f["name"] for f in backend.features(part)["features"] if f["type"] == "RefPlane"]
        if not planes:
            raise EngineeringError("NO_REFERENCE_PLANE", "Template has no reference plane")
        web, leg = args["web_mm"], args["leg_mm"]
        sketch = step("open_u_profile", lambda: backend.sketch_polyline(part, planes[0],
                      [(0, leg), (0, 0), (web, 0), (web, leg)], name="MCP_U_Profile"))["sketch"]
        step("sheet_metal", lambda: backend.sheet_base_flange(part, sketch, args["thickness_mm"],
             args["bend_radius_mm"], args["length_mm"], args["k_factor"]))
        step("properties", lambda: backend.set_properties(part, {
             "Обозначение": args["designation"] or args["name"], "Наименование": args["title"],
             "Толщина": str(args["thickness_mm"]), "Description": args["title"]}))
        model = step("save_model", lambda: backend.save_document(part, f"{folder}/{stem}.sldprt"))
        part, outputs["model"] = model["path"], model["path"]
        outputs["dxf"] = step("flat_pattern_dxf", lambda: backend.sheet_export_dxf(part, f"{folder}/{stem}.dxf"))["path"]
        outputs["step"] = step("model_step", lambda: backend.model_export_step(part, f"{folder}/{stem}.step"))["path"]
        if args["drawing"]:
            drawing = step("new_drawing", lambda: backend.new_document("drawing"))["document"]
            if backend.settings.drawing_sheet_format:
                step("sheet_format", lambda: backend.drawing_setup_sheet(drawing,
                     backend.settings.drawing_sheet_format, backend.settings.drawing_width_mm,
                     backend.settings.drawing_height_mm, 1, 2))
            step("drawing_properties", lambda: backend.set_properties(drawing, {
                 "Обозначение": args["designation"] or args["name"], "Наименование": args["title"]}))
            step("sheet_scale", lambda: backend.drawing_scale(drawing, 1, 2))
            # Positions are adapted to the template's actual sheet size and model size.
            sheet = backend._cast(backend._doc(drawing, 3), "IDrawingDoc").GetCurrentSheet()
            props = sheet.GetProperties2()
            width, height = props[5] * 1000, props[6] * 1000
            if width < 200 or height < 200:
                raise EngineeringError("SHEET_TOO_SMALL", "Use an A4 or larger drawing template")
            step("folded_view", lambda: backend.drawing_view(drawing, outputs["model"], "*Isometric", width * .30, height * .68))
            step("flat_view", lambda: backend.drawing_flat_view(drawing, outputs["model"], x_mm=width * .70, y_mm=height * .68))
            step("final_sheet_scale", lambda: backend.drawing_scale(drawing, 1, 2))
            step("technical_note", lambda: backend.drawing_note(drawing,
                 f"{args['title']}\nТолщина {args['thickness_mm']:g} мм. Rвн {args['bend_radius_mm']:g} мм.\n"
                 "ПРОБНЫЙ ЧЕРТЁЖ - проверить размеры, допуски и основную надпись.", 25, height * .30))
            drw = step("save_drawing", lambda: backend.save_document(drawing, f"{folder}/{stem}.slddrw"))
            drawing, outputs["drawing"] = drw["path"], drw["path"]
            outputs["pdf"] = step("drawing_pdf", lambda: backend.drawing_export(drawing, f"{folder}/{stem}.pdf"))["path"]
        content = manifest("completed")
        return {**content, "manifest": str(manifest_path)}
    except Exception as exc:
        logging.getLogger(__name__).exception("Bracket package failed")
        error = exc.as_dict() if isinstance(exc, EngineeringError) else {"message": str(exc)}
        manifest("failed", error)
        raise EngineeringError("WORKFLOW_FAILED", "Package stopped; partial files and documents retained for inspection",
                               cause=error, manifest=str(manifest_path), completed_steps=[s["step"] for s in steps],
                               open_documents={"part": part, "drawing": drawing}) from exc
