from pathlib import Path

import pytest

from codexrelay.single_instance import AlreadyRunningError, SingleInstanceLock


def test_single_instance_lock(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    first = SingleInstanceLock(path)
    second = SingleInstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
