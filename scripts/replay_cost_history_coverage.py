#!/usr/bin/env python3
"""Print offline coverage or an evidence-backed cost preview; never apply costs."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.cost_history_coverage import replay_coverage  # noqa: E402
from application.cost_history_basis import replay_cost_basis  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Private saved cost_history.v1 JSON")
    parser.add_argument("--mode", default="coverage-only", choices=["coverage-only"])
    parser.add_argument("--basis-evidence", help="Private cost_basis_evidence.v1 JSON for a cost preview")
    args = parser.parse_args()
    try:
        with Path(args.input).open(encoding="utf-8") as source:
            history = json.load(source)
        if args.basis_evidence:
            with Path(args.basis_evidence).open(encoding="utf-8") as source:
                report = replay_cost_basis(history, json.load(source), mode=args.mode)
        else:
            report = replay_coverage(history, mode=args.mode)
    except (ValueError, TypeError, KeyError, OSError):
        print("cost_history_input_invalid", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
