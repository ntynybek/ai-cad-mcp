"""SOLIDWORKS COM implementation. Construct and use only inside StaActor."""
from contextlib import contextmanager
import math
from pathlib import Path

from .config import Paths
from .errors import EngineeringError
from .library import Library
from . import __version__

DOC_TYPES = {"part": 1, "assembly": 2, "drawing": 3}
NATIVE = {1: ".sldprt", 2: ".sldasm", 3: ".slddrw"}
INPUTS = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}


class SolidWorks:
    def __init__(self, settings):
        self.settings, self.paths = settings, Paths(settings)
        self.library = Library(settings, self.paths)
        self.app = None
        self.interfaces = {}
        self.session = {}
        # Imports are deliberately lazy: server/schema discovery also runs without Windows.

    def execute(self, operation, args):
        if operation.startswith("_") or operation not in OPERATIONS:
            raise EngineeringError("UNKNOWN_OPERATION", operation)
        try:
            return getattr(self, operation)(**args)
        except EngineeringError:
            raise
        except Exception as exc:
            if hasattr(exc, "hresult"):
                raise EngineeringError("COM_ERROR", str(exc), hresult=f"0x{exc.hresult & 0xffffffff:08X}",
                                       partial_changes_possible=True,
                                       advice="Check SOLIDWORKS dialogs and document state before retrying.") from exc
            raise

    @staticmethod
    def _dispatch(obj):
        from .com_dispatch import dispatch
        return dispatch(obj)

    def _cast(self, obj, interface):
        import pythoncom
        if interface not in self.interfaces:
            raise EngineeringError("INTERFACE_UNAVAILABLE", interface)
        return self._dispatch(obj._oleobj_.QueryInterface(self.interfaces[interface], pythoncom.IID_IDispatch))

    @staticmethod
    def _null():
        import pythoncom
        from win32com.client import VARIANT
        return VARIANT(pythoncom.VT_DISPATCH, None)

    @staticmethod
    def _out(vt, value):
        import pythoncom
        from win32com.client import VARIANT
        return VARIANT(pythoncom.VT_BYREF | vt, value)

    @staticmethod
    def _objects(values):
        import pythoncom
        from win32com.client import VARIANT
        return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, values)

    def _connected(self):
        if self.app is None:
            raise EngineeringError("NOT_CONNECTED", "Call sw_connect first; launch=true may start SOLIDWORKS")
        return self.app

    def connect(self, launch=False, visible=True):
        import pythoncom
        origin = self.session.get("origin", "attached")
        if self.app is None:
            try:
                self.app = self._dispatch(pythoncom.GetActiveObject(self.settings.prog_id).QueryInterface(pythoncom.IID_IDispatch))
                origin = "attached"
            except pythoncom.com_error as exc:
                if not launch:
                    raise EngineeringError("SOLIDWORKS_NOT_RUNNING", "Start SOLIDWORKS or use launch=true") from exc
                self.app = self._dispatch(pythoncom.CoCreateInstance(self.settings.prog_id, None,
                                           pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch))
                origin = "launched"
        try:
            revision = self.app.RevisionNumber()
            major = int(revision.split(".")[0])
            executable = Path(self.app.GetExecutablePath())
            directory = executable.parent if executable.suffix.lower() == ".exe" else executable
            lib = pythoncom.LoadTypeLib(str(directory / "sldworks.tlb"))
            for i in range(lib.GetTypeInfoCount()):
                self.interfaces[lib.GetDocumentation(i)[0]] = lib.GetTypeInfo(i).GetTypeAttr().iid
            self.session = {"origin": origin, "revision": revision, "year": 1992 + major,
                            "target_version": 32 <= major <= 34}
            self.app.Visible = visible
        except Exception:
            self.app, self.interfaces, self.session = None, {}, {}
            raise
        return {**self.session, "connected": True, "defaults": {
            kind: self.app.GetUserPreferenceStringValue(8 + i) for i, kind in enumerate(DOC_TYPES)}}

    def status(self):
        return {"server_version": __version__, "connected": self.app is not None, "session": self.session,
                "workspace": str(self.settings.workspace), "standard": self.settings.standard,
                "target_versions": [2024, 2025, 2026]}

    def _documents(self):
        return list(self._connected().GetDocuments() or ())

    @staticmethod
    def _info(doc):
        return {"document": doc.GetTitle(), "path": doc.GetPathName(), "type": doc.GetType(),
                "dirty": bool(doc.GetSaveFlag()), "read_only": bool(doc.IsOpenedReadOnly()),
                "configuration": doc.ConfigurationManager.ActiveConfiguration.Name if doc.GetType() != 3 else None}

    def documents(self):
        return {"documents": [self._info(d) for d in self._documents()]}

    def _doc(self, document, kind=None):
        matches = [d for d in self._documents()
                   if d.GetTitle().casefold() == document.casefold() or
                   (d.GetPathName() and d.GetPathName().casefold() == document.casefold())]
        if len(matches) != 1:
            raise EngineeringError("DOCUMENT_NOT_FOUND", "Provide an unambiguous open document title or path",
                                   document=document, matches=len(matches))
        doc = self._cast(matches[0], "IModelDoc2")
        if kind and doc.GetType() != kind:
            raise EngineeringError("DOCUMENT_TYPE", f"Operation requires document type {kind}")
        return doc

    def _activate(self, doc):
        import pythoncom
        # Sketch/feature and selection APIs act on the active document.
        error = self._out(pythoncom.VT_I4, 0)
        activated = self.app.ActivateDoc3(doc.GetPathName() or doc.GetTitle(), False, 1, error)
        if activated is None or error.value not in (0, 2):
            raise EngineeringError("ACTIVATION_FAILED", "Cannot activate target document", api_code=error.value)
        active = self.app.ActiveDoc
        if active is None or active.GetTitle() != doc.GetTitle() or active.GetPathName() != doc.GetPathName():
            raise EngineeringError("ACTIVE_DOCUMENT_CHANGED", "Target activation could not be verified")
        if error.value == 2 and not doc.ForceRebuild3(False):
            raise EngineeringError("REBUILD_FAILED", "Target document needs a rebuild that did not succeed")

    def _writable(self, doc):
        self._activate(doc)
        if doc.IsOpenedReadOnly():
            raise EngineeringError("READ_ONLY", "Document is open read-only")
        if doc.GetType() != 3 and doc.GetActiveSketch2() is not None:
            raise EngineeringError("SKETCH_EDIT_ACTIVE", "Finish the current sketch before this operation")

    def _template(self, kind, value=""):
        template = value or getattr(self.settings, f"{kind}_template")
        if not template:
            raise EngineeringError("TEMPLATE_REQUIRED", f"Configure {kind}_template or supply template explicitly")
        return self.paths.read(template, {f".{ {'part': 'prt', 'assembly': 'asm', 'drawing': 'drw'}[kind]}dot"})

    def new_document(self, kind, template=""):
        self._connected()
        path = self._template(kind, template)
        doc = self.app.NewDocument(str(path), 0, 0.0, 0.0)
        if doc is None:
            raise EngineeringError("NEW_DOCUMENT_FAILED", f"Template rejected: {path}")
        return self._info(self._cast(doc, "IModelDoc2"))

    def open_document(self, path, read_only=True, configuration=""):
        import pythoncom
        self._connected()
        path = self.paths.read(path, set(INPUTS))
        for d in self._documents():
            if d.GetPathName().casefold() == str(path).casefold():
                return {**self._info(d), "already_open": True, "requested_read_only": read_only}
            if d.GetPathName() and Path(d.GetPathName()).name.casefold() == path.name.casefold():
                raise EngineeringError("DOCUMENT_TITLE_COLLISION", "SOLIDWORKS already has another file with this name open",
                                       requested=str(path), already_open=d.GetPathName())
        error, warning = self._out(pythoncom.VT_I4, 0), self._out(pythoncom.VT_I4, 0)
        doc = self.app.OpenDoc6(str(path), INPUTS[path.suffix.lower()], 1 | (2 if read_only else 0), configuration, error, warning)
        if doc is None or error.value:
            raise EngineeringError("OPEN_FAILED", "SOLIDWORKS could not open file", errors=error.value, warnings=warning.value)
        return {**self._info(self._cast(doc, "IModelDoc2")), "warnings": warning.value}

    def save_document(self, document, path, overwrite=False):
        doc = self._doc(document)
        self._writable(doc)
        target = self.paths.output(path, {NATIVE[doc.GetType()]}, overwrite)
        # Never replace another currently open document.
        for other in self._documents():
            if other.GetPathName().casefold() == str(target).casefold() and other.GetTitle() != doc.GetTitle():
                raise EngineeringError("DESTINATION_OPEN", "Another document is open at the destination")
        result = self._save(doc, target)
        return {**self._info(doc), **result}

    def _save(self, doc, path, export_data=None, copy=False):
        import pythoncom
        error, warning = self._out(pythoncom.VT_I4, 0), self._out(pythoncom.VT_I4, 0)
        doc.ClearSelection2(True)
        ok = doc.Extension.SaveAs(str(path), 0, 1 | (2 if copy else 0), export_data or self._null(), error, warning)
        if not ok or error.value:
            raise EngineeringError("SAVE_FAILED", "SOLIDWORKS failed to save", path=str(path),
                                   errors=error.value, warnings=warning.value)
        if not path.is_file() or not path.stat().st_size:
            raise EngineeringError("OUTPUT_MISSING", "API returned success but no nonempty output exists", path=str(path))
        return {"path": str(path), "bytes": path.stat().st_size, "warnings": warning.value}

    def close_document(self, document, discard_changes=False):
        doc = self._doc(document)
        if doc.GetSaveFlag() and not discard_changes:
            raise EngineeringError("UNSAVED_CHANGES", "Save first or explicitly set discard_changes=true")
        title = doc.GetTitle()
        path = doc.GetPathName()
        self.app.CloseDoc(path or title)
        if any((path and d.GetPathName().casefold() == path.casefold()) or
               (not path and d.GetTitle() == title) for d in self._documents()):
            raise EngineeringError("CLOSE_FAILED", "Document is still open")
        return {"closed": title}

    def document_info(self, document):
        return self._info(self._doc(document))

    def _features(self, doc):
        seen = set()
        def walk(feature, depth=0):
            while feature is not None and depth < 30:
                key = (feature.Name, feature.GetTypeName2())
                if key not in seen:
                    seen.add(key)
                    yield feature, depth
                    yield from walk(feature.GetFirstSubFeature(), depth + 1)
                feature = feature.GetNextSubFeature() if depth else feature.GetNextFeature()
        yield from walk(doc.FirstFeature())

    def features(self, document, limit=500):
        items = []
        for feature, depth in self._features(self._doc(document)):
            items.append({"name": feature.Name, "type": feature.GetTypeName2(), "depth": depth})
            if len(items) > limit:
                break
        return {"features": items[:limit], "truncated": len(items) > limit}

    def rebuild(self, document):
        doc = self._doc(document)
        self._writable(doc)
        if not doc.ForceRebuild3(False):
            raise EngineeringError("REBUILD_FAILED", "Document rebuild failed; inspect feature tree")
        return {"rebuilt": doc.GetTitle()}

    def configurations(self, document):
        doc = self._doc(document)
        return {"configurations": list(doc.GetConfigurationNames() or ()), **self._info(doc)}

    def activate_configuration(self, document, configuration):
        doc = self._doc(document)
        self._writable(doc)
        if configuration not in (doc.GetConfigurationNames() or ()):
            raise EngineeringError("CONFIGURATION_NOT_FOUND", configuration)
        if not doc.ShowConfiguration2(configuration):
            raise EngineeringError("CONFIGURATION_FAILED", configuration)
        return self._info(doc)

    def _property_manager(self, doc, configuration):
        if configuration and configuration not in (doc.GetConfigurationNames() or ()):
            raise EngineeringError("CONFIGURATION_NOT_FOUND", configuration)
        return doc.Extension.CustomPropertyManager(configuration)

    def _properties(self, manager):
        import pythoncom
        result = {}
        for name in manager.GetNames() or ():
            raw, resolved = self._out(pythoncom.VT_BSTR, ""), self._out(pythoncom.VT_BSTR, "")
            was_resolved, linked = self._out(pythoncom.VT_BOOL, False), self._out(pythoncom.VT_BOOL, False)
            code = manager.Get6(name, False, raw, resolved, was_resolved, linked)
            result[name] = {"value": raw.value, "resolved": resolved.value, "was_resolved": was_resolved.value,
                            "linked": linked.value, "api_code": code}
        return result

    def properties(self, document, configuration=""):
        return {"properties": self._properties(self._property_manager(self._doc(document), configuration))}

    def set_properties(self, document, values, configuration=""):
        doc = self._doc(document)
        self._writable(doc)
        manager = self._property_manager(doc, configuration)
        codes = {}
        for key, value in values.items():
            if not key or len(key) > 255 or len(value) > 4096:
                raise EngineeringError("PROPERTY_INVALID", "Property key/value is empty or too long")
        for key, value in values.items():
            code = manager.Add3(key, 30, value, 2)  # Text, replace value.
            if code != 0:
                raise EngineeringError("PROPERTY_WRITE_FAILED", key, api_code=code, completed=codes)
            codes[key] = code
        return {"written": codes, **self.properties(doc.GetTitle(), configuration)}

    def set_dimension(self, document, name, value, unit="mm"):
        doc = self._doc(document)
        self._writable(doc)
        dimension = doc.Parameter(name)
        if dimension is None:
            raise EngineeringError("DIMENSION_NOT_FOUND", name)
        actual = dimension.GetType()  # swDimensionType_e: linear=2, angular=3.
        if (unit == "mm" and actual != 2) or (unit == "deg" and actual != 3):
            raise EngineeringError("DIMENSION_UNIT", "Requested unit does not match dimension type", type=actual)
        converted = value / 1000 if unit == "mm" else math.radians(value) if unit == "deg" else value
        code = dimension.SetSystemValue3(converted, 1, None)
        if code != 0:
            raise EngineeringError("DIMENSION_WRITE_FAILED", name, api_code=code)
        self.rebuild(document)
        return {"dimension": name, "value": value, "unit": unit, "configuration_scope": "current"}

    def mass_properties(self, document):
        doc = self._doc(document)
        if doc.GetType() == 3:
            raise EngineeringError("DOCUMENT_TYPE", "Mass properties require a model")
        doc.ClearSelection2(True)
        mass = doc.Extension.CreateMassProperty()
        if mass is None:
            raise EngineeringError("MASS_UNAVAILABLE", "No resolved solid bodies with mass properties")
        mass.UseSystemUnits = True
        return {"mass_kg": mass.Mass, "volume_m3": mass.Volume, "area_m2": mass.SurfaceArea,
                "center_mm": [v * 1000 for v in mass.CenterOfMass]}

    def set_material(self, document, database, material, configuration=""):
        import pythoncom
        doc = self._doc(document, 1)
        self._writable(doc)
        database = str(self.paths.read(database, {".sldmat"}))
        config = configuration or doc.ConfigurationManager.ActiveConfiguration.Name
        self._property_manager(doc, config)
        part = self._cast(doc, "IPartDoc")
        part.SetMaterialPropertyName2(config, database, material)
        db = self._out(pythoncom.VT_BSTR, "")
        actual = part.GetMaterialPropertyName2(config, db)
        if actual != material:
            raise EngineeringError("MATERIAL_FAILED", "Requested material was not applied", actual=actual)
        return {"material": actual, "database": db.value, "configuration": config}

    @contextmanager
    def _sketch(self, doc, plane=None, three_d=False):
        self._writable(doc)
        manager = doc.SketchManager
        doc.ClearSelection2(True)
        if plane:
            if not doc.Extension.SelectByID2(plane, "PLANE", 0., 0., 0., False, 0, self._null(), 0):
                raise EngineeringError("PLANE_NOT_FOUND", plane)
        if three_d:
            manager.Insert3DSketch(True)
        else:
            manager.InsertSketch(True)
        sketch = doc.GetActiveSketch2()
        if sketch is None:
            raise EngineeringError("SKETCH_START_FAILED", "No active sketch")
        old_add = manager.AddToDB
        try:
            # Keep endpoint inference/merging enabled so adjacent lines form one contour.
            manager.AddToDB = False
            yield manager, sketch
        finally:
            manager.AddToDB = old_add
            if doc.GetActiveSketch2() is not None:
                if three_d:
                    manager.Insert3DSketch(True)
                else:
                    manager.InsertSketch(True)
            doc.ClearSelection2(True)

    def sketch_polyline(self, document, plane, points_mm, closed=False, name=""):
        doc = self._doc(document, 1)
        pairs = list(zip(points_mm, points_mm[1:] + ([points_mm[0]] if closed else [])))
        with self._sketch(doc, plane) as (manager, sketch):
            for (x1, y1), (x2, y2) in pairs:
                if manager.CreateLine(x1 / 1000, y1 / 1000, 0., x2 / 1000, y2 / 1000, 0.) is None:
                    raise EngineeringError("GEOMETRY_FAILED", "Sketch line could not be created")
            feature = self._cast(sketch, "IFeature")
        if name:
            feature.Name = name
        return {"sketch": feature.Name, "segments": len(pairs), "closed": closed}

    def sketch_3d(self, document, segments_mm, name=""):
        doc = self._doc(document, 1)
        with self._sketch(doc, three_d=True) as (manager, sketch):
            for a, b in segments_mm:
                if manager.CreateLine(*[v / 1000 for v in (*a, *b)]) is None:
                    raise EngineeringError("GEOMETRY_FAILED", "3D sketch line could not be created")
            feature = self._cast(sketch, "IFeature")
        if name:
            feature.Name = name
        # Actual segment names are obtained through the sketch API, not invented.
        segments = sketch.GetSketchSegments() or ()
        return {"sketch": feature.Name, "segments": [f"{s.GetName()}@{feature.Name}" for s in segments]}

    def sketch_circles(self, document, plane, circles_mm, name=""):
        doc = self._doc(document, 1)
        with self._sketch(doc, plane) as (manager, sketch):
            for x, y, radius in circles_mm:
                if manager.CreateCircleByRadius(x / 1000, y / 1000, 0., radius / 1000) is None:
                    raise EngineeringError("GEOMETRY_FAILED", "Circle could not be created")
            feature = self._cast(sketch, "IFeature")
        if name:
            feature.Name = name
        return {"sketch": feature.Name, "circles": len(circles_mm)}

    def cut_extrude(self, document, sketch, mode="through_all", depth_mm=10, reverse=False,
                     both_directions=False, normal_cut=True):
        doc = self._doc(document, 1)
        self._writable(doc)
        if normal_cut and not self.sheet_info(document)["sheet_metal"]:
            raise EngineeringError("NOT_SHEET_METAL", "normal_cut=true requires a sheet-metal part")
        profile = self._feature(doc, sketch, {"ProfileFeature"})
        doc.ClearSelection2(True)
        if not profile.Select2(False, 0):
            raise EngineeringError("SELECTION_FAILED", sketch)
        end = 1 if mode == "through_all" else 0
        try:
            feature = doc.FeatureManager.FeatureCut4(
                not both_directions, False, reverse, end, end, depth_mm / 1000, depth_mm / 1000,
                False, False, False, False, 0., 0., False, False, False, False, normal_cut,
                False, True, False, False, False, 0, 0., False, normal_cut)
        finally:
            doc.ClearSelection2(True)
        if feature is None:
            raise EngineeringError("CUT_FAILED", "Check closed contours and intersection with the selected cut direction")
        self.rebuild(document)
        return {"feature": feature.Name, "mode": mode, "normal_cut": normal_cut}

    def feature_dimensions(self, document, feature):
        doc = self._doc(document)
        feat = self._feature(doc, feature)
        display = feat.GetFirstDisplayDimension()
        items = []
        seen = set()
        while display is not None and len(items) < 500:
            dimension = display.GetDimension2(0)
            full_name = dimension.FullName
            if full_name in seen:
                break
            seen.add(full_name)
            kind = dimension.GetType()
            values = list(dimension.GetSystemValue3(1, None) or ())
            converted = [v * 1000 if kind == 2 else math.degrees(v) if kind == 3 else v for v in values]
            items.append({"name": "@".join(full_name.split("@")[:2]), "full_name": full_name,
                          "type": kind, "values": converted, "unit": "mm" if kind == 2 else "deg" if kind == 3 else "scalar"})
            display = feat.GetNextDisplayDimension(display)
        return {"dimensions": items, "configuration_scope": "current"}

    def _feature(self, doc, name, expected=None):
        feature = doc.FeatureByName(name)
        if feature is None or (expected and feature.GetTypeName2() not in expected):
            raise EngineeringError("FEATURE_NOT_FOUND", name, expected=expected)
        return feature

    def sheet_base_flange(self, document, sketch, thickness_mm, bend_radius_mm, depth_mm=100,
                          k_factor=0.5, reverse=False):
        doc = self._doc(document, 1)
        self._writable(doc)
        feature = self._feature(doc, sketch, {"ProfileFeature"})
        doc.ClearSelection2(True)
        if not feature.Select2(False, 0):
            raise EngineeringError("SELECTION_FAILED", sketch)
        manager = doc.FeatureManager
        allowance = manager.CreateCustomBendAllowance()
        allowance.Type = 2  # swBendAllowanceKFactor
        allowance.KFactor = k_factor
        try:
            # CreateDefinition/Initialize replaces the obsolete InsertSheetMetalBaseFlange2.
            data = manager.CreateDefinition(34)  # swFmBaseFlange
            data.Initialize(False, True, allowance, True, 1, True, 0.5, thickness_mm / 1000, thickness_mm / 1000)
            data.Thickness = thickness_mm / 1000
            data.BendRadius = bend_radius_mm / 1000
            data.ReverseThickness = reverse
            data.OffsetDirections = 1
            data.D1EndConditionType = 0  # swEndCondBlind
            data.D1EndConditionDistance = depth_mm / 1000
            created = manager.CreateFeature(data)
        finally:
            doc.ClearSelection2(True)
        if created is None:
            raise EngineeringError("BASE_FLANGE_FAILED", "Check sketch contour, thickness and radius")
        self.rebuild(document)
        return {"feature": created.Name, **self.sheet_info(document)}

    def sheet_info(self, document):
        doc = self._doc(document, 1)
        items = []
        flats = []
        for feature, _ in self._features(doc):
            if feature.GetTypeName2() == "SheetMetal":
                data = feature.GetDefinition()
                items.append({"feature": feature.Name, "thickness_mm": data.Thickness * 1000,
                              "bend_radius_mm": data.BendRadius * 1000, "k_factor": data.KFactor})
            if feature.GetTypeName2() == "FlatPattern":
                flats.append(feature.Name)
        return {"sheet_metal": items, "flat_patterns": flats}

    def sheet_set_parameters(self, document, feature, thickness_mm, bend_radius_mm, k_factor):
        doc = self._doc(document, 1)
        self._writable(doc)
        feat = self._feature(doc, feature, {"SheetMetal"})
        data = feat.GetDefinition()
        if not data.AccessSelections(doc, self._null()):
            raise EngineeringError("FEATURE_ACCESS_FAILED", feature)
        consumed = False
        try:
            data.Thickness, data.BendRadius, data.KFactor = thickness_mm / 1000, bend_radius_mm / 1000, k_factor
            if not feat.ModifyDefinition(data, doc, self._null()):
                raise EngineeringError("FEATURE_MODIFY_FAILED", feature)
            consumed = True
        finally:
            if not consumed:
                data.ReleaseSelectionAccess()
        self.rebuild(document)
        return self.sheet_info(document)

    def set_suppression(self, document, feature, suppressed):
        doc = self._doc(document)
        self._writable(doc)
        feat = self._feature(doc, feature)
        if not feat.SetSuppression2(0 if suppressed else 1, 1, None):
            raise EngineeringError("SUPPRESSION_FAILED", feature)
        return {"feature": feature, "suppressed": suppressed, "configuration_scope": "current"}

    def sheet_export_dxf(self, document, path, overwrite=False, bend_lines=True, sketches=False,
                         bounding_box=False, flat_pattern=""):
        doc = self._doc(document, 1)
        self._writable(doc)
        if not doc.GetPathName() or doc.GetSaveFlag():
            raise EngineeringError("SAVE_REQUIRED", "Save the model before DXF export")
        flats = self.sheet_info(document)["flat_patterns"]
        if not flats:
            raise EngineeringError("NOT_SHEET_METAL", "No flat-pattern feature found")
        if len(flats) > 1 and not flat_pattern:
            raise EngineeringError("MULTIBODY_SELECTION_REQUIRED", "Specify flat_pattern for a multibody part", flat_patterns=flats)
        doc.ClearSelection2(True)
        if flat_pattern:
            if flat_pattern not in flats:
                raise EngineeringError("FLAT_PATTERN_NOT_FOUND", flat_pattern)
            if not self._feature(doc, flat_pattern).Select2(False, 0):
                raise EngineeringError("SELECTION_FAILED", flat_pattern)
        target = self.paths.output(path, {".dxf", ".dwg"}, overwrite)
        mask = 1 | (4 if bend_lines else 0) | (8 if sketches else 0) | (2048 if bounding_box else 0)
        try:
            success = self._cast(doc, "IPartDoc").ExportToDWG2(str(target), doc.GetPathName(), 1, True,
                      None, False, False, mask, None)
        finally:
            doc.ClearSelection2(True)
        if not success or not target.is_file() or not target.stat().st_size:
            raise EngineeringError("DXF_EXPORT_FAILED", "Check flat pattern and export options", path=str(target))
        return {"path": str(target), "bytes": target.stat().st_size, "sheet_metal_options": mask}

    def weldment_create(self, document, profile, groups, angle_deg=0, gap_mm=0, corner_treatment=1,
                         profile_configuration=""):
        doc = self._doc(document, 1)
        self._writable(doc)
        profile = self.paths.read(profile, {".sldlfp"})
        configs = list(self.app.GetConfigurationNames(str(profile)) or ())
        if not profile_configuration and len(configs) > 1:
            raise EngineeringError("PROFILE_CONFIGURATION_REQUIRED", "Choose an explicit profile size", configurations=configs)
        profile_configuration = profile_configuration or (configs[0] if configs else "")
        if profile_configuration and profile_configuration not in configs:
            raise EngineeringError("PROFILE_CONFIGURATION_NOT_FOUND", profile_configuration, configurations=configs)
        created_groups = []
        try:
            for names in groups:
                doc.ClearSelection2(True)
                segments = []
                for i, name in enumerate(names):
                    if not doc.Extension.SelectByID2(name, "EXTSKETCHSEGMENT", 0., 0., 0., i > 0, 0, self._null(), 0):
                        raise EngineeringError("SEGMENT_NOT_FOUND", name)
                    segments.append(doc.SelectionManager.GetSelectedObject6(i + 1, 0))
                group = doc.FeatureManager.CreateStructuralMemberGroup()
                group.Segments = self._objects(segments)
                group.Angle, group.GapWithinGroup = math.radians(angle_deg), gap_mm / 1000
                group.ApplyCornerTreatment = True
                group.CornerTreatmentType = corner_treatment
                created_groups.append(group)
            # A structural member alone does not enable the weldment cut-list machinery.
            if not self._cast(doc, "IPartDoc").IsWeldment():
                if doc.FeatureManager.InsertWeldmentFeature() is None:
                    raise EngineeringError("WELDMENT_INITIALIZATION_FAILED", "Cannot create weldment feature")
                for body_feature, _ in self._features(doc):
                    if body_feature.GetTypeName2() == "SolidBodyFolder":
                        folder = body_feature.GetSpecificFeature2()
                        if not folder.GetAutomaticCutList() and not folder.SetAutomaticCutList(True):
                            raise EngineeringError("CUT_LIST_ENABLE_FAILED", "Cannot enable cut list for new weldment")
            feature = doc.FeatureManager.InsertStructuralWeldment5(str(profile), corner_treatment, False,
                                    self._objects(created_groups), profile_configuration)
        finally:
            doc.ClearSelection2(True)
        if feature is None:
            raise EngineeringError("WELDMENT_FAILED", "Check profile and connected sketch segments")
        self.rebuild(document)
        return {"feature": feature.Name, "groups": len(groups), "profile_configuration": profile_configuration}

    def cut_list(self, document, update=False):
        doc = self._doc(document, 1)
        if update:
            self._writable(doc)
            for feature, _ in self._features(doc):
                if feature.GetTypeName2() == "SolidBodyFolder":
                    if not feature.GetSpecificFeature2().UpdateCutList():
                        raise EngineeringError("CUT_LIST_UPDATE_FAILED", feature.Name)
        items = []
        for feature, _ in self._features(doc):
            if feature.GetTypeName2() == "CutListFolder":
                folder = feature.GetSpecificFeature2()
                items.append({"name": feature.Name, "body_count": folder.GetBodyCount(),
                              "bodies": [b.Name for b in folder.GetBodies() or ()],
                              "properties": self._properties(feature.CustomPropertyManager)})
        return {"items": items}

    def cut_list_read(self, document):
        return self.cut_list(document)

    def cut_list_update(self, document):
        return self.cut_list(document, update=True)

    def cut_list_csv(self, document, path, overwrite=False):
        import csv
        data = self.cut_list(document)["items"]
        target = self.paths.output(path, {".csv"}, overwrite)
        keys = sorted({key for row in data for key in row["properties"]})
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            def safe(value):
                text = str(value)
                return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
            writer.writerow(["CutListFolder", "BodyCount", *keys])
            for row in data:
                writer.writerow([safe(row["name"]), row["body_count"],
                                 *[safe(row["properties"].get(k, {}).get("resolved", "")) for k in keys]])
        return {"path": str(target), "rows": len(data)}

    def library_index(self):
        return self.library.reindex()

    def library_search(self, query="", kind="all", limit=50):
        return self.library.search(query, kind, limit)

    def library_configurations(self, path):
        self._connected()
        source = self.paths.read(path, {".sldprt", ".sldasm", ".sldlfp"})
        return {"path": str(source), "configurations": list(self.app.GetConfigurationNames(str(source)) or ())}

    def assembly_components(self, document):
        doc = self._doc(document, 2)
        components = self._cast(doc, "IAssemblyDoc").GetComponents(False) or ()
        return {"components": [{"name": c.Name2, "path": c.GetPathName(),
                                "configuration": c.ReferencedConfiguration,
                                "suppression": c.GetSuppression()} for c in components]}

    def assembly_insert(self, document, path, configuration="", position_mm=(0, 0, 0)):
        doc = self._doc(document, 2)
        self._writable(doc)
        path = self.paths.read(path, {".sldprt", ".sldasm"})
        # AddComponent5 requires the component model already loaded in memory.
        opened = self.open_document(str(path), read_only=True)
        if configuration and configuration not in self.configurations(opened["document"])["configurations"]:
            raise EngineeringError("CONFIGURATION_NOT_FOUND", configuration)
        self._activate(doc)
        component = self._cast(doc, "IAssemblyDoc").AddComponent5(str(path), 0, "", bool(configuration),
                      configuration, *[v / 1000 for v in position_mm])
        if component is None:
            raise EngineeringError("INSERT_COMPONENT_FAILED", str(path))
        return {"component": component.Name2, "path": component.GetPathName(),
                "configuration": component.ReferencedConfiguration, "position_mm": position_mm}

    def drawing_view(self, document, model, orientation="*Front", x_mm=100, y_mm=150, configuration=""):
        doc = self._doc(document, 3)
        self._writable(doc)
        model = self.paths.read(model, {".sldprt", ".sldasm"})
        self.open_document(str(model), read_only=True)
        model_doc = self._doc(str(model))
        names = list(model_doc.GetModelViewNames() or ())
        aliases = {"*Front": "*Спереди", "*Back": "*Сзади", "*Left": "*Слева", "*Right": "*Справа",
                   "*Top": "*Сверху", "*Bottom": "*Снизу", "*Isometric": "*Изометрия", "*Trimetric": "*Триметрия"}
        if orientation not in names:
            orientation = aliases.get(orientation, orientation)
        if orientation not in names:
            raise EngineeringError("MODEL_VIEW_NOT_FOUND", orientation, available=names)
        if configuration and configuration not in (model_doc.GetConfigurationNames() or ()):
            raise EngineeringError("CONFIGURATION_NOT_FOUND", configuration)
        self._activate(doc)
        view = self._cast(doc, "IDrawingDoc").CreateDrawViewFromModelView3(str(model), orientation, x_mm / 1000, y_mm / 1000, 0.)
        if view is None:
            raise EngineeringError("DRAWING_VIEW_FAILED", "Check model, configuration and orientation")
        if configuration:
            view.ReferencedConfiguration = configuration
            if view.ReferencedConfiguration != configuration:
                raise EngineeringError("CONFIGURATION_FAILED", configuration, created_view=view.GetName2())
        return {"view": view.GetName2(), "outline_m": list(view.GetOutline() or ())}

    def model_views(self, document):
        doc = self._doc(document)
        if doc.GetType() == 3:
            raise EngineeringError("DOCUMENT_TYPE", "View names require a model")
        return {"model_views": list(doc.GetModelViewNames() or ())}

    def drawing_flat_view(self, document, model, orientation="*Front", x_mm=100, y_mm=150,
                          configuration="", hide_bend_lines=False, flip=False):
        doc = self._doc(document, 3)
        self._writable(doc)
        model = self.paths.read(model, {".sldprt"})
        view = self._cast(doc, "IDrawingDoc").CreateFlatPatternViewFromModelView3(str(model), configuration,
                    x_mm / 1000, y_mm / 1000, 0., hide_bend_lines, flip)
        if view is None:
            raise EngineeringError("FLAT_VIEW_FAILED", "Check saved sheet-metal model/configuration")
        return {"view": view.GetName2(), "outline_m": list(view.GetOutline() or ())}

    def drawing_views(self, document):
        doc = self._doc(document, 3)
        items = []
        for sheet_views in self._cast(doc, "IDrawingDoc").GetViews() or ():
            for view in sheet_views or ():
                items.append({"name": view.GetName2(), "type": view.Type,
                              "outline_m": list(view.GetOutline() or ())})
        return {"views": items}

    def drawing_scale(self, document, numerator=1, denominator=1):
        doc = self._doc(document, 3)
        self._writable(doc)
        sheet = self._cast(doc, "IDrawingDoc").GetCurrentSheet()
        if not sheet.SetScale(numerator, denominator, False, False):
            raise EngineeringError("SCALE_FAILED", "Sheet scale was not applied")
        return {"sheet": sheet.GetName(), "scale": [numerator, denominator]}

    def drawing_load_standard(self, document, path):
        doc = self._doc(document, 3)
        self._writable(doc)
        path = self.paths.read(path, {".sldstd"})
        if not doc.Extension.LoadDraftingStandard(str(path)):
            raise EngineeringError("DRAFTING_STANDARD_FAILED", str(path))
        return {"standard_file": str(path), "review_required": True}

    def drawing_setup_sheet(self, document, sheet_format, width_mm=420, height_mm=297,
                             numerator=1, denominator=1, property_view=""):
        doc = self._doc(document, 3)
        self._writable(doc)
        source = self.paths.read(sheet_format, {".slddrt"})
        drawing = self._cast(doc, "IDrawingDoc")
        sheet = drawing.GetCurrentSheet()
        if not drawing.SetupSheet6(sheet.GetName(), 12, 12, numerator, denominator, True, str(source),
                     width_mm / 1000, height_mm / 1000, property_view, False, .02, .005, .005, .005, 1, 1):
            raise EngineeringError("SHEET_SETUP_FAILED", "Check sheet format and sheet size")
        self.rebuild(document)
        properties = drawing.GetCurrentSheet().GetProperties2()
        return {"sheet": sheet.GetName(), "sheet_format": str(source),
                "width_mm": properties[5] * 1000, "height_mm": properties[6] * 1000,
                "first_angle": True, "scale": [numerator, denominator]}

    def drawing_note(self, document, text, x_mm, y_mm):
        doc = self._doc(document, 3)
        self._writable(doc)
        doc.ClearSelection2(True)
        note = doc.InsertNote(text)
        if note is None:
            raise EngineeringError("NOTE_FAILED", "Cannot insert drawing note")
        annotation = note.GetAnnotation()
        if not annotation.SetPosition2(x_mm / 1000, y_mm / 1000, 0.):
            raise EngineeringError("NOTE_POSITION_FAILED", "Note exists, but positioning failed")
        return {"text": text, "position_mm": [x_mm, y_mm]}

    def _drawing_view_by_name(self, doc, name):
        for sheet_views in self._cast(doc, "IDrawingDoc").GetViews() or ():
            for view in sheet_views or ():
                if view.GetName2() == name:
                    return view
        raise EngineeringError("VIEW_NOT_FOUND", name)

    def drawing_dimensions(self, document, view):
        doc = self._doc(document, 3)
        self._writable(doc)
        self._drawing_view_by_name(doc, view)
        drawing = self._cast(doc, "IDrawingDoc")
        if not drawing.ActivateView(view):
            raise EngineeringError("VIEW_ACTIVATION_FAILED", view)
        doc.ClearSelection2(True)
        if not doc.Extension.SelectByID2(view, "DRAWINGVIEW", 0., 0., 0., False, 0, self._null(), 0):
            raise EngineeringError("SELECTION_FAILED", view)
        try:
            annotations = drawing.InsertModelAnnotations3(0, 32768, False, False, False, False) or ()
        finally:
            doc.ClearSelection2(True)
        return {"inserted": len(annotations), "source": "Model dimensions marked for drawing",
                "requires_review": True}

    def drawing_bom(self, document, view, template, configuration, x_mm=20, y_mm=20, bom_type=1):
        doc = self._doc(document, 3)
        self._writable(doc)
        template = self.paths.read(template, {".sldbomtbt"})
        target = self._drawing_view_by_name(doc, view)
        table = target.InsertBomTable4(False, x_mm / 1000, y_mm / 1000, 1, bom_type,
                                      configuration, str(template), False, 0, True)
        if table is None:
            raise EngineeringError("BOM_FAILED", "Check view and BOM template")
        return {"inserted": True, "view": view, "bom_type": bom_type,
                "note": "SOLIDWORKS BOM; ESKD specification requires an approved custom template/workflow."}

    def drawing_export(self, document, path, overwrite=False):
        doc = self._doc(document, 3)
        self._activate(doc)
        target = self.paths.output(path, {".pdf", ".dxf", ".dwg"}, overwrite)
        export = None
        if target.suffix.lower() == ".pdf":
            export = self.app.GetExportFileData(1)
            export.ViewPdfAfterSaving = False
            from win32com.client import VARIANT
            import pythoncom
            sheet_names = self._cast(doc, "IDrawingDoc").GetSheetNames() or ()
            # Explicitly enumerate every sheet. SW 2025 rejects SetSheets(ExportAllSheets, ...)
            # on this API path, but accepts ExportSpecifiedSheets with a typed array.
            if not sheet_names or not export.SetSheets(3, VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, list(sheet_names))):
                raise EngineeringError("PDF_SHEETS_FAILED", "Cannot select all drawing sheets")
        return self._save(doc, target, export, copy=True)

    def model_export_step(self, document, path, overwrite=False):
        doc = self._doc(document)
        if doc.GetType() not in (1, 2):
            raise EngineeringError("DOCUMENT_TYPE", "STEP export requires part or assembly")
        self._activate(doc)
        return self._save(doc, self.paths.output(path, {".step", ".stp"}, overwrite), copy=True)

    def bracket_package(self, **args):
        from .workflows import bracket_package
        return bracket_package(self, **args)


OPERATIONS = {
    "connect", "status", "documents", "document_info", "new_document", "open_document", "save_document",
    "close_document", "features", "rebuild", "configurations", "activate_configuration", "properties",
    "set_properties", "set_dimension", "mass_properties", "set_material", "sketch_polyline", "sketch_3d",
    "sheet_base_flange", "sheet_info", "sheet_set_parameters", "set_suppression", "sheet_export_dxf",
    "weldment_create", "cut_list_read", "cut_list_update", "cut_list_csv", "library_index", "library_search",
    "assembly_components", "assembly_insert", "drawing_view", "drawing_flat_view", "drawing_views", "drawing_scale",
    "drawing_note", "drawing_dimensions", "drawing_bom", "drawing_export", "model_export_step", "bracket_package", "model_views",
    "library_configurations", "drawing_load_standard", "drawing_setup_sheet", "sketch_circles", "cut_extrude", "feature_dimensions",
}
