from contextlib import asynccontextmanager
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations, CallToolResult, TextContent

from .actor import StaActor
from .backend import SolidWorks
from .errors import EngineeringError
from . import schemas as s
from . import __version__

# Every public operation has a strict, dedicated input model.
TOOLS = [
    ("sw_connect", "connect", s.Connect, False, "Подключиться к SolidWorks. launch=true разрешает запуск приложения."),
    ("sw_status", "status", s.Empty, True, "Состояние подключения и версия SolidWorks."),
    ("sw_documents", "documents", s.Empty, True, "Открытые документы с точными именами, путями и флагом изменений."),
    ("sw_document_info", "document_info", s.Document, True, "Тип, путь и конфигурация указанного документа."),
    ("sw_new_document", "new_document", s.New, False, "Создать деталь, сборку или чертёж из разрешённого шаблона."),
    ("sw_open_document", "open_document", s.Open, False, "Открыть файл; по умолчанию только чтение."),
    ("sw_save_document", "save_document", s.Save, False, "Сохранить модель/чертёж в рабочую папку. Перезапись только явно."),
    ("sw_close_document", "close_document", s.Close, False, "Закрыть документ; потеря изменений требует discard_changes=true."),
    ("sw_features", "features", s.Features, True, "Дерево элементов, включая плоскости, эскизы, гибы и развёртки."),
    ("sw_rebuild", "rebuild", s.Document, False, "Перестроить указанный документ и проверить код результата."),
    ("sw_configurations", "configurations", s.Document, True, "Получить конфигурации и активную конфигурацию."),
    ("sw_activate_configuration", "activate_configuration", s.Configuration, False, "Включить существующую конфигурацию."),
    ("sw_properties", "properties", s.PropertyRead, True, "Свойства документа/конфигурации с вычисленными значениями."),
    ("sw_set_properties", "set_properties", s.Properties, False, "Записать свойства, в том числе Обозначение и Наименование для ЕСКД."),
    ("sw_set_dimension", "set_dimension", s.Dimension, False, "Изменить именованный размер в текущей конфигурации и перестроить."),
    ("sw_feature_dimensions", "feature_dimensions", s.FeatureDimensions, True, "Получить имена, типы и значения размеров элемента для последующего редактирования."),
    ("sw_mass_properties", "mass_properties", s.Document, True, "Масса, объём, площадь, центр масс. SI, координаты в мм."),
    ("sw_set_material", "set_material", s.Material, False, "Назначить материал из .sldmat и проверить результат."),
    ("sw_sketch_polyline", "sketch_polyline", s.Sketch, False, "Создать открытый/замкнутый эскиз на именованной плоскости. Размеры в мм."),
    ("sw_sketch_3d", "sketch_3d", s.Sketch3D, False, "Создать 3D эскиз из отрезков для каркаса. Вернуть имена сегментов."),
    ("sw_sketch_circles", "sketch_circles", s.SketchCircles, False, "Создать окружности на именованной плоскости, центры и радиусы в мм."),
    ("sw_cut_extrude", "cut_extrude", s.CutExtrude, False, "Вырез по эскизу: насквозь/на глубину, в одном/двух направлениях; нормальный вырез листа."),
    ("sw_sheet_base_flange", "sheet_base_flange", s.BaseFlange, False, "Создать листовое основание по эскизу, задать толщину, радиус, K-фактор."),
    ("sw_sheet_info", "sheet_info", s.Document, True, "Толщины, радиусы, K-факторы и элементы развёртки."),
    ("sw_sheet_set_parameters", "sheet_set_parameters", s.SheetParameters, False, "Изменить толщину, радиус и K-фактор листового элемента."),
    ("sw_set_suppression", "set_suppression", s.Suppression, False, "Погасить/включить элемент, например FlatPattern, в текущей конфигурации."),
    ("sw_sheet_export_dxf", "sheet_export_dxf", s.FlatExport, False, "Экспорт развёртки DXF/DWG с линиями гиба. Модель должна быть сохранена."),
    ("sw_weldment_create", "weldment_create", s.Weldment, False, "Создать профильные элементы по группам сегментов и профилю .sldlfp."),
    ("sw_cut_list", "cut_list_read", s.Document, True, "Прочитать тела и свойства списка резки, без обновления."),
    ("sw_cut_list_update", "cut_list_update", s.Document, False, "Обновить список резки и прочитать результат."),
    ("sw_cut_list_csv", "cut_list_csv", s.Save, False, "Экспорт списка резки в CSV UTF-8 с разделителем ;."),
    ("sw_library_index", "library_index", s.Empty, False, "Переиндексировать разрешённые папки библиотеки без запуска SolidWorks."),
    ("sw_library_search", "library_search", s.LibrarySearch, True, "Найти детали, сборки, профили и шаблоны в индексе имён файлов."),
    ("sw_library_configurations", "library_configurations", s.LibraryFile, True, "Прочитать конфигурации файла, включая размеры профиля .sldlfp."),
    ("sw_assembly_components", "assembly_components", s.Document, True, "Получить компоненты сборки, пути и конфигурации."),
    ("sw_assembly_insert", "assembly_insert", s.InsertComponent, False, "Вставить библиотечную деталь/сборку. Положение в мм; сопряжения отдельно."),
    ("sw_drawing_view", "drawing_view", s.DrawingView, False, "Добавить вид сохранённой модели. Стандартные имена *Front, *Top, *Isometric."),
    ("sw_model_views", "model_views", s.Document, True, "Получить точные локализованные имена стандартных и пользовательских видов модели."),
    ("sw_drawing_flat_view", "drawing_flat_view", s.DrawingFlat, False, "Добавить вид развёртки листовой детали."),
    ("sw_drawing_views", "drawing_views", s.Document, True, "Получить виды на всех листах и габариты видов."),
    ("sw_drawing_scale", "drawing_scale", s.DrawingScale, False, "Установить масштаб текущего листа."),
    ("sw_drawing_load_standard", "drawing_load_standard", s.DraftingStandard, False, "Загрузить утверждённые настройки оформления из .sldstd в чертёж."),
    ("sw_drawing_setup_sheet", "drawing_setup_sheet", s.DrawingSheet, False, "Установить формат листа .slddrt, размеры, масштаб и проекцию по первому углу."),
    ("sw_drawing_note", "drawing_note", s.DrawingNote, False, "Добавить технический текст в координатах листа, мм."),
    ("sw_drawing_dimensions", "drawing_dimensions", s.DrawingDimensions, False, "Перенести размеры модели, помеченные для чертежа, в выбранный вид."),
    ("sw_drawing_bom", "drawing_bom", s.DrawingBom, False, "Добавить BOM по шаблону. Это не автоматическая спецификация ЕСКД."),
    ("sw_drawing_export", "drawing_export", s.Save, False, "Экспорт всех листов чертежа в PDF либо DXF/DWG."),
    ("sw_model_export_step", "model_export_step", s.Save, False, "Экспорт всей модели в STEP без переименования исходного документа."),
    ("sw_bracket_package", "bracket_package", s.Bracket, False, "Сквозной пробный сценарий: U-кронштейн → SLDPRT, STEP, DXF, SLDDRW, PDF и манифест."),
]


def result_error(error):
    return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(error, ensure_ascii=False))])


def result_success(value):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                          structuredContent=value)


def create_server(settings, actor=None):
    owns_actor = actor is None
    worker = actor or StaActor(lambda: SolidWorks(settings), settings.workspace / ".state" / "audit.jsonl",
                               operation_root=settings.workspace)

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            if owns_actor:
                worker.close()

    mcp = FastMCP("solidworks-engineering", lifespan=lifespan,
                  instructions="All lengths in mm, angles in degrees. Select documents explicitly. "
                  "For TIMEOUT_PENDING poll sw_job_status, never repeat a mutation. "
                  "Drawing output needs engineering review and an approved ESKD template. "
                  "Use local stdio transport. Multiple clients share a per-workspace operation lock; "
                  "job IDs belong to the current MCP process.")

    def register(name, operation, schema, read_only, description):
        async def invoke(request):
            try:
                result = await worker.invoke(operation, request.model_dump(), settings.timeout_seconds)
                if not result["ok"] and result.get("state") != "pending":
                    return result_error(result)
                return result_success(result)
            except EngineeringError as exc:
                return result_error(exc.as_dict())
        invoke.__name__ = name
        invoke.__annotations__ = {"request": schema, "return": CallToolResult}
        mcp.add_tool(invoke, name=name, description=description, annotations=ToolAnnotations(
            readOnlyHint=read_only, destructiveHint=not read_only, idempotentHint=read_only, openWorldHint=False))

    for spec in TOOLS:
        register(*spec)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def sw_job_status(job_id: str) -> CallToolResult:
        """Check a queued/running/completed operation without entering the COM queue."""
        try:
            return result_success(worker.status(job_id))
        except EngineeringError as exc:
            return result_error(exc.as_dict())

    @mcp.resource("solidworks://capabilities")
    def capabilities() -> str:
        return json.dumps({"server_version": __version__, "target_versions": [2024, 2025, 2026], "standard": settings.standard,
                           "transport": "stdio", "units": {"length": "mm", "angle": "deg", "mass": "kg"},
                           "tools": [{"name": row[0], "read_only": row[3], "description": row[4]} for row in TOOLS],
                           "limitations": ["No general macro/code execution", "No automatic production release",
                                           "ESKD appearance comes from approved templates",
                                           "No automatic feature-library reference mapping or assembly mates"]}, ensure_ascii=False)

    @mcp.prompt()
    def sheet_metal_package(task: str) -> str:
        """Порядок подготовки листовой детали и комплекта документации."""
        return (f"Задача: {task}\nПодключись к SW, проверь документы и шаблоны. Согласуй геометрию, материал, "
                "толщину, радиусы и правила гибки. Для пробного U-кронштейна используй sw_bracket_package. "
                "Для существующей модели проверь sw_sheet_info, свойства и конфигурацию, сохрани модель, "
                "экспортируй развёртку. Создай чертёж из утверждённого шаблона ЕСКД, размести виды и размеры. "
                "Проверь компоновку, основную надпись, допуски и технические требования до выдачи в производство.")

    return mcp
