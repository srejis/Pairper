import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from pairper.app.controller import ApplicationController
from pairper.app.instance import SingleInstance


def qt_app():
    return QApplication.instance() or QApplication([])


class App(QObject):
    aboutToQuit = Signal()

    def __init__(self):
        super().__init__()
        self.quit_count = 0

    def setQuitOnLastWindowClosed(self, value):
        self.quit_on_close = value

    def quit(self):
        self.quit_count += 1


class Instance(QObject):
    activated = Signal()

    def __init__(self, owned=True):
        super().__init__()
        self.owned = owned
        self.shown = False
        self.closed = False

    def acquire(self):
        return self.owned

    def activate_existing(self):
        self.shown = True
        return True

    def close(self):
        self.closed = True


class Supervisor(QObject):
    statusChanged = Signal(dict)
    stopped = Signal()
    thread = None

    def __init__(self):
        super().__init__()
        self.starts = self.stops = self.reconnects = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1
        self.stopped.emit()

    def reconnect(self):
        self.reconnects += 1

    def snapshot(self):
        return {"state": "connected", "message": "ready"}


class Window:
    visible = False
    closed = False

    def setWindowIcon(self, icon):
        pass

    def showNormal(self):
        self.visible = True

    def raise_(self):
        pass

    def activateWindow(self):
        pass

    def hide(self):
        self.visible = False

    def close(self):
        event = QCloseEvent()
        if not self.close_handler(event):
            self.closed = True
            self.visible = False


class Tray:
    visible = True
    notices = 0

    def isVisible(self):
        return self.visible

    def showMessage(self, *a):
        self.notices += 1

    def setToolTip(self, *a):
        pass

    def hide(self):
        self.visible = False


@pytest.mark.parametrize("start_in_tray", [False, True])
def test_close_hides_restore_and_explicit_exit_cleans_owned_session(
    tmp_path, monkeypatch, start_in_tray
):
    app_ref = qt_app()
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
    app, supervisor, instance, window = App(), Supervisor(), Instance(), Window()
    controller = ApplicationController(
        app,
        None,
        tmp_path,
        window_factory=lambda: window,
        supervisor=supervisor,
        instance=instance,
        tray_available=True,
    )

    def create():
        controller.tray = Tray()
        controller.actions = {"다시 연결": type("Action", (), {"setEnabled": lambda *a: None})()}

    controller.create_tray = create
    assert controller.start(start_in_tray=start_in_tray) and supervisor.starts == 1
    assert window.visible is (not start_in_tray)
    controller.show_window()
    window.close()
    assert not window.visible and not window.closed and supervisor.stops == 0
    assert controller.tray.notices == 1 and controller.tray.visible
    instance.activated.emit()
    assert window.visible
    window.close()
    assert controller.tray.notices == 1
    controller.quit()
    assert (
        window.closed and not controller.tray.visible and instance.closed and supervisor.stops == 1
    )
    assert app.quit_count == 1 and not app.quit_on_close
    assert app_ref is not None


def test_start_in_tray_and_no_tray_fallback(tmp_path):
    app_ref = qt_app()
    app, supervisor, window = App(), Supervisor(), Window()
    controller = ApplicationController(
        app,
        None,
        tmp_path,
        window_factory=lambda: window,
        supervisor=supervisor,
        instance=Instance(),
        tray_available=False,
    )
    assert controller.start(start_in_tray=True) and window.visible
    window.close()
    assert window.closed and supervisor.stops == 1 and app.quit_count == 1
    assert app_ref is not None


def test_second_controller_only_activates_existing_app(tmp_path):
    app_ref = qt_app()
    instance, supervisor = Instance(False), Supervisor()
    controller = ApplicationController(
        App(), None, tmp_path, instance=instance, supervisor=supervisor
    )
    assert not controller.start()
    assert instance.shown and supervisor.starts == 0 and controller.window is None
    assert app_ref is not None


def test_single_instance_lock_and_ipc_restore(tmp_path):
    import subprocess
    import sys
    import time

    app = qt_app()
    first, second = SingleInstance(tmp_path), SingleInstance(tmp_path)
    activations = []
    first.activated.connect(lambda: activations.append(True))
    assert first.acquire() and not second.acquire()
    script = "from pathlib import Path; import sys; from PySide6.QtCore import QCoreApplication; from pairper.app.instance import SingleInstance; a=QCoreApplication([]); i=SingleInstance(Path(sys.argv[1])); sys.exit(0 if i.activate_existing() else 1)"
    worker = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while (worker.poll() is None or not activations) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    worker.communicate(timeout=2)
    assert worker.returncode == 0
    assert activations == [True]
    first.close()
    assert second.acquire()
    second.close()
