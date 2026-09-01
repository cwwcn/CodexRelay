from __future__ import annotations

import asyncio
from types import TracebackType


class SleepInhibitor:
    """Keeps macOS awake while at least one running job owns a lease."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._process: asyncio.subprocess.Process | None = None
        self._leases = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def acquire(self) -> None:
        async with self._lock:
            self._leases += 1
            if not self.enabled:
                return
            if self.active:
                return
            try:
                self._process = await asyncio.create_subprocess_exec(
                    "/usr/bin/caffeinate",
                    "-dimsu",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except BaseException:
                self._leases -= 1
                raise

    async def release(self) -> None:
        async with self._lock:
            if self._leases == 0:
                return
            self._leases -= 1
            if not self.enabled:
                return
            if self._leases > 0 or not self.active:
                return
            assert self._process is not None
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
            finally:
                self._process = None

    def lease(self) -> SleepLease:
        return SleepLease(self)

    async def close(self) -> None:
        async with self._lock:
            self._leases = 1 if self.active else 0
        await self.release()


class SleepLease:
    def __init__(self, inhibitor: SleepInhibitor) -> None:
        self._inhibitor = inhibitor

    async def __aenter__(self) -> SleepLease:
        await self._inhibitor.acquire()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self._inhibitor.release()
