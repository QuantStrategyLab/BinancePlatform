"""
Monthly execution report aggregation script.

Reads hourly JSON execution reports from a directory and produces:
  - monthly_execution_bundle.json  — structured aggregate data
  - ai_review_input.md             — markdown document for human/AI review
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------

def _load_reports(hourly_dir: str) -> list[tuple[str, dict[str, Any]]]:
    """Return (filename, report_dict) pairs sorted by filename (chronological)."""
    entries = []
    for fname in sorted(os.listdir(hourly_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(hourly_dir, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
            entries.append((fname, data))
        except (json.JSONDecodeError, OSError):
            pass
    return entries


def _intent_usdt(intent: dict) -> float:
    """Extract USDT value from a trade intent dict."""
    return float(intent.get("quoteOrderQty", 0) or intent.get("usdt_value", 0) or 0)


def _is_subscribe(intent: dict) -> bool:
    action = (intent.get("action", "") or intent.get("effect_type", "") or "").lower()
    return "subscri" in action


def _is_redeem(intent: dict) -> bool:
    action = (intent.get("action", "") or intent.get("effect_type", "") or "").lower()
    return "redeem" in action or "redemption" in action


def aggregate_hourly_reports(hourly_dir: str, report_month: str) -> dict[str, Any]:
    """Aggregate all hourly JSON files in hourly_dir into a monthly bundle."""
    entries = _load_reports(hourly_dir)

    # --- run statistics ---
    total_runs = len(entries)
    successful_runs = 0
    failed_runs = 0
    dry_run_runs = 0

    # --- side effects / gating ---
    executed_side_effects = 0
    suppressed_side_effects = 0
    gating_counts: dict[str, int] = {}

    # --- trade summary accumulators ---
    btc_buys = 0
    btc_sells = 0
    btc_usdt = 0.0
    trend_buys = 0
    trend_sells = 0
    trend_usdt = 0.0
    trend_symbols: set[str] = set()

    # --- pnl ---
    start_equity: float | None = None
    end_equity: float | None = None

    # --- events ---
    circuit_breaker_events: list[dict] = []
    degraded_mode_events: list[dict] = []

    # --- pool change tracking ---
    upstream_pool_changes: list[dict] = []
    prev_pool: set[str] | None = None

    # --- error summary ---
    all_errors: list[dict] = []

    # --- earn buffer ---
    earn_subscribes = 0
    earn_redeems = 0

    for fname, report in entries:
        status = report.get("status", "ok")
        run_id = report.get("run_id", fname)
        if report.get("dry_run"):
            dry_run_runs += 1

        # Success / failure
        if status == "ok":
            successful_runs += 1
        else:
            failed_runs += 1
            for err in (report.get("error_summary") or {}).get("errors", []):
                all_errors.append({"run_id": run_id, "file": fname, **err})

        # Equity (first and last)
        equity = report.get("total_equity_usdt")
        if equity is not None:
            if start_equity is None:
                start_equity = float(equity)
            end_equity = float(equity)

        side_effect_summary = report.get("side_effect_summary") or {}
        executed_side_effects += int(side_effect_summary.get("executed_call_count", 0) or 0)
        suppressed_side_effects += int(side_effect_summary.get("suppressed_call_count", 0) or 0)

        for gate, count in (report.get("gating_summary") or {}).items():
            gate_name = str(gate)
            gating_counts[gate_name] = gating_counts.get(gate_name, 0) + int(count or 0)

        # BTC DCA intents
        for intent in report.get("btc_dca_intents", []) or []:
            side = (intent.get("side") or "").upper()
            usdt = _intent_usdt(intent)
            if side == "BUY":
                btc_buys += 1
            elif side == "SELL":
                btc_sells += 1
            btc_usdt += usdt

        # Trend rotation intents
        for intent in report.get("buy_sell_intents", []) or []:
            side = (intent.get("side") or "").upper()
            usdt = _intent_usdt(intent)
            symbol = intent.get("symbol", "")
            if side == "BUY":
                trend_buys += 1
            elif side == "SELL":
                trend_sells += 1
            trend_usdt += usdt
            if symbol:
                trend_symbols.add(symbol)

        # Circuit breaker
        if report.get("circuit_breaker_triggered"):
            circuit_breaker_events.append({"run_id": run_id, "file": fname})

        # Degraded mode
        degraded = report.get("degraded_mode_level")
        if degraded is not None:
            degraded_mode_events.append({"run_id": run_id, "file": fname, "level": degraded})

        # Upstream pool changes
        pool_symbols = set(report.get("upstream_pool_symbols", []) or [])
        if prev_pool is not None and pool_symbols != prev_pool:
            added = sorted(pool_symbols - prev_pool)
            removed = sorted(prev_pool - pool_symbols)
            upstream_pool_changes.append({
                "run_id": run_id,
                "file": fname,
                "added": added,
                "removed": removed,
            })
        prev_pool = pool_symbols

        # Earn buffer ops
        for intent in report.get("redemption_subscription_intents", []) or []:
            if _is_subscribe(intent):
                earn_subscribes += 1
            elif _is_redeem(intent):
                earn_redeems += 1

    # pnl calculations
    pnl_usdt = (end_equity or 0.0) - (start_equity or 0.0)
    pnl_pct: float | None = None
    if start_equity and start_equity != 0:
        pnl_pct = round(pnl_usdt / start_equity * 100, 4)

    bundle: dict[str, Any] = {
        "report_month": report_month,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_statistics": {
            "total_runs": total_runs,
            "successful_runs": successful_runs,
            "failed_runs": failed_runs,
            "dry_run_runs": dry_run_runs,
        },
        "side_effect_summary": {
            "executed_call_count": executed_side_effects,
            "suppressed_call_count": suppressed_side_effects,
        },
        "execution_gating": {
            "total_events": int(sum(gating_counts.values())),
            "counts": dict(sorted(gating_counts.items())),
        },
        "trade_summary": {
            "btc_core": {
                "buys": btc_buys,
                "sells": btc_sells,
                "total_usdt": round(btc_usdt, 2),
            },
            "trend_rotation": {
                "buys": trend_buys,
                "sells": trend_sells,
                "total_usdt": round(trend_usdt, 2),
                "symbols_traded": sorted(trend_symbols),
            },
        },
        "pnl_overview": {
            "start_equity_usdt": start_equity,
            "end_equity_usdt": end_equity,
            "pnl_usdt": round(pnl_usdt, 2),
            "pnl_pct": pnl_pct,
        },
        "circuit_breaker_events": circuit_breaker_events,
        "degraded_mode_events": degraded_mode_events,
        "upstream_pool_changes": upstream_pool_changes,
        "error_summary": {
            "total_errors": len(all_errors),
            "errors": all_errors,
        },
        "earn_buffer_ops": {
            "subscribes": earn_subscribes,
            "redeems": earn_redeems,
        },
    }
    return bundle


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------

def format_review_markdown(bundle: dict[str, Any]) -> str:
    month = bundle["report_month"]
    generated = bundle.get("generated_at_utc", "")
    stats = bundle["run_statistics"]
    trade = bundle["trade_summary"]
    pnl = bundle["pnl_overview"]
    side_effects = bundle["side_effect_summary"]
    gating = bundle["execution_gating"]
    cb_events = bundle["circuit_breaker_events"]
    dg_events = bundle["degraded_mode_events"]
    pool_changes = bundle["upstream_pool_changes"]
    err_summary = bundle["error_summary"]
    earn = bundle["earn_buffer_ops"]

    lines: list[str] = []

    lines.append(f"# Monthly Execution Review — {month}")
    lines.append("")
    lines.append(f"_Generated: {generated}_")
    lines.append("")

    # Scope / interpretation notes
    lines.append("## Report Scope")
    lines.append("")
    lines.append("- This is BinancePlatform's downstream monthly execution review, not a pure upstream pool publication.")
    lines.append("- It summarizes runtime health, recorded trade intents, no-trade / gating reasons, earn buffer operations, circuit breaker activity, degraded mode, and upstream pool changes.")
    lines.append("- Upstream pool changes are included as execution context from CryptoSnapshotPipelines, but they are only one input section of this report.")
    lines.append("- Equity deltas in this report are raw month-start vs month-end snapshots and may include manual deposits, withdrawals, or other external balance flows.")
    lines.append("- Trade and earn sections reflect execution intents/actions recorded in hourly reports, not a separate exchange fill reconciliation ledger.")
    lines.append("- Side-effect counts indicate how many client / notification / state-write calls were actually executed versus suppressed (for example in dry-run or replay contexts).")
    lines.append("")

    # Run statistics
    lines.append("## Run Statistics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total runs | {stats['total_runs']} |")
    lines.append(f"| Successful runs | {stats['successful_runs']} |")
    lines.append(f"| Failed runs | {stats['failed_runs']} |")
    lines.append(f"| Dry-run runs | {stats['dry_run_runs']} |")
    success_rate = (
        round(stats["successful_runs"] / stats["total_runs"] * 100, 1)
        if stats["total_runs"] > 0 else 0
    )
    lines.append(f"| Success rate | {success_rate}% |")
    lines.append("")

    # Side effects
    lines.append("## Side Effects")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Executed calls | {side_effects['executed_call_count']} |")
    lines.append(f"| Suppressed calls | {side_effects['suppressed_call_count']} |")
    lines.append("")

    # Gating summary
    lines.append("## Execution Gating / No-Trade Reasons")
    lines.append("")
    if gating["counts"]:
        lines.append("| Gate | Count |")
        lines.append("|------|-------|")
        for gate_name, count in sorted(gating["counts"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {gate_name} | {count} |")
    else:
        lines.append("_No explicit gating or no-trade reasons were recorded this month._")
    lines.append("")

    # PnL overview
    lines.append("## PnL Overview")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Start equity (USDT) | {pnl['start_equity_usdt']} |")
    lines.append(f"| End equity (USDT) | {pnl['end_equity_usdt']} |")
    lines.append(f"| PnL (USDT) | {pnl['pnl_usdt']} |")
    lines.append(f"| PnL (%) | {pnl['pnl_pct']} |")
    lines.append("")
    lines.append("> Note: Equity deltas may include external balance flows and should not be interpreted as pure strategy PnL without separate cash-flow reconciliation.")
    lines.append("")

    # Trade summary
    lines.append("## Trade Summary")
    lines.append("")
    lines.append("> Note: Trade counts below are based on recorded strategy intents in hourly execution reports, not exchange fill reconciliation.")
    lines.append("")
    lines.append("### BTC Core (DCA)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    btc = trade["btc_core"]
    lines.append(f"| Buys | {btc['buys']} |")
    lines.append(f"| Sells | {btc['sells']} |")
    lines.append(f"| Total USDT | {btc['total_usdt']} |")
    lines.append("")

    lines.append("### Trend Rotation")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    tr = trade["trend_rotation"]
    lines.append(f"| Buys | {tr['buys']} |")
    lines.append(f"| Sells | {tr['sells']} |")
    lines.append(f"| Total USDT | {tr['total_usdt']} |")
    symbols_str = ", ".join(tr["symbols_traded"]) if tr["symbols_traded"] else "—"
    lines.append(f"| Symbols traded | {symbols_str} |")
    lines.append("")

    # Earn buffer
    lines.append("## Earn Buffer Operations")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Subscribes | {earn['subscribes']} |")
    lines.append(f"| Redeems | {earn['redeems']} |")
    lines.append("")

    # Circuit breaker events
    lines.append("## Circuit Breaker Events")
    lines.append("")
    if cb_events:
        lines.append("| Run ID | File |")
        lines.append("|--------|------|")
        for ev in cb_events:
            lines.append(f"| {ev['run_id']} | {ev['file']} |")
    else:
        lines.append("_No circuit breaker events this month._")
    lines.append("")

    # Degraded mode events
    lines.append("## Degraded Mode Events")
    lines.append("")
    if dg_events:
        lines.append("| Run ID | File | Level |")
        lines.append("|--------|------|-------|")
        for ev in dg_events:
            lines.append(f"| {ev['run_id']} | {ev['file']} | {ev['level']} |")
    else:
        lines.append("_No degraded mode events this month._")
    lines.append("")

    # Upstream pool changes
    lines.append("## Upstream Pool Changes")
    lines.append("")
    if pool_changes:
        lines.append("| Run ID | File | Added | Removed |")
        lines.append("|--------|------|-------|---------|")
        for ch in pool_changes:
            added = ", ".join(ch["added"]) or "—"
            removed = ", ".join(ch["removed"]) or "—"
            lines.append(f"| {ch['run_id']} | {ch['file']} | {added} | {removed} |")
    else:
        lines.append("_No pool composition changes this month._")
    lines.append("")

    # Error summary
    lines.append("## Error Summary")
    lines.append("")
    if err_summary["total_errors"] > 0:
        lines.append(f"Total errors: **{err_summary['total_errors']}**")
        lines.append("")
        lines.append("| Run ID | Stage | Message |")
        lines.append("|--------|-------|---------|")
        for err in err_summary["errors"]:
            stage = err.get("stage", "—")
            msg = err.get("message", "—")
            run_id = err.get("run_id", "—")
            lines.append(f"| {run_id} | {stage} | {msg} |")
    else:
        lines.append("_No errors recorded this month._")
    lines.append("")

    # Review questions
    lines.append("## Review Questions")
    lines.append("")
    lines.append("1. Does the equity trend look explainable once possible external deposits/withdrawals are considered?")
    lines.append("2. Do the recorded gating / no-trade reasons look expected, or do any thresholds / cooldowns appear too restrictive?")
    lines.append("3. Were any circuit breaker events justified, or do thresholds need adjusting?")
    lines.append("4. Did upstream pool changes have a noticeable impact on performance?")
    lines.append("5. Are the failed runs isolated incidents or part of a pattern?")
    lines.append("6. Do the recorded trade intents suggest BTC DCA cadence or trend sizing should be adjusted?")
    lines.append("7. Were earn buffer subscribe/redeem operations executed at appropriate times?")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate hourly execution reports into a monthly bundle."
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Month in YYYY-MM format (e.g. 2026-03)",
    )
    parser.add_argument(
        "--hourly-dir",
        required=True,
        help="Directory containing hourly JSON files for the given month.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where output files will be written.",
    )
    args = parser.parse_args()

    hourly_dir = args.hourly_dir
    if not os.path.isdir(hourly_dir):
        print(f"ERROR: hourly-dir does not exist: {hourly_dir}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"Aggregating reports from: {hourly_dir}")
    bundle = aggregate_hourly_reports(hourly_dir, args.month)

    bundle_path = os.path.join(args.output_dir, "monthly_execution_bundle.json")
    with open(bundle_path, "w") as f:
        json.dump(bundle, f, indent=2)
    print(f"Written: {bundle_path}")

    md = format_review_markdown(bundle)
    md_path = os.path.join(args.output_dir, "ai_review_input.md")
    with open(md_path, "w") as f:
        f.write(md)
    print(f"Written: {md_path}")

    stats = bundle["run_statistics"]
    print(
        f"Summary: {stats['total_runs']} runs, "
        f"{stats['successful_runs']} ok, "
        f"{stats['failed_runs']} failed"
    )


if __name__ == "__main__":
    main()
