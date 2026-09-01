#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main as strategy


DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "cycle_replay"
DEFAULT_REPLAY_TIME = datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class FixtureStateStore:
    def __init__(self, raw_state: dict[str, Any]):
        self.raw_state = copy.deepcopy(raw_state)
        self.write_calls: list[dict[str, Any]] = []

    def load(self, *, normalize: bool = False):
        return copy.deepcopy(self.raw_state)

    def write(self, state: dict[str, Any]):
        self.write_calls.append(copy.deepcopy(state))
        self.raw_state = copy.deepcopy(state)


class FixtureNotifier:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []

    def send(self, *, token: str, chat_id: str, text: str, run_id: str, dry_run: bool):
        self.messages.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "run_id": run_id,
                "dry_run": dry_run,
            }
        )


class ReplayClient:
    def __init__(self, account_snapshot: dict[str, Any]):
        self.account_snapshot = copy.deepcopy(account_snapshot)
        self.side_effect_calls: list[dict[str, Any]] = []

    def ping(self):
        return None

    def get_asset_balance(self, *, asset: str):
        return copy.deepcopy(
            self.account_snapshot.get("spot_balances", {}).get(asset, {"free": "0", "locked": "0"})
        )

    def get_simple_earn_flexible_product_position(self, *, asset: str):
        return copy.deepcopy(
            self.account_snapshot.get("earn_positions", {}).get(asset, {"rows": []})
        )

    def get_simple_earn_flexible_product_list(self, *, asset: str):
        return copy.deepcopy(
            self.account_snapshot.get("earn_product_list", {}).get(asset, {"rows": []})
        )

    def get_avg_price(self, *, symbol: str):
        price = self.account_snapshot.get("avg_prices", {}).get(symbol)
        if price is None:
            raise KeyError(f"Missing avg price fixture for {symbol}")
        return {"mins": 5, "price": str(price)}

    def get_symbol_info(self, symbol: str):
        info = self.account_snapshot.get("symbol_info", {}).get(symbol)
        if info is None:
            raise KeyError(f"Missing symbol info fixture for {symbol}")
        return copy.deepcopy(info)

    def get_historical_klines(self, symbol: str, interval: str, lookback: str):
        raise RuntimeError(
            f"Historical klines were requested for {symbol}, but the replay runtime should supply fixed indicator snapshots."
        )

    def _record(self, method: str, payload: dict[str, Any]):
        self.side_effect_calls.append({"method": method, "payload": copy.deepcopy(payload)})
        return {"status": "captured", "method": method, "payload": copy.deepcopy(payload)}

    def order_market_buy(self, **kwargs):
        return self._record("order_market_buy", kwargs)

    def order_market_sell(self, **kwargs):
        return self._record("order_market_sell", kwargs)

    def redeem_simple_earn_flexible_product(self, **kwargs):
        return self._record("redeem_simple_earn_flexible_product", kwargs)

    def subscribe_simple_earn_flexible_product(self, **kwargs):
        return self._record("subscribe_simple_earn_flexible_product", kwargs)


def load_cycle_snapshots(fixtures_dir: Path = DEFAULT_FIXTURE_DIR) -> dict[str, Any]:
    base_dir = Path(fixtures_dir)
    return {
        "account_balances": load_json(base_dir / "account_balances_snapshot.json"),
        "initial_state": load_json(base_dir / "initial_state_snapshot.json"),
        "pool_input": load_json(base_dir / "pool_input_snapshot.json"),
        "market_data": load_json(base_dir / "market_data_snapshot.json"),
    }


def build_replay_runtime(
    *,
    fixtures_dir: Path = DEFAULT_FIXTURE_DIR,
    run_id: str = "fixture-cycle-run",
    dry_run: bool = True,
    now_utc: datetime = DEFAULT_REPLAY_TIME,
):
    snapshots = load_cycle_snapshots(fixtures_dir)
    client = ReplayClient(snapshots["account_balances"])
    state_store = FixtureStateStore(snapshots["initial_state"])
    notifier = FixtureNotifier()
    runtime = strategy.ExecutionRuntime(
        dry_run=dry_run,
        run_id=run_id,
        now_utc=now_utc,
        client=client,
        state_loader=state_store.load,
        state_writer=state_store.write,
        notifier=notifier.send,
        trend_pool_payload=snapshots["pool_input"],
        btc_market_snapshot=snapshots["market_data"]["btc_snapshot"],
        trend_indicator_snapshots=snapshots["market_data"]["trend_indicators"],
        research_cycle_settings=SimpleNamespace(
            btc_status_report_interval_hours=24,
            allow_new_trend_entries_on_degraded=False,
        ),
        print_traceback=False,
    )
    return runtime, client, state_store, notifier


def run_replay_cycle(
    *,
    fixtures_dir: Path = DEFAULT_FIXTURE_DIR,
    run_id: str = "fixture-cycle-run",
    dry_run: bool = True,
    now_utc: datetime = DEFAULT_REPLAY_TIME,
) -> dict[str, Any]:
    runtime, client, state_store, notifier = build_replay_runtime(
        fixtures_dir=fixtures_dir,
        run_id=run_id,
        dry_run=dry_run,
        now_utc=now_utc,
    )
    report = strategy.execute_cycle(runtime)
    return {
        "report": report,
        "client": client,
        "state_store": state_store,
        "notifier": notifier,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute one fixed-input strategy cycle from local fixtures without live external systems."
    )
    parser.add_argument("--fixtures-dir", default=str(DEFAULT_FIXTURE_DIR), help="Directory containing replay fixtures.")
    parser.add_argument("--run-id", default="replay-cycle", help="Run identifier to embed in logs and the report.")
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to write the structured execution report as JSON.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_replay_cycle(
        fixtures_dir=Path(args.fixtures_dir),
        run_id=args.run_id,
        dry_run=True,
        now_utc=DEFAULT_REPLAY_TIME,
    )
    report_json = json.dumps(result["report"], indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_json + "\n", encoding="utf-8")
    print(report_json)


if __name__ == "__main__":
    main()
