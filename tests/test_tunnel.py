import json
import os
from pathlib import Path

import pytest

from pairper.tunnel.config import (
    ConfigurationError,
    TunnelConfig,
    load_config,
    prepare_profile,
    read_env,
    redact,
)
from pairper.tunnel.runtime import health_url, public_status, runtime_lock


def test_env_literal_parser(tmp_path):
    path = tmp_path / ".env"
    content = "# comment\nCONTROL_PLANE_API_KEY=\"literal$(noexec)\"\nPAIRPER_DATA_DIR='D:/path with space'\n"
    path.write_text(content, "utf-8")
    values = read_env(path)
    assert values["CONTROL_PLANE_API_KEY"] == "literal$(noexec)"
    assert values["PAIRPER_DATA_DIR"] == "D:/path with space"
    assert path.read_text("utf-8") == content


@pytest.mark.parametrize(
    "content",
    [
        "CONTROL_PLANE_API_KEY=\n",
        'CONTROL_PLANE_API_KEY="unterminated\n',
        "CONTROL_PLANE_API_KEY=a\nCONTROL_PLANE_API_KEY=b",
        "export CONTROL_PLANE_API_KEY=a",
        "UNKNOWN=secret",
        "CONTROL_PLANE_API_KEY=a # comment",
    ],
)
def test_invalid_env_never_echoes_values(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content, "utf-8")
    with pytest.raises(ConfigurationError) as error:
        read_env(path)
    assert "secret" not in str(error.value) and "unterminated" not in str(error.value)


def test_missing_and_tracking_id(tmp_path):
    with pytest.raises(ConfigurationError):
        read_env(tmp_path / ".env")
    (tmp_path / ".env").write_text("CONTROL_PLANE_API_KEY=key_tracking_id\n", "utf-8")
    with pytest.raises(ConfigurationError, match="Tracking ID"):
        load_config(tmp_path)


def test_profile_idempotent_and_environment_isolation(tmp_path, monkeypatch):
    runtime = tmp_path / ".pairper"
    runtime.mkdir()
    config = TunnelConfig(
        tmp_path,
        {"CONTROL_PLANE_API_KEY": "synthetic-only", "CONTROL_PLANE_TUNNEL_ID": "tunnel_synthetic"},
        Path("client"),
    )
    profile = prepare_profile(config)
    before = profile.stat().st_mtime_ns
    prepare_profile(config)
    assert profile.stat().st_mtime_ns == before
    text = profile.read_text("utf-8")
    assert "synthetic-only" not in text and "env:CONTROL_PLANE_API_KEY" in text
    assert json.loads(text)["health"]["listen_addr"] == "127.0.0.1:0"
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "other-project")
    monkeypatch.setenv("OPENAI_API_KEY", "other-project")
    monkeypatch.setenv("MCP_COMMAND", "unexpected")
    monkeypatch.setenv("TUNNEL_CLIENT_CONFIG", "unexpected")
    env = config.environment()
    assert env["CONTROL_PLANE_API_KEY"] == "synthetic-only"
    assert not any(k in env for k in ("OPENAI_API_KEY", "MCP_COMMAND", "TUNNEL_CLIENT_CONFIG"))
    assert os.environ["CONTROL_PLANE_API_KEY"] == "other-project"
    changed = json.loads(text)
    changed["mcp"]["commands"][0]["command"] = "arbitrary-command"
    profile.write_text(json.dumps(changed), "utf-8")
    with pytest.raises(ConfigurationError):
        prepare_profile(config)


def test_redaction_and_status_do_not_expose_config(tmp_path):
    secret = "sk-proj-SYNTHETIC_not_a_real_key"
    assert secret not in redact(f"exception {secret} tunnel_synthetic")
    runtime = tmp_path / ".pairper"
    runtime.mkdir()
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "state": "configuration_error",
                "api_key": secret,
                "message": secret,
                "last_healthy_at": secret,
            }
        ),
        "utf-8",
    )
    assert secret not in json.dumps(public_status(tmp_path))
    (runtime / "health.url").write_text("http://evil.example:1234", "utf-8")
    assert health_url(tmp_path) is None
    (runtime / "health.url").write_text("http://127.0.0.1:1234/path", "utf-8")
    assert health_url(tmp_path) is None


def test_runtime_lock_prevents_duplicates(tmp_path):
    with runtime_lock(tmp_path):
        with pytest.raises(ConfigurationError):
            with runtime_lock(tmp_path):
                pass
