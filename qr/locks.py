"""Cross-platform non-blocking process lock (one QuickReply per account)."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def account_lock_path(account_id: int) -> Path:
    return Path(tempfile.gettempdir()) / f"quickreply-account-{int(account_id)}.lock"


def session_lock_path(session_path: str) -> Path:
    digest = hashlib.sha256(str(Path(session_path).resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"quickreply-session-{digest}.lock"


class ProcessLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write("\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        try:
            handle.seek(1 if os.name == "nt" else 0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
        except OSError:
            pass
        return True

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
