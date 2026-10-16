import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from pydantic import ValidationError

from solidworks_mcp.actor import StaActor
from solidworks_mcp.config import Settings, Paths
from solidworks_mcp.errors import EngineeringError
from solidworks_mcp.library import Library
from solidworks_mcp.lock import WorkspaceLock
from solidworks_mcp import schemas as s


class PathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "work"
        self.root.mkdir()
        self.library = self.base / "library"
        self.library.mkdir()
        self.settings = Settings(workspace=self.root, library_roots=[self.library])
        self.paths = Paths(self.settings)

    def test_outputs_resolve_inside_workspace(self):
        self.assertEqual(self.paths.output("a/b.dxf", {".dxf"}), self.root / "a" / "b.dxf")

    def test_traversal_denied(self):
        with self.assertRaises(EngineeringError) as caught:
            self.paths.output("../escape.dxf", {".dxf"})
        self.assertEqual(caught.exception.code, "PATH_DENIED")

    def test_prefix_sibling_denied(self):
        with self.assertRaises(EngineeringError):
            self.paths.output(str(self.base / "work-other" / "escape.dxf"), {".dxf"})

    def test_reserved_state_path_denied(self):
        with self.assertRaises(EngineeringError):
            self.paths.output(".state/audit.jsonl", {".jsonl"})

    def test_overwrite_must_be_explicit(self):
        path = self.root / "a.dxf"
        path.write_text("original")
        with self.assertRaises(EngineeringError):
            self.paths.output("a.dxf", {".dxf"})
        self.assertEqual(path.read_text(), "original")
        self.assertEqual(self.paths.output("a.dxf", {".dxf"}, True), path)

    def test_extension_allowlist(self):
        with self.assertRaises(EngineeringError):
            self.paths.output("a.exe", {".dxf"})

    def test_library_read_allowed_but_write_denied(self):
        path = self.library / "part.sldprt"
        path.write_bytes(b"x")
        self.assertEqual(self.paths.read(str(path)), path)
        with self.assertRaises(EngineeringError):
            self.paths.output(str(path), {".sldprt"}, True)

    def test_symlink_escape(self):
        target = self.base / "outside"
        target.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit directory symlinks")
        with self.assertRaises(EngineeringError):
            self.paths.output("link/a.dxf", {".dxf"})

    def test_reindex_search_literal_unicode_and_remove_stale(self):
        (self.library / "Кронштейн100%_x.sldprt").write_bytes(b"part")
        (self.library / "other.sldprt").write_bytes(b"part")
        (self.library / "ignored.exe").write_bytes(b"exe")
        library = Library(self.settings, self.paths)
        self.assertEqual(library.reindex()["indexed"], 2)
        found = library.search("КРОНШТЕЙН100%_", "part")
        self.assertEqual(len(found["items"]), 1)
        (self.library / "other.sldprt").unlink()
        self.assertEqual(library.reindex()["indexed"], 1)

    def test_missing_library_does_not_erase_index(self):
        (self.library / "keep.sldprt").write_bytes(b"part")
        library = Library(self.settings, self.paths)
        library.reindex()
        self.settings.library_roots.append(self.base / "missing")
        with self.assertRaises(EngineeringError):
            library.reindex()
        self.assertEqual(len(library.search()["items"]), 1)

    def test_config_relative_to_config_file(self):
        config = self.base / "settings.toml"
        config.write_text("workspace='work'\nlibrary_roots=['library']", encoding="utf-8")
        settings = Settings.load(str(config))
        self.assertEqual(settings.workspace, self.root)
        self.assertEqual(settings.library_roots, [self.library])

    def test_config_unknown_fields_fail(self):
        with self.assertRaises(ValidationError):
            Settings.model_validate({"workspce": "typo"})

    def test_workspace_lock_excludes_second_operation_and_releases(self):
        with WorkspaceLock(self.root):
            with self.assertRaises(EngineeringError):
                with WorkspaceLock(self.root):
                    pass
        with WorkspaceLock(self.root):
            pass


class SchemaTests(unittest.TestCase):
    def test_unknown_arguments_fail(self):
        with self.assertRaises(ValidationError):
            s.Connect.model_validate({"execute_macro": "bad"})

    def test_thickness_positive_and_finite(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                s.BaseFlange(document="x", sketch="sketch", thickness_mm=value, bend_radius_mm=2)

    def test_kfactor_range(self):
        with self.assertRaises(ValidationError):
            s.BaseFlange(document="x", sketch="s", thickness_mm=2, bend_radius_mm=2, k_factor=1.1)

    def test_sketch_degeneracy(self):
        for points, closed in (([(0, 0), (0, 0)], False), ([(0, 0), (1, 0)], True)):
            with self.assertRaises(ValidationError):
                s.Sketch(document="x", plane="p", points_mm=points, closed=closed)

    def test_3d_sketch_zero_segment(self):
        with self.assertRaises(ValidationError):
            s.Sketch3D(document="x", segments_mm=[((0, 0, 0), (0, 0, 0))])

    def test_weldment_empty_and_duplicate_groups(self):
        for groups in ([[]], [["Line1@Sketch"], ["Line1@Sketch"]]):
            with self.assertRaises(ValidationError):
                s.Weldment(document="x", profile="p.sldlfp", groups=groups)

    def test_bracket_geometric_limits(self):
        with self.assertRaises(ValidationError):
            s.Bracket(leg_mm=2)
        with self.assertRaises(ValidationError):
            s.Bracket(name="../escape")
        self.assertEqual(s.Bracket().thickness_mm, 2)

    def test_circle_radius_positive(self):
        with self.assertRaises(ValidationError):
            s.SketchCircles(document="x", plane="front", circles_mm=[(10, 20, 0)])

    def test_cut_mode_and_depth(self):
        with self.assertRaises(ValidationError):
            s.CutExtrude(document="x", sketch="s", mode="unbounded")
        with self.assertRaises(ValidationError):
            s.CutExtrude(document="x", sketch="s", depth_mm=-10)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audit = Path(self.temp.name) / "audit.jsonl"
        self.calls = []
        self.thread_ids = []
        self.gate = threading.Event()
        owner = self

        class Backend:
            def execute(self, operation, args):
                owner.thread_ids.append(threading.get_ident())
                if operation == "block":
                    owner.gate.wait(3)
                if operation == "error":
                    raise EngineeringError("EXPECTED", "test error")
                owner.calls.append(operation)
                return {"operation": operation}

        self.actor = StaActor(Backend, self.audit, use_com=False)
        self.addCleanup(self.actor.close)
        self.addCleanup(self.gate.set)

    def test_serial_same_thread_and_audit(self):
        first = self.actor.submit("one", {})
        second = self.actor.submit("two", {})
        first.future.result(2)
        second.future.result(2)
        self.assertEqual(self.calls, ["one", "two"])
        self.assertEqual(len(set(self.thread_ids)), 1)
        self.assertNotEqual(self.thread_ids[0], threading.get_ident())
        events = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual(sum(e["event"] == "finished" for e in events), 2)

    def test_timeout_does_not_repeat_or_cancel_mutation(self):
        result = asyncio.run(self.actor.invoke("block", {}, .02))
        self.assertEqual(result["state"], "pending")
        self.assertIn(self.actor.status(result["job_id"])["state"], ("queued", "running"))
        self.gate.set()
        deadline = time.monotonic() + 2
        while self.actor.status(result["job_id"])["state"] == "running" and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.actor.status(result["job_id"])["state"], "succeeded")
        self.assertEqual(self.calls, ["block"])

    def test_cancelled_client_does_not_cancel_mutation(self):
        async def scenario():
            task = asyncio.create_task(self.actor.invoke("block", {}, 2))
            await asyncio.sleep(.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.gate.set()
            await asyncio.sleep(.05)
        asyncio.run(scenario())
        self.assertEqual(self.calls, ["block"])

    def test_errors_have_stable_code(self):
        result = self.actor.submit("error", {}).future.result(2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "EXPECTED")

    def test_waiting_for_another_process_does_not_block_polling_or_repeat(self):
        root = Path(self.temp.name)
        self.actor.operation_root = root
        with WorkspaceLock(root):
            result = asyncio.run(self.actor.invoke("one", {}, .1))
            self.assertEqual(result["state"], "pending")
            self.assertEqual(self.actor.status(result["job_id"])["state"], "waiting_for_lock")
            self.assertEqual(self.calls, [])
        job = self.actor.jobs[result["job_id"]]
        self.assertTrue(job.future.result(2)["ok"])
        self.assertEqual(self.calls, ["one"])

    def test_failed_operation_releases_shared_lock(self):
        self.actor.operation_root = Path(self.temp.name)
        self.assertFalse(self.actor.submit("error", {}).future.result(2)["ok"])
        self.assertTrue(self.actor.submit("one", {}).future.result(2)["ok"])

    def test_completion_audit_lock_timeout_keeps_worker_running(self):
        original_audit = self.actor._audit

        def audit(event, job):
            if event == "finished" and job.operation == "one":
                raise EngineeringError("WORKSPACE_IN_USE", "Audit lock timed out")
            original_audit(event, job)

        self.actor._audit = audit
        with self.assertLogs("solidworks_mcp.actor", level="ERROR"):
            self.assertTrue(self.actor.submit("one", {}).future.result(2)["ok"])
        self.assertTrue(self.actor.submit("two", {}).future.result(2)["ok"])
        self.assertEqual(self.calls, ["one", "two"])

    def test_poll_unknown_job(self):
        with self.assertRaises(EngineeringError):
            self.actor.status("unknown")

    def test_closed_worker_rejects_work(self):
        self.actor.close()
        with self.assertRaises(EngineeringError):
            self.actor.submit("one", {})


if __name__ == "__main__":
    unittest.main()
