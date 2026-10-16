"""Isolated fixture: fake CAD work through the actual actor and OS locks."""
import json
import os
from pathlib import Path
import sys
import time

from solidworks_mcp.actor import StaActor

root, token = Path(sys.argv[1]), sys.argv[2]


class Backend:
    def execute(self, operation, args):
        for phase in ("start", "end"):
            with (root / "trace.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"phase": phase, "token": token, "pid": os.getpid()}) + "\n")
            if phase == "start":
                time.sleep(.1)
        return {"token": token, "unicode_payload": "Кронштейн" * 3000}


actor = StaActor(Backend, root / ".state" / "audit.jsonl", use_com=False, operation_root=root)
try:
    job = actor.submit("fake_cad", {"token": token})
    (root / f"ready-{token}").write_text("ready")
    result = job.future.result(10)
    if not result["ok"]:
        raise RuntimeError(result["error"])
finally:
    actor.close()
