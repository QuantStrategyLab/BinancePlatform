#!/usr/bin/env python3
"""Emit a bounded read-only observation of the reviewed runtime scheduler."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from runtime_scheduler_observation import observe_runtime_scheduler_state


_SCHEDULER_STATES = frozenset({"enabled", "disabled", "unknown"})


def observe() -> dict[str, str]:
    try:
        state = observe_runtime_scheduler_state()
    except Exception:  # noqa: BLE001 - probe failures must not expose host details
        state = "unknown"
    if not isinstance(state, str) or state not in _SCHEDULER_STATES:
        state = "unknown"
    return {
        "scheduler_state": state,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


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
    args = parser.parse_args(argv)
    result = observe()
    if args.output_path:
        try:
            Path(args.output_path).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            result["scheduler_state"] = "unknown"
            print(json.dumps(result, sort_keys=True))
            return 1
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path and not _write_github_output(output_path, result):
        result["scheduler_state"] = "unknown"
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
