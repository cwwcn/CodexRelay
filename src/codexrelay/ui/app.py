from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast

from PySide6.QtCore import (
    QLineF,
    QObject,
    QPoint,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from codexrelay.codex.app_server import codex_subprocess_environment, discover_codex_bin
from codexrelay.codex.model_catalog import (
    CodexModelCatalog,
    reasoning_effort_label,
)
from codexrelay.connectors.telegram.api import TelegramClient
from codexrelay.database import Database
from codexrelay.logging_setup import configure_logging
from codexrelay.models import Conversation, JobStatus, Project
from codexrelay.pairing import PairingService
from codexrelay.paths import AppPaths
from codexrelay.projects import ProjectService
from codexrelay.runtime import CodexRelayRuntime
from codexrelay.secrets import SecretStore
from codexrelay.settings import AppSection, Settings, SettingsStore
from codexrelay.single_instance import AlreadyRunningError, SingleInstanceLock
from codexrelay.startup import StartupService
from codexrelay.ui.state import AppStatusSnapshot, RuntimeState
from codexrelay.updates import (
    GitHubReleaseUpdateProvider,
    UpdateProvider,
    UpdateState,
)
from codexrelay.version import __build_time__, __version__

AsyncFactory = Callable[[], Coroutine[Any, Any, object]]
LOGGER = logging.getLogger("codexrelay.ui")


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    completed = Signal(object)


class AsyncWorker(QRunnable):
    def __init__(self, factory: AsyncFactory) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.factory = factory
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = asyncio.run(self.factory())
        except Exception as error:
            LOGGER.error("background operation failed: %s: %s", type(error).__name__, error)
            self.signals.failed.emit(f"{type(error).__name__}: {error}")
        else:
            self.signals.finished.emit(result)
        finally:
            self.signals.completed.emit(self)


class ChoiceButton(QPushButton):
    """A compact, platform-neutral alternative to the native combo box."""

    currentIndexChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__("—")
        self.setObjectName("choiceButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40)
        self._items: list[tuple[str, object]] = []
        self._current_index = -1
        self._menu = QMenu(self)
        self._menu.setObjectName("choiceMenu")
        self.clicked.connect(self._show_menu)

    def clear(self) -> None:
        self._menu.clear()
        self._items.clear()
        self._current_index = -1
        self.setText("—")

    def addItem(self, text: str, data: object) -> None:
        index = len(self._items)
        self._items.append((text, data))
        action = self._menu.addAction(text)
        action.setCheckable(True)
        action.triggered.connect(lambda _checked=False, item=index: self._select(item))
        if self._current_index < 0:
            self.setCurrentIndex(0)

    def count(self) -> int:
        return len(self._items)

    def currentData(self) -> object | None:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index][1]
        return None

    def currentText(self) -> str:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index][0]
        return ""

    def findData(self, data: object) -> int:
        return next(
            (index for index, (_text, value) in enumerate(self._items) if value == data),
            -1,
        )

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        changed = index != self._current_index
        self._current_index = index
        self.setText(self._items[index][0])
        for action_index, action in enumerate(self._menu.actions()):
            action.setChecked(action_index == index)
        if changed:
            self.currentIndexChanged.emit(index)

    def _select(self, index: int) -> None:
        self.setCurrentIndex(index)

    def _show_menu(self) -> None:
        if not self.isEnabled() or not self._items:
            return
        self._menu.setMinimumWidth(self.width())
        self._menu.popup(self.mapToGlobal(QPoint(0, self.height() + 5)))

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#687786" if self.isEnabled() else "#A9B1B9")
        painter.setPen(QPen(color, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        center_x = float(self.width() - 17)
        center_y = float(self.height()) / 2.0
        painter.drawLine(QLineF(center_x - 4, center_y - 2, center_x, center_y + 2))
        painter.drawLine(QLineF(center_x, center_y + 2, center_x + 4, center_y - 2))


class RuntimeThread(QThread):
    connected = Signal(str, object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, paths: AppPaths) -> None:
        super().__init__()
        self.paths = paths
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.runtime: CodexRelayRuntime | None = None

    def run(self) -> None:
        try:
            asyncio.run(self._run_runtime())
        except Exception as error:
            LOGGER.error("runtime stopped with an error: %s: %s", type(error).__name__, error)
            self.failed.emit(f"{type(error).__name__}: {error}")
        finally:
            self.stopped.emit()

    async def _run_runtime(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        runtime = CodexRelayRuntime(self.paths)
        self.runtime = runtime
        try:
            identity = await runtime.start()
            self.connected.emit(identity.bot_username, runtime.model_catalog)
            await runtime.run(self._stop)
        finally:
            self.runtime = None

    def request_stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    def interrupt_current_task(self) -> None:
        if self._loop is None or self.runtime is None or self.runtime.relay is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            self.runtime.relay.interrupt_active(), self._loop
        )
        future.add_done_callback(self._log_interrupt_result)

    @staticmethod
    def _log_interrupt_result(future: Any) -> None:
        try:
            LOGGER.info("current task interrupt requested; active=%s", future.result())
        except Exception as error:
            LOGGER.warning("current task interrupt failed: %s: %s", type(error).__name__, error)


class UpdateDialog(QDialog):
    """Deprecated: update actions now live directly in the About page."""

    checkRequested = Signal()
    downloadRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateDialog")
        self.setWindowTitle("GitHub Release 更新")
        self.setModal(False)
        self.setMinimumSize(620, 480)
        self.resize(680, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        title = QLabel("GitHub Release 更新")
        title.setObjectName("updateDialogTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        self.current_version = QLabel(f"当前版本 {__version__}")
        self.current_version.setObjectName("updateDialogCurrent")
        heading.addWidget(self.current_version)
        layout.addLayout(heading)

        self.rows: dict[str, QLabel] = {}
        for key, label in (
            ("status", "状态"),
            ("latest", "最新版本"),
            ("resource", "资源"),
            ("progress", "进度"),
        ):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            caption = QLabel(label)
            caption.setObjectName("updateDialogLabel")
            value = QLabel("—")
            value.setObjectName("updateDialogValue")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(caption)
            row.addSpacing(28)
            row.addWidget(value, 1)
            layout.addLayout(row)
            self.rows[key] = value

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("updateProgress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        notes_label = QLabel("版本说明")
        notes_label.setObjectName("updateDialogLabel")
        layout.addWidget(notes_label)
        self.release_notes = QTextBrowser()
        self.release_notes.setObjectName("updateNotes")
        self.release_notes.setReadOnly(True)
        self.release_notes.setOpenExternalLinks(False)
        self.release_notes.setPlaceholderText("发行说明将在检查后显示")
        layout.addWidget(self.release_notes, 1)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.check_button = QPushButton("检查更新")
        self.check_button.setObjectName("primaryButton")
        self.check_button.clicked.connect(self.checkRequested)
        self.download_button = QPushButton("下载并打开 DMG")
        self.download_button.setObjectName("secondaryButton")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.downloadRequested)
        actions.addWidget(self.check_button)
        actions.addWidget(self.download_button)
        actions.addStretch(1)
        layout.addLayout(actions)

    def refresh(self, state: UpdateState) -> None:
        self.rows["status"].setText(state.message or "尚未检查")
        self.rows["latest"].setText(
            f"v{state.available_version}" if state.available_version else f"v{__version__}"
        )
        self.rows["resource"].setText(state.asset_name or "当前架构暂无安装包")
        if state.downloaded_path:
            self.rows["progress"].setText("下载完成 · 校验通过")
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.progress_bar.show()
        elif state.downloading:
            if state.total_bytes:
                percentage = min(100.0, state.downloaded_bytes / state.total_bytes * 100)
                self.rows["progress"].setText(
                    f"{percentage:.1f}% · {format_bytes(state.downloaded_bytes)} / "
                    f"{format_bytes(state.total_bytes)}"
                )
                self.progress_bar.setRange(0, 1000)
                self.progress_bar.setValue(round(percentage * 10))
            else:
                self.rows["progress"].setText(
                    f"已下载 {format_bytes(state.downloaded_bytes)}"
                )
                self.progress_bar.setRange(0, 0)
            self.progress_bar.show()
        else:
            self.rows["progress"].setText("尚未下载")
            self.progress_bar.hide()
        self.release_notes.setMarkdown(state.release_notes or "暂无发行说明。")
        self.check_button.setEnabled(not state.checking and not state.downloading)
        can_download = bool(state.available_version and state.asset_url)
        self.download_button.setEnabled(
            not state.checking
            and not state.downloading
            and (can_download or bool(state.downloaded_path))
        )
        self.download_button.setText("打开 DMG" if state.downloaded_path else "下载并打开 DMG")
        download_is_primary = can_download or bool(state.downloaded_path)
        self._set_button_role(
            self.check_button,
            "secondaryButton" if download_is_primary else "primaryButton",
        )
        self._set_button_role(
            self.download_button,
            "primaryButton" if download_is_primary else "secondaryButton",
        )

    @staticmethod
    def _set_button_role(button: QPushButton, role: str) -> None:
        if button.objectName() == role:
            return
        button.setObjectName(role)
        button.style().unpolish(button)
        button.style().polish(button)

    def set_busy(self, busy: bool, *, downloading: bool = False) -> None:
        self.check_button.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        if downloading:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.show()
            self.rows["progress"].setText("下载中…")


class StatusNode(QWidget):
    def __init__(self, title: str, detail: str) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setSpacing(10)
        self.dot = QLabel("●")
        self.dot.setObjectName("statusDot")
        self.dot.setProperty("state", "idle")
        text = QVBoxLayout()
        text.setSpacing(0)
        title_label = QLabel(title)
        title_label.setObjectName("statusTitle")
        self.detail = QLabel(detail)
        self.detail.setObjectName("statusDetail")
        text.addWidget(title_label)
        text.addWidget(self.detail)
        layout.addWidget(self.dot)
        layout.addLayout(text, 1)

    def set_state(self, state: str, detail: str) -> None:
        self.dot.setProperty("state", state)
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        self.detail.setText(detail)


class TaskStatusDot(QWidget):
    """Small anti-aliased status dot independent of the system font glyphs."""

    _COLORS: ClassVar[dict[str, QColor]] = {
        "idle": QColor("#6F8291"),
        "running": QColor("#2F8BB4"),
        "attention": QColor("#D15B45"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("menuTaskDot")
        self.setFixedSize(9, 9)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._state = "idle"

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._COLORS.get(self._state, self._COLORS["idle"]))
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class AboutMark(QWidget):
    """Scalable brand mark shared by the About and confirmation surfaces."""

    def __init__(self, size: int = 72) -> None:
        super().__init__()
        self.setFixedSize(size, size)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = float(min(self.width(), self.height()))
        painter.translate((self.width() - side) / 2, (self.height() - side) / 2)
        painter.scale(side / 72.0, side / 72.0)
        painter.setPen(Qt.PenStyle.NoPen)
        gradient = QLinearGradient(9, 6, 63, 67)
        gradient.setColorAt(0, QColor("#287CB1"))
        gradient.setColorAt(0.55, QColor("#1B5B88"))
        gradient.setColorAt(1, QColor("#102F4C"))
        painter.setBrush(gradient)
        painter.drawRoundedRect(QRectF(0, 0, 72, 72), 17, 17)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 28), 1))
        painter.drawRoundedRect(QRectF(0.5, 0.5, 71, 71), 16.5, 16.5)

        pen = QPen(QColor("#F8FBFD"), 4.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        left = QPainterPath()
        left.moveTo(31, 17)
        left.lineTo(27, 17)
        left.cubicTo(21.5, 17, 19, 20.5, 19, 26)
        left.lineTo(19, 46)
        left.cubicTo(19, 51.5, 21.5, 55, 27, 55)
        left.lineTo(31, 55)
        painter.drawPath(left)

        right = QPainterPath()
        right.moveTo(41, 17)
        right.lineTo(45, 17)
        right.cubicTo(50.5, 17, 53, 20.5, 53, 26)
        right.lineTo(53, 46)
        right.cubicTo(53, 51.5, 50.5, 55, 45, 55)
        right.lineTo(41, 55)
        painter.drawPath(right)
        painter.drawLine(QLineF(31, 36, 41, 36))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#72E2BD"))
        painter.drawEllipse(QRectF(32, 32, 8, 8))


class QuitConfirmationDialog(QDialog):
    """Compact, frameless confirmation card tailored to a menu-bar app."""

    def __init__(self, *, active_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("quitDialog")
        self.setWindowTitle("退出 CodexRelay")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(448, 232 if active_count else 220)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 22)
        outer.setSpacing(0)
        card = QFrame()
        card.setObjectName("quitCard")
        outer.addWidget(card)

        content = QVBoxLayout(card)
        content.setContentsMargins(22, 20, 22, 18)
        content.setSpacing(0)

        heading = QHBoxLayout()
        heading.setSpacing(14)
        heading.addWidget(AboutMark(44), alignment=Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        # Keep the consequence text visually separate from the primary action.
        copy.setSpacing(15)
        self.title_label = QLabel("当前任务仍在运行" if active_count else "退出 CodexRelay？")
        self.title_label.setObjectName("quitTitle")
        self.message_label = QLabel(
            "退出会中断当前 Codex 任务。\n"
            "任务会标记为“已中断”。项目和会话数据会保留。"
            if active_count
            else "退出后，Telegram 将暂时无法连接这台 Mac。\n"
            "已保存的项目和会话会保留到下次启动。"
        )
        self.message_label.setObjectName("quitMessage")
        self.message_label.setWordWrap(True)
        copy.addWidget(self.title_label)
        copy.addWidget(self.message_label)
        heading.addLayout(copy, 1)
        content.addLayout(heading)
        content.addStretch(1)

        divider = QFrame()
        divider.setObjectName("quitDivider")
        divider.setFrameShape(QFrame.Shape.HLine)
        content.addWidget(divider)
        content.addSpacing(14)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("quitCancelButton")
        self.cancel_button.setFixedHeight(32)
        self.cancel_button.setMinimumWidth(82)
        self.cancel_button.clicked.connect(self.reject)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        self.confirm_button = QPushButton("停止并退出" if active_count else "退出")
        self.confirm_button.setObjectName(
            "quitDangerButton" if active_count else "quitPrimaryButton"
        )
        self.confirm_button.setFixedHeight(32)
        self.confirm_button.setMinimumWidth(96 if active_count else 82)
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.clicked.connect(self.accept)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.confirm_button)
        content.addLayout(actions)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        frame = self.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        self.move(frame.topLeft())


class ToggleSwitch(QAbstractButton):
    """Small keyboard-accessible switch matching the macOS on/off pattern."""

    def __init__(self) -> None:
        super().__init__()
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(44, 26)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(1.5, 3.5, 41.0, 19.0)
        if self.hasFocus():
            painter.setPen(QPen(QColor("#88B9E2"), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0.5, 2.5, 43.0, 21.0), 10.5, 10.5)
        painter.setPen(Qt.PenStyle.NoPen)
        if not self.isEnabled():
            track_color = QColor("#D8DEE4")
        elif self.isChecked():
            track_color = QColor("#1677E8")
        else:
            track_color = QColor("#B8C1CA")
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 9.5, 9.5)
        knob_x = 23.0 if self.isChecked() else 3.0
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(knob_x, 5.0, 16.0, 16.0))


class MenuOverview(QWidget):
    """Read-only status surface embedded in the menu-bar menu."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("menuOverview")
        self.setFixedWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 13, 14, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("CodexRelay")
        title.setObjectName("menuTitle")
        self.connection = QLabel("正在连接")
        self.connection.setObjectName("menuConnection")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.connection)
        layout.addLayout(header)

        self.identity = QLabel("正在读取 Telegram 状态…")
        self.identity.setObjectName("menuMuted")
        layout.addWidget(self.identity)

        context_card = QFrame()
        context_card.setObjectName("menuContextCard")
        context_layout = QVBoxLayout(context_card)
        context_layout.setContentsMargins(11, 10, 11, 10)
        context_layout.setSpacing(6)
        context_header = QHBoxLayout()
        context_header.setSpacing(8)
        project_caption = QLabel("工作区")
        project_caption.setObjectName("menuSectionLabel")
        self.project = QLabel("尚未选择")
        self.project.setObjectName("menuProjectValue")
        context_header.addWidget(project_caption)
        context_header.addStretch(1)
        context_layout.addLayout(context_header)
        context_layout.addWidget(self.project)

        context_separator = QFrame()
        context_separator.setObjectName("menuCardSeparator")
        context_separator.setFrameShape(QFrame.Shape.HLine)
        context_layout.addWidget(context_separator)

        config_row = QHBoxLayout()
        config_row.setSpacing(8)
        session_caption = QLabel("会话配置")
        session_caption.setObjectName("menuCaption")
        self.session_value = QLabel("本机默认模型 · 默认")
        self.session_value.setObjectName("menuSessionValue")
        config_row.addWidget(session_caption)
        config_row.addWidget(self.session_value, 1)
        context_layout.addLayout(config_row)
        layout.addWidget(context_card)

        task_card = QFrame()
        task_card.setObjectName("menuTaskCard")
        task_layout = QHBoxLayout(task_card)
        task_layout.setContentsMargins(11, 9, 11, 9)
        task_layout.setSpacing(8)
        task_column = QVBoxLayout()
        task_column.setSpacing(2)
        task_header = QHBoxLayout()
        task_header.setSpacing(8)
        task_caption = QLabel("当前任务")
        task_caption.setObjectName("menuSectionLabel")
        task_status = QHBoxLayout()
        task_status.setSpacing(6)
        self.task_dot = TaskStatusDot()
        self.task = QLabel("空闲")
        self.task.setObjectName("menuTaskValue")
        task_header.addWidget(task_caption)
        task_header.addStretch(1)
        task_status.addWidget(self.task_dot, alignment=Qt.AlignmentFlag.AlignVCenter)
        task_status.addWidget(self.task)
        task_header.addLayout(task_status)
        task_column.addLayout(task_header)
        self.task_detail = QLabel("没有运行中的任务")
        self.task_detail.setObjectName("menuTaskDetail")
        task_column.addWidget(self.task_detail)
        task_layout.addLayout(task_column, 1)
        layout.addWidget(task_card)

        self.notice = QLabel("")
        self.notice.setObjectName("menuNotice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)

    def set_snapshot(self, snapshot: AppStatusSnapshot) -> None:
        self.connection.setText(snapshot.connection_title)
        self.connection.setProperty("state", snapshot.runtime_state.value)
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)
        connection_colors = {
            RuntimeState.CONNECTED: "#1A9A5B",
            RuntimeState.STARTING: "#C47A16",
            RuntimeState.RESTARTING: "#C47A16",
            RuntimeState.ATTENTION: "#B24736",
            RuntimeState.STOPPING: "#607B91",
            RuntimeState.STOPPED: "#7C8792",
        }
        self.connection.setStyleSheet(
            f"color: {connection_colors[snapshot.runtime_state]};"
            " font-size: 12px; font-weight: 700;"
        )
        self.identity.setText(
            f"@{snapshot.bot_username}"
            if snapshot.bot_username
            else "Telegram 尚未连接"
        )
        self.project.setText(snapshot.current_project or "尚未选择")
        self.session_value.setText(snapshot.model_title)
        self.task.setText(snapshot.task_title)
        if snapshot.active_job_count:
            self.task_detail.setText(
                f"项目：{snapshot.active_project or snapshot.current_project or '当前项目'}"
            )
        else:
            self.task_detail.setText("没有运行中的任务")
        task_state = "attention" if snapshot.active_job_status is JobStatus.WAITING_APPROVAL else (
            "running" if snapshot.active_job_count else "idle"
        )
        self.task_dot.set_state(task_state)
        if snapshot.last_error:
            self.notice.setText(snapshot.last_error)
            self.notice.show()
        else:
            self.notice.clear()
            self.notice.hide()


class SettingsWindow(QMainWindow):
    runtime_configuration_changed = Signal()
    settings_closed = Signal()
    update_available = Signal(str)
    update_state_changed = Signal(object)

    def __init__(self, paths: AppPaths, pool: QThreadPool) -> None:
        super().__init__()
        self.paths = paths
        self.pool = pool
        self._telegram_username: str | None = None
        self._telegram_paired = False
        self.settings_store = SettingsStore(paths.settings)
        self.settings = self.settings_store.load()
        self.secret_store = SecretStore(paths.telegram_tokens)
        self.startup_service = StartupService()
        self.model_catalog: CodexModelCatalog | None = None
        self.model_project_id: str | None = None
        self._updating_model_controls = False
        self._workers: set[AsyncWorker] = set()
        self.update_provider: UpdateProvider | None = None
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(6 * 60 * 60 * 1000)
        self.update_timer.timeout.connect(lambda: self.check_for_updates(automatic=True))
        self._notified_update_version: str | None = None
        self.setWindowTitle("CodexRelay")
        self.setMinimumSize(760, 700)
        self.resize(860, 780)
        self.setUnifiedTitleAndToolBarOnMac(True)
        self._build()
        self._load_codex_status()
        self._load_projects()
        self._load_token_status()

    def install_application_menu(
        self,
        *,
        settings_action: QAction,
        quit_action: QAction,
        about_action: QAction,
    ) -> None:
        menu_bar = self.menuBar()
        menu_bar.setNativeMenuBar(True)
        file_menu = menu_bar.addMenu("文件")
        close_action = QAction("关闭窗口", self)
        close_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Close))
        close_action.triggered.connect(self.close)
        file_menu.addAction(close_action)
        app_menu = menu_bar.addMenu("CodexRelay")
        app_menu.addAction(about_action)
        app_menu.addAction(settings_action)
        app_menu.addSeparator()
        app_menu.addAction(quit_action)
        self.addAction(close_action)

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 22, 28, 24)
        outer.setSpacing(14)

        eyebrow = QLabel("LOCAL RELAY / MAC")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("CodexRelay")
        title.setObjectName("windowTitle")
        subtitle = QLabel("设置连接、项目与安全边界")
        subtitle.setObjectName("subtitle")
        heading = QHBoxLayout()
        heading.setSpacing(14)
        heading.addWidget(eyebrow)
        heading.addStretch(1)
        heading.addWidget(subtitle)
        outer.addLayout(heading)
        outer.addWidget(title)

        self.telegram_status = StatusNode("Telegram", "检查中")
        self.codex_status = StatusNode("Codex", "本机运行时")
        self.project_status = StatusNode("当前项目", "尚未选择")

        status_strip = QFrame()
        status_strip.setObjectName("statusStrip")
        status_layout = QHBoxLayout(status_strip)
        status_layout.setContentsMargins(12, 4, 12, 4)
        status_layout.setSpacing(18)
        status_layout.addWidget(self.telegram_status, 1)
        status_layout.addWidget(self.codex_status, 1)
        status_layout.addWidget(self.project_status, 1)
        outer.addWidget(status_strip)

        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setFlow(QListWidget.Flow.LeftToRight)
        self.navigation.setWrapping(False)
        self.navigation.setMovement(QListWidget.Movement.Static)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.navigation.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.navigation.setSpacing(4)
        self.navigation.setFixedHeight(38)
        self.navigation.addItems(["Telegram", "Codex", "项目", "系统", "关于"])
        self.navigation.setCurrentRow(0)
        outer.addWidget(self.navigation)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._telegram_page())
        self.pages.addWidget(self._codex_page())
        self.pages.addWidget(self._projects_page())
        self.pages.addWidget(self._system_page())
        self.pages.addWidget(self._about_page())
        self.navigation.currentRowChanged.connect(self._navigation_changed)
        outer.addWidget(self.pages, 1)
        self.overview_message = QLabel("")
        self.overview_message.setObjectName("inlineStatus")
        self.overview_message.setWordWrap(True)
        outer.addWidget(self.overview_message)
        self.setStyleSheet(STYLE_SHEET)

    def _navigation_changed(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if index == 1:
            self._load_model_configuration()
        elif index == 2:
            self._load_projects()

    def set_update_provider(self, provider: UpdateProvider) -> None:
        self.update_provider = provider
        self._refresh_update_view()
        if self.settings.app.update_checks_automatically:
            self.update_timer.start()

    @staticmethod
    def _rail_connector() -> QLabel:
        connector = QLabel("│")
        connector.setObjectName("railConnector")
        connector.setAlignment(Qt.AlignmentFlag.AlignLeft)
        connector.setContentsMargins(4, 0, 0, 0)
        return connector

    def _overview_page(self) -> QWidget:
        page, layout = self._page("概览", "运行状态与下一步操作")
        self.overview_message = QLabel("正在读取本机状态…")
        self.overview_message.setObjectName("heroStatus")
        self.overview_message.setWordWrap(True)
        layout.addWidget(self.overview_message)

        explanation = QLabel(
            "消息只会从已配对的Telegram私聊进入。每个任务都绑定到当前授权项目，"
            "运行期间Mac保持唤醒，完成后恢复正常睡眠策略。"
        )
        explanation.setObjectName("bodyText")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addStretch(1)
        return page

    def _telegram_page(self) -> QWidget:
        page, layout = self._page("Telegram", "Token仅保存在CodexRelay私有数据中")
        token_label = QLabel("Bot Token")
        token_label.setObjectName("fieldLabel")
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("输入BotFather生成的Token")
        self.save_token_button = QPushButton("验证并保存")
        self.save_token_button.setObjectName("primaryButton")
        self.save_token_button.clicked.connect(self._save_token)
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit, 1)
        token_row.addWidget(self.save_token_button)
        layout.addWidget(token_label)
        layout.addLayout(token_row)
        self.token_status = QLabel("尚未检查")
        self.token_status.setObjectName("hint")
        layout.addWidget(self.token_status)

        layout.addSpacing(18)
        pair_label = QLabel("设备配对")
        pair_label.setObjectName("fieldLabel")
        self.pairing_code = QLabel("—— —— ——")
        self.pairing_code.setObjectName("pairingCode")
        self.pairing_hint = QLabel("生成后10分钟内，在Telegram发送 /pair 配对码")
        self.pairing_hint.setObjectName("hint")
        self.pair_button = QPushButton("生成一次性配对码")
        self.pair_button.setObjectName("secondaryButton")
        self.pair_button.clicked.connect(self._generate_pairing_code)
        layout.addWidget(pair_label)
        layout.addWidget(self.pairing_code)
        layout.addWidget(self.pairing_hint)
        layout.addWidget(self.pair_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _projects_page(self) -> QWidget:
        page, layout = self._page("项目", "Telegram只能查看和切换这里授权的目录")
        self.project_list = QListWidget()
        self.project_list.setObjectName("projectList")
        layout.addWidget(self.project_list, 1)
        buttons = QHBoxLayout()
        add_button = QPushButton("添加项目…")
        add_button.clicked.connect(self._add_project)
        scan_button = QPushButton("扫描项目")
        scan_button.clicked.connect(self._scan_projects)
        current_button = QPushButton("设为当前项目")
        current_button.setObjectName("primaryButton")
        current_button.clicked.connect(self._switch_project)
        buttons.addWidget(add_button)
        buttons.addWidget(scan_button)
        buttons.addStretch(1)
        buttons.addWidget(current_button)
        layout.addLayout(buttons)
        return page

    def _codex_page(self) -> QWidget:
        page, layout = self._page("Codex", "为当前项目的当前会话选择执行模型")
        self.model_scope = QLabel("正在读取当前项目…")
        self.model_scope.setObjectName("scopeStatus")
        self.model_scope.setMaximumWidth(520)
        layout.addWidget(self.model_scope)

        model_label = QLabel("模型")
        model_label.setObjectName("compactFieldLabel")
        self.model_combo = ChoiceButton()
        self.model_combo.setFixedWidth(235)
        self.model_combo.setEnabled(False)
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        self.model_description = QLabel("连接本机Codex后读取可用模型。")
        self.model_description.setObjectName("hint")
        self.model_description.setWordWrap(True)
        self.model_description.setMaximumWidth(500)

        effort_label = QLabel("推理强度")
        effort_label.setObjectName("compactFieldLabel")
        self.reasoning_combo = ChoiceButton()
        self.reasoning_combo.setFixedWidth(235)
        self.reasoning_combo.setEnabled(False)
        self.reasoning_combo.currentIndexChanged.connect(self._reasoning_changed)

        selectors = QHBoxLayout()
        selectors.setSpacing(14)
        model_column = QVBoxLayout()
        model_column.setSpacing(6)
        model_column.addWidget(model_label)
        model_column.addWidget(self.model_combo)
        effort_column = QVBoxLayout()
        effort_column.setSpacing(6)
        effort_column.addWidget(effort_label)
        effort_column.addWidget(self.reasoning_combo)
        selectors.addLayout(model_column)
        selectors.addLayout(effort_column)
        selectors.addStretch(1)
        layout.addLayout(selectors)
        layout.addWidget(self.model_description)

        safety_note = QLabel(
            "仅作用于 CodexRelay，不修改 Codex 全局配置。"
            "切换项目自动恢复；任务运行中锁定。"
        )
        safety_note.setObjectName("hint")
        safety_note.setWordWrap(True)
        safety_note.setMaximumWidth(520)
        layout.addWidget(safety_note)

        self.save_model_button = QPushButton("保存设置")
        self.save_model_button.setObjectName("primaryButton")
        self.save_model_button.setEnabled(False)
        self.save_model_button.clicked.connect(self._save_model_configuration)
        self.model_save_status = QLabel("")
        self.model_save_status.setObjectName("hint")
        actions = QHBoxLayout()
        actions.addWidget(self.save_model_button)
        actions.addWidget(self.model_save_status, 1)
        layout.addLayout(actions)
        layout.addStretch(1)
        return page

    def _system_page(self) -> QWidget:
        page, layout = self._page("系统", "本机运行策略")
        self.auto_connect = QCheckBox("启动后自动连接Telegram")
        self.auto_connect.setChecked(self.settings.app.auto_connect)
        self.prevent_sleep = QCheckBox("任务运行期间阻止Mac自动睡眠")
        self.prevent_sleep.setChecked(self.settings.app.prevent_sleep_while_running)
        self.launch_at_login = QCheckBox("登录Mac时启动CodexRelay")
        self.launch_at_login.setChecked(self.startup_service.enabled)
        self.launch_at_login.setEnabled(self.startup_service.available)
        launch_hint = QLabel(
            "登录启动只在打包后的个人版中开放。"
            if not self.startup_service.available
            else "修改后在下次登录Mac时生效。"
        )
        launch_hint.setObjectName("hint")
        save_button = QPushButton("保存系统设置")
        save_button.setObjectName("primaryButton")
        save_button.clicked.connect(self._save_system_settings)
        self.storage_status = QLabel(self._storage_summary())
        self.storage_status.setObjectName("hint")
        data_button = QPushButton("打开数据目录")
        data_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.data_dir)))
        )
        log_button = QPushButton("打开日志目录")
        log_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.log_dir)))
        )
        directory_buttons = QHBoxLayout()
        directory_buttons.addWidget(data_button)
        directory_buttons.addWidget(log_button)
        directory_buttons.addStretch(1)
        layout.addWidget(self.auto_connect)
        layout.addWidget(self.prevent_sleep)
        layout.addWidget(self.launch_at_login)
        layout.addWidget(launch_hint)
        layout.addSpacing(14)
        layout.addWidget(save_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(18)
        layout.addWidget(self.storage_status)
        layout.addLayout(directory_buttons)
        layout.addStretch(1)
        return page

    def _about_page(self) -> QWidget:
        page, layout = self._page("关于", "CodexRelay 的版本、发行信息与更新")
        layout.setSpacing(12)

        hero = QFrame()
        hero.setObjectName("aboutHero")
        hero.setFixedHeight(112)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(18, 14, 18, 14)
        hero_layout.setSpacing(14)
        hero_layout.addWidget(AboutMark(), alignment=Qt.AlignmentFlag.AlignVCenter)
        identity = QVBoxLayout()
        identity.setSpacing(2)
        app_name = QLabel("CodexRelay")
        app_name.setObjectName("aboutAppName")
        tagline = QLabel("Telegram 与本机 Codex 之间的本地安全中继")
        tagline.setObjectName("aboutTagline")
        identity.addWidget(app_name)
        identity.addWidget(tagline)
        hero_layout.addLayout(identity, 1)
        build_info = QVBoxLayout()
        build_info.setSpacing(2)
        version = QLabel(f"版本 {__version__}")
        version.setObjectName("aboutVersion")
        build = QLabel(f"构建于 {__build_time__}")
        build.setObjectName("aboutMeta")
        build_info.addWidget(version, alignment=Qt.AlignmentFlag.AlignRight)
        build_info.addWidget(build, alignment=Qt.AlignmentFlag.AlignRight)
        hero_layout.addLayout(build_info)
        layout.addWidget(hero)

        meta_card = QFrame()
        meta_card.setObjectName("aboutCard")
        meta_card.setFixedHeight(64)
        meta_layout = QHBoxLayout(meta_card)
        meta_layout.setContentsMargins(14, 7, 14, 7)
        meta_layout.setSpacing(18)
        for label, value in (
            ("发行状态", "公开发行"),
            ("平台", f"macOS · {platform.machine()}"),
            ("许可证", "MIT"),
        ):
            column = QVBoxLayout()
            column.setSpacing(1)
            caption = QLabel(label)
            caption.setObjectName("aboutMetaLabel")
            detail = QLabel(value)
            detail.setObjectName("aboutMetaValue")
            column.addWidget(caption)
            column.addWidget(detail)
            meta_layout.addLayout(column, 1)
        layout.addWidget(meta_card)

        panels = QHBoxLayout()
        panels.setSpacing(12)

        update_card = QFrame()
        update_card.setObjectName("aboutCard")
        update_card.setFixedHeight(156)
        update_layout = QVBoxLayout(update_card)
        update_layout.setContentsMargins(14, 10, 14, 10)
        update_layout.setSpacing(5)
        update_heading = QLabel("更新")
        update_heading.setObjectName("aboutSectionTitle")
        update_layout.addWidget(update_heading)
        update_hint = QLabel("GitHub Releases · 下载前会先校验安装包")
        update_hint.setObjectName("aboutMeta")
        update_layout.addWidget(update_hint)

        auto_row = QHBoxLayout()
        auto_label = QLabel("自动检查更新")
        auto_label.setObjectName("aboutRowTitle")
        self.auto_update_checks = ToggleSwitch()
        self.auto_update_checks.setAccessibleName("自动检查更新")
        self.auto_update_checks.setChecked(self.settings.app.update_checks_automatically)
        self.auto_update_checks.toggled.connect(self._save_update_settings)
        auto_row.addWidget(auto_label)
        auto_row.addStretch(1)
        auto_row.addWidget(self.auto_update_checks)
        update_layout.addLayout(auto_row)

        check_row = QHBoxLayout()
        self.update_status = QLabel("尚未检查更新")
        self.update_status.setObjectName("aboutMeta")
        self.update_status.setWordWrap(True)
        self.update_detail = QLabel()
        self.update_detail.setObjectName("aboutMeta")
        self.update_detail.setWordWrap(True)
        self.update_detail.setMaximumHeight(34)
        self.update_detail.setVisible(False)
        update_layout.addWidget(self.update_detail)
        self.check_updates_button = QPushButton("检查更新")
        self.check_updates_button.setObjectName("primaryButton")
        self.check_updates_button.setFixedHeight(32)
        self.check_updates_button.clicked.connect(self._trigger_update_action)
        check_row.addWidget(self.update_status, 1)
        check_row.addWidget(self.check_updates_button)
        update_layout.addLayout(check_row)
        panels.addWidget(update_card, 1)

        links_card = QFrame()
        links_card.setObjectName("aboutCard")
        links_card.setFixedHeight(178)
        links_layout = QVBoxLayout(links_card)
        links_layout.setContentsMargins(14, 10, 14, 10)
        links_layout.setSpacing(3)
        links_heading = QLabel("资源")
        links_heading.setObjectName("aboutSectionTitle")
        links_layout.addWidget(links_heading)
        for label, url in (
            ("GitHub 仓库", "https://github.com/cwwcn/CodexRelay"),
            ("GitHub Releases", "https://github.com/cwwcn/CodexRelay/releases"),
            ("MIT License", "https://github.com/cwwcn/CodexRelay/blob/main/LICENSE"),
        ):
            link = QPushButton(f"{label}  ↗")
            link.setObjectName("linkButton")
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            link.clicked.connect(
                lambda _checked=False, target=url: QDesktopServices.openUrl(QUrl(target))
            )
            links_layout.addWidget(link)
        panels.addWidget(links_card, 1)
        layout.addLayout(panels)

        layout.addStretch(1)
        return page

    def _save_update_settings(self, _value: object = None) -> None:
        if not hasattr(self, "auto_update_checks"):
            return
        self.settings = Settings(
            app=AppSection(
                auto_connect=self.settings.app.auto_connect,
                launch_at_login=self.settings.app.launch_at_login,
                prevent_sleep_while_running=self.settings.app.prevent_sleep_while_running,
                update_checks_automatically=self.auto_update_checks.isChecked(),
            ),
            telegram=self.settings.telegram,
            projects=self.settings.projects,
        )
        self.settings_store.save(self.settings)
        if self.settings.app.update_checks_automatically:
            self.update_timer.start()
            QTimer.singleShot(250, lambda: self.check_for_updates(automatic=True))
        else:
            self.update_timer.stop()
        self._refresh_update_view()

    def _refresh_update_view(self) -> None:
        if not hasattr(self, "update_status"):
            return
        provider = self.update_provider
        if provider is None:
            self.update_status.setText("更新检查将在正式发行版中启用")
            self.check_updates_button.setEnabled(False)
            return
        state = provider.state
        if state.available_version and state.asset_url:
            status_text = f"发现新版本 v{state.available_version}"
        elif state.available_version:
            status_text = f"发现新版本 v{state.available_version}，当前架构暂无安装包"
        elif state.message.startswith("当前已是最新版本"):
            status_text = "已是最新"
        else:
            status_text = state.message or "尚未检查更新"
        self.update_status.setText(status_text)
        downloaded = bool(state.downloaded_path)
        downloadable = bool(state.available_version and state.asset_url)
        if state.checking:
            self.check_updates_button.setText("检查中…")
            self.check_updates_button.setEnabled(False)
        elif state.downloading:
            self.check_updates_button.setText("下载中…")
            self.check_updates_button.setEnabled(False)
        elif downloaded:
            self.check_updates_button.setText("打开 DMG")
            self.check_updates_button.setEnabled(True)
            self._set_update_button_role("primaryButton")
        elif downloadable:
            self.check_updates_button.setText("下载更新")
            self.check_updates_button.setEnabled(True)
            self._set_update_button_role("primaryButton")
        elif state.available_version is not None:
            self.check_updates_button.setText("重新检查")
            self.check_updates_button.setEnabled(True)
            self._set_update_button_role("secondaryButton")
        elif state.message.startswith("当前已是最新版本"):
            self.check_updates_button.setText("已是最新")
            self.check_updates_button.setEnabled(True)
            self._set_update_button_role("secondaryButton")
        else:
            self.check_updates_button.setText("检查更新")
            self.check_updates_button.setEnabled(True)
            self._set_update_button_role("primaryButton")

        detail = ""
        if state.available_version:
            detail = f"最新版本 v{state.available_version}"
            if state.asset_name:
                asset_name = state.asset_name
                if len(asset_name) > 54:
                    asset_name = asset_name[:51] + "…"
                detail += f" · {asset_name}"
            if state.release_notes:
                note = next(
                    (
                        line.strip(" -*#")
                        for line in state.release_notes.splitlines()
                        if line.strip()
                    ),
                    "",
                )
                if note:
                    detail += f"\n发行说明：{note[:42]}"
        elif state.downloaded_path:
            detail = "更新包已下载并通过 SHA-256 校验"
        self.update_detail.setText(detail)
        self.update_detail.setVisible(bool(detail))
        self.update_state_changed.emit(state)

    def _trigger_update_action(self) -> None:
        provider = self.update_provider
        if provider is None:
            return
        state = provider.state
        if state.downloaded_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(state.downloaded_path))
        elif state.available_version and state.asset_url:
            self.download_update(auto_open=True)
        else:
            self.check_for_updates(automatic=False)

    def _set_update_button_role(self, role: str) -> None:
        if self.check_updates_button.objectName() == role:
            return
        self.check_updates_button.setObjectName(role)
        self.check_updates_button.style().unpolish(self.check_updates_button)
        self.check_updates_button.style().polish(self.check_updates_button)

    def check_for_updates(self, automatic: bool = False) -> None:
        provider = self.update_provider
        if provider is None:
            self._refresh_update_view()
            return
        self.check_updates_button.setEnabled(False)
        self.update_status.setText("正在检查 GitHub Releases…")

        def finished(value: object) -> None:
            if not isinstance(value, UpdateState):
                self.update_status.setText("更新检查返回了无效结果")
                self.check_updates_button.setEnabled(True)
                return
            state = value
            self._refresh_update_view()
            if state.available_version:
                self.update_status.setText(
                    f"发现新版本 v{state.available_version}"
                    if state.asset_url
                    else f"发现新版本 v{state.available_version}，当前架构暂无安装包"
                )
                if automatic and state.available_version != self._notified_update_version:
                    self._notified_update_version = state.available_version
                    self.update_available.emit(state.available_version)

        async def check() -> object:
            return provider.check_for_updates()

        self._run(check, finished=finished)

    def download_update(self, auto_open: bool = False) -> None:
        provider = self.update_provider
        if provider is None:
            self._refresh_update_view()
            return
        state = provider.state
        if state.downloaded_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(state.downloaded_path))
            return
        self.check_updates_button.setText("下载中…")
        self.check_updates_button.setEnabled(False)
        self.update_status.setText("正在下载并校验更新包…")

        def finished(value: object) -> None:
            if not isinstance(value, UpdateState):
                self.update_status.setText("更新下载返回了无效结果")
                self._refresh_update_view()
                return
            self._refresh_update_view()
            if value.downloaded_path:
                if auto_open:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(value.downloaded_path))
                    return
                box = QMessageBox(self)
                box.setWindowTitle("更新包已准备好")
                box.setText("更新包已下载并通过 SHA-256 校验。")
                box.setInformativeText("打开 DMG 后，将 CodexRelay 拖入“应用程序”文件夹完成更新。")
                open_button = box.addButton("打开 DMG", QMessageBox.ButtonRole.AcceptRole)
                box.addButton("稍后再说", QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is open_button:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(value.downloaded_path))

        async def download() -> object:
            return provider.download_update()

        self._run(download, finished=finished)

    def _load_codex_status(self) -> None:
        environment = codex_subprocess_environment()
        codex_bin = discover_codex_bin(environment["PATH"])
        if codex_bin is None:
            self.codex_status.set_state("warning", "未找到本机 Codex CLI")
            LOGGER.warning("Codex CLI was not found in the menu-bar application PATH")
        else:
            self.codex_status.set_state("ready", "本机 CLI 已找到")
            LOGGER.info("found Codex CLI at %s", codex_bin)
        self._refresh_overview()

    def _storage_summary(self) -> str:
        total = 0
        for path in self.paths.data_dir.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
        return f"本地数据占用：{format_bytes(total)} · 日志自动轮转，最多约 8 MB"

    @staticmethod
    def _page(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QFrame()
        page.setObjectName("page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("pageSubtitle")
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        layout.addSpacing(8)
        return page, layout

    def _run(
        self,
        factory: AsyncFactory,
        *,
        finished: Callable[[object], None] | None = None,
        failed: Callable[[str], None] | None = None,
    ) -> None:
        worker = AsyncWorker(factory)
        if finished is not None:
            worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed or self._show_error)
        worker.signals.completed.connect(self._retire_worker)
        self._workers.add(worker)
        self.pool.start(worker)

    def _retire_worker(self, value: object) -> None:
        if not isinstance(value, AsyncWorker):
            return
        QTimer.singleShot(500, lambda: self._workers.discard(value))

    def set_model_catalog(self, catalog: CodexModelCatalog) -> None:
        self.model_catalog = catalog
        self._updating_model_controls = True
        self.model_combo.clear()
        for option in catalog.models:
            self.model_combo.addItem(option.display_name, option.model)
        self._updating_model_controls = False
        self._load_model_configuration()

    def _load_model_configuration(self) -> None:
        if self.model_catalog is None:
            return

        async def load() -> object:
            async with Database(self.paths.database) as database:
                project = await database.current_project()
                conversation = (
                    None
                    if project is None
                    else await database.get_or_create_active_conversation(
                        project.id, title=project.name
                    )
                )
                return project, conversation

        def finished(value: object) -> None:
            project, conversation = cast(tuple[Project | None, Conversation | None], value)
            if project is None or conversation is None or self.model_catalog is None:
                self.model_project_id = None
                self.model_scope.setText("尚未选择项目")
                self.model_description.setText("请先在“项目”页面添加并选择一个项目。")
                self.model_combo.setEnabled(False)
                self.reasoning_combo.setEnabled(False)
                self.save_model_button.setEnabled(False)
                return
            option, effort = self.model_catalog.effective(
                conversation.model, conversation.reasoning_effort
            )
            self.model_project_id = project.id
            self._updating_model_controls = True
            model_index = self.model_combo.findData(option.model)
            self.model_combo.setCurrentIndex(max(model_index, 0))
            self._populate_reasoning_efforts(effort)
            self._updating_model_controls = False
            self.model_combo.setEnabled(True)
            self.reasoning_combo.setEnabled(True)
            self.save_model_button.setEnabled(True)
            self.model_scope.setText(
                f"当前会话  ·  {project.name}  ·  设置随项目保留"
            )
            inherited = conversation.model is None or conversation.reasoning_effort is None
            self.model_save_status.setText("当前沿用本机默认值" if inherited else "已保存")
            self._update_model_description()

        self._run(load, finished=finished)

    def _model_changed(self, _index: int) -> None:
        if self._updating_model_controls:
            return
        current_effort = self.reasoning_combo.currentData()
        self._populate_reasoning_efforts(
            str(current_effort) if current_effort is not None else None
        )
        self._update_model_description()
        self.model_save_status.setText("有未保存的更改")

    def _reasoning_changed(self, _index: int) -> None:
        if not self._updating_model_controls:
            self.model_save_status.setText("有未保存的更改")

    def _populate_reasoning_efforts(self, preferred: str | None) -> None:
        if self.model_catalog is None:
            return
        model = self.model_combo.currentData()
        option = self.model_catalog.get(str(model)) if model is not None else None
        if option is None:
            return
        selected = preferred if preferred is not None and option.supports(preferred) else None
        selected = selected or option.default_reasoning_effort
        previous_state = self._updating_model_controls
        self._updating_model_controls = True
        self.reasoning_combo.clear()
        for effort in option.supported_reasoning_efforts:
            suffix = " · 默认" if effort == option.default_reasoning_effort else ""
            self.reasoning_combo.addItem(
                f"{reasoning_effort_label(effort)} ({effort}){suffix}", effort
            )
        effort_index = self.reasoning_combo.findData(selected)
        self.reasoning_combo.setCurrentIndex(max(effort_index, 0))
        self._updating_model_controls = previous_state

    def _update_model_description(self) -> None:
        if self.model_catalog is None:
            return
        model = self.model_combo.currentData()
        option = self.model_catalog.get(str(model)) if model is not None else None
        if option is not None:
            self.model_description.setText(f"{option.description}\n标识：{option.model}")

    def _save_model_configuration(self) -> None:
        project_id = self.model_project_id
        model = self.model_combo.currentData()
        effort = self.reasoning_combo.currentData()
        if project_id is None or model is None or effort is None or self.model_catalog is None:
            self._show_error("当前没有可保存的模型设置")
            return
        option = self.model_catalog.get(str(model))
        if option is None or not option.supports(str(effort)):
            self._show_error("所选模型与推理强度不匹配，请重新选择")
            return
        self.save_model_button.setEnabled(False)
        self.model_save_status.setText("正在保存…")

        async def save() -> object:
            async with Database(self.paths.database) as database:
                project = await database.get_project(project_id)
                if project is None:
                    raise RuntimeError("当前项目已不存在")
                return await database.set_active_conversation_model(
                    project.id,
                    model=option.model,
                    reasoning_effort=str(effort),
                    title=project.name,
                )

        def finished(_value: object) -> None:
            self.save_model_button.setEnabled(True)
            self.model_save_status.setText("已保存，从下一条任务开始生效；上下文保持不变")

        def failed(message: str) -> None:
            self.save_model_button.setEnabled(True)
            self.model_save_status.setText("保存失败")
            self._show_error(message)

        self._run(save, finished=finished, failed=failed)

    def _load_token_status(self) -> None:
        async def load() -> object:
            return await self.secret_store.get_telegram_token(self.settings.telegram.account_id)

        def finished(value: object) -> None:
            if isinstance(value, str) and value:
                self.token_status.setText("Token已保存在CodexRelay私有数据中")
                self.telegram_status.set_state("ready", "已配置")
            else:
                self.token_status.setText("尚未配置Token")
                self.telegram_status.set_state("warning", "需要配置")
            self._refresh_overview()

        self._run(load, finished=finished)

    def _save_token(self) -> None:
        token = self.token_edit.text().strip()
        if not token:
            self._show_error("请输入Telegram Bot Token")
            return
        self.save_token_button.setEnabled(False)
        self.token_status.setText("正在验证…")

        async def save() -> object:
            async with TelegramClient(token) as client:
                bot = await client.get_me()
            await self.secret_store.set_telegram_token(token, self.settings.telegram.account_id)
            return bot

        def finished(value: object) -> None:
            self.save_token_button.setEnabled(True)
            self.token_edit.clear()
            bot = value if isinstance(value, dict) else {}
            username = bot.get("username", "Telegram Bot")
            self.token_status.setText(f"已验证并保存：@{username}")
            self.telegram_status.set_state("ready", f"@{username}")
            self._refresh_overview()
            self.runtime_configuration_changed.emit()

        self._run(save, finished=finished)

    def _generate_pairing_code(self) -> None:
        async def generate() -> object:
            async with Database(self.paths.database) as database:
                return await PairingService(database).generate(
                    account_id=self.settings.telegram.account_id
                )

        def finished(value: object) -> None:
            code = getattr(value, "code", None)
            expires_at = getattr(value, "expires_at", None)
            if isinstance(code, str):
                self.pairing_code.setText(" ".join((code[:3], code[3:])))
                self.pairing_hint.setText(f"在Telegram发送 /pair {code}，有效期至 {expires_at}")

        self._run(generate, finished=finished)

    def _load_projects(self) -> None:
        async def load() -> object:
            async with Database(self.paths.database) as database:
                return await ProjectService(database).list_projects()

        def finished(value: object) -> None:
            projects = value if isinstance(value, list) else []
            self.project_list.clear()
            current_name = "尚未选择"
            for project in projects:
                prefix = "✓ 当前" if project.is_current else "○"
                item = QListWidgetItem(f"{prefix}  {project.name}\n    {project.path}")
                item.setData(Qt.ItemDataRole.UserRole, project.id)
                item.setToolTip(str(project.path))
                self.project_list.addItem(item)
                if project.is_current:
                    current_name = project.name
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    self.project_list.setCurrentItem(item)
            state = "ready" if projects else "warning"
            self.project_status.set_state(state, current_name)
            self._refresh_overview()
            self._load_model_configuration()

        self._run(load, finished=finished)

    def _add_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择项目目录")
        if not selected:
            return

        async def add() -> object:
            async with Database(self.paths.database) as database:
                return await ProjectService(database).register(Path(selected))

        self._run(add, finished=lambda _value: self._load_projects())

    def _scan_projects(self) -> None:
        async def scan() -> object:
            async with Database(self.paths.database) as database:
                service = ProjectService(database)
                roots = [Path(root).expanduser() for root in self.settings.projects.scan_roots]
                found = service.discover(roots, max_depth=self.settings.projects.scan_depth)
                for path in found:
                    await service.register(path)
                return len(found)

        def finished(value: object) -> None:
            self._load_projects()
            self.overview_message.setText(f"扫描完成，找到并登记 {value} 个项目。")

        self._run(scan, finished=finished)

    def _switch_project(self) -> None:
        item = self.project_list.currentItem()
        if item is None:
            self._show_error("请先选择一个项目")
            return
        project_id = item.data(Qt.ItemDataRole.UserRole)

        async def switch() -> object:
            async with Database(self.paths.database) as database:
                return await ProjectService(database).switch(str(project_id))

        self._run(switch, finished=lambda _value: self._load_projects())

    def _save_system_settings(self) -> None:
        if self.startup_service.available:
            self.startup_service.set_enabled(self.launch_at_login.isChecked())
        self.settings = Settings(
            app=AppSection(
                auto_connect=self.auto_connect.isChecked(),
                launch_at_login=self.launch_at_login.isChecked(),
                prevent_sleep_while_running=self.prevent_sleep.isChecked(),
                update_checks_automatically=self.settings.app.update_checks_automatically,
            ),
            telegram=self.settings.telegram,
            projects=self.settings.projects,
        )
        self.settings_store.save(self.settings)
        self.storage_status.setText(self._storage_summary())
        self.overview_message.setText("系统设置已保存。")
        self.runtime_configuration_changed.emit()

    def set_runtime_connected(self, username: str) -> None:
        self._telegram_username = username
        self._update_telegram_status()
        self.codex_status.set_state("ready", "App Server已就绪")
        self._refresh_overview()

    def set_telegram_pairing_state(self, paired: bool) -> None:
        was_paired = self._telegram_paired
        self._telegram_paired = paired
        self.pair_button.setEnabled(not paired)
        self.pair_button.setText("已配对" if paired else "生成一次性配对码")
        if paired:
            self.pairing_hint.setText("当前 Telegram 账号已完成配对。配对失效后可重新生成。")
        elif was_paired:
            self.pairing_code.setText("—— —— ——")
            self.pairing_hint.setText("生成后10分钟内，在Telegram发送 /pair 配对码")
        self._update_telegram_status()
        self._refresh_overview()

    def _update_telegram_status(self) -> None:
        if self._telegram_username is None:
            return
        pairing_title = "已配对" if self._telegram_paired else "待完成配对"
        self.telegram_status.set_state(
            "ready", f"@{self._telegram_username} · 已连接 · {pairing_title}"
        )

    def set_runtime_failed(self, message: str) -> None:
        if "Token is not configured" in message:
            self.telegram_status.set_state("warning", "需要配置")
        else:
            self.telegram_status.set_state("warning", "连接失败")
        if "Codex CLI was not found" in message:
            self.codex_status.set_state("warning", "未找到本机 Codex CLI")
        self.overview_message.setText(message)

    def _refresh_overview(self) -> None:
        telegram = self.telegram_status.detail.text()
        project = self.project_status.detail.text()
        self.overview_message.setText(f"Telegram {telegram} · 当前项目 {project}")

    def _show_error(self, message: str) -> None:
        self.save_token_button.setEnabled(True)
        QMessageBox.critical(self, "CodexRelay", message)

    def closeEvent(self, event: Any) -> None:
        event.ignore()
        self.hide()
        self.settings_closed.emit()


class TrayApplication(QObject):
    def __init__(self, application: QApplication) -> None:
        super().__init__()
        self.application = application
        self.paths = AppPaths.default()
        self.paths.ensure()
        self.pool = QThreadPool.globalInstance()
        self.update_provider: UpdateProvider = GitHubReleaseUpdateProvider(
            download_directory=self.paths.data_dir / "updates"
        )
        self.window = SettingsWindow(self.paths, self.pool)
        self.runtime_thread: RuntimeThread | None = None
        self.restart_requested = False
        self._quitting = False
        self._status_refresh_running = False
        self._workers: set[AsyncWorker] = set()
        self.snapshot = AppStatusSnapshot()
        self.window.set_update_provider(self.update_provider)

        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("CodexRelay")
        self.menu = QMenu()
        self.menu.setObjectName("trayMenu")
        self.menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.overview = MenuOverview()
        overview_action = QWidgetAction(self.menu)
        overview_action.setDefaultWidget(self.overview)
        self.menu.addAction(overview_action)
        self.menu.addSeparator()

        self.restart_action = QAction("重新连接", self.menu)
        self.restart_action.triggered.connect(self.restart_runtime)
        self.menu.addAction(self.restart_action)
        self.stop_action = QAction("停止当前任务", self.menu)
        self.stop_action.triggered.connect(self.stop_current_task)
        self.stop_action.setEnabled(False)
        self.menu.addAction(self.stop_action)
        self.menu.addSeparator()

        # Keep tray-menu actions separate from the native application-menu
        # actions. On macOS, QAction.MenuRole entries are merged into the
        # application menu and can disappear from a QSystemTrayIcon menu.
        tray_settings_action = QAction("打开设置…", self.menu)
        tray_settings_action.triggered.connect(self.show_window)
        self.menu.addAction(tray_settings_action)
        tray_about_action = QAction("关于 CodexRelay", self.menu)
        tray_about_action.triggered.connect(self.show_about)
        self.menu.addAction(tray_about_action)
        self.update_action = QAction("", self.menu)
        self.update_action.setVisible(False)
        self.update_action.triggered.connect(self._start_update_from_menu)
        self.menu.addAction(self.update_action)
        self.menu.addSeparator()
        self.tray_quit_action = QAction("退出 CodexRelay", self.menu)
        self.tray_quit_action.triggered.connect(self.request_quit)
        self.menu.addAction(self.tray_quit_action)

        settings_action = QAction("设置…", self.window)
        settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        settings_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Preferences))
        settings_action.triggered.connect(self.show_window)
        about_action = QAction("关于 CodexRelay", self.window)
        about_action.setMenuRole(QAction.MenuRole.AboutRole)
        about_action.triggered.connect(self.show_about)
        self.quit_action = QAction("退出 CodexRelay", self.window)
        self.quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self.quit_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Quit))
        self.quit_action.triggered.connect(self.request_quit)
        self.window.update_available.connect(self._notify_update_available)
        self.window.update_state_changed.connect(self._sync_update_action)
        self.tray.messageClicked.connect(self.show_about)
        self.tray.setContextMenu(self.menu)
        self.window.install_application_menu(
            settings_action=settings_action,
            quit_action=self.quit_action,
            about_action=about_action,
        )
        self.window.runtime_configuration_changed.connect(self.apply_runtime_configuration)
        self.tray.show()

        self.status_timer = QTimer(self)
        self.status_timer.setInterval(2000)
        self.status_timer.timeout.connect(self.refresh_snapshot)
        self.status_timer.start()
        self.refresh_snapshot()
        if self.window.settings.app.update_checks_automatically:
            QTimer.singleShot(1500, lambda: self.window.check_for_updates(automatic=True))
        if self.window.settings.app.auto_connect:
            self.start_runtime()

    def show_window(self) -> None:
        self.menu.close()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def show_about(self) -> None:
        self.show_window()
        if self.window.height() < 760:
            self.window.resize(max(self.window.width(), 860), 780)
        self.window.navigation.setCurrentRow(4)

    def _notify_update_available(self, version: str) -> None:
        self.update_action.setText("发现新版本，下载更新…")
        self.update_action.setVisible(True)
        self.tray.showMessage(
            "CodexRelay 更新可用",
            f"检测到新版本 v{version}。点击菜单栏提示下载更新。",
            QSystemTrayIcon.MessageIcon.Information,
            10000,
        )

    def _sync_update_action(self, value: object) -> None:
        if not isinstance(value, UpdateState):
            return
        if value.available_version and value.asset_url:
            verb = "打开 DMG" if value.downloaded_path else "下载更新"
            self.update_action.setText(f"发现新版本，{verb}…")
            self.update_action.setVisible(True)
        else:
            self.update_action.setVisible(False)

    def _start_update_from_menu(self) -> None:
        self.show_about()
        self.window._trigger_update_action()

    def _activated(self, _reason: QSystemTrayIcon.ActivationReason) -> None:
        # On macOS, a tray context menu opens on the primary click. Keeping this
        # handler intentionally empty prevents a click from opening the settings window.
        return

    def _run(self, factory: AsyncFactory, finished: Callable[[object], None]) -> None:
        worker = AsyncWorker(factory)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(self._background_error)
        worker.signals.completed.connect(self._retire_worker)
        self._workers.add(worker)
        self.pool.start(worker)

    def _retire_worker(self, value: object) -> None:
        if isinstance(value, AsyncWorker):
            QTimer.singleShot(500, lambda: self._workers.discard(value))

    def _background_error(self, message: str) -> None:
        LOGGER.warning("menu-bar background operation failed: %s", message)

    def refresh_snapshot(self) -> None:
        if self._status_refresh_running or self._quitting:
            return
        self._status_refresh_running = True

        async def load() -> object:
            async with Database(self.paths.database) as database:
                current = await database.current_project()
                active_project = await database.active_job_project()
                active_count = await database.active_job_count()
                active_status = await database.active_job_status()
                telegram_paired = await database.has_enabled_identity(
                    connector_type="telegram", account_id="main-bot"
                )
                conversation = (
                    None
                    if current is None
                    else await database.active_conversation(current.id)
                )
                return (
                    current,
                    active_project,
                    active_count,
                    active_status,
                    conversation,
                    telegram_paired,
                )

        def finished(value: object) -> None:
            self._status_refresh_running = False
            (
                current,
                active_project,
                active_count,
                active_status,
                conversation,
                telegram_paired,
            ) = cast(
                tuple[
                    Project | None,
                    Project | None,
                    int,
                    JobStatus | None,
                    Conversation | None,
                    bool,
                ],
                value,
            )
            self.snapshot = self.snapshot.persisted(
                telegram_paired=telegram_paired,
                current_project=None if current is None else current.name,
                active_project=None if active_project is None else active_project.name,
                active_job_count=active_count,
                active_job_status=active_status,
                model=None if conversation is None else conversation.model,
                reasoning_effort=None if conversation is None else conversation.reasoning_effort,
            )
            self.overview.set_snapshot(self.snapshot)
            self.window.set_telegram_pairing_state(telegram_paired)
            self.stop_action.setEnabled(active_count > 0)
            self.tray.setToolTip(f"CodexRelay · {self.snapshot.connection_title}")

        self._run(load, finished)

    def start_runtime(self) -> None:
        if self.runtime_thread is not None and self.runtime_thread.isRunning():
            return
        thread = RuntimeThread(self.paths)
        thread.connected.connect(self._runtime_connected)
        thread.failed.connect(self._runtime_failed)
        thread.stopped.connect(self._runtime_stopped)
        self.runtime_thread = thread
        self.snapshot = replace(self.snapshot, runtime_state=RuntimeState.STARTING, last_error=None)
        self.overview.set_snapshot(self.snapshot)
        LOGGER.info("starting relay runtime")
        thread.start()

    def restart_runtime(self) -> None:
        if self._quitting:
            return
        thread = self.runtime_thread
        if thread is not None and thread.isRunning():
            self.restart_requested = True
            self.snapshot = replace(self.snapshot, runtime_state=RuntimeState.RESTARTING)
            self.overview.set_snapshot(self.snapshot)
            thread.request_stop()
            return
        self.start_runtime()

    def apply_runtime_configuration(self) -> None:
        if self.window.settings.app.auto_connect:
            self.restart_runtime()
            return
        thread = self.runtime_thread
        if thread is not None and thread.isRunning():
            thread.request_stop()
        self.snapshot = replace(self.snapshot, runtime_state=RuntimeState.STOPPED)
        self.overview.set_snapshot(self.snapshot)

    def stop_current_task(self) -> None:
        if self.snapshot.active_job_count == 0 or self.runtime_thread is None:
            return
        self._confirm_stop_task()

    def _confirm_stop_task(self) -> None:
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("停止当前任务")
        box.setText("确定停止当前任务吗？")
        box.setInformativeText("Codex 将收到中断请求，任务会保留为“已中断”。")
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        stop = box.addButton("停止任务", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is stop:
            thread = self.runtime_thread
            if thread is not None:
                thread.interrupt_current_task()

    def _runtime_connected(self, username: str, catalog: object) -> None:
        LOGGER.info("relay connected to Telegram bot @%s", username)
        self.snapshot = replace(
            self.snapshot,
            runtime_state=RuntimeState.CONNECTED,
            bot_username=username,
            last_error=None,
        )
        self.overview.set_snapshot(self.snapshot)
        if isinstance(catalog, CodexModelCatalog):
            self.window.set_model_catalog(catalog)
        self.window.set_runtime_connected(username)
        self.refresh_snapshot()

    def _runtime_failed(self, message: str) -> None:
        LOGGER.warning("relay requires attention: %s", message)
        if hasattr(self, "snapshot"):
            self.snapshot = replace(
                self.snapshot,
                runtime_state=RuntimeState.ATTENTION,
                last_error=message,
            )
        if hasattr(self, "overview"):
            self.overview.set_snapshot(self.snapshot)
        if hasattr(self, "status_action"):
            self.status_action.setText("CodexRelay · 需要处理")
        self.window.set_runtime_failed(message)
        if "Telegram Bot Token is not configured" in message:
            self.window.navigation.setCurrentRow(0 if hasattr(self.window, "pages") else 1)
            self.show_window()

    def _runtime_stopped(self) -> None:
        LOGGER.info("relay runtime stopped")
        thread = self.runtime_thread
        self.runtime_thread = None
        if thread is not None:
            thread.deleteLater()
        if self._quitting:
            self._finalize_quit()
            return
        if self.restart_requested:
            self.restart_requested = False
            self.start_runtime()
            return
        self.snapshot = replace(self.snapshot, runtime_state=RuntimeState.STOPPED)
        self.overview.set_snapshot(self.snapshot)

    def request_quit(self) -> None:
        if self._quitting:
            return

        async def load() -> object:
            async with Database(self.paths.database) as database:
                return await database.active_job_count()

        def finished(value: object) -> None:
            self._show_quit_confirmation(int(value) if isinstance(value, int) else 0)

        self._run(load, finished)

    def _show_quit_confirmation(self, active_count: int) -> None:
        dialog = QuitConfirmationDialog(active_count=active_count, parent=self.window)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._begin_quit()

    def _begin_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.status_timer.stop()
        self.snapshot = replace(self.snapshot, runtime_state=RuntimeState.STOPPING)
        self.overview.set_snapshot(self.snapshot)
        # Make the user-visible quit instantaneous. The runtime thread still
        # performs its orderly shutdown in the background; keeping the tray
        # icon visible until then makes a normal network drain look like a hang.
        self.tray.hide()
        self.window.hide()
        self.restart_action.setEnabled(False)
        self.stop_action.setEnabled(False)
        self.tray_quit_action.setEnabled(False)
        self.quit_action.setEnabled(False)
        thread = self.runtime_thread
        if thread is None or not thread.isRunning():
            self._finalize_quit()
            return
        thread.request_stop()
        QTimer.singleShot(35_000, self._quit_timeout)

    def _quit_timeout(self) -> None:
        thread = self.runtime_thread
        if not self._quitting or thread is None or not thread.isRunning():
            return
        self._quitting = False
        self.snapshot = replace(
            self.snapshot,
            runtime_state=RuntimeState.ATTENTION,
            last_error="后台服务仍在停止，请稍后再试。",
        )
        self.overview.set_snapshot(self.snapshot)
        self.tray.show()
        self.tray_quit_action.setEnabled(True)
        self.quit_action.setEnabled(True)
        QMessageBox.warning(
            self.window,
            "退出未完成",
            "后台服务仍在停止，CodexRelay 保持运行以避免丢失状态。",
        )

    def _finalize_quit(self) -> None:
        if not self._quitting:
            return
        LOGGER.info("CodexRelay is quitting")
        self.tray.hide()
        self.application.quit()

    def shutdown_for_test(self) -> None:
        self._begin_quit()

    def quit(self) -> None:
        self.request_quit()


def make_icon() -> QIcon:
    icon = QIcon()
    logical_size = 22
    for device_pixel_ratio in (1.0, 2.0):
        physical_size = round(logical_size * device_pixel_ratio)
        pixmap = QPixmap(QSize(physical_size, physical_size))
        pixmap.setDevicePixelRatio(device_pixel_ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(Qt.GlobalColor.black), 2.1)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        left = QPainterPath()
        left.moveTo(9, 4)
        left.lineTo(7.2, 4)
        left.cubicTo(5, 4, 4, 5.5, 4, 7.6)
        left.lineTo(4, 14.4)
        left.cubicTo(4, 16.5, 5, 18, 7.2, 18)
        left.lineTo(9, 18)
        painter.drawPath(left)
        right = QPainterPath()
        right.moveTo(13, 4)
        right.lineTo(14.8, 4)
        right.cubicTo(17, 4, 18, 5.5, 18, 7.6)
        right.lineTo(18, 14.4)
        right.cubicTo(18, 16.5, 17, 18, 14.8, 18)
        right.lineTo(13, 18)
        painter.drawPath(right)
        painter.drawLine(QLineF(9, 11, 13, 11))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.black)
        painter.drawEllipse(QRectF(9.9, 9.9, 2.2, 2.2))
        painter.end()
        icon.addPixmap(pixmap)
    # macOS renders mask icons as Template images and automatically switches
    # between black and white for light/dark menu bars.
    icon.setIsMask(True)
    return icon


def format_bytes(value: int) -> str:
    amount = float(max(value, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            precision = 0 if unit == "B" else 1
            return f"{amount:.{precision}f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


STYLE_SHEET = """
QDialog#quitDialog { background: transparent; }
QFrame#quitCard { background: rgba(250, 251, 253, 250); border: 1px solid rgba(194, 204, 214, 180);
    border-radius: 18px; }
QLabel#quitTitle { color: #1C1C1E; font-size: 17px; font-weight: 700; }
QLabel#quitMessage { color: #636A73; font-size: 13px; line-height: 1.35; }
QFrame#quitDivider { background: #E5E7EB; max-height: 1px; border: 0; }
QPushButton#quitCancelButton, QPushButton#quitPrimaryButton, QPushButton#quitDangerButton {
    min-height: 30px; max-height: 30px; padding: 0 14px; border-radius: 8px;
    font-size: 12px; font-weight: 600; }
QPushButton#quitCancelButton { background: rgba(233, 237, 242, 190); color: #30343A;
    border: 1px solid rgba(190, 199, 208, 170); }
QPushButton#quitCancelButton:hover { background: rgba(222, 228, 235, 220); }
QPushButton#quitCancelButton:pressed { background: rgba(211, 218, 226, 230); }
QPushButton#quitPrimaryButton { background: #1476E8; color: #FFFFFF; border: 1px solid #1476E8; }
QPushButton#quitPrimaryButton:hover { background: #0D68D2; border-color: #0D68D2; }
QPushButton#quitPrimaryButton:pressed { background: #095DBD; border-color: #095DBD; }
QPushButton#quitDangerButton { background: #D94B4B; color: #FFFFFF; border: 1px solid #D94B4B; }
QPushButton#quitDangerButton:hover { background: #C73F3F; border-color: #C73F3F; }
QPushButton#quitDangerButton:pressed { background: #B73535; border-color: #B73535; }
QWidget#root { background: #F3F5F7; color: #18202A; }
QLabel#eyebrow { color: #57708C; font-size: 10px; font-weight: 700; letter-spacing: 2px; }
QLabel#windowTitle { font-size: 30px; font-weight: 650; }
QLabel#subtitle, QLabel#pageSubtitle, QLabel#hint, QLabel#statusDetail {
    color: #66727F;
}
QFrame#signalRail { background: #E8EDF2; border-radius: 16px; }
QFrame#statusStrip { background: #E9EFF4; border: 1px solid #DCE4EB; border-radius: 10px; }
QFrame#page { background: #FFFFFF; border: 1px solid #DDE3E9; border-radius: 16px; }
QLabel#sectionLabel { color: #728096; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QLabel#statusTitle { font-weight: 600; }
QLabel#statusDot { color: #8A96A3; font-size: 13px; }
QLabel#statusDot[state="ready"] { color: #16875B; }
QLabel#statusDot[state="warning"] { color: #D9891B; }
QLabel#menuConnection { color: #1A9A5B; font-size: 12px; font-weight: 700; }
QLabel#menuConnection[state="connected"] { color: #1A9A5B; }
QLabel#menuConnection[state="starting"], QLabel#menuConnection[state="restarting"] {
    color: #C47A16;
}
QLabel#menuConnection[state="attention"] { color: #B24736; }
QLabel#menuConnection[state="stopped"] { color: #7C8792; }
QLabel#menuConnection[state="stopping"] { color: #607B91; }
QWidget#menuOverview { background: transparent; color: #18202A; }
QLabel#menuTitle { color: #18202A; font-size: 15px; font-weight: 700; }
QLabel#menuMuted { color: #707B86; font-size: 12px; }
QLabel#menuSectionLabel { color: #7A8793; font-size: 10px; font-weight: 700; }
QLabel#menuCaption { color: #7A8793; font-size: 10px; font-weight: 600; }
QLabel#menuProjectValue { color: #18202A; font-size: 15px; font-weight: 700; }
QLabel#menuSessionValue { color: #304252; font-size: 12px; font-weight: 650; }
QFrame#menuContextCard { background: rgba(255, 255, 255, 78);
    border: 1px solid rgba(166, 184, 198, 115);
    border-radius: 12px; }
QFrame#menuTaskCard { background: rgba(231, 242, 249, 92);
    border: 1px solid rgba(133, 174, 198, 120);
    border-radius: 12px; }
QFrame#menuCardSeparator { color: rgba(166, 184, 198, 95); max-height: 1px; }
QLabel#menuTaskValue { color: #18202A; font-size: 14px; font-weight: 700; }
QLabel#menuTaskDetail { color: #667986; font-size: 11px; }
QLabel#menuNotice { background: #FFF4E9; color: #8B4E1D; border: 1px solid #F0D5B7;
    border-radius: 7px; padding: 7px 9px; font-size: 11px; }
QFrame#menuSeparator { color: #E3E7EB; max-height: 1px; }
QMenu { background: #FFFFFF; color: #18202A; border: 1px solid #D9E0E7;
    border-radius: 11px; padding: 7px; }
/*
 * The overview is intentionally a little translucent: enough of the desktop
 * to show through to feel native, while retaining a calm, high-contrast
 * surface for status text and actions.  Keep this override scoped to the
 * menu-bar panel so settings menus remain fully opaque and easy to scan.
 */
QMenu#trayMenu { background: rgba(250, 252, 255, 202); border-color: rgba(190, 203, 214, 170);
    padding: 7px; }
QMenu::item { padding: 8px 28px 8px 10px; border-radius: 6px; }
QMenu::item:selected { background: #EAF2F8; color: #164C75; }
QMenu::separator { height: 1px; background: #E3E7EB; margin: 6px 8px; }
QLabel#railConnector { color: #B5C0CB; }
QListWidget#navigation { background: transparent; border: 0; outline: 0; }
QListWidget#navigation::item { padding: 8px 14px; border-radius: 8px; }
QListWidget#navigation::item:selected { background: #D5E5F6; color: #174A78; }
QLabel#pageTitle { color: #18202A; font-size: 22px; font-weight: 650; min-height: 28px; }
QFrame#aboutHero { background: #EEF4F8; border: 1px solid #D9E5EC; border-radius: 16px; }
QLabel#aboutAppName { color: #18202A; font-size: 23px; font-weight: 700; }
QLabel#aboutTagline { color: #536574; font-size: 13px; }
QLabel#aboutVersion { color: #246AA5; font-size: 13px; font-weight: 700; }
QLabel#aboutMeta { color: #74818B; font-size: 11px; }
QLabel#aboutMetaLabel { color: #82909B; font-size: 10px; font-weight: 650; }
QLabel#aboutMetaValue { color: #2A3945; font-size: 12px; font-weight: 650; }
QLabel#aboutSectionTitle { color: #18202A; font-size: 16px; font-weight: 700; padding-top: 7px; }
QFrame#aboutCard { background: #FFFFFF; border: 1px solid #DDE3E9; border-radius: 14px; }
QLabel#aboutRowTitle { color: #273541; font-size: 13px; font-weight: 600; padding: 9px 0; }
QFrame#aboutDivider { color: #E3E8ED; max-height: 1px; }
QDialog#updateDialog { background: #F6F8FA; }
QLabel#updateDialogTitle { color: #18202A; font-size: 21px; font-weight: 700; }
QLabel#updateDialogCurrent { color: #778492; font-size: 12px; }
QLabel#updateDialogLabel { color: #7A8793; font-size: 13px; min-width: 66px; }
QLabel#updateDialogValue { color: #263746; font-size: 14px; font-weight: 650; }
QTextBrowser#updateNotes { background: #FFFFFF; color: #263746; border: 1px solid #DDE3E9;
    border-radius: 10px; padding: 10px; font-size: 13px; }
QProgressBar#updateProgress { border: 0; border-radius: 3px; background: #DDE6ED; max-height: 6px; }
QProgressBar#updateProgress::chunk { background: #2E8BCA; border-radius: 3px; }
QPushButton#secondaryButton { background: #F3F6F8; color: #246AA5; border-color: #C9D8E3; }
QPushButton#secondaryButton:hover { background: #EAF2F8; border-color: #9CB4C8; }
QPushButton#linkButton { background: transparent; color: #246AA5; border: 0; border-radius: 7px;
    text-align: left; padding: 9px 2px; font-size: 13px; }
QPushButton#linkButton:hover { background: #F0F5F8; color: #174A78; }
QLabel#inlineStatus { color: #66727F; font-size: 11px; padding: 3px 2px; }
QLabel#heroStatus { background: #EAF2FA; color: #174A78; padding: 18px; border-radius: 12px;
    font-size: 16px; font-weight: 600; }
QLabel#scopeStatus { background: #F1F6F4; color: #2F654F; border: 1px solid #D9E8E1;
    padding: 8px 11px; border-radius: 8px; font-size: 12px; font-weight: 600; }
QLabel#bodyText { color: #465464; font-size: 13px; line-height: 1.5; }
QLabel#fieldLabel { font-weight: 650; }
QLabel#compactFieldLabel { color: #596878; font-size: 11px; font-weight: 650; }
QLabel#pairingCode { color: #174A78; font-family: Menlo; font-size: 30px; font-weight: 700;
    letter-spacing: 4px; padding: 14px 0; }
QLineEdit, QListWidget#projectList { background: #F7F9FB; color: #18202A;
    selection-background-color: #D5E5F6; selection-color: #174A78;
    border: 1px solid #CDD6DF; border-radius: 9px; padding: 9px; outline: 0; }
QPushButton#choiceButton { background: #F7F9FB; color: #1D2733;
    border: 1px solid #D1DAE3; border-radius: 9px; padding: 8px 34px 8px 12px;
    text-align: left; font-size: 13px; }
QPushButton#choiceButton:hover { background: #F1F5F8; border-color: #9CB4C8; }
QPushButton#choiceButton:focus { border: 2px solid #5A97C7;
    padding: 7px 33px 7px 11px; }
QPushButton#choiceButton:disabled { background: #F3F5F7; color: #9AA4AE;
    border-color: #E0E5EA; }
QMenu#choiceMenu { background: #FFFFFF; color: #1D2733; border: 1px solid #D9E0E7;
    border-radius: 10px; padding: 6px; }
QMenu#choiceMenu::item { padding: 8px 28px 8px 10px; border-radius: 6px; }
QMenu#choiceMenu::item:selected { background: #EAF2F8; color: #164C75; }
QMenu#choiceMenu::item:checked { font-weight: 650; }
QListWidget#projectList::item { color: #18202A; padding: 9px;
    border-bottom: 1px solid #E3E8ED; border-radius: 6px; }
QListWidget#projectList::item:hover { background: #EAF1F7; color: #18202A; }
QListWidget#projectList::item:selected, QListWidget#projectList::item:selected:!active {
    background: #D5E5F6; color: #174A78; border: 1px solid #8AB6DE;
}
QPushButton { background: #EDF1F5; border: 1px solid #CDD6DF; border-radius: 8px;
    padding: 8px 13px; }
QPushButton:hover { background: #E3EAF1; }
QPushButton#primaryButton { background: #246AA5; color: white; border-color: #246AA5; }
QPushButton#primaryButton:hover { background: #1C5A8D; }
QPushButton:disabled { color: #9AA5AF; }
"""


def main() -> None:
    paths = AppPaths.default()
    paths.ensure()
    log_path = configure_logging(paths.log_dir)
    LOGGER.info("CodexRelay starting; log=%s", log_path)
    instance_lock = SingleInstanceLock(paths.data_dir / "instance.lock")
    try:
        instance_lock.acquire()
    except AlreadyRunningError:
        return
    application = QApplication(sys.argv)
    application.setApplicationName("CodexRelay")
    application.setOrganizationName("CodexRelay")
    application.setQuitOnLastWindowClosed(False)
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    font.setPointSize(13)
    application.setFont(font)
    tray_application = TrayApplication(application)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        tray_application.show_window()
    smoke_quit_ms = os.environ.get("CODEXRELAY_SMOKE_QUIT_MS")
    if smoke_quit_ms is not None:
        QTimer.singleShot(max(int(smoke_quit_ms), 0), tray_application.shutdown_for_test)
    try:
        raise SystemExit(application.exec())
    finally:
        instance_lock.release()


if __name__ == "__main__":
    main()
