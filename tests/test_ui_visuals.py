from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QSize, Qt, QThreadPool
from PySide6.QtWidgets import QApplication, QFrame, QPushButton

from codexrelay.models import GlobalSession, JobStatus
from codexrelay.paths import AppPaths
from codexrelay.ui.app import (
    STYLE_SHEET,
    AsyncWorker,
    ChoiceButton,
    MarqueeLabel,
    MenuOverview,
    QuitConfirmationDialog,
    SettingsWindow,
    ToggleSwitch,
    make_icon,
)
from codexrelay.ui.state import AppStatusSnapshot, RuntimeState
from codexrelay.updates import DisabledUpdateProvider


@pytest.fixture(autouse=True)
def drain_qt_workers() -> object:
    """Do not let SettingsWindow background reads outlive the test process."""
    yield
    assert QThreadPool.globalInstance().waitForDone(5000)


def isolated_app_paths(tmp_path: Path) -> AppPaths:
    """Keep UI workers away from the user's live CodexRelay data."""
    return AppPaths(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )


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


def test_marquee_label_keeps_short_text_static_and_long_text_recoverable() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    label = MarqueeLabel("这是一段很长的当前会话标题，用于验证菜单栏面板中的横向滚动显示")
    label.resize(120, 28)
    label.show()
    application.processEvents()

    assert label.text() == "这是一段很长的当前会话标题，用于验证菜单栏面板中的横向滚动显示"
    assert label.toolTip() == label.text()
    assert label.accessibleDescription() == label.text()
    assert label._timer.isActive()

    max_offset = max(0, label._text_width() - label.contentsRect().width())
    label._pause_ticks = 0
    label._advance()
    assert 0 <= label._offset <= max_offset
    while not label._pause_at_end:
        label._pause_ticks = 0
        label._advance()
    assert label._offset == max_offset

    label._pause_ticks = 1
    label._advance()
    assert label._offset == 0
    assert not label._pause_at_end

    label.setText("短标题")
    application.processEvents()
    assert not label._timer.isActive()
    assert label.text() == "短标题"


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
    assert overview.connection.text() == "Telegram 已连接 · 待完成配对"
    assert overview.project.text() == "CodexRelay"
    assert overview.task.text() == "空闲"
    assert overview.session_value.text() == "未选择会话"
    assert overview.model_value.text() == "gpt-5.6-sol · medium"
    assert overview.task_detail.text() == "没有运行中的任务"


def test_menu_overview_renders_paired_telegram_state() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    overview = MenuOverview()
    overview.set_snapshot(
        AppStatusSnapshot(
            runtime_state=RuntimeState.CONNECTED,
            bot_username="cwwen_codexrelay_bot",
            telegram_paired=True,
        )
    )

    assert application is not None
    assert overview.connection.text() == "Telegram 已连接 · 已配对"


def test_menu_overview_keeps_unassigned_session_as_primary_context() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    overview = MenuOverview()
    overview.set_snapshot(
        AppStatusSnapshot(
            runtime_state=RuntimeState.CONNECTED,
            conversation_title="临时会话",
            active_job_count=1,
            active_job_status=JobStatus.RUNNING,
        )
    )

    assert application is not None
    assert overview.project.text() == "尚未选择"
    assert overview.session_value.text().startswith("临时会话")
    assert overview.task_detail.text() == "会话：临时会话"


def test_runtime_failure_clears_model_loading_state(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    window = SettingsWindow(isolated_app_paths(tmp_path), QThreadPool.globalInstance())

    window.set_runtime_failed("TelegramTransportError: Telegram getMe request failed")

    assert application is not None
    assert window.model_scope.text() == "连接服务失败，暂时无法读取当前会话配置。"
    assert "重新连接" in window.model_description.text()
    assert not window.model_combo.isEnabled()
    assert not window.reasoning_combo.isEnabled()
    assert window.overview_message.text() == "Telegram 暂时无法连接，请检查网络后点击“重新连接”。"


def test_runtime_missing_token_uses_setup_message(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    window = SettingsWindow(isolated_app_paths(tmp_path), QThreadPool.globalInstance())

    window.set_runtime_failed("RuntimeError: Telegram Bot Token is not configured in CodexRelay")

    assert application is not None
    assert window.overview_message.text() == "尚未配置 Telegram Bot Token，请打开设置完成配置。"


def test_sessions_page_is_a_separate_global_view(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    window = SettingsWindow(isolated_app_paths(tmp_path), QThreadPool.globalInstance())

    assert application is not None
    assert window.navigation.count() == 5
    assert window.navigation.item(2).text() == "会话"
    assert window.global_session_list.objectName() == "globalSessionList"
    assert window.global_sessions_summary.objectName() == "sessionSummary"
    assert window.global_session_filter.width() == 156
    assert window.global_assign_button.text() == "归属到项目…"
    assert window.global_activate_button.text() == "切换到会话"
    assert window.global_session_filter._items[1][0] == "有项目"


def test_system_page_preserves_readable_control_heights(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    window = SettingsWindow(isolated_app_paths(tmp_path), QThreadPool.globalInstance())
    window.resize(860, 780)
    window.navigation.setCurrentRow(3)
    window.show()
    application.processEvents()

    assert application is not None
    assert window.project_selector.width() == 250
    assert window.project_selector.height() >= 30
    dividers = window.pages.currentWidget().findChildren(QFrame, "systemDivider")
    assert len(dividers) == 2
    for divider in dividers:
        assert divider.height() == 1
    for button in window.pages.currentWidget().findChildren(QPushButton):
        if button.text() in {"添加项目…", "扫描", "设为当前项目", "保存系统设置"}:
            assert button.height() >= 30
    assert window.auto_connect.height() >= 24
    assert window.prevent_sleep.height() >= 24
    assert window.launch_at_login.height() >= 24


def test_sessions_page_groups_projects_and_keeps_long_paths_compact(
    tmp_path: Path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    project_path = tmp_path / "a-project-with-a-long-name"
    unassigned_path = tmp_path / "an-unassigned-session-with-a-long-path"
    project_path.mkdir()
    unassigned_path.mkdir()
    window = SettingsWindow(isolated_app_paths(tmp_path), QThreadPool.globalInstance())
    window._global_sessions = [
        GlobalSession(
            thread_id="project-thread",
            title="Project session",
            cwd=project_path,
            source="desktop",
            codex_updated_at=2,
            is_active=False,
            project_id="project-id",
            project_name="Relay",
            project_enabled=True,
            conversation_id="conversation-id",
            is_current_project=True,
            is_current_conversation=True,
            path_available=True,
            archived_at=None,
        ),
        GlobalSession(
            thread_id="other-project-thread",
            title="Another project session",
            cwd=project_path,
            source="desktop",
            codex_updated_at=1,
            is_active=False,
            project_id="project-id",
            project_name="Relay",
            project_enabled=True,
            conversation_id="other-conversation-id",
            is_current_project=True,
            is_current_conversation=False,
            path_available=True,
            archived_at=None,
        ),
        GlobalSession(
            thread_id="unassigned-thread",
            title="Unassigned session",
            cwd=unassigned_path,
            source="desktop",
            codex_updated_at=1,
            is_active=False,
            project_id=None,
            project_name=None,
            project_enabled=False,
            conversation_id=None,
            is_current_project=False,
            is_current_conversation=False,
            path_available=True,
            archived_at=None,
        ),
    ]
    window._render_global_sessions()

    assert application is not None
    assert window.global_session_list.topLevelItemCount() == 2
    project_group = window.global_session_list.topLevelItem(0)
    unassigned_group = window.global_session_list.topLevelItem(1)
    assert project_group.text(0) == "Relay  ·  2"
    assert unassigned_group.text(0) == "未归属  ·  1"
    assert "当前会话" in project_group.child(0).text(0)
    assert (
        window.global_session_list.horizontalScrollBarPolicy()
        is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert window.global_activate_button.text() == "当前会话"
    assert not window.global_activate_button.isEnabled()
    assert window.global_session_feedback.text() == "当前正在使用这个会话"
    assert window.global_session_feedback.property("state") == "success"

    window.global_session_list.setCurrentItem(project_group.child(1))
    assert not window.global_assign_button.isEnabled()
    assert window.global_activate_button.isEnabled()
    assert window.global_activate_button.text() == "切换到会话"
    assert window.global_session_feedback.text() == "已选择 · 点击右侧切换"

    window.global_session_list.setCurrentItem(unassigned_group.child(0))
    assert window.global_assign_button.isEnabled()
    assert window.global_activate_button.isEnabled()
    assert window.global_session_feedback.property("state") == "neutral"


def test_session_action_bar_has_distinct_disabled_and_feedback_states() -> None:
    assert "QFrame#sessionActionBar" in STYLE_SHEET
    assert "QLabel#sessionActionStatus[state=\"success\"]" in STYLE_SHEET
    assert "QLabel#sessionActionStatus[state=\"loading\"]" in STYLE_SHEET
    assert "QPushButton#primaryButton:disabled" in STYLE_SHEET


def test_updates_are_disabled_until_a_signed_release_provider_is_configured() -> None:
    provider = DisabledUpdateProvider()

    assert not provider.state.enabled
    assert "正式 GitHub 发行版" in provider.state.message
