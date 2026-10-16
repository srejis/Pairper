"""Fixed, hidden Windows scheduler operations for the desktop settings bridge."""

import json
import os
import subprocess

from pairper.tunnel.config import project_root


def _run(script, root):
    if os.name != "nt":
        raise RuntimeError("Windows only")
    shell = os.path.join(
        os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    return subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / script),
        ],
        cwd=root,
        capture_output=True,
        timeout=45,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout.decode("utf-8-sig")


def autostart_status(root=None):
    return json.loads(_run("get-pairper-autostart.ps1", root or project_root()))


def set_autostart(enabled, root=None):
    root = root or project_root()
    _run("install-pairper-autostart.ps1" if enabled else "uninstall-pairper-autostart.ps1", root)
    status = autostart_status(root)
    if status["enabled"] != enabled:
        raise RuntimeError("Autostart change was not applied")
    return status
