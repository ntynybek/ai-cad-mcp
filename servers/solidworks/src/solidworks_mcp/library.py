from pathlib import Path
import sqlite3
from contextlib import contextmanager

from .errors import EngineeringError

KINDS = {".sldprt": "part", ".sldasm": "assembly", ".sldlfp": "profile",
         ".prtdot": "template", ".asmdot": "template", ".drwdot": "template",
         ".slddrt": "template", ".sldbomtbt": "template", ".sldblk": "feature"}
# Library features and weldment profiles both use .sldlfp; file extension cannot distinguish them.


class Library:
    def __init__(self, settings, paths):
        self.settings, self.paths = settings, paths
        self.db = settings.workspace / ".state" / "library.sqlite3"

    @contextmanager
    def _connect(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, name TEXT, kind TEXT, size INT, mtime REAL)")
        try:
            with con:
                yield con
        finally:
            con.close()

    def reindex(self):
        rows, errors = [], []
        for root in self.settings.library_roots:
            if not root.is_dir():
                raise EngineeringError("LIBRARY_NOT_FOUND", f"Library root not found: {root}")
            for path in root.rglob("*"):
                if path.suffix.lower() not in KINDS:
                    continue
                try:
                    canonical = path.resolve()
                    if not self.paths.inside(canonical, self.settings.library_roots) or not path.is_file():
                        continue
                    stat = path.stat()
                    kind = KINDS[path.suffix.lower()]
                    if path.suffix.lower() == ".sldlfp":
                        folders = [part.casefold() for part in path.parts]
                        kind = "profile" if "weldment profiles" in folders else "feature" if "features" in folders else "profile_or_feature"
                    rows.append((str(canonical), path.stem.casefold(), kind, stat.st_size, stat.st_mtime))
                except OSError as exc:
                    errors.append({"path": str(path), "error": str(exc)})
        with self._connect() as con:
            con.execute("DELETE FROM files")
            con.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", rows)
        return {"indexed": len({row[0] for row in rows}), "errors": errors,
                "note": "Filename/path index only; metadata inside CAD files is read through SOLIDWORKS."}

    def search(self, query="", kind="all", limit=50):
        # literal substring; SQL wildcard characters supplied by a caller are escaped.
        pattern = "%" + query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._connect() as con:
            rows = con.execute("SELECT path,kind,size,mtime FROM files WHERE name LIKE ? ESCAPE '\\' "
                               "AND (?='all' OR kind=? OR (kind='profile_or_feature' AND ? IN ('profile','feature'))) "
                               "ORDER BY name,path LIMIT ?", (pattern, kind, kind, kind, limit)).fetchall()
        # Recheck canonical roots if config changed since the index was built.
        return {"items": [{"path": p, "kind": k, "size": s, "mtime": t} for p, k, s, t in rows
                          if self.paths.inside(Path(p).resolve(), self.settings.library_roots)],
                "limit": limit, "metadata_source": "filesystem"}
