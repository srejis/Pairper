"""One owned tunnel process tree, shared by the desktop supervisor and diagnostic CLI."""

import ctypes
import os
import queue
import re
import subprocess
import threading
import time

from pairper.storage.library import write_json
from pairper.tunnel.config import prepare_profile, redact
from pairper.tunnel.runtime import kill_on_parent_exit, probe, runtime_lock


def permanent_log_error(line):
    lower = line.lower()
    if any(
        term in lower
        for term in (
            "invalid_api_key",
            "invalid api key",
            "unauthorized",
            "authentication failed",
            "permission_denied",
            "permission denied",
        )
    ) or re.search(r"(?:status(?:[_ ]?code)?|http(?:/[0-9.]+)?)[\s\"':=_-]*(?:401|403)\b", lower):
        return "authentication_failed"
    if any(
        term in lower for term in ("tunnel_not_found", "invalid tunnel", "tunnel not found")
    ) or re.search(r"(?:status(?:[_ ]?code)?|http(?:/[0-9.]+)?)[\s\"':=_-]*404\b", lower):
        return "invalid_tunnel"
    return None


class TunnelSession:
    def __init__(self, config):
        self.config = config
        self.process = None
        self.job_handle = None
        self.lock = None
        self.lines = queue.Queue(maxsize=1000)
        self.reader = None
        self.log = None
        self.permanent_error = None

    @property
    def pid(self):
        return self.process.pid if self.process else None

    def start(self):
        prepare_profile(self.config)
        self.lock = runtime_lock(self.config.runtime)
        try:
            self.lock.__enter__()
        except Exception:
            self.lock = None
            raise
        try:
            (self.config.runtime / "health.url").unlink(missing_ok=True)
            self.process = subprocess.Popen(
                [str(self.config.executable), "run", "--config", str(self.config.profile)],
                cwd=self.config.root,
                env=self.config.environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.job_handle = kill_on_parent_exit(self.process)
            write_json(
                self.config.runtime / "process.json",
                {"runner_pid": os.getpid(), "tunnel_pid": self.pid, "owner": "Pairper"},
            )
            self.log = (self.config.runtime / "tunnel.log").open("a", encoding="utf-8")
            self.reader = threading.Thread(
                target=self._read, name="Pairper tunnel log", daemon=True
            )
            self.reader.start()
        except Exception:
            self.stop()
            raise

    def _read(self):
        for line in self.process.stdout:
            # Secret values are removed before entering queues, files or callbacks.
            safe = redact(line, self.config.values.values())
            try:
                self.lines.put(safe, timeout=0.1)
            except queue.Full:
                # Still preserve a terminal authentication error under excessive log volume.
                error = permanent_log_error(safe)
                if error:
                    self.permanent_error = error

    def drain(self):
        for _ in range(1000):
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            self.permanent_error = permanent_log_error(line) or self.permanent_error
            if self.log:
                self.log.write(line)
                if self.log.tell() > 2_000_000:
                    self.log.close()
                    os.replace(
                        self.config.runtime / "tunnel.log",
                        self.config.runtime / "tunnel.previous.log",
                    )
                    self.log = (self.config.runtime / "tunnel.log").open("a", encoding="utf-8")
        if self.log:
            self.log.flush()

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def poll(self):
        if not self.alive() and self.reader:
            self.reader.join(timeout=0.3)
        self.drain()
        live = (
            probe(self.config.root)
            if self.alive()
            else {"healthy": False, "ready": False, "admin_url": None}
        )
        return {
            **live,
            "alive": self.alive(),
            "permanent_error": self.permanent_error,
            "tunnel_pid": self.pid,
        }

    def stop(self):
        # Only handles created by this session are touched. No name-based process termination.
        try:
            if self.process and self.alive():
                self.process.terminate()
            if self.job_handle:
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                if not kernel.TerminateJobObject(self.job_handle, 0):
                    raise RuntimeError("소유한 MCP 프로세스 트리를 종료하지 못했습니다.")

                # Accounting query confirms all descendants have left the owned job.
                class Accounting(ctypes.Structure):
                    _fields_ = [
                        (name, ctypes.c_int64)
                        for name in ("user", "kernel", "period_user", "period_kernel")
                    ] + [
                        (name, ctypes.c_uint32)
                        for name in ("faults", "total", "active", "terminated")
                    ]

                kernel.QueryInformationJobObject.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                ]
                end = time.monotonic() + 5
                while True:
                    accounting = Accounting()
                    if not kernel.QueryInformationJobObject(
                        self.job_handle,
                        1,
                        ctypes.byref(accounting),
                        ctypes.sizeof(accounting),
                        None,
                    ):
                        raise RuntimeError("소유한 MCP 자식 프로세스 종료를 확인하지 못했습니다.")
                    if accounting.active == 0:
                        break
                    if time.monotonic() >= end:
                        raise RuntimeError("소유한 MCP 자식 프로세스 종료가 지연되고 있습니다.")
                    time.sleep(0.05)
                kernel.CloseHandle(self.job_handle)
                self.job_handle = None
            if self.process:
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            if self.reader:
                self.reader.join(timeout=1)
            self.drain()
        finally:
            if self.job_handle:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.job_handle))
                self.job_handle = None
            if self.log:
                self.log.close()
                self.log = None
            if self.process and self.process.stdout:
                self.process.stdout.close()
            if self.lock:
                self.lock.__exit__(None, None, None)
                self.lock = None
