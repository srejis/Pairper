import argparse
import ctypes
import json
import os
import subprocess
import sys
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


def _safe_time(value):
    try:
        return datetime.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        return None


def tool_audit(root):
    result = {}
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
            result["last_tunnel_tool_at"] = _safe_time(audit["at"])
            result["last_tunnel_tool"] = audit["tool"]
    except (OSError, ValueError, KeyError):
        pass
    return result


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
        "connected": "로컬 MCP 터널 준비 완료",
        "reconnecting": "MCP 터널 다시 연결 중",
        "error": "MCP 설정 또는 실행 상태를 확인하세요.",
        "stopped": "MCP 연결이 종료되었습니다.",
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
    live = (
        {"healthy": False, "ready": False, "admin_url": None}
        if data.get("state") == "stopped"
        else probe(root)
    )
    if data.get("state") == "authentication_failed":
        live["ready"] = False
    allowed.update(live)
    if live["ready"] and data.get("state") != "authentication_failed":
        allowed.update(
            state="connected" if data.get("state") == "connected" else "runtime_ready",
            message="로컬 터널 런타임 준비 완료 · 실제 호출은 별도 기록",
        )
    elif data.get("state") in {"runtime_ready", "connecting", "connected"}:
        try:
            recent = time.time() - state_path(root).stat().st_mtime < 15
        except OSError:
            recent = False
        if not recent or data.get("state") in {"runtime_ready", "connected"}:
            allowed.update(state="disconnected", message="터널 연결이 끊겼습니다.")
    allowed.update(tool_audit(root))
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
    """Diagnostic standalone mode. The desktop uses TunnelSupervisor instead."""
    from pairper.tunnel.session import TunnelSession

    session = TunnelSession(config)
    last_check = 0
    try:
        session.start()
        while session.alive():
            session.drain()
            if session.permanent_error:
                break
            if time.monotonic() - last_check >= 10:
                live = session.poll()
                write_json(
                    state_path(config.root),
                    {
                        "state": "runtime_ready" if live["ready"] else "connecting",
                        "checked_at": timestamp(),
                        "last_healthy_at": timestamp() if live["ready"] else None,
                    },
                )
                last_check = time.monotonic()
            time.sleep(0.2)
        session.poll()
        if session.permanent_error:
            write_json(
                state_path(config.root),
                {
                    "state": "authentication_failed"
                    if session.permanent_error == "authentication_failed"
                    else "error",
                    "checked_at": timestamp(),
                },
            )
            return 0
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        session.stop()


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
