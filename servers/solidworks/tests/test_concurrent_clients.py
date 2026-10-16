import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from solidworks_mcp.lock import WorkspaceLock

REPO = Path(__file__).resolve().parents[1]


class ConcurrentProcessTests(unittest.TestCase):
    def test_two_processes_serialize_cad_and_write_complete_shared_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
            children = []
            try:
                with WorkspaceLock(root):
                    for token in ("chat", "cowork"):
                        child = subprocess.Popen(
                            [sys.executable, str(REPO / "tests/helpers/operation_process.py"), str(root), token],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8")
                        children.append(child)
                    deadline = time.monotonic() + 8
                    while not all((root / f"ready-{token}").exists() for token in ("chat", "cowork")):
                        if any(child.poll() is not None for child in children):
                            self.fail("Fixture exited before waiting for the operation lock")
                        if time.monotonic() >= deadline:
                            self.fail("Fixtures did not reach the shared lock")
                        time.sleep(.02)
                    self.assertFalse((root / "trace.jsonl").exists())
                for child in children:
                    out, error = child.communicate(timeout=10)
                    self.assertEqual(child.returncode, 0, out + error)
                trace = [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()]
                self.assertEqual([row["phase"] for row in trace], ["start", "end", "start", "end"])
                self.assertEqual(trace[0]["token"], trace[1]["token"])
                self.assertEqual(trace[2]["token"], trace[3]["token"])
                self.assertNotEqual(trace[0]["pid"], trace[2]["pid"])
                audit = [json.loads(line) for line in (root / ".state/audit.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(audit), 6)
                finished = [row for row in audit if row["event"] == "finished"]
                self.assertEqual(len(finished), 2)
                self.assertTrue(all(row["state"] == "succeeded" for row in finished))
                self.assertTrue(all(row["result"]["unicode_payload"] == "Кронштейн" * 3000 for row in finished))
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.communicate(timeout=5)

    @unittest.skipUnless(sys.platform == "win32", "Real server initializes a Windows COM apartment")
    def test_three_claude_style_stdio_clients_start_and_discover_while_cad_is_busy(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def scenario(root):
            config = root / "settings.toml"
            config.write_text("workspace = 'work'\ntimeout_seconds = 1\n", encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
            params = StdioServerParameters(command=sys.executable,
                                          args=[str(REPO / "tools/serve.py"), "--config", str(config)], env=env)
            blocker = WorkspaceLock(root / "work")
            blocker.__enter__()
            held = True
            try:
                async with AsyncExitStack() as stack:
                    sessions = []
                    for _ in range(3):
                        read, write = await stack.enter_async_context(stdio_client(params))
                        session = await stack.enter_async_context(ClientSession(read, write))
                        initialized = await session.initialize()
                        self.assertEqual(initialized.serverInfo.name, "solidworks-engineering")
                        sessions.append(session)
                    # Match Claude's discovery traffic; no SW instance is started or modified.
                    for session in sessions:
                        tools, prompts, resources = await asyncio.gather(
                            session.list_tools(), session.list_prompts(), session.list_resources())
                        self.assertEqual(len(tools.tools), 50)
                        self.assertEqual(len(prompts.prompts), 1)
                        self.assertEqual(len(resources.resources), 1)
                    pending = await asyncio.gather(*[
                        session.call_tool("sw_status", {"request": {}}) for session in sessions])
                    job_ids = []
                    for session, response in zip(sessions, pending):
                        self.assertFalse(response.isError)
                        data = response.structuredContent
                        self.assertEqual(data["state"], "pending")
                        job_ids.append(data["job_id"])
                        status = await session.call_tool("sw_job_status", {"job_id": data["job_id"]})
                        self.assertEqual(status.structuredContent["state"], "waiting_for_lock")
                    blocker.__exit__(None, None, None)
                    held = False
                    for session, job_id in zip(sessions, job_ids):
                        deadline = time.monotonic() + 5
                        while True:
                            status = await session.call_tool("sw_job_status", {"job_id": job_id})
                            data = status.structuredContent
                            if data["state"] in ("succeeded", "failed"):
                                self.assertEqual(data["state"], "succeeded", data)
                                self.assertEqual(data["result"]["server_version"], "0.1.1")
                                break
                            if time.monotonic() >= deadline:
                                self.fail("Queued status operation did not resume after lock release")
                            await asyncio.sleep(.02)
            finally:
                if held:
                    blocker.__exit__(None, None, None)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))


if __name__ == "__main__":
    unittest.main()
