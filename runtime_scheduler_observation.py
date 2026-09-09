"""Read-only observation of the reviewed production runtime scheduler."""

from __future__ import annotations

import os
import pwd
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

_EXPECTED_CONTEXT = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "QuantStrategyLab/BinancePlatform",
    "GITHUB_REF": "refs/heads/main",
    "RUNNER_NAME": "binance-quant-runner",
}
_DISPATCH_SCRIPT = "/home/ubuntu/binance-quant/ops/dispatch-runtime.sh"
_EXACT_CRON = re.compile(
    rf"^0 \* \* \* \* {re.escape(_DISPATCH_SCRIPT)}"
    r"(?:\s+(?:(?:1|2)?>>?)\s*[A-Za-z0-9_./-]+)?(?:\s+2>&1)?$"
)
_STOPPED_DAEMON_STATES = frozenset({"failed", "inactive"})
_COMMAND_TIMEOUT_SECONDS = 2.0


def _run_probe(command: list[str], run_command: Callable[..., Any]) -> subprocess.CompletedProcess[str] | None:
    try:
        result = run_command(
            command,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return result if isinstance(result, subprocess.CompletedProcess) else None


def observe_runtime_scheduler_state(
    *,
    environ: Mapping[str, str] | None = None,
    run_command: Callable[..., Any] | None = None,
) -> str:
    """Return enabled, disabled, or unknown without changing the scheduler."""
    environment = os.environ if environ is None else environ
    if any(environment.get(name) != value for name, value in _EXPECTED_CONTEXT.items()):
        return "unknown"
    try:
        current_user = pwd.getpwuid(os.getuid()).pw_name
        script_stat = os.stat(_DISPATCH_SCRIPT)
    except (KeyError, OSError):
        return "unknown"
    if current_user != "ubuntu" or not stat.S_ISREG(script_stat.st_mode) or not os.access(_DISPATCH_SCRIPT, os.X_OK):
        return "unknown"

    command_runner = subprocess.run if run_command is None else run_command
    crontab = _run_probe(["crontab", "-l"], command_runner)
    if crontab is None or crontab.returncode != 0 or not isinstance(crontab.stdout, str):
        return "unknown"

    active_lines = [
        line.strip()
        for line in crontab.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    dispatch_lines = [line for line in active_lines if _DISPATCH_SCRIPT in line]
    matching_lines = [line for line in dispatch_lines if _EXACT_CRON.fullmatch(line)]
    if dispatch_lines != matching_lines or len(matching_lines) > 1:
        return "unknown"
    if not matching_lines:
        return "unknown"

    daemon = _run_probe(["systemctl", "is-active", "cron"], command_runner)
    if daemon is None or not isinstance(daemon.stdout, str):
        return "unknown"
    daemon_state = daemon.stdout.strip()
    if daemon.returncode == 0 and daemon_state == "active":
        return "enabled"
    if daemon.returncode == 3 and daemon_state in _STOPPED_DAEMON_STATES:
        return "disabled"
    return "unknown"
