from __future__ import annotations

from pathlib import Path

import pytest

from codexrelay.sleep import SleepInhibitor


@pytest.mark.asyncio
async def test_real_macos_sleep_inhibitor_lifecycle() -> None:
    if not Path("/usr/bin/caffeinate").exists():
        pytest.skip("macOS caffeinate is unavailable")
    inhibitor = SleepInhibitor(enabled=True)

    await inhibitor.acquire()
    assert inhibitor.active

    await inhibitor.release()
    assert not inhibitor.active


@pytest.mark.asyncio
async def test_disabled_sleep_inhibitor_keeps_lease_semantics() -> None:
    inhibitor = SleepInhibitor(enabled=False)

    async with inhibitor.lease():
        assert not inhibitor.active

    assert not inhibitor.active
