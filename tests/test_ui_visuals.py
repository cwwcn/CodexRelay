from __future__ import annotations

import os

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

from codexrelay.ui.app import (
    STYLE_SHEET,
    AsyncWorker,
    ChoiceButton,
    MenuOverview,
    QuitConfirmationDialog,
    ToggleSwitch,
    make_icon,
)
from codexrelay.ui.state import AppStatusSnapshot, RuntimeState
from codexrelay.updates import DisabledUpdateProvider


def test_tray_icon_is_a_macos_adaptive_mask() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])

    icon = make_icon()
    pixmap = icon.pixmap(QSize(22, 22))

    assert application is not None
    assert icon.isMask()
    assert not pixmap.isNull()
    assert pixmap.toImage().hasAlphaChannel()


def test_quit_confirmation_is_compact_and_distinguishes_active_task() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])

    idle = QuitConfirmationDialog(active_count=0)
    active = QuitConfirmationDialog(active_count=1)

    assert application is not None
    assert idle.title_label.text() == "退出 CodexRelay？"
    assert idle.confirm_button.text() == "退出"
    assert idle.confirm_button.objectName() == "quitPrimaryButton"
    assert active.title_label.text() == "当前任务仍在运行"
    assert active.confirm_button.text() == "停止并退出"
    assert active.confirm_button.objectName() == "quitDangerButton"
    assert idle.size().width() <= 448
    assert active.size().width() <= 448


def test_project_selection_has_explicit_high_contrast_colors() -> None:
    assert "QListWidget#projectList::item:selected" in STYLE_SHEET
    assert "background: #D5E5F6" in STYLE_SHEET
    assert "color: #174A78" in STYLE_SHEET


def test_tray_overview_uses_scoped_translucent_surface() -> None:
    assert "QMenu#trayMenu" in STYLE_SHEET
    assert "background: rgba(250, 252, 255, 202)" in STYLE_SHEET
    assert "border-color: rgba(190, 203, 214, 170)" in STYLE_SHEET
    assert "QFrame#menuContextCard" in STYLE_SHEET
    assert "QFrame#menuTaskCard" in STYLE_SHEET
    assert "QLabel#menuConnection[state=\"connected\"]" in STYLE_SHEET


def test_codex_model_controls_use_modern_choice_buttons() -> None:
    assert "QPushButton#choiceButton" in STYLE_SHEET
    assert "QMenu#choiceMenu" in STYLE_SHEET
    assert "QComboBox" not in STYLE_SHEET
    assert "QLabel#scopeStatus" in STYLE_SHEET


def test_choice_button_supports_combo_box_selection_api() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    button = ChoiceButton()
    changes: list[int] = []
    button.currentIndexChanged.connect(changes.append)

    button.addItem("GPT-5.6-Sol", "gpt-5.6-sol")
    button.addItem("GPT-5.6-Luna", "gpt-5.6-luna")
    button.setCurrentIndex(1)

    assert application is not None
    assert button.count() == 2
    assert button.currentText() == "GPT-5.6-Luna"
    assert button.currentData() == "gpt-5.6-luna"
    assert button.findData("gpt-5.6-sol") == 0
    assert changes[-1] == 1


def test_toggle_switch_exposes_binary_state_without_checkbox_indicator() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    switch = ToggleSwitch()

    assert application is not None
    assert switch.isCheckable()
    assert not switch.isChecked()
    assert switch.size() == QSize(44, 26)

    switch.click()

    assert switch.isChecked()


def test_async_workers_are_released_by_the_ui_thread() -> None:
    async def operation() -> object:
        return None

    worker = AsyncWorker(operation)

    assert not worker.autoDelete()


def test_menu_overview_renders_connected_project_and_task_state() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    overview = MenuOverview()
    overview.set_snapshot(
        AppStatusSnapshot(
            runtime_state=RuntimeState.CONNECTED,
            bot_username="cwwen_codexrelay_bot",
            current_project="CodexRelay",
            model="gpt-5.6-sol",
            reasoning_effort="medium",
        )
    )

    assert application is not None
    assert overview.connection.text() == "已连接"
    assert overview.project.text() == "CodexRelay"
    assert overview.task.text() == "空闲"
    assert overview.session_value.text() == "gpt-5.6-sol · medium"
    assert overview.task_detail.text() == "没有运行中的任务"


def test_updates_are_disabled_until_a_signed_release_provider_is_configured() -> None:
    provider = DisabledUpdateProvider()

    assert not provider.state.enabled
    assert "正式 GitHub 发行版" in provider.state.message
