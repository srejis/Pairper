"""Qt-independent retry engine plus a signal-only Qt facade."""

import copy
import random
import threading
import time

from PySide6.QtCore import QObject, Signal

from pairper.storage.library import write_json
from pairper.tunnel.config import ConfigurationError, load_config
from pairper.tunnel.runtime import timestamp
from pairper.tunnel.session import TunnelSession

MESSAGES = {
    "not_configured": "MCP 설정이 없습니다. .env를 확인하세요.",
    "connecting": "MCP 터널 연결 시도 중",
    "connected": "로컬 MCP 터널 준비 완료",
    "disconnected": "터널 연결이 끊겼습니다.",
    "reconnecting": "MCP 터널 다시 연결 중",
    "authentication_failed": "인증 또는 터널 사용 권한을 확인하세요. 자동 재시도를 중단했습니다.",
    "error": "MCP 설정 또는 실행 상태를 확인하세요.",
    "stopped": "MCP 연결이 종료되었습니다.",
}


def retry_delay(attempt, jitter=random.uniform):
    base = (3, 5, 10, 20, 40, 60)[min(attempt, 5)]
    return min(60, max(0, base + jitter(-0.1 * base, 0.1 * base)))


class SupervisorEngine:
    def __init__(
        self,
        root,
        on_status,
        *,
        loader=load_config,
        session_factory=TunnelSession,
        interval=10,
        failure_limit=3,
        stable_seconds=60,
        delay=retry_delay,
    ):
        self.root, self.on_status = root, on_status
        self.loader, self.session_factory = loader, session_factory
        self.interval, self.failure_limit, self.stable_seconds = (
            interval,
            failure_limit,
            stable_seconds,
        )
        self.delay = delay
        self.stop_requested = threading.Event()
        self.reconnect_requested = threading.Event()
        self.wake = threading.Event()
        self.session = None
        self.attempt = 0
        self.snapshot = {
            "state": "stopped",
            "message": MESSAGES["stopped"],
            "ready": False,
            "healthy": False,
            "chatgpt_verified": False,
            "checked_at": None,
            "last_healthy_at": None,
            "changed_at": timestamp(),
            "admin_url": None,
            "tunnel_pid": None,
            "retry_in_seconds": None,
        }
        self.lock = threading.Lock()

    def current(self):
        with self.lock:
            return copy.deepcopy(self.snapshot)

    def publish(self, state, **values):
        with self.lock:
            if self.snapshot["state"] != state:
                self.snapshot["changed_at"] = timestamp()
            self.snapshot.update(
                state=state, message=MESSAGES[state], checked_at=timestamp(), **values
            )
            result = copy.deepcopy(self.snapshot)
        # Status contains fixed safe messages and timestamps, never config/exception text.
        if (self.root / ".pairper").is_dir():
            try:
                write_json(self.root / ".pairper/status.json", result)
            except OSError:
                pass
        self.on_status(result)

    def request_stop(self):
        self.stop_requested.set()
        self.wake.set()

    def request_reconnect(self):
        if not self.stop_requested.is_set():
            self.reconnect_requested.set()
            self.wake.set()

    def wait(self, seconds):
        self.wake.wait(seconds)
        self.wake.clear()

    def _close_session(self):
        if self.session:
            session, self.session = self.session, None
            session.stop()

    def _monitor(self):
        failures = 0
        stable_since = None
        while not self.stop_requested.is_set() and not self.reconnect_requested.is_set():
            self.session.drain()
            if self.session.permanent_error:
                return "permanent", self.session.permanent_error
            if not self.session.alive():
                # Capture the final stderr/stdout before classifying a fast process exit.
                live = self.session.poll()
                return (
                    ("permanent", live["permanent_error"])
                    if live["permanent_error"]
                    else ("retry", None)
                )
            live = self.session.poll()
            if live["permanent_error"]:
                return "permanent", live["permanent_error"]
            healthy = live["healthy"] and live["ready"] and live["alive"]
            if healthy:
                failures = 0
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= self.stable_seconds:
                    self.attempt = 0
                # Read only allowlisted tool audit/timestamps. Avoid additional network probes.
                from pairper.tunnel.runtime import tool_audit

                self.publish(
                    "connected",
                    healthy=True,
                    ready=True,
                    admin_url=live["admin_url"],
                    last_healthy_at=timestamp(),
                    tunnel_pid=self.session.pid,
                    retry_in_seconds=None,
                    **tool_audit(self.root),
                )
            else:
                failures += 1
                stable_since = None
                self.publish(
                    "disconnected" if self.current()["state"] == "connected" else "connecting",
                    healthy=live["healthy"],
                    ready=False,
                    admin_url=live["admin_url"],
                    tunnel_pid=self.session.pid,
                )
                if failures >= self.failure_limit:
                    return "retry", None
            # Wake once a second to observe process death/auth errors quickly; HTTP probes stay 10s apart.
            end = time.monotonic() + self.interval
            while (
                time.monotonic() < end
                and not self.stop_requested.is_set()
                and not self.reconnect_requested.is_set()
            ):
                self.session.drain()
                if self.session.permanent_error:
                    return "permanent", self.session.permanent_error
                if not self.session.alive():
                    live = self.session.poll()
                    return (
                        ("permanent", live["permanent_error"])
                        if live["permanent_error"]
                        else ("retry", None)
                    )
                self.wait(min(1, max(0, end - time.monotonic())))
        return "manual", None

    def run(self):
        initial = True
        try:
            while not self.stop_requested.is_set():
                self.reconnect_requested.clear()
                self.publish(
                    "connecting" if initial else "reconnecting",
                    ready=False,
                    healthy=False,
                    tunnel_pid=None,
                    retry_in_seconds=None,
                )
                initial = False
                reason, detail = "retry", None
                try:
                    config = self.loader(self.root, secure=True)
                    self.session = self.session_factory(config)
                    self.session.start()
                    reason, detail = self._monitor()
                except ConfigurationError as exc:
                    reason, detail = "config", exc.code
                except (OSError, ValueError, RuntimeError):
                    reason = "retry"
                finally:
                    # Never launch a replacement until the owned tree has been cleaned up.
                    self._close_session()
                if self.stop_requested.is_set():
                    break
                if self.reconnect_requested.is_set() or reason == "manual":
                    self.attempt = 0
                    continue
                if reason in {"permanent", "config"}:
                    state = (
                        "authentication_failed"
                        if detail == "authentication_failed"
                        else ("not_configured" if detail == "not_configured" else "error")
                    )
                    self.publish(
                        state, ready=False, healthy=False, tunnel_pid=None, retry_in_seconds=None
                    )
                    while (
                        not self.stop_requested.is_set() and not self.reconnect_requested.is_set()
                    ):
                        self.wait(1)
                    self.attempt = 0
                    continue
                delay = self.delay(self.attempt)
                self.attempt += 1
                self.publish(
                    "reconnecting",
                    healthy=False,
                    ready=False,
                    tunnel_pid=None,
                    retry_in_seconds=delay,
                )
                end = time.monotonic() + delay
                while (
                    time.monotonic() < end
                    and not self.stop_requested.is_set()
                    and not self.reconnect_requested.is_set()
                ):
                    self.wait(min(1, max(0, end - time.monotonic())))
        except Exception:
            self.publish("error", ready=False, healthy=False, tunnel_pid=None)
        finally:
            try:
                self._close_session()
            finally:
                self.publish("stopped", ready=False, healthy=False, tunnel_pid=None)


class TunnelSupervisor(QObject):
    statusChanged = Signal(dict)
    stopped = Signal()

    def __init__(self, root, parent=None, **engine_options):
        super().__init__(parent)
        self.engine = SupervisorEngine(root, self.statusChanged.emit, **engine_options)
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        def work():
            try:
                self.engine.run()
            finally:
                self.stopped.emit()

        self.thread = threading.Thread(target=work, name="Pairper tunnel supervisor", daemon=False)
        self.thread.start()

    def snapshot(self):
        return self.engine.current()

    def reconnect(self):
        self.engine.request_reconnect()
        if (
            not self.thread or not self.thread.is_alive()
        ) and not self.engine.stop_requested.is_set():
            self.start()

    def stop(self):
        self.engine.request_stop()
