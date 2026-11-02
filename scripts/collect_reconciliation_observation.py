#!/usr/bin/env python3
"""Emit only synthetic observations. No credentials, network or cloud adapters."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from application.reconciliation_observation import collect_mock_observation, parse_time
from application.forward_reconciliation import build_forward_report, forward_envelope


def _read(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("mock_duplicate_key")
            result[key] = value
        return result

    raw = Path(path).read_bytes()
    if len(raw) > 131072:
        raise ValueError("mock_input_too_large")
    return json.loads(raw, object_pairs_hook=unique_object)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Required for the original comparison mock")
    fixtures = parser.add_mutually_exclusive_group(required=True)
    fixtures.add_argument("--fixture", help="Original synthetic comparison JSON")
    fixtures.add_argument("--forward-fixture", help="Synthetic forward native-quantity JSON")
    parser.add_argument("--report-dir", required=True, help="Local synthetic reports, create-only")
    parser.add_argument("--now", help="Explicit simulated clock for fixture replay; defaults to UTC now")
    args = parser.parse_args()
    try:
        if args.forward_fixture:
            if args.config or args.now:
                raise ValueError("forward_options_invalid")
            fixture = _read(args.forward_fixture)
            if not isinstance(fixture, dict) or fixture.get("environment") != "synthetic":
                raise ValueError("forward_synthetic_input_required")
            report = build_forward_report(fixture)
            directory = Path(args.report_dir)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "forward-synthetic-report.json").open("x", encoding="utf-8") as output:
                json.dump(report, output, indent=2)
                output.write("\n")
            print(json.dumps(forward_envelope(report), indent=2))
            return 0
        if not args.config:
            raise ValueError("mock_config_required")
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
