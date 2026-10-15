import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pairper.storage.library import write_json


class ConfigurationError(ValueError):
    def __init__(self, message, code="configuration_error"):
        super().__init__(message)
        self.code = code


def project_root() -> Path:
    override = os.environ.get("PAIRPER_PROJECT_DIR")
    return Path(override).resolve() if override else Path(__file__).resolve().parents[3]


def read_env(path: Path) -> dict[str, str]:
    """Literal UTF-8 dotenv subset: no shell, interpolation, escapes or multiline values."""
    if not path.is_file():
        raise ConfigurationError(
            ".env 파일이 없습니다. .env.example을 참고해 설정하세요.", "not_configured"
        )
    result = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise ConfigurationError(f".env {number}행: KEY=value 형식이 필요합니다.")
        key, value = match.groups()
        if key not in {"CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID", "PAIRPER_DATA_DIR"}:
            raise ConfigurationError(f".env {number}행: 지원하지 않는 설정 이름입니다.")
        if key in result:
            raise ConfigurationError(f".env {number}행: 중복 설정 이름입니다.")
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote) or quote in value[1:-1]:
                raise ConfigurationError(f".env {number}행: 따옴표 형식이 잘못되었습니다.")
            value = value[1:-1]
        elif any(ch.isspace() for ch in value) or "#" in value or "'" in value or '"' in value:
            raise ConfigurationError(f".env {number}행: 공백이 있는 값은 따옴표로 감싸세요.")
        if not value or "\x00" in value:
            raise ConfigurationError(f".env {number}행: 빈 값 또는 잘못된 값입니다.")
        result[key] = value
    return result


def redact(text: str, secrets=()) -> str:
    for value in secrets:
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED_KEY]", text)
    return re.sub(r"tunnel_[A-Za-z0-9_-]+", "[REDACTED_TUNNEL]", text)


def protect_local(path: Path):
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    # Fixed script, literal path from the process environment. No eval/interpolation of .env.
    script = """$ErrorActionPreference='Stop';
    $target=$env:PAIRPER_ACL_TARGET;
    $identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;
    if((Get-Item -LiteralPath $target).PSIsContainer) {
      $acl=New-Object System.Security.AccessControl.DirectorySecurity;
      $acl.SetAccessRuleProtection($true,$false);
      $rule=New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow');
      $acl.AddAccessRule($rule); [System.IO.Directory]::SetAccessControl($target,$acl)
    } else {
      $acl=New-Object System.Security.AccessControl.FileSecurity;
      $acl.SetAccessRuleProtection($true,$false);
      $rule=New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity,'FullControl','Allow');
      $acl.AddAccessRule($rule); [System.IO.File]::SetAccessControl($target,$acl)
    };"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "PAIRPER_ACL_TARGET": str(path)},
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise ConfigurationError("로컬 설정 파일의 현재 사용자 접근 권한 제한에 실패했습니다.")


@dataclass
class TunnelConfig:
    root: Path
    values: dict[str, str] = field(repr=False)
    executable: Path

    @property
    def runtime(self):
        return self.root / ".pairper"

    @property
    def profile(self):
        return self.runtime / "tunnel-client.yaml"

    @property
    def python(self):
        return self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def environment(self):
        # Discard other projects' tunnel/OpenAI overrides. Only this .env selects credentials.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(
                (
                    "CONTROL_PLANE_",
                    "TUNNEL_CLIENT_",
                    "MCP_",
                    "HEALTH_",
                    "ADMIN_UI_",
                    "LOG_",
                    "CLOUDFLARED_",
                    "HARPOON_",
                )
            )
            and k
            not in {
                "OPENAI_API_KEY",
                "OPENAI_ADMIN_KEY",
                "PAIRPER_DATA_DIR",
                "ALLOW_REMOTE_UI",
                "OPEN_WEB_UI",
                "PID_FILE",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
            }
        }
        env.update(self.values)
        env["PAIRPER_PROJECT_DIR"] = str(self.root)
        env["PAIRPER_TUNNEL_PROCESS"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env


def load_config(
    root: Path | None = None, client_path: str | None = None, secure=False
) -> TunnelConfig:
    root = (root or project_root()).resolve()
    values = read_env(root / ".env")
    key = values.get("CONTROL_PLANE_API_KEY", "")
    if not re.fullmatch(r"sk-proj-[A-Za-z0-9_-]{16,}", key):
        raise ConfigurationError(
            "CONTROL_PLANE_API_KEY에 실제 sk-proj- 비밀 키가 필요합니다. Tracking ID는 사용할 수 없습니다."
        )
    if not re.fullmatch(r"tunnel_[A-Za-z0-9_-]{8,128}", values.get("CONTROL_PLANE_TUNNEL_ID", "")):
        raise ConfigurationError("CONTROL_PLANE_TUNNEL_ID 형식이 잘못되었거나 누락되었습니다.")
    if values.get("PAIRPER_DATA_DIR") and not Path(values["PAIRPER_DATA_DIR"]).is_absolute():
        raise ConfigurationError("PAIRPER_DATA_DIR은 절대 경로여야 합니다.")
    settings = root / ".pairper/settings.json"
    try:
        stored = json.loads(settings.read_text("utf-8")) if settings.exists() else {}
    except (ValueError, OSError) as exc:
        raise ConfigurationError("프로젝트 터널 경로 설정 파일 형식을 확인하세요.") from exc
    if not isinstance(stored, dict) or (
        "tunnel_client_path" in stored and not isinstance(stored["tunnel_client_path"], str)
    ):
        raise ConfigurationError("프로젝트 터널 실행 파일 경로는 문자열이어야 합니다.")
    candidate = (
        client_path
        or stored.get("tunnel_client_path")
        or shutil.which("tunnel-client")
        or "D:/tools/tunnel-client-v0.0.16-windows-amd64/tunnel-client.exe"
    )
    executable = Path(candidate).resolve()
    if not executable.is_file():
        raise ConfigurationError(
            "tunnel-client 실행 파일이 없습니다. -ClientPath로 위치를 지정하세요.",
            "executable_missing",
        )
    config = TunnelConfig(root, values, executable)
    if not config.python.is_file():
        raise ConfigurationError(
            "프로젝트 .venv의 Python이 없습니다. uv sync --locked를 실행하세요."
        )
    config.runtime.mkdir(exist_ok=True)
    if secure:
        protect_local(root / ".env")
        protect_local(config.runtime)
    if client_path:
        write_json(settings, {**stored, "tunnel_client_path": str(executable)})
    return config


def prepare_profile(config: TunnelConfig):
    # JSON is a YAML 1.2 subset. Serialize paths rather than interpolating shell expressions.
    command = f'"{config.python.as_posix()}" -m pairper.mcp.server'
    data = {
        "config_version": 1,
        "control_plane": {
            "base_url": "https://api.openai.com",
            "tunnel_id": config.values["CONTROL_PLANE_TUNNEL_ID"],
            "api_key": "env:CONTROL_PLANE_API_KEY",
        },
        "health": {"listen_addr": "127.0.0.1:0", "url_file": str(config.runtime / "health.url")},
        "admin_ui": {"open_browser": False, "log_buffer_events": 200},
        "log": {"level": "info", "format": "json"},
        "mcp": {"commands": [{"channel": "main", "command": command}]},
    }
    if config.profile.exists():
        # Recognize only our constrained profile: never execute user-edited arbitrary commands.
        try:
            old = json.loads(config.profile.read_text("utf-8"))
        except (ValueError, OSError) as exc:
            raise ConfigurationError(
                "기존 터널 프로필 형식을 확인하세요. 자동 덮어쓰기는 하지 않습니다."
            ) from exc
        if old != data:
            raise ConfigurationError(
                "기존 터널 프로필이 예상 설정과 다릅니다. 백업 후 제거하고 다시 준비하세요."
            )
    else:
        write_json(config.profile, data)
    return config.profile
