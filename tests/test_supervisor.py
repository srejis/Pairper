import threading
import time

import pytest

from pairper.tunnel.config import ConfigurationError
from pairper.tunnel.session import permanent_log_error
from pairper.tunnel.supervisor import SupervisorEngine, retry_delay


class FakeSession:
    def __init__(self, readings=(True,), *, permanent=None, dead=False):
        self.readings = list(readings)
        self.permanent_error = permanent
        self.dead = dead
        self.pid = 123
        self.started = 0
        self.stopped = 0
        self.polls = 0

    def start(self):
        self.started += 1

    def drain(self):
        pass

    def alive(self):
        return not self.dead and not self.stopped

    def poll(self):
        self.polls += 1
        ready = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        return {
            "ready": ready,
            "healthy": ready,
            "alive": self.alive(),
            "admin_url": "http://127.0.0.1:1234/ui",
            "permanent_error": self.permanent_error,
        }

    def stop(self):
        self.stopped += 1


def launch(engine):
    thread = threading.Thread(target=engine.run)
    thread.start()
    return thread


def test_healthy_connection_and_single_transient_failure_do_not_restart(tmp_path):
    session = FakeSession([True, False, True, True])
    states = []

    def status(value):
        states.append(value["state"])
        if session.polls >= 4:
            engine.request_stop()

    engine = SupervisorEngine(
        tmp_path,
        status,
        loader=lambda *a, **k: object(),
        session_factory=lambda _: session,
        interval=0.005,
    )
    thread = launch(engine)
    thread.join(2)
    assert not thread.is_alive()
    assert session.started == session.stopped == 1
    assert states.count("connected") >= 2 and "disconnected" in states
    assert engine.current()["state"] == "stopped"


@pytest.mark.parametrize("dead", [False, True])
def test_sustained_failure_or_dead_process_restarts_after_cleanup(tmp_path, dead):
    first, second = FakeSession([False], dead=dead), FakeSession()
    created, delays = [], []

    def factory(_):
        if created:
            assert first.stopped == 1  # no overlapping owned sessions
        session = second if created else first
        created.append(session)
        return session

    def status(value):
        if value["state"] == "connected":
            engine.request_stop()

    def delay(attempt):
        delays.append(attempt)
        return 0.005

    engine = SupervisorEngine(
        tmp_path,
        status,
        loader=lambda *a, **k: object(),
        session_factory=factory,
        interval=0.005,
        delay=delay,
    )
    thread = launch(engine)
    thread.join(2)
    assert not thread.is_alive()
    assert created == [first, second] and delays == [0]
    assert first.polls == (1 if dead else 3)
    assert second.stopped == 1


@pytest.mark.parametrize(
    "code,state",
    [
        ("not_configured", "not_configured"),
        ("executable_missing", "error"),
        ("configuration_error", "error"),
    ],
)
def test_permanent_configuration_error_waits_for_manual_reconnect(tmp_path, code, state):
    calls, seen = [], threading.Event()
    valid = False

    def loader(*a, **k):
        calls.append(1)
        if not valid:
            raise ConfigurationError("safe message", code)
        return object()

    def status(value):
        if value["state"] == state:
            seen.set()
        if value["state"] == "connected":
            engine.request_stop()

    engine = SupervisorEngine(
        tmp_path, status, loader=loader, session_factory=lambda _: FakeSession(), interval=0.005
    )
    thread = launch(engine)
    try:
        assert seen.wait(1)
        assert calls == [1]
        valid = True
        engine.request_reconnect()
    finally:
        thread.join(2)
        engine.request_stop()
        thread.join(1)
    assert calls == [1, 1]
    assert not thread.is_alive()


def test_authentication_error_does_not_retry_until_user_requests(tmp_path):
    failed = FakeSession(permanent="authentication_failed")
    good = FakeSession()
    sessions = []
    error = threading.Event()

    def factory(_):
        item = good if sessions else failed
        sessions.append(item)
        return item

    def status(value):
        if value["state"] == "authentication_failed":
            error.set()
        elif value["state"] == "connected":
            engine.request_stop()

    engine = SupervisorEngine(
        tmp_path, status, loader=lambda *a, **k: object(), session_factory=factory, interval=0.005
    )
    thread = launch(engine)
    try:
        assert error.wait(1)
        assert sessions == [failed] and failed.stopped == 1
        engine.request_reconnect()
        thread.join(2)
    finally:
        engine.request_stop()
        thread.join(1)
    assert sessions == [failed, good]


def test_retry_schedule_and_cancel_during_backoff(tmp_path):
    assert [retry_delay(i, lambda a, b: 0) for i in range(8)] == [3, 5, 10, 20, 40, 60, 60, 60]
    assert all(2.7 <= retry_delay(0) <= 3.3 for _ in range(20))
    assert all(54 <= retry_delay(20) <= 60 for _ in range(20))
    states = []

    def status(value):
        states.append(value["state"])
        if value["state"] == "reconnecting":
            engine.request_stop()

    engine = SupervisorEngine(
        tmp_path,
        status,
        loader=lambda *a, **k: object(),
        session_factory=lambda _: FakeSession(dead=True),
    )
    start = time.monotonic()
    thread = launch(engine)
    thread.join(1)
    assert not thread.is_alive() and time.monotonic() - start < 1
    assert states[-1] == "stopped"


def test_stable_connection_resets_retry_count(tmp_path):
    ticks = []

    def status(value):
        if value["state"] == "connected":
            ticks.append(engine.attempt)
            if engine.attempt == 0:
                engine.request_stop()

    engine = SupervisorEngine(
        tmp_path,
        status,
        loader=lambda *a, **k: object(),
        session_factory=lambda _: FakeSession(),
        interval=0.005,
        stable_seconds=0.01,
    )
    engine.attempt = 4
    thread = launch(engine)
    thread.join(2)
    assert not thread.is_alive() and ticks[0] == 4 and ticks[-1] == 0


@pytest.mark.parametrize(
    "line,result",
    [
        ("http status 401", "authentication_failed"),
        ("HTTP status code 401", "authentication_failed"),
        ("HTTP/1.1 403", "authentication_failed"),
        ('status_code": 403', "authentication_failed"),
        ("invalid_api_key", "authentication_failed"),
        ("http 404", "invalid_tunnel"),
        ("tunnel_not_found", "invalid_tunnel"),
        ("connection reset", ""),
    ],
)
def test_terminal_error_classification(line, result):
    assert permanent_log_error(line) == (result or None)
