"""Opt-in workflow fallback; uses the same durable runtime incident marker."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notifications.operational import notify_workflow  # noqa: E402


def main():
    acknowledged = notify_workflow({"reason": "runtime_startup_failed"}, "")
    return 0 if acknowledged else 1


if __name__ == "__main__":
    raise SystemExit(main())
