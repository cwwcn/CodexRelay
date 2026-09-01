from __future__ import annotations

from dataclasses import dataclass, field

from codexrelay.ui.app import TrayApplication


@dataclass
class FakeAction:
    text: str = ""

    def setText(self, value: str) -> None:
        self.text = value


@dataclass
class FakeNavigation:
    current_row: int = 0

    def setCurrentRow(self, value: int) -> None:
        self.current_row = value


@dataclass
class FakeWindow:
    navigation: FakeNavigation = field(default_factory=FakeNavigation)
    failure: str = ""

    def set_runtime_failed(self, message: str) -> None:
        self.failure = message


@dataclass
class FakeTrayApplication:
    status_action: FakeAction = field(default_factory=FakeAction)
    window: FakeWindow = field(default_factory=FakeWindow)
    shown: bool = False

    def show_window(self) -> None:
        self.shown = True


def test_missing_token_opens_telegram_settings() -> None:
    application = FakeTrayApplication()

    TrayApplication._runtime_failed(
        application, "RuntimeError: Telegram Bot Token is not configured in CodexRelay"
    )

    assert application.status_action.text == "CodexRelay · 需要处理"
    assert application.window.navigation.current_row == 1
    assert application.shown is True


def test_other_runtime_failure_does_not_force_open_settings() -> None:
    application = FakeTrayApplication()

    TrayApplication._runtime_failed(application, "RuntimeError: network unavailable")

    assert application.status_action.text == "CodexRelay · 需要处理"
    assert application.window.navigation.current_row == 0
    assert application.shown is False
