"""Interactive-with-tools integration harness; actual project, no paper mutations or task installation."""

import json
import os
import subprocess
import sys
import time

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from pairper.app.controller import ApplicationController
from pairper.storage.library import Library, write_json
from pairper.tunnel.config import project_root, read_env


def main():
    root = project_root()
    values = read_env(root / ".env")
    library = Library(values.get("PAIRPER_DATA_DIR"))
    app = QApplication(sys.argv[:1])
    controller = ApplicationController(app, library, root)
    if not controller.start():
        print(
            "Existing desktop instance was restored; verification needs an isolated app lifetime."
        )
        return 2
    result = {
        "phase": "starting",
        "initial_window_visible": controller.window.isVisible(),
        "tray_visible": bool(controller.tray and controller.tray.isVisible()),
        "initial_papers": [
            (p["id"], p["sha256"], p["current_view_id"]) for p in library.list_papers()
        ],
    }
    path = root / ".pairper/tray-verification.json"
    command_path = root / ".pairper/tray-verification-command.json"
    command_path.unlink(missing_ok=True)
    duplicate = None
    initial_pid = None
    broken_pid = None
    due = time.monotonic() + 600

    def tick():
        nonlocal duplicate, initial_pid, broken_pid
        state = controller.supervisor.snapshot()
        result.update(
            state=state["state"],
            ready=state["ready"],
            tunnel_pid=state["tunnel_pid"],
            window_visible=controller.window.isVisible(),
            tray_visible=bool(controller.tray and controller.tray.isVisible()),
        )
        if result["phase"] == "starting" and state["ready"]:
            initial_pid = state["tunnel_pid"]
            controller.window.close()
            result.update(
                phase="hidden_connected",
                hidden_keeps_supervisor=not controller.window.isVisible(),
                initial_pid=initial_pid,
            )
        if duplicate and duplicate.poll() is not None:
            result.update(
                duplicate_exit=duplicate.returncode,
                ipc_restored=controller.window.isVisible(),
                duplicate_kept_tunnel=state["tunnel_pid"] == initial_pid,
                phase="duplicate_verified",
            )
            duplicate = None
        if result["phase"] == "recovering" and state["ready"] and state["tunnel_pid"] != broken_pid:
            result.update(
                phase="recovered", old_tunnel_pid=broken_pid, new_tunnel_pid=state["tunnel_pid"]
            )
            controller.window.close()
        if command_path.is_file():
            try:
                command = json.loads(command_path.read_text("utf-8"))["command"]
            except (OSError, ValueError, KeyError):
                command = None
            command_path.unlink(missing_ok=True)
            if command == "duplicate":
                duplicate = subprocess.Popen(
                    [sys.executable, "-m", "pairper.app.main", "--tray"],
                    cwd=root,
                    env={**os.environ, "PAIRPER_PROJECT_DIR": str(root)},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            elif command == "break":
                session = controller.supervisor.engine.session
                if session and session.process:
                    broken_pid = session.pid
                    session.process.terminate()
                    result.update(phase="recovering")
            elif command == "reconnect":
                broken_pid = state["tunnel_pid"]
                controller.supervisor.reconnect()
                result.update(phase="recovering")
            elif command == "quit":
                result.update(
                    phase="quitting",
                    final_papers=[
                        (p["id"], p["sha256"], p["current_view_id"]) for p in library.list_papers()
                    ],
                )
                write_json(path, result)
                timer.stop()
                controller.quit()
                return
        write_json(path, result)
        if time.monotonic() > due:
            result.update(phase="timeout")
            write_json(path, result)
            timer.stop()
            controller.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(500)
    code = app.exec()
    controller.supervisor.stop()
    if controller.supervisor.thread:
        controller.supervisor.thread.join(25)
    result.update(
        clean_shutdown=controller.supervisor.snapshot()["state"] == "stopped",
        tray_removed=not controller.tray.isVisible() if controller.tray else True,
        phase="stopped",
    )
    write_json(path, result)
    return code


if __name__ == "__main__":
    sys.exit(main())
