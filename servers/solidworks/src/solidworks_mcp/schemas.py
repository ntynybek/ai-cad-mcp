from typing import Annotated, Literal
import math

from pydantic import BaseModel, ConfigDict, Field, model_validator

Positive = Annotated[float, Field(gt=0, le=100000, allow_inf_nan=False)]
Coordinate = Annotated[float, Field(ge=-1000000, le=1000000, allow_inf_nan=False)]
Point = tuple[Coordinate, Coordinate, Coordinate]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_max_length=4096)


class Empty(Request):
    pass


class Connect(Request):
    launch: bool = False
    visible: bool = True


class Document(Request):
    document: str = Field(min_length=1, description="Exact open document title or absolute full path")


class New(Request):
    kind: Literal["part", "assembly", "drawing"]
    template: str = ""


class Open(Request):
    path: str
    read_only: bool = True
    configuration: str = ""


class Save(Document):
    path: str
    overwrite: bool = False


class Close(Document):
    discard_changes: bool = False


class Features(Document):
    limit: int = Field(default=500, ge=1, le=5000)


class Configuration(Document):
    configuration: str = Field(min_length=1)


class PropertyRead(Document):
    configuration: str = ""


class Properties(PropertyRead):
    values: dict[str, str] = Field(min_length=1, max_length=100)


class Dimension(Document):
    name: str = Field(min_length=1, description="Fully qualified dimension, e.g. D1@Sketch1")
    value: float = Field(allow_inf_nan=False)
    unit: Literal["mm", "deg", "scalar"] = "mm"


class Material(PropertyRead):
    database: str
    material: str = Field(min_length=1)


class Sketch(Document):
    plane: str = Field(min_length=1, description="Exact reference plane name; use sw_features to discover")
    points_mm: list[tuple[Coordinate, Coordinate]] = Field(min_length=2, max_length=1000)
    closed: bool = False
    name: str = ""

    @model_validator(mode="after")
    def geometry(self):
        if self.closed and len(self.points_mm) < 3:
            raise ValueError("Closed sketch requires at least three points")
        pairs = list(zip(self.points_mm, self.points_mm[1:]))
        if self.closed:
            pairs.append((self.points_mm[-1], self.points_mm[0]))
        if any(math.dist(a, b) < 1e-7 for a, b in pairs):
            raise ValueError("Zero length sketch segment")
        return self


class Sketch3D(Document):
    segments_mm: list[tuple[Point, Point]] = Field(min_length=1, max_length=1000)
    name: str = ""

    @model_validator(mode="after")
    def geometry(self):
        if any(math.dist(a, b) < 1e-7 for a, b in self.segments_mm):
            raise ValueError("Zero length 3D segment")
        return self


class SketchCircles(Document):
    plane: str = Field(min_length=1)
    circles_mm: list[tuple[Coordinate, Coordinate, Positive]] = Field(min_length=1, max_length=500)
    name: str = ""


class CutExtrude(Document):
    sketch: str = Field(min_length=1)
    mode: Literal["through_all", "blind"] = "through_all"
    depth_mm: Positive = 10
    reverse: bool = False
    both_directions: bool = False
    normal_cut: bool = True


class FeatureDimensions(Document):
    feature: str = Field(min_length=1)


class BaseFlange(Document):
    sketch: str = Field(min_length=1)
    thickness_mm: Positive
    bend_radius_mm: Positive
    depth_mm: Positive = 100
    k_factor: float = Field(default=0.5, ge=0, le=1)
    reverse: bool = False


class SheetParameters(Document):
    feature: str
    thickness_mm: Positive
    bend_radius_mm: Positive
    k_factor: float = Field(ge=0, le=1)


class Suppression(Document):
    feature: str
    suppressed: bool


class FlatExport(Save):
    bend_lines: bool = True
    sketches: bool = False
    bounding_box: bool = False
    flat_pattern: str = ""


class Weldment(Document):
    profile: str
    profile_configuration: str = ""
    groups: list[list[str]] = Field(min_length=1, max_length=100,
                                   description="Groups of LineN@3DSketchName selection names")
    angle_deg: float = Field(default=0, ge=-360, le=360)
    gap_mm: float = Field(default=0, ge=0, le=1000)
    corner_treatment: Literal[1, 2, 3] = 1

    @model_validator(mode="after")
    def groups_valid(self):
        names = [name for group in self.groups for name in group]
        if any(not group for group in self.groups) or len(names) > 1000 or any(not s for s in names):
            raise ValueError("Groups must contain 1..1000 named segments in total")
        if len(names) != len(set(names)):
            raise ValueError("A segment cannot be used in multiple groups")
        return self


class LibrarySearch(Request):
    query: str = ""
    kind: Literal["all", "part", "assembly", "profile", "template", "feature"] = "all"
    limit: int = Field(default=50, ge=1, le=500)


class LibraryFile(Request):
    path: str


class DraftingStandard(Document):
    path: str


class InsertComponent(Document):
    path: str
    configuration: str = ""
    position_mm: Point = (0, 0, 0)


class ModelLocation(Document):
    model: str
    x_mm: Positive = 100
    y_mm: Positive = 150
    configuration: str = ""


class DrawingView(ModelLocation):
    orientation: str = "*Front"


class DrawingFlat(ModelLocation):
    hide_bend_lines: bool = False
    flip: bool = False


class DrawingScale(Document):
    numerator: Positive = 1
    denominator: Positive = 1


class DrawingSheet(DrawingScale):
    sheet_format: str
    width_mm: Positive = 420
    height_mm: Positive = 297
    property_view: str = ""


class DrawingNote(Document):
    text: str = Field(min_length=1, max_length=10000)
    x_mm: Coordinate
    y_mm: Coordinate


class DrawingDimensions(Document):
    view: str = Field(min_length=1)


class DrawingBom(Document):
    view: str
    template: str
    configuration: str
    x_mm: Coordinate = 20
    y_mm: Coordinate = 20
    bom_type: Literal[1, 2, 3] = 1


class Bracket(Request):
    name: str = Field(default="bracket", pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    web_mm: Positive = 100
    leg_mm: Positive = 40
    length_mm: Positive = 120
    thickness_mm: Positive = 2
    bend_radius_mm: Positive = 2
    k_factor: float = Field(default=0.42, ge=0, le=1)
    drawing: bool = True
    designation: str = ""
    title: str = "Кронштейн"

    @model_validator(mode="after")
    def feasible(self):
        allowance = self.bend_radius_mm + self.thickness_mm
        if self.leg_mm <= 2 * allowance or self.web_mm <= 4 * allowance:
            raise ValueError("Leg and web are too small for the selected thickness/radius")
        return self
