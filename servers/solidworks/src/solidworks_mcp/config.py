from pathlib import Path
import os
import tomllib

from pydantic import BaseModel, ConfigDict, Field

from .errors import EngineeringError


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace: Path = Path("workspace")
    read_roots: list[Path] = Field(default_factory=list)
    library_roots: list[Path] = Field(default_factory=list)
    part_template: str = ""
    assembly_template: str = ""
    drawing_template: str = ""
    drawing_sheet_format: str = ""
    drawing_width_mm: float = Field(default=420, gt=0, le=5000)
    drawing_height_mm: float = Field(default=297, gt=0, le=5000)
    prog_id: str = "SldWorks.Application"
    timeout_seconds: float = Field(default=120, ge=1, le=3600)
    standard: str = "ESKD"

    @classmethod
    def load(cls, filename: str | None = None):
        name = filename or os.environ.get("SW_MCP_CONFIG", "solidworks-mcp.local.toml")
        source = Path(name).resolve()
        if not source.exists() and (filename or os.environ.get("SW_MCP_CONFIG")):
            raise EngineeringError("CONFIG_NOT_FOUND", f"Configuration not found: {source}")
        data = tomllib.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
        obj = cls.model_validate(data)
        base = source.parent if source.exists() else Path.cwd()
        obj.workspace = (base / obj.workspace).resolve()
        obj.read_roots = [(base / p).resolve() for p in obj.read_roots]
        obj.library_roots = [(base / p).resolve() for p in obj.library_roots]
        for field in ("part_template", "assembly_template", "drawing_template", "drawing_sheet_format"):
            if value := getattr(obj, field):
                setattr(obj, field, str((base / value).resolve()))
        return obj


class Paths:
    """Canonical path policy, including junction/symlink resolution."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.workspace.resolve()
        self.read_roots = [self.root, *settings.read_roots, *settings.library_roots]
        self.read_roots += [Path(p).resolve().parent for p in
                            (settings.part_template, settings.assembly_template, settings.drawing_template,
                             settings.drawing_sheet_format) if p]

    @staticmethod
    def inside(path: Path, roots: list[Path]):
        return any(path.is_relative_to(root.resolve()) for root in roots)

    def read(self, value: str, suffixes: set[str] | None = None) -> Path:
        path = (self.root / value).resolve()
        if not self.inside(path, self.read_roots):
            raise EngineeringError("PATH_DENIED", "Input path is outside configured roots", path=str(path))
        if not path.is_file():
            raise EngineeringError("FILE_NOT_FOUND", f"Input file not found: {path}")
        self.check_suffix(path, suffixes)
        return path

    def output(self, value: str, suffixes: set[str], overwrite: bool = False) -> Path:
        path = (self.root / value).resolve()
        if not self.inside(path, [self.root]):
            raise EngineeringError("PATH_DENIED", "Output must be inside workspace", path=str(path))
        if any(part.startswith(".") for part in path.relative_to(self.root).parts):
            raise EngineeringError("PATH_DENIED", "Reserved internal output path")
        self.check_suffix(path, suffixes)
        if path.exists() and not overwrite:
            raise EngineeringError("FILE_EXISTS", "Destination exists; choose another name or overwrite=true",
                                   path=str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def check_suffix(path, suffixes):
        if suffixes and path.suffix.lower() not in suffixes:
            raise EngineeringError("FILE_TYPE", f"Expected one of: {', '.join(sorted(suffixes))}")
