#!/usr/bin/env python3
"""Emit a bounded read-only observation of the reviewed runtime scheduler."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from runtime_scheduler_observation import observe_runtime_scheduler_assessment, observe_runtime_scheduler_state


_SCHEDULER_STATES = frozenset({"enabled", "disabled", "unknown"})
_REASONS = frozenset({
    "unexpected_execution_context", "host_inspection_unavailable", "unexpected_os_user",
    "dispatch_script_missing", "host_permission_denied",
    "dispatch_script_invalid", "crontab_unavailable", "dispatch_schedule_unrecognized",
    "dispatch_schedule_missing", "daemon_probe_unavailable", "cron_active", "cron_stopped",
    "daemon_state_unrecognized",
})


def observe(*, diagnose_reason: bool = False) -> dict[str, str]:
    reason = "probe_failed"
    try:
        if diagnose_reason:
            state, reason = observe_runtime_scheduler_assessment()
            if reason not in _REASONS:
                state, reason = "unknown", "invalid_assessment"
        else:
            state = observe_runtime_scheduler_state()
    except Exception:  # noqa: BLE001 - probe failures must not expose host details
        state, reason = "unknown", "probe_failed"
    if not isinstance(state, str) or state not in _SCHEDULER_STATES:
        state, reason = "unknown", "invalid_assessment"
    result = {
        "scheduler_state": state,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    if diagnose_reason:
        result["reason"] = reason
    return result


def _write_github_output(path: str, result: dict[str, str]) -> bool:
    try:
        with open(path, "a", encoding="utf-8") as output:
            output.write(
                f"scheduler_state={result['scheduler_state']}\n"
                f"observed_at={result['observed_at']}\n"
            )
    except OSError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-path")
    parser.add_argument("--diagnose-reason", action="store_true")
    args = parser.parse_args(argv)
    result = observe(diagnose_reason=args.diagnose_reason)
    if args.output_path:
        try:
            Path(args.output_path).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            result["scheduler_state"] = "unknown"
            if args.diagnose_reason:
                result["reason"] = "output_unavailable"
            print(json.dumps(result, sort_keys=True))
            return 1
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path and not _write_github_output(output_path, result):
        result["scheduler_state"] = "unknown"
        if args.diagnose_reason:
            result["reason"] = "output_unavailable"
        if args.output_path:
            try:
                Path(args.output_path).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            except OSError:
                print(json.dumps(result, sort_keys=True))
                return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
