import argparse
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from pairper.storage.library import write_json
from pairper.tunnel.config import (
    ConfigurationError,
    load_config,
    prepare_profile,
    project_root,
    redact,
)


def timestamp():
    return datetime.now(UTC).isoformat()


def state_path(root):
    return root / ".pairper/status.json"


def health_url(root: Path) -> str | None:
    try:
        value = (root / ".pairper/health.url").read_text("utf-8").strip().rstrip("/")
        parsed = urlparse(value)
        if (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port
            and parsed.path in ("", "/")
            and not parsed.username
            and not parsed.query
        ):
            return value
    except (OSError, ValueError):
        pass
    return None


def probe(root):
    url = health_url(root)
    if not url:
        return {"healthy": False, "ready": False, "admin_url": None}
    opener = build_opener(ProxyHandler({}))
    values = {}
    for endpoint, key in (("healthz", "healthy"), ("readyz", "ready")):
        try:
            with opener.open(Request(f"{url}/{endpoint}"), timeout=1) as response:
                values[key] = response.status == 200
        except (OSError, URLError, HTTPError):
            values[key] = False
    return {**values, "admin_url": f"{url}/ui"}


def public_status(root: Path | None = None):
    root = root or project_root()
    try:
        data = json.loads(state_path(root).read_text("utf-8"))
    except (OSError, ValueError):
        data = {
            "state": "not_configured",
            "message": "터널 진단을 먼저 실행하세요.",
            "checked_at": None,
            "last_healthy_at": None,
        }
    messages = {
        "not_configured": "터널 설정이 없습니다. 연결 진단을 확인하세요.",
        "configuration_error": "터널 설정을 확인하세요.",
        "executable_missing": "터널 또는 Python 실행 파일을 찾을 수 없습니다.",
        "authentication_failed": "터널 인증 또는 권한을 확인하세요.",
        "connecting": "터널 연결 시도 중",
        "runtime_ready": "로컬 터널 런타임 준비 완료",
        "disconnected": "터널 연결이 끊겼습니다.",
    }
    state = data.get("state") if data.get("state") in messages else "configuration_error"

    def safe_time(value):
        try:
            return datetime.fromisoformat(value).isoformat()
        except (ValueError, TypeError):
            return None

    allowed = {
        "state": state,
        "message": messages[state],
        "checked_at": safe_time(data.get("checked_at")),
        "last_healthy_at": safe_time(data.get("last_healthy_at")),
    }
    live = probe(root)
    if data.get("state") == "authentication_failed":
        live["ready"] = False
    allowed.update(live)
    if live["ready"] and data.get("state") != "authentication_failed":
        allowed.update(
            state="runtime_ready", message="로컬 터널 런타임 준비 완료 · ChatGPT 호출 미확인"
        )
    elif data.get("state") in {"runtime_ready", "connecting"}:
        try:
            recent = time.time() - state_path(root).stat().st_mtime < 15
        except OSError:
            recent = False
        if not recent or data.get("state") == "runtime_ready":
            allowed.update(state="disconnected", message="터널 연결이 끊겼습니다.")
    try:
        audit = json.loads((root / ".pairper/tool-call.json").read_text("utf-8"))
        tools = {
            "list_papers",
            "list_pending_papers",
            "get_paper_manifest",
            "get_page_content",
            "get_page_image",
            "get_region_image",
            "begin_view_build",
            "append_document_blocks",
            "validate_view_build",
            "finalize_view_build",
            "get_build_report",
        }
        if audit["tool"] in tools:
            allowed["last_tunnel_tool_at"] = safe_time(audit["at"])
            allowed["last_tunnel_tool"] = audit["tool"]
    except (OSError, ValueError, KeyError):
        pass
    allowed["chatgpt_verified"] = False  # Local health/tool audit cannot identify caller product.
    return allowed


@contextmanager
def runtime_lock(directory):
    path = directory / "runner.lock"
    stream = path.open("a+b")
    stream.seek(0)
    if path.stat().st_size == 0:
        stream.write(b"1")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ConfigurationError(
            "Pairper 터널 실행기가 이미 실행 중입니다.", "already_running"
        ) from None
    try:
        yield
    finally:
        stream.close()


def kill_on_parent_exit(process):
    """Keep the tunnel and its MCP subprocess in a Windows kill-on-close Job Object."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("per_process", ctypes.c_int64),
            ("per_job", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("min_ws", ctypes.c_size_t),
            ("max_ws", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", IO),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise RuntimeError("터널 프로세스 종료 보호를 설정할 수 없습니다.")
    if not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
        kernel.CloseHandle(wintypes.HANDLE(handle))
        raise RuntimeError("터널 프로세스 종료 보호에 연결할 수 없습니다.")
    return handle


def doctor(config):
    prepare_profile(config)
    result = subprocess.run(
        [str(config.executable), "doctor", "--config", str(config.profile), "--explain", "--json"],
        cwd=config.root,
        env=config.environment(),
        capture_output=True,
        timeout=90,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    safe = redact(result.stdout + "\n" + result.stderr, config.values.values())
    (config.runtime / "doctor.log").write_text(safe, encoding="utf-8")
    write_json(
        config.runtime / "doctor-result.json",
        {"passed": result.returncode == 0, "checked_at": timestamp()},
    )
    print(safe[:20000])
    return result.returncode


def run(config):
    prepare_profile(config)
    with runtime_lock(config.runtime):
        (config.runtime / "health.url").unlink(missing_ok=True)
        status = {
            "state": "connecting",
            "message": "터널 연결 시도 중",
            "checked_at": timestamp(),
            "last_healthy_at": None,
        }
        write_json(state_path(config.root), status)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(config.executable), "run", "--config", str(config.profile)],
            cwd=config.root,
            env=config.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        write_json(
            config.runtime / "process.json", {"runner_pid": os.getpid(), "tunnel_pid": process.pid}
        )
        job_handle = None
        try:
            job_handle = kill_on_parent_exit(process)
            lines = queue.Queue(maxsize=1000)

            def consume():
                for line in process.stdout:
                    safe = redact(line, config.values.values())
                    try:
                        lines.put(safe, timeout=1)
                    except queue.Full:
                        pass

            threading.Thread(target=consume, daemon=True).start()
            auth_failed = False
            last_check = 0
            log_path = config.runtime / "tunnel.log"
            if log_path.exists() and log_path.stat().st_size > 2_000_000:
                os.replace(log_path, config.runtime / "tunnel.previous.log")
            with log_path.open("a", encoding="utf-8") as log:
                while process.poll() is None:
                    try:
                        line = lines.get(timeout=0.2)
                        log.write(line)
                        log.flush()
                        if any(
                            term in line.lower()
                            for term in (
                                "unauthorized",
                                '"status":401',
                                '"status":403',
                                "invalid_api_key",
                                "authentication failed",
                                "permission_denied",
                                "status=401",
                                "status=403",
                            )
                        ):
                            auth_failed = True
                        if re.search(
                            r"(?:status(?:_code)?|http)[\s\"':=_-]*(?:401|403)\b", line, re.I
                        ):
                            auth_failed = True
                    except queue.Empty:
                        pass
                    if time.monotonic() - last_check > 3:
                        live = probe(config.root)
                        status["checked_at"] = timestamp()
                        if auth_failed:
                            status.update(
                                state="authentication_failed",
                                message="터널 인증 또는 권한을 확인하세요.",
                            )
                        elif live["ready"]:
                            status.update(
                                state="runtime_ready",
                                message="로컬 터널 런타임 준비 완료",
                                last_healthy_at=timestamp(),
                            )
                        else:
                            status.update(state="connecting", message="터널 연결 시도 중")
                        write_json(state_path(config.root), status)
                        last_check = time.monotonic()
                    if auth_failed:
                        status.update(
                            state="authentication_failed",
                            message="터널 인증 또는 권한을 확인하세요.",
                            checked_at=timestamp(),
                        )
                        write_json(state_path(config.root), status)
                        process.terminate()
                        process.wait(timeout=10)
                        # Exit success: task scheduler must not endlessly retry bad credentials.
                        return 0
            status.update(
                state="disconnected",
                message="터널 프로세스가 종료되었습니다.",
                checked_at=timestamp(),
            )
            write_json(state_path(config.root), status)
            return process.returncode or 1
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if job_handle:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job_handle))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "doctor", "run", "status"])
    parser.add_argument("--project-dir", type=Path, default=project_root())
    parser.add_argument("--client-path")
    args = parser.parse_args()
    root = args.project_dir.resolve()
    try:
        if args.action == "status":
            print(json.dumps(public_status(root), ensure_ascii=False))
            return 0
        config = load_config(root, args.client_path, secure=True)
        if args.action == "prepare":
            prepare_profile(config)
            print("프로젝트 터널 설정과 파일 접근 권한을 확인했습니다. 비밀값은 출력하지 않습니다.")
            return 0
        return doctor(config) if args.action == "doctor" else run(config)
    except (
        ConfigurationError,
        OSError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        # Error strings originate in our fixed validation messages; scrub dynamic exceptions too.
        message = redact(str(exc))
        if getattr(exc, "code", None) == "already_running":
            print(message)
            return 0
        if (root / ".pairper").is_dir():
            write_json(
                state_path(root),
                {
                    "state": getattr(exc, "code", "configuration_error"),
                    "message": message,
                    "checked_at": timestamp(),
                },
            )
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
