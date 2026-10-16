import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from solidworks_mcp.config import Settings
from solidworks_mcp.server import TOOLS, create_server
from solidworks_mcp.backend import OPERATIONS, SolidWorks


class FakeActor:
    def __init__(self):
        self.calls = []

    async def invoke(self, operation, args, timeout):
        self.calls.append((operation, args))
        return {"ok": True, "state": "succeeded", "result": {"operation": operation}, "job_id": "fake"}

    def status(self, job):
        return {"job_id": job, "state": "running"}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.actor = FakeActor()
        self.server = create_server(Settings(workspace=Path(self.temp.name)), actor=self.actor)

    def test_tools_discover_and_map_to_implementation(self):
        tools = asyncio.run(self.server.list_tools())
        self.assertEqual(len(tools), len(TOOLS) + 1)
        self.assertEqual({spec[1] for spec in TOOLS}, OPERATIONS)
        for name, operation, schema, read_only, description in TOOLS:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(SolidWorks, operation)))
                tool = next(t for t in tools if t.name == name)
                self.assertEqual(tool.annotations.readOnlyHint, read_only)
                self.assertIn("request", tool.inputSchema["properties"])
                self.assertEqual(schema.model_config["extra"], "forbid")

    def test_tool_calls_validated_then_dispatched(self):
        result = asyncio.run(self.server.call_tool("sw_connect", {"request": {"launch": False}}))
        self.assertEqual(self.actor.calls, [("connect", {"launch": False, "visible": True})])
        self.assertFalse(result.isError)

    def test_bad_input_never_reaches_worker(self):
        with self.assertRaises(Exception):
            asyncio.run(self.server.call_tool("sw_sheet_base_flange", {"request": {
                "document": "x", "sketch": "s", "thickness_mm": -1, "bend_radius_mm": 2}}))
        self.assertEqual(self.actor.calls, [])

    def test_poll_bypasses_worker_queue(self):
        result = asyncio.run(self.server.call_tool("sw_job_status", {"job_id": "test"}))
        self.assertEqual(result.structuredContent["state"], "running")
        self.assertEqual(self.actor.calls, [])

    def test_capabilities_resource(self):
        result = asyncio.run(self.server.read_resource("solidworks://capabilities"))
        data = json.loads(list(result)[0].content)
        self.assertEqual(data["target_versions"], [2024, 2025, 2026])
        self.assertEqual(data["transport"], "stdio")


if __name__ == "__main__":
    unittest.main()
