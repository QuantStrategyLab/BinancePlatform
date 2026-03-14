#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import run_shadow_candidate_monitor as monitor
import shadow_replay


DEFAULT_UPSTREAM_ROOT = Path(__file__).resolve().parents[1] / "CryptoLeaderRotation"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the monthly baseline-vs-challenger shadow monitor without affecting live execution."
    )
    parser.add_argument("--upstream-root", default=str(DEFAULT_UPSTREAM_ROOT), help="Path to the CryptoLeaderRotation repo.")
    parser.add_argument("--output-dir", default="reports", help="Directory for monthly shadow monitor outputs.")
    parser.add_argument("--raw-dir", default=None, help="Optional override for local daily raw OHLCV CSVs.")
    parser.add_argument("--max-age-days", type=int, default=45, help="Freshness window used for both tracks.")
    parser.add_argument("--activation-lag-days", type=int, default=None, help="Optional shared activation lag override.")
    parser.add_argument("--cost-bps", type=float, default=0.0, help="Optional shared turnover-scaled cost assumption.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    upstream_root = Path(args.upstream_root).resolve()
    output_dir = shadow_replay.ensure_directory(args.output_dir)
    raw_dir = Path(args.raw_dir).resolve() if args.raw_dir else upstream_root / "data" / "raw"

    results = monitor.run_shadow_candidate_monitor(
        upstream_root=upstream_root,
        output_dir=output_dir,
        raw_dir=raw_dir,
        max_age_days=max(0, int(args.max_age_days)),
        activation_lag_days=args.activation_lag_days,
        cost_bps=float(args.cost_bps),
    )
    monitor.print_monitor_console_summary(results)
    print(f"track_summary_path={output_dir / 'shadow_candidate_track_summary.csv'}")
    print(f"watchlist_path={output_dir / 'shadow_candidate_promotion_watchlist.csv'}")


if __name__ == "__main__":
    main()
