"""Inter-process locking: reentrancy, and a real multi-process increment race."""

import dataclasses
import subprocess
import sys

from slowstore import FileLock, Store


@dataclasses.dataclass
class Counter:
    id: str
    value: int


def test_lock_is_reentrant(tmp_path):
    lock = FileLock(str(tmp_path))
    with lock:
        assert lock.held
        with lock:
            assert lock.held
        assert lock.held  # inner exit must not release the outer hold
    assert not lock.held


def test_two_locks_on_same_directory_exclude(tmp_path):
    """A second process blocks until the first releases the directory lock."""
    a = FileLock(str(tmp_path))
    a.acquire()
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, fcntl\n"
            "fh = open(sys.argv[1], 'a+')\n"
            "try:\n"
            "    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    print('acquired')\n"
            "except BlockingIOError:\n"
            "    print('blocked')\n",
            a.path,
        ],
        capture_output=True,
        text=True,
    )
    a.release()
    assert probe.stdout.strip() == "blocked"


WORKER = """
import dataclasses, sys
from slowstore import Store

@dataclasses.dataclass
class Counter:
    id: str
    value: int

directory, iterations = sys.argv[1], int(sys.argv[2])
store = Store(Counter, directory, lock_writes=True)
for _ in range(iterations):
    with store.lock():
        store.reload("c")
        c = store.get("c")
        c.value += 1  # auto-commits while the lock is held
"""


def test_multiprocess_increments_do_not_lose_updates(tmp_path):
    directory = str(tmp_path / "counters")
    with Store(Counter, directory) as store:
        store.upsert("c", Counter(id="c", value=0))

    procs, iterations = 4, 25
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, directory, str(iterations)],
            stderr=subprocess.PIPE,
        )
        for _ in range(procs)
    ]
    for w in workers:
        _, err = w.communicate(timeout=120)
        assert w.returncode == 0, err.decode()

    final = Store(Counter, directory).get_model("c")
    assert final is not None
    assert final.value == procs * iterations
