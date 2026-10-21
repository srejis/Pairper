"""Real, source-packet-only Codex CLI execution with durable run evidence.

No provider key is requested, no account entitlement/cost is inferred, and no
previous translation/session is loaded. The existing ChatGPT login is reused by
Codex itself. Host-controlled files are written outside the isolated model cwd.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path

from .prompts import PROMPT_VERSION, build_prompt, validate_protocol


class RunnerError(RuntimeError):
    def __init__(self, message: str, *, kind="execution", evidence_dir=None):
        super().__init__(message)
        self.kind = kind
        self.evidence_dir = str(evidence_dir) if evidence_dir else None


class RunnerCancelled(RunnerError):
    pass


def _json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _redact(text: str) -> str:
    # Do not persist credentials if a CLI diagnostic unexpectedly echoes one.
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED_API_KEY]", text)
    return re.sub(r'(?i)(["\']?(?:access_token|refresh_token|id_token|api_key)["\']?\s*[:=]\s*["\'])[^"\']+', r'\1[REDACTED]', text)


def resolve_codex_executable(cli_path: str | Path | None = None) -> Path:
    """Prefer the desktop native CLI, resolving npm shims without a shell.

    Desktop discovery inspects only one installation level and file metadata;
    it never opens account/configuration files.
    """
    candidates = []
    if cli_path is not None:
        candidates.append(Path(cli_path).expanduser())
    else:
        if os.environ.get("CODEX_CLI_PATH"):
            candidates.append(Path(os.environ["CODEX_CLI_PATH"]))
        if os.name == "nt":
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                app_bin = Path(local_appdata) / "OpenAI/Codex/bin"
                try:
                    installed = [(path.stat().st_mtime_ns, str(path), path)
                                 for path in app_bin.glob("*/codex.exe") if path.is_file()]
                    candidates.extend(item[2] for item in sorted(installed, reverse=True))
                except OSError:
                    pass  # An inaccessible desktop install can use the npm fallback.
            if os.environ.get("APPDATA"):
                candidates.append(Path(os.environ["APPDATA"]) / "npm/codex.ps1")
        for name in ("codex.exe", "codex", "codex.cmd", "codex.ps1"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    for candidate in candidates:
        candidate = candidate.resolve()
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() == ".exe":
            return candidate
        roots = [candidate.parent / "node_modules/@openai/codex", candidate.parent.parent]
        for root in roots:
            vendor_roots = [root / "vendor"]
            arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
            system = "win32" if os.name == "nt" else ("darwin" if platform.system() == "Darwin" else "linux")
            vendor_roots.append(root / f"node_modules/@openai/codex-{system}-{arch}/vendor")
            for vendor in vendor_roots:
                native_name = "codex.exe" if os.name == "nt" else "codex"
                matches = list(vendor.glob(f"*/bin/{native_name}"))
                if matches:
                    return matches[0].resolve()
        if os.name != "nt" and candidate.suffix not in {".ps1", ".cmd", ".bat", ".js"}:
            return candidate
    raise RunnerError("Codex native executable not found; install/login to Codex CLI first", kind="configuration")


def configured_user_model() -> str | None:
    """Read only the top-level model assignment; never copy other config/auth."""
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    try:
        with (home / "config.toml").open(encoding="utf-8") as source:
            for line in source:
                if line.lstrip().startswith("["):
                    break
                if re.match(r"^\s*model\s*=", line):
                    model = tomllib.loads(line).get("model")
                    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", model):
                        return model
                    return None
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return None


class CodexRunner:
    """run(request, schema, images, artifact_dir) -> schema-validated response dict.

    max_retries bounds protocol/process retries only; the host separately bounds
    semantic repair rounds. cancel() terminates active invocations and never
    retries them. Evidence paths/usage are available via last_evidence.
    """
    DISABLED_FEATURES = (
        "shell_tool", "shell_snapshot", "apps", "plugins", "hooks", "browser_use", "browser_use_external",
        "browser_use_full_cdp_access", "computer_use", "in_app_browser", "image_generation",
        "multi_agent", "multi_agent_v2", "workspace_dependencies", "memories", "artifact",
        "tool_suggest", "enable_mcp_apps", "network_proxy", "chronicle",
        "code_mode", "code_mode_host", "code_mode_only", "code_mode_prewarm",
        "view_image", "skill_search", "sleep_tool", "remote_plugin", "external_agent_memory_import",
        "in_app_chat", "in_app_local_automation", "agent_message_board",
    )

    prompt_protocol_version = PROMPT_VERSION

    def _build_prompt(self, request: dict) -> str:
        return build_prompt(request)

    def _validate_protocol(self, request: dict, response: dict, *, image_count: int) -> None:
        validate_protocol(request, response, image_count=image_count)

    def __init__(self, cli_path: str | Path | None = None, *, timeout: float = 600,
                 max_retries: int = 2, cancel_event: threading.Event | None = None,
                 model: str | None = None, model_catalog_path: str | Path | None = None):
        if not 0 < timeout <= 600 or not 0 <= max_retries <= 2:
            raise ValueError("timeout must be in (0,600], max_retries in [0,2]")
        self.executable = resolve_codex_executable(cli_path)
        if model is not None and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", model):
            raise ValueError("Invalid caller-selected Codex model name")
        self.model = model
        self.model_catalog_path = Path(model_catalog_path).resolve() if model_catalog_path else None
        self.timeout = timeout
        self.max_retries = max_retries
        self.cancel_event = cancel_event or threading.Event()
        self.last_evidence = None
        self._active = set()
        self._lock = threading.Lock()
        self._capabilities = None

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            for process in tuple(self._active):
                if process.poll() is None:
                    process.terminate()

    def _environment(self):
        env = dict(os.environ)
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "AZURE_OPENAI_API_KEY",
                     "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
                     "CODEX_THREAD_ID", "CODEX_PARENT_THREAD_ID", "CODEX_INHERITED_CONTEXT",
                     "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_APP_TOOLS_PIPE_PATH",
                     "CODEX_MCP_NODE_PATH", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID",
                     "CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY"):
            env.pop(name, None)
        return env

    def _preflight(self) -> dict:
        if self._capabilities is not None:
            return self._capabilities
        # No AI calls in capability discovery; never print user config/auth.
        kwargs = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
                  "timeout": 20, "shell": False, "env": self._environment()}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        help_result = subprocess.run([str(self.executable), "exec", "--help"], **kwargs)
        required = ("--ephemeral", "--ignore-user-config", "--output-schema", "--output-last-message", "--json")
        if help_result.returncode or any(flag not in help_result.stdout for flag in required):
            raise RunnerError("Codex CLI lacks required isolated structured-output options", kind="configuration")
        features_result = subprocess.run([str(self.executable), "features", "list", "-c", "features.shell_tool=false"], **kwargs)
        features = {line.split()[0] for line in features_result.stdout.splitlines() if line.split()}
        shell_line = next((line for line in features_result.stdout.splitlines() if line.startswith("shell_tool ")), "")
        if features_result.returncode or "shell_tool" not in features or not shell_line.rstrip().endswith("false"):
            raise RunnerError("Cannot establish command-tool isolation for this Codex CLI", kind="configuration")
        self._capabilities = {"required_flags_present": True, "shell_tool_disabled_supported": True,
                              "disabled_features": [f for f in self.DISABLED_FEATURES if f in features]}
        return self._capabilities

    def _communicate(self, process, prompt):
        deadline = time.monotonic() + self.timeout
        first = True
        while True:
            if self.cancel_event.is_set():
                if process.poll() is None:
                    process.terminate()
                try:
                    out, err = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    out, err = process.communicate()
                return out, err, "cancelled"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                out, err = process.communicate()
                return out, err, "timeout"
            try:
                return (*process.communicate(input=prompt if first else None, timeout=min(0.25, remaining)), None)
            except subprocess.TimeoutExpired:
                first = False

    def run(self, request: dict, schema: dict, images: list[str], artifact_dir: Path) -> dict:
        try:
            import jsonschema
        except ImportError as exc:
            raise RunnerError("jsonschema is required to validate actual AI output", kind="configuration") from exc
        jsonschema.Draft202012Validator.check_schema(schema)
        prompt = self._build_prompt(request)
        attached = [Path(path).resolve() for path in images]
        if any(not path.is_file() for path in attached):
            raise RunnerError("An attached original image is missing", kind="input")
        if self.cancel_event.is_set():
            raise RunnerCancelled("Run cancelled before invocation", kind="cancelled")
        capabilities = self._preflight()
        model = configured_user_model()
        run_dir = Path(artifact_dir).resolve() / ("call-" + uuid.uuid4().hex)
        run_dir.mkdir(parents=True, exist_ok=False)
        _json(run_dir / "request.json", request)
        _json(run_dir / "schema.json", schema)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        evidence = {"status": "running", "directory": str(run_dir), "configured_user_model": model,
            "model_override_requested": self.model is not None, "configured_user_model_applied": False,
            "caller_selected_model": self.model,
            "model_selection": "Caller selected; workflow must retain account-list provenance" if self.model else "Native CLI default; app config model aliases are not assumed CLI-compatible",
            "model_reported": None, "usage": [], "attempts": [],
            "auth": "Existing Codex ChatGPT login; auth values not read or copied", "capabilities": capabilities,
            "source_request_sha256": hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "provider_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_protocol_version": self.prompt_protocol_version,
            "raw_request_characters": len(json.dumps(request, ensure_ascii=False)),
            "provider_prompt_characters": len(prompt),
            "history": "Fresh empty temporary cwd each attempt, ephemeral, user config ignored, AGENTS disabled; no resume/history lookup"}
        self.last_evidence = evidence
        if self.model_catalog_path:
            catalog = self.model_catalog_path.read_bytes()
            # Caller provides the credential-free model/list evidence, not auth.
            catalog_data = json.loads(catalog)
            names = {x.get('model', x.get('id')) for x in catalog_data.get('actual_account_model_list', [])}
            if self.model and self.model not in names:
                raise RunnerError("Selected model absent from supplied account catalog", kind="configuration", evidence_dir=run_dir)
            (run_dir / "account-model-catalog.json").write_bytes(catalog)
            evidence['account_model_catalog'] = {'source': str(self.model_catalog_path),
                'sha256': hashlib.sha256(catalog).hexdigest(), 'selected_model_listed': self.model in names,
                'catalog_is_not_execution_success_proof': True}
        _json(run_dir / "evidence.json", evidence)
        failure = None
        for attempt in range(self.max_retries + 1):
            attempt_dir = run_dir / f"attempt-{attempt + 1:02}"
            attempt_dir.mkdir()
            output = attempt_dir / "last-message.json"
            with tempfile.TemporaryDirectory(prefix="pairper-agent-") as fresh_cwd:
                args = [str(self.executable), "exec", "--ephemeral", "--ignore-user-config",
                    "--sandbox", "read-only", "--skip-git-repo-check", "--json", "--color", "never",
                    "--cd", fresh_cwd, "--output-schema", str(run_dir / "schema.json"),
                    "--output-last-message", str(output), "-c", "mcp_servers={}",
                    "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                    "-c", 'history.persistence="none"', "-c", 'forced_login_method="chatgpt"']
                for feature in capabilities["disabled_features"]:
                    args.extend(["--disable", feature])
                if self.model:
                    args.extend(["--model", self.model])
                for path in attached:
                    args.extend(["--image", str(path)])
                args.append("-")
                _json(attempt_dir / "invocation.json", {"argv": args, "fresh_empty_cwd": fresh_cwd,
                    "stdin_prompt": str(run_dir / "prompt.txt"), "shell": False})
                record = {"attempt": attempt + 1, "exit_code": None, "status": "running"}
                started = time.monotonic()
                process = None
                try:
                    if self.cancel_event.is_set():
                        raise RunnerCancelled("Run cancelled before retry", kind="cancelled", evidence_dir=run_dir)
                    kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                        "text": True, "encoding": "utf-8", "errors": "replace", "shell": False,
                        "cwd": fresh_cwd, "env": self._environment()}
                    if os.name == "nt":
                        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                    process = subprocess.Popen(args, **kwargs)
                    with self._lock:
                        self._active.add(process)
                    stdout, stderr, stopped = self._communicate(process, prompt)
                    (attempt_dir / "events.jsonl").write_text(_redact(stdout), encoding="utf-8")
                    (attempt_dir / "stderr.txt").write_text(_redact(stderr), encoding="utf-8")
                    record["exit_code"] = process.returncode
                    events = []
                    for line in stdout.splitlines():
                        try:
                            item = json.loads(line)
                            if isinstance(item, dict):
                                events.append(item)
                        except json.JSONDecodeError:
                            pass
                    usage = [e["usage"] for e in events if isinstance(e.get("usage"), dict)]
                    evidence["usage"].extend(usage)
                    reported = next((e["model"] for e in events if isinstance(e.get("model"), str)), None)
                    evidence["model_reported"] = reported or evidence["model_reported"]
                    record["usage"] = usage
                    if stopped == "cancelled" or self.cancel_event.is_set():
                        raise RunnerCancelled("Codex execution cancelled", kind="cancelled", evidence_dir=run_dir)
                    if stopped == "timeout":
                        raise RunnerError("Codex execution timed out", kind="timeout", evidence_dir=run_dir)
                    if process.returncode != 0 or not any(e.get("type") == "turn.completed" for e in events):
                        raise RunnerError("Codex failed; inspect saved sanitized diagnostics", kind="execution", evidence_dir=run_dir)
                    disallowed = [e for e in events if e.get("type", "").startswith("item.")
                        and e.get("item", {}).get("type") not in {"agent_message", "reasoning", "error"}]
                    if disallowed:
                        raise RunnerError("Unexpected tool activity in source-only AI run", kind="isolation", evidence_dir=run_dir)
                    raw = output.read_text(encoding="utf-8")
                    response = json.loads(raw)
                    jsonschema.Draft202012Validator(schema).validate(response)
                    self._validate_protocol(request, response, image_count=len(attached))
                    record["status"] = "success"
                    evidence["status"] = "success"
                    evidence["response_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
                    _json(run_dir / "response.json", response)
                    return response
                except RunnerCancelled as exc:
                    failure = exc
                    record.update(status="cancelled", error_kind="cancelled")
                    evidence["status"] = "cancelled"
                    raise
                except (RunnerError, OSError, ValueError, jsonschema.ValidationError) as exc:
                    failure = exc if isinstance(exc, RunnerError) else RunnerError(
                        "Invalid CLI response/schema/protocol or local execution failure", kind="protocol", evidence_dir=run_dir)
                    record.update(status="failed", error_kind=failure.kind)
                    # Cancellation, credentials/quota and isolation failures are not retry candidates.
                    diagnostic = (attempt_dir / "stderr.txt").read_text("utf-8") if (attempt_dir / "stderr.txt").exists() else ""
                    terminal = failure.kind == "isolation" or any(word in diagnostic.lower() for word in
                        ("not logged in", "unauthorized", "authentication", "usage limit", "insufficient_quota",
                         "not supported when using codex", "model_not_found"))
                    if terminal or attempt == self.max_retries:
                        evidence["status"] = "failed"
                        if failure is exc:
                            raise
                        raise failure from exc
                finally:
                    if process is not None:
                        with self._lock:
                            self._active.discard(process)
                        if process.poll() is None:
                            process.kill()
                            process.communicate()
                    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    evidence["attempts"].append(record)
                    _json(run_dir / "evidence.json", evidence)
        raise failure or RunnerError("No Codex result", evidence_dir=run_dir)
