"""Advisory inter-process locking for a store directory.

A :class:`FileLock` serializes critical sections between *processes* sharing
one store directory (``flock`` on POSIX, ``msvcrt.locking`` on Windows). It is
reentrant within the owning object, so ``store.lock()`` can wrap operations
that themselves acquire the lock (e.g. an auto-committing mutation).

This does not make Slowstore concurrent: within a process it stays
single-threaded by design. The lock only prevents two *processes* from
interleaving read-modify-write cycles on the same directory. Combine it with
``on_conflict``/``reload`` for a safe cross-process recipe::

    with store.lock():
        store.reload(key)      # see the other process's committed state
        item = store.get(key)
        item.count += 1        # auto-commits while the lock is held
"""

from __future__ import annotations

import os
from typing import IO, Any

try:
    import fcntl

    def _lock_fh(fh: IO[Any]) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _unlock_fh(fh: IO[Any]) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)

except ImportError:  # Windows
    import msvcrt

    def _lock_fh(fh: IO[Any]) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_fh(fh: IO[Any]) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


LOCK_FILENAME = ".slowstore.lock"


class FileLock:
    """Reentrant advisory lock backed by ``{directory}/.slowstore.lock``.

    The lock file itself carries no data; it exists only to be locked. It is
    left in place (removing it would race other processes opening it).
    """

    def __init__(self, directory: str):
        self.path: str = os.path.join(directory, LOCK_FILENAME)
        self.__fh__: IO[Any] | None = None
        self.__depth__: int = 0

    def acquire(self) -> None:
        if self.__depth__ == 0:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fh = open(self.path, "a+")
            try:
                _lock_fh(fh)
            except BaseException:
                fh.close()
                raise
            self.__fh__ = fh
        self.__depth__ += 1

    def release(self) -> None:
        if self.__depth__ == 0:
            raise RuntimeError("release() without a matching acquire()")
        self.__depth__ -= 1
        if self.__depth__ == 0 and self.__fh__ is not None:
            try:
                _unlock_fh(self.__fh__)
            finally:
                self.__fh__.close()
                self.__fh__ = None

    @property
    def held(self) -> bool:
        return self.__depth__ > 0

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> bool:
        self.release()
        return False
