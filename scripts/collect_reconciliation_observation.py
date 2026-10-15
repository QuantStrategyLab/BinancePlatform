#!/usr/bin/env python3
"""Emit only synthetic observations. No credentials, network or cloud adapters."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from application.reconciliation_observation import collect_mock_observation, parse_time


def _read(path):
    raw = Path(path).read_bytes()
    if len(raw) > 131072:
        raise ValueError("mock_input_too_large")
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--fixture", required=True, help="Synthetic JSON only")
    parser.add_argument("--report-dir", required=True, help="Local synthetic reports, create-only")
    parser.add_argument("--now", help="Explicit simulated clock for fixture replay; defaults to UTC now")
    args = parser.parse_args()
    try:
        def persist(report):
            directory = Path(args.report_dir)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / (report["report_id"] + ".json")).open("x", encoding="utf-8") as output:
                json.dump(report, output, indent=2)
                output.write("\n")

        payload = collect_mock_observation(
            _read(args.config), _read(args.fixture), persist_report=persist,
            now=parse_time(args.now) if args.now else None,
        )
    except (OSError, ValueError, TypeError):
        print("mock_input_invalid", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
