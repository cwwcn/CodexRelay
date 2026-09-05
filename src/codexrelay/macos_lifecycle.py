from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger("codexrelay.macos_lifecycle")


class MacLifecycleMonitor:
    """Bridge native macOS workspace power notifications into Python callbacks."""

    def __init__(
        self,
        *,
        on_sleep: Callable[[], None],
        on_wake: Callable[[], None],
        on_power_off: Callable[[], None],
    ) -> None:
        self.on_sleep = on_sleep
        self.on_wake = on_wake
        self.on_power_off = on_power_off
        self._center: Any = None
        self._observer: Any = None

    def start(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import objc
            from AppKit import (
                NSWorkspace,
                NSWorkspaceDidWakeNotification,
                NSWorkspaceWillPowerOffNotification,
                NSWorkspaceWillSleepNotification,
            )
            from Foundation import NSObject
        except ImportError:
            LOGGER.warning("PyObjC is unavailable; using elapsed-time wake detection only")
            return False

        callbacks: tuple[Callable[[], None], Callable[[], None], Callable[[], None]] = (
            self.on_sleep,
            self.on_wake,
            self.on_power_off,
        )

        class WorkspaceObserver(NSObject):  # type: ignore[misc]
            def initWithCallbacks_(self, values: object) -> Any:
                self = objc.super(WorkspaceObserver, self).init()
                if self is not None:
                    self.callbacks: tuple[
                        Callable[[], None], Callable[[], None], Callable[[], None]
                    ] = values  # type: ignore[assignment]
                return self

            def workspaceWillSleep_(self, _notification: object) -> None:
                self.callbacks[0]()

            def workspaceDidWake_(self, _notification: object) -> None:
                self.callbacks[1]()

            def workspaceWillPowerOff_(self, _notification: object) -> None:
                self.callbacks[2]()

        observer = WorkspaceObserver.alloc().initWithCallbacks_(callbacks)
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            observer, "workspaceWillSleep:", NSWorkspaceWillSleepNotification, None
        )
        center.addObserver_selector_name_object_(
            observer, "workspaceDidWake:", NSWorkspaceDidWakeNotification, None
        )
        center.addObserver_selector_name_object_(
            observer, "workspaceWillPowerOff:", NSWorkspaceWillPowerOffNotification, None
        )
        self._center = center
        self._observer = observer
        return True

    def stop(self) -> None:
        if self._center is not None and self._observer is not None:
            self._center.removeObserver_(self._observer)
        self._center = None
        self._observer = None
