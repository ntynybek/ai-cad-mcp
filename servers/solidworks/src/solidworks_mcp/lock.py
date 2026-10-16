"""OS-released locks for operations shared by several MCP client processes."""
import errno
import os
from pathlib import Path
import time

from .errors import EngineeringError


class FileLock:
    def __init__(self, path, *, timeout=0.0, on_wait=None):
        self.path = Path(path)
        self.timeout = timeout
        self.on_wait = on_wait
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if os.fstat(self.stream.fileno()).st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        started = time.monotonic()
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if self.timeout is not None and time.monotonic() - started >= self.timeout:
                        raise EngineeringError("WORKSPACE_IN_USE", "Another operation owns the workspace lock",
                                               lock=str(self.path)) from exc
                    if self.on_wait:
                        self.on_wait()
                    time.sleep(0.05)
        except BaseException:
            self.stream.close()
            self.stream = None
            raise
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


class WorkspaceLock(FileLock):
    """Protect one complete CAD operation, not the lifetime of an MCP connection.

    timeout=None waits on the worker thread and can pump STA messages while waiting.
    The filename is kept compatible with v0.1.0's lifetime lock during migration.
    """

    def __init__(self, root, *, timeout=0.0, on_wait=None):
        super().__init__(root / ".state" / "server.lock", timeout=timeout, on_wait=on_wait)
