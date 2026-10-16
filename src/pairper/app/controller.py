"""Desktop lifetime owns tray, window and supervisor; React owns no background processes."""

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QMessageBox, QSystemTrayIcon

from pairper.app.instance import SingleInstance
from pairper.tunnel.supervisor import TunnelSupervisor


def tray_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("transparent"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#285e57"))
    painter.setPen(QColor("#285e57"))
    painter.drawRoundedRect(3, 3, 58, 58, 14, 14)
    painter.setFont(QFont("Segoe UI", 32, QFont.Weight.Bold))
    painter.setPen(QColor("white"))
    painter.drawText(pixmap.rect(), 0x84, "P")
    painter.end()
    return QIcon(pixmap)


class ApplicationController(QObject):
    def __init__(
        self,
        app,
        library,
        root,
        *,
        window_factory=None,
        supervisor=None,
        instance=None,
        tray_available=None,
    ):
        super().__init__()
        self.app, self.library, self.root = app, library, root
        self.instance = instance or SingleInstance(root, self)
        self.instance.activated.connect(self.show_window)
        self.supervisor = supervisor or TunnelSupervisor(root, self)
        self.supervisor.statusChanged.connect(self.update_status)
        self.supervisor.stopped.connect(self.finished)
        self.window_factory = window_factory
        self.window = None
        self.tray = None
        self.menu = None
        self.quitting = False
        self.finished_once = False
        self.notified = False
        self.tray_available = (
            QSystemTrayIcon.isSystemTrayAvailable() if tray_available is None else tray_available
        )

    def start(self, start_in_tray=False):
        self.app.setQuitOnLastWindowClosed(False)
        if not self.instance.acquire():
            if not self.instance.activate_existing():
                raise RuntimeError("기존 Pairper가 시작 중입니다. 잠시 후 다시 실행하세요.")
            return False
        try:
            if self.window_factory:
                self.window = self.window_factory()
            else:
                from pairper.ui.window import MainWindow

                self.window = MainWindow(self.library, project=self.root)
            self.window.setWindowIcon(tray_icon())
            # The close handler is a Python callback; it is never published through WebChannel.
            self.window.close_handler = self.on_window_close
            if hasattr(self.window, "bridge"):
                self.window.bridge.attach_supervisor(self.supervisor)
            if self.tray_available:
                self.create_tray()
            self.supervisor.start()
            if not start_in_tray or not self.tray_available:
                self.show_window()
            self.app.aboutToQuit.connect(self.emergency_stop)
            return True
        except Exception:
            self.supervisor.stop()
            self.instance.close()
            raise

    def create_tray(self):
        self.tray = QSystemTrayIcon(tray_icon(), self)
        self.menu = QMenu()
        self.actions = {}
        for text, callback in (
            ("Pairper 열기", self.show_window),
            ("연결 상태", self.show_status),
            ("다시 연결", self.supervisor.reconnect),
            ("Pairper 종료", self.quit),
        ):
            action = QAction(text, self.menu)
            action.triggered.connect(callback)
            self.menu.addAction(action)
            self.actions[text] = action
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.setToolTip("Pairper · MCP 연결 준비 중")
        self.tray.show()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()

    def show_window(self):
        if self.window and not self.quitting:
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()

    def on_window_close(self, event):
        if self.quitting:
            return False  # Allow the window to release frontend resources after tunnel shutdown.
        if self.tray and self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.window.hide()
            if not self.notified:
                self.notified = True
                self.tray.showMessage(
                    "Pairper",
                    "시스템 트레이에서 계속 실행됩니다. 종료하려면 'Pairper 종료'를 선택하세요.",
                )
        else:
            # Never leave a hidden, un-restorable app in environments without a tray.
            event.ignore()
            self.quit()
        return True

    def update_status(self, status):
        if self.tray:
            self.tray.setToolTip(f"Pairper · {status['message']}")
        if self.window and hasattr(self.window, "bridge"):
            self.window.bridge.connectionChanged.emit()

    def show_status(self):
        data = self.supervisor.snapshot()
        QMessageBox.information(
            self.window,
            "Pairper 연결 상태",
            f"{data['message']}\n최근 확인: {data.get('checked_at') or '없음'}\n"
            f"최근 정상 통신: {data.get('last_healthy_at') or '없음'}\n"
            f"최근 터널 도구: {data.get('last_tunnel_tool') or '없음'}\n"
            "런타임 준비와 ChatGPT 실제 호출 기록은 별도로 확인합니다.",
        )

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        if self.tray:
            self.tray.setToolTip("Pairper · 연결을 정리하고 종료하는 중")
            self.actions["다시 연결"].setEnabled(False)
        self.supervisor.stop()
        # The worker emits stopped only after terminating its tunnel/MCP process tree.
        if not self.supervisor.thread or not self.supervisor.thread.is_alive():
            QTimer.singleShot(0, self.finished)

    def finished(self):
        if not self.quitting or self.finished_once:
            return
        self.finished_once = True
        if self.window:
            self.window.close()
        if self.tray:
            self.tray.hide()
        self.instance.close()
        self.app.quit()

    def emergency_stop(self):
        # Covers OS session shutdown or an external QApplication.quit(). No GUI thread waits.
        self.supervisor.stop()
        self.quitting = True
        if self.window and not self.finished_once:
            self.window.close()
        if self.tray:
            self.tray.hide()
        self.instance.close()
