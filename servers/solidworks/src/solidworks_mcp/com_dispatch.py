"""Late-bound SW dispatch that preserves zero-argument methods and child wrappers.

Some SW automation objects answer PROPERTYGET for methods such as GetTitle.
Explicit method flags prevent pywin32 from invoking them while fetching the attribute.
No makepy files are generated in the user's Python installation.
"""
from win32com.client import dynamic

METHODS = frozenset("""
RevisionNumber GetExecutablePath GetUserPreferenceStringValue GetDocuments ActivateDoc3 GetTitle GetPathName GetType GetSaveFlag
IsOpenedReadOnly GetActiveSketch2 NewDocument OpenDoc6 CloseDoc SaveAs FirstFeature GetFirstSubFeature
GetNextFeature GetNextSubFeature GetTypeName2 ForceRebuild3 GetConfigurationNames ShowConfiguration2
GetNames Get6 Add3 Parameter SetSystemValue3 CreateMassProperty SetMaterialPropertyName2 GetMaterialPropertyName2
SelectByID2 Insert3DSketch InsertSketch CreateLine GetSketchSegments GetName FeatureByName Select2
CreateCustomBendAllowance InsertSheetMetalBaseFlange2 GetDefinition AccessSelections ModifyDefinition
ReleaseSelectionAccess SetSuppression2 ExportToDWG2 GetSelectedObject6 CreateStructuralMemberGroup
InsertStructuralWeldment4 GetSpecificFeature2 UpdateCutList GetBodyCount GetBodies GetComponents
GetSuppression AddComponent5 CreateDrawViewFromModelView3 GetName2 GetOutline CreateFlatPatternViewFromModelView3
GetViews GetCurrentSheet SetScale InsertNote GetAnnotation SetPosition2 ActivateView InsertModelAnnotations3
InsertBomTable4 GetExportFileData SetSheets ClearSelection2 GetProperties2 LoadDraftingStandard
CreateDefinition CreateFeature Initialize
GetModelViewNames GetSheetNames InsertStructuralWeldment5
SetupSheet6
IsWeldment InsertWeldmentFeature GetAutomaticCutList SetAutomaticCutList
CreateCircleByRadius FeatureCut4 GetFirstDisplayDimension GetNextDisplayDimension GetDimension2 GetSystemValue3
""".split())


class SwDispatch(dynamic.CDispatch):
    def __getattr__(self, name):
        if name in METHODS and name not in self._olerepr_.mapFuncs:
            self._FlagAsMethod(name)
        return super().__getattr__(name)

    def _wrap_dispatch_(self, obj, userName=None, returnCLSID=None):
        return dispatch(obj, userName)


def dispatch(obj, name=None):
    return dynamic.Dispatch(obj, userName=name, createClass=SwDispatch)
