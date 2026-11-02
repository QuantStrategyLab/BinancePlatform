"""Approved accounting policies, using invented offline evidence only."""

import builtins
import ast
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from application.cost_history_basis import replay_cost_basis
from application.cost_history_collector import HistoryReadError, ReadPage, collect_history


COINS = ["BTC", "USDT", "BNB", "ETH", "DROP"]


def material():
    history = {
        "schema_version": "cost_history.v1", "mode": "coverage-only",
        "account_open_ms": 0, "end_ms": 100, "request_count": 1,
        "universe": {"coins": COINS, "symbols": {"BTCUSDT": ["BTC", "USDT"]}},
        "symbols": {"BTCUSDT": {"covered_ranges": [[0, 100]], "gaps": [],
                                  "trades": [], "duplicate_count": 0}},
    }
    evidence = {
        "schema_version": "cost_basis_evidence.v1", "reporting_asset": "USDT",
        "account_open_ms": 0, "end_ms": 100,
        "universe_source": "synthetic-account-history", "events": [], "trades": {},
        "opening": {c: {"quantity": "0", "cost": "0", "source": "synthetic-opening"}
                    for c in COINS},
        "nontrade_sources": dict.fromkeys(COINS, "synthetic-complete-nontrade-export"),
    }
    evidence["opening"]["USDT"].update(quantity="1000", cost="1000")
    evidence["opening"]["BNB"].update(quantity="10", cost="20")
    return history, evidence


def price(value, time=10):
    return {"price": value, "time_ms": time, "source": "synthetic-historical-price"}


def fill(history, evidence, *, id=1, time=10, buy=True, qty="2", quote="20",
         fee="0", fee_coin="BTC", prices=None):
    history["symbols"]["BTCUSDT"]["trades"].append({
        "symbol": "BTCUSDT", "id": id, "time": time, "isBuyer": buy,
        "qty": qty, "quoteQty": quote, "price": "10",
        "commission": fee, "commissionAsset": fee_coin,
    })
    evidence["trades"][f"BTCUSDT:{id}"] = {"prices": prices or {}}


def event(evidence, kind, *, id="event-1", time=10, coin="ETH", qty="2", **extra):
    row = {"id": id, "kind": kind, "time_ms": time, "coin": coin,
           "quantity": qty, "source": "synthetic-full-record", **extra}
    evidence["events"].append(row)
    return row


def coin(history, evidence, name):
    return replay_cost_basis(history, evidence)["coins"][name]


def test_moving_average_after_buy_sell_and_rebuy():
    history, evidence = material()
    fill(history, evidence)
    fill(history, evidence, id=2, time=20, qty="2", quote="60")
    fill(history, evidence, id=3, time=30, buy=False, qty="1", quote="50")
    fill(history, evidence, id=4, time=40, qty="1", quote="40")
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["BTC"]["quantity"] == "4"
    assert result["coins"]["BTC"]["total_cost"] == "100"
    assert result["coins"]["BTC"]["average_cost"] == "25"
    assert result["disposals"][2]["removed_cost"] == "20"
    assert result["coins"]["USDT"]["total_cost"] == "930"


@pytest.mark.parametrize("buy", [True, False])
def test_third_coin_fee_capitalized_and_removed_at_own_average(buy):
    history, evidence = material()
    evidence["opening"]["BTC"].update(quantity="4", cost="40")
    fill(history, evidence, buy=buy, fee="1", fee_coin="BNB", prices={"BNB": price("5")})
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["BNB"]["quantity"] == "9"
    assert result["coins"]["BNB"]["total_cost"] == "18"
    assert result["disposals"][1]["removed_cost"] == "2"
    acquired = result["coins"]["BTC" if buy else "USDT"]
    assert acquired["total_cost"] == ("65" if buy else "1025")


@pytest.mark.parametrize("fee_coin,expected_qty,expected_cost", [
    ("BTC", "1.9", "20"), ("USDT", "2", "20.1"),
])
def test_received_and_spent_asset_fees(fee_coin, expected_qty, expected_cost):
    history, evidence = material()
    fill(history, evidence, fee="0.1", fee_coin=fee_coin)
    result = coin(history, evidence, "BTC")
    assert result["quantity"] == expected_qty
    assert result["total_cost"] == expected_cost


def test_unvalued_third_coin_fee_keeps_purchase_unknown_but_debits_fee_basis():
    history, evidence = material()
    fill(history, evidence, fee="1", fee_coin="BNB")
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["BTC"]["total_cost"] is None
    assert "fee_valuation_missing" in result["coins"]["BTC"]["reasons"]
    assert result["coins"]["BNB"]["total_cost"] == "18"


@pytest.mark.parametrize("kind", ["earn_reward", "staking_reward", "airdrop"])
def test_rewards_and_airdrops_at_receipt_fair_value(kind):
    history, evidence = material()
    event(evidence, kind, prices={"ETH": price("7")})
    result = coin(history, evidence, "ETH")
    assert result["status"] == "KNOWN"
    assert result["quantity"] == "2"
    assert result["total_cost"] == "14"
    assert result["average_cost"] == "7"
    acquisition = replay_cost_basis(history, evidence)["acquisitions"][0]
    assert acquisition["source"] == "synthetic-full-record"
    assert acquisition["valuations"] == [{"coin": "ETH", "price": "7", "time_ms": 10,
                                          "source": "synthetic-historical-price"}]


@pytest.mark.parametrize("valuation", [None, price("7", 11), price("0")])
def test_airdrop_without_reliable_receipt_price_is_unknown(valuation):
    history, evidence = material()
    event(evidence, "airdrop", coin="DROP", prices={"DROP": valuation})
    result = coin(history, evidence, "DROP")
    assert result["quantity"] == "2"
    assert result["total_cost"] is None
    assert "receipt_valuation_missing" in result["reasons"]


def transfer(evidence):
    return event(
        evidence, "transfer_in", coin="BTC", qty="9", transfer_id="tx-1",
        from_account="synthetic-wallet", to_account="synthetic-exchange", owner="synthetic-owner",
        source_leg={"transfer_id": "tx-1", "coin": "BTC", "quantity": "10", "fee": "1", "fee_coin": "BTC",
                    "source_cost": "100", "source": "synthetic-wallet-acquisition-record",
                    "from_account": "synthetic-wallet", "to_account": "synthetic-exchange",
                    "owner": "synthetic-owner", "time_ms": 5},
    )


@pytest.mark.parametrize("kind", ["deposit", "transfer_in"])
def test_external_transfer_carries_matched_source_cost_and_removes_fee_basis(kind):
    history, evidence = material()
    row = transfer(evidence)
    row["kind"] = kind
    result = coin(history, evidence, "BTC")
    assert result["quantity"] == "9"
    assert result["total_cost"] == "90"


@pytest.mark.parametrize("field,value", [
    ("owner", "other-owner"), ("quantity", "11"), ("transfer_id", "other-tx"),
    ("coin", "ETH"), ("from_account", "other-wallet"),
    ("to_account", "other-exchange"), ("source_cost", None), ("source", ""),
    ("time_ms", 20),
])
def test_unmatched_transfer_keeps_basis_unknown(field, value):
    history, evidence = material()
    transfer(evidence)["source_leg"][field] = value
    result = coin(history, evidence, "BTC")
    assert result["quantity"] == "9"
    assert result["total_cost"] is None
    assert "transfer_source_unmatched" in result["reasons"]


def test_internal_principal_movements_preserve_basis_without_reward_duplication():
    history, evidence = material()
    evidence["opening"]["ETH"].update(quantity="10", cost="30")
    event(evidence, "internal_transfer", qty="10", transfer_id="earn-subscribe",
          from_account="spot", to_account="earn", owner="synthetic-owner", fee_quantity="0",
          source_leg={"transfer_id": "earn-subscribe", "coin": "ETH", "quantity": "10",
                      "from_account": "spot", "to_account": "earn",
                      "owner": "synthetic-owner",
                      "fee_quantity": "0",
                      "source": "synthetic-principal-record"})
    event(evidence, "earn_reward", id="reward", time=20, qty="1",
          prices={"ETH": price("5", 20)})
    result = coin(history, evidence, "ETH")
    assert result["quantity"] == "11"
    assert result["total_cost"] == "35"


def test_cross_crypto_spot_uses_quote_valuation_and_fee_at_execution():
    history, evidence = material()
    history["universe"]["symbols"]["ETHBTC"] = ["ETH", "BTC"]
    history["symbols"]["ETHBTC"] = {"covered_ranges": [[0, 100]], "gaps": [],
                                    "duplicate_count": 0, "trades": [{
                                        "symbol": "ETHBTC", "id": 1, "time": 10,
                                        "qty": "2", "quoteQty": "1", "price": "0.5",
                                        "isBuyer": True, "commission": "1", "commissionAsset": "BNB"}]}
    evidence["opening"]["BTC"].update(quantity="2", cost="20")
    evidence["trades"]["ETHBTC:1"] = {"prices": {
        "BTC": price("20"), "BNB": price("5"), "ETH": price("12"),
    }}
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["BTC"]["total_cost"] == "10"
    assert result["coins"]["ETH"]["total_cost"] == "25"
    assert result["coins"]["BNB"]["total_cost"] == "18"
    evidence["trades"]["ETHBTC:1"]["prices"].pop("BTC")
    assert coin(history, evidence, "ETH")["total_cost"] is None


@pytest.mark.parametrize("kind", ["convert", "dust"])
def test_convert_and_dust_are_linked_disposal_and_acquisition(kind):
    history, evidence = material()
    evidence["opening"]["ETH"].update(quantity="10", cost="30")
    event(evidence, kind, sold_coin="ETH", sold_quantity="2", bought_coin="BTC",
          bought_quantity="0.5", fee_coin="BNB", fee_quantity="1", execution_id="conversion-1",
          prices={"ETH": price("10"), "BNB": price("5")})
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["ETH"]["quantity"] == "8"
    assert result["coins"]["ETH"]["total_cost"] == "24"
    assert result["coins"]["BTC"]["total_cost"] == "25"
    assert result["disposals"][0]["removed_cost"] == "6"
    assert result["disposals"][0]["event_id"] == "event-1"


@pytest.mark.parametrize("missing", ["execution_id", "fee_quantity", "source", "prices"])
def test_incomplete_conversion_does_not_invent_a_spot_fill(missing):
    history, evidence = material()
    row = event(evidence, "convert", sold_coin="BNB", sold_quantity="2", bought_coin="BTC",
                bought_quantity="1", fee_coin="BNB", fee_quantity="0", execution_id="c-1",
                prices={"BNB": price("10")})
    row.pop(missing)
    original = copy.deepcopy(history)
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["BTC"]["total_cost"] is None
    assert history == original


def test_crypto_quote_uses_execution_value_instead_of_disposed_historical_cost():
    history, evidence = material()
    event(evidence, "convert", sold_coin="BNB", sold_quantity="2", bought_coin="BTC",
          bought_quantity="1", fee_coin="BNB", fee_quantity="0", execution_id="c-1",
          prices={"BNB": price("10")})
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["BTC"]["total_cost"] == "20"
    assert result["disposals"][0]["removed_cost"] == "4"


def test_unknown_acquisition_taints_mixed_pool_then_clears_only_at_proven_zero():
    history, evidence = material()
    event(evidence, "airdrop", coin="BTC", qty="1")
    fill(history, evidence, time=20, qty="1", quote="10")
    assert coin(history, evidence, "BTC")["total_cost"] is None
    fill(history, evidence, id=2, time=30, buy=False, qty="2", quote="20")
    fill(history, evidence, id=3, time=40, qty="1", quote="30")
    assert coin(history, evidence, "BTC")["total_cost"] == "30"


@pytest.mark.parametrize("gap", ["opening", "nontrade", "universe", "query"])
def test_missing_completeness_proof_never_becomes_known_at_zero(gap):
    history, evidence = material()
    if gap == "opening":
        evidence["opening"].pop("BTC")
    elif gap == "nontrade":
        evidence["nontrade_sources"].pop("BTC")
    elif gap == "universe":
        evidence.pop("universe_source")
    else:
        history["symbols"]["BTCUSDT"]["covered_ranges"] = [[10, 100]]
    fill(history, evidence)
    fill(history, evidence, id=2, time=20, buy=False)
    assert coin(history, evidence, "BTC")["status"] == "UNKNOWN"
    assert coin(history, evidence, "BTC")["quantity"] is None


def test_failed_pair_query_also_invalidates_possible_third_coin_fee_inventory():
    history, evidence = material()
    history["symbols"]["BTCUSDT"]["covered_ranges"] = []
    history["symbols"]["BTCUSDT"]["gaps"] = [
        {"start_ms": 0, "end_ms": 100, "reason": "cost_history_get_failed"},
    ]
    result = coin(history, evidence, "BNB")
    assert result["status"] == "UNKNOWN"
    assert result["total_cost"] is None
    assert result["quantity"] is None


def test_globally_stopped_collector_cannot_establish_known_costs():
    history, evidence = material()
    universe = copy.deepcopy(history["universe"])
    universe["symbols"]["ETHBTC"] = ["ETH", "BTC"]
    class BannedReader:
        calls = 0

        def request(self, *args):
            self.calls += 1
            assert self.calls == 1
            return ReadPage(418)

    history = collect_history(BannedReader(), universe=universe, account_open_ms=0,
                              end_ms=100, sleep=lambda _: None)
    result = replay_cost_basis(history, evidence)
    assert result["cost_writes_allowed"] is False
    assert result["cost_proof_status"] == "UNKNOWN"
    assert all(c["status"] == "UNKNOWN" and c["total_cost"] is None
               for c in result["coins"].values())


@pytest.mark.parametrize("missing", ["quantity", "fee_quantity"])
def test_incomplete_internal_principal_leg_is_unknown_instead_of_rejected(missing):
    history, evidence = material()
    row = event(evidence, "internal_transfer", coin="BNB", qty="2", transfer_id="principal-1",
                from_account="spot", to_account="earn", owner="synthetic-owner", fee_quantity="0",
                source_leg={"transfer_id": "principal-1", "coin": "BNB", "quantity": "2",
                            "from_account": "spot", "to_account": "earn", "owner": "synthetic-owner",
                            "fee_quantity": "0", "source": "synthetic-principal"})
    row["source_leg"].pop(missing)
    result = coin(history, evidence, "BNB")
    assert result["status"] == "UNKNOWN"
    assert "internal_principal_unmatched" in result["reasons"]


def test_same_timestamp_requires_evidenced_order_across_all_sources():
    history, evidence = material()
    fill(history, evidence)
    event(evidence, "airdrop", coin="BTC", qty="1", prices={"BTC": price("30")})
    assert coin(history, evidence, "BTC")["total_cost"] is None
    evidence["trades"]["BTCUSDT:1"]["sequence"] = 1
    evidence["events"][0]["sequence"] = 2
    assert coin(history, evidence, "BTC")["total_cost"] == "50"


def test_duplicate_event_is_deduplicated_and_conflict_rejected():
    history, evidence = material()
    row = event(evidence, "airdrop", prices={"ETH": price("5")})
    evidence["events"].append(copy.deepcopy(row))
    assert coin(history, evidence, "ETH")["quantity"] == "2"
    evidence["events"][-1]["quantity"] = "3"
    with pytest.raises(HistoryReadError, match="duplicate_conflict"):
        replay_cost_basis(history, evidence)


def test_same_transfer_cannot_be_credited_twice_under_different_event_ids():
    history, evidence = material()
    row = transfer(evidence)
    evidence["events"].append({**row, "id": "other-id"})
    with pytest.raises(HistoryReadError, match="transfer_reused"):
        replay_cost_basis(history, evidence)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "other"), ("account_open_ms", 1), ("end_ms", 99),
    ("reporting_asset", "other"),
])
def test_evidence_must_match_scope(field, value):
    history, evidence = material()
    evidence[field] = value
    with pytest.raises(HistoryReadError, match="scope_invalid"):
        replay_cost_basis(history, evidence)


@pytest.mark.parametrize("kind", ["convert", "dust"])
@pytest.mark.parametrize("sequenced", [True, False])
def test_conversion_execution_is_counted_once_across_distinct_event_ids(kind, sequenced):
    history, evidence = material()
    row = event(evidence, kind, sold_coin="BNB", sold_quantity="2", bought_coin="BTC",
                bought_quantity="1", fee_coin="BNB", fee_quantity="1", execution_id="synthetic-conversion",
                prices={"BNB": price("10")}, sequence=1)
    if not sequenced:
        row.pop("sequence")
    single = replay_cost_basis(history, evidence)
    evidence["events"].append({**copy.deepcopy(row), "id": "second-import"})
    original = copy.deepcopy(evidence)
    result = replay_cost_basis(history, evidence)
    assert result == single
    assert evidence == original
    assert result["cost_writes_allowed"] is False
    assert result["cost_proof_status"] == "KNOWN"
    assert result["coins"]["BNB"]["quantity"] == "7"
    assert result["coins"]["BTC"]["quantity"] == "1"
    assert result["coins"]["BTC"]["total_cost"] == "30"
    assert len(result["acquisitions"]) == 1
    assert len(result["disposals"]) == 2


def test_distinct_conversion_execution_ids_are_both_replayed():
    history, evidence = material()
    row = event(evidence, "convert", sold_coin="BNB", sold_quantity="2", bought_coin="BTC",
                bought_quantity="1", fee_coin="BNB", fee_quantity="0", execution_id="synthetic-first",
                prices={"BNB": price("10")}, sequence=1)
    evidence["events"].append({**row, "id": "second-event", "execution_id": "synthetic-second",
                               "sequence": 2})
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["BTC"]["quantity"] == "2"
    assert result["coins"]["BTC"]["total_cost"] == "40"
    assert len(result["acquisitions"]) == 2


@pytest.mark.parametrize("field,value", [
    ("sold_quantity", "3"), ("bought_quantity", "2"), ("time_ms", 20),
    ("fee_quantity", "1"), ("bought_coin", "ETH"),
])
def test_conflicting_conversion_execution_cannot_produce_known_basis(field, value):
    history, evidence = material()
    row = event(evidence, "convert", sold_coin="BNB", sold_quantity="2", bought_coin="BTC",
                bought_quantity="1", fee_coin="BNB", fee_quantity="0", execution_id="synthetic-conversion",
                prices={"BNB": price("10")}, sequence=1)
    evidence["events"].append({**copy.deepcopy(row), "id": "second-import", field: value})
    with pytest.raises(HistoryReadError, match="cost_history_duplicate_conflict"):
        replay_cost_basis(history, evidence)


def test_external_evidence_cannot_invent_spot_fills():
    history, evidence = material()
    event(evidence, "spot")
    with pytest.raises(HistoryReadError, match="kind_unsupported"):
        replay_cost_basis(history, evidence)


def test_shortfall_cannot_create_known_cost_after_apparent_liquidation():
    history, evidence = material()
    fill(history, evidence, buy=False)
    fill(history, evidence, id=2, time=20)
    result = coin(history, evidence, "BTC")
    assert result["status"] == "UNKNOWN"
    assert result["quantity"] is None
    assert "inventory_shortfall" in result["reasons"]


def test_third_coin_fee_requires_fee_coin_cost_even_with_price():
    history, evidence = material()
    evidence["opening"]["BNB"]["cost"] = None
    fill(history, evidence, fee="1", fee_coin="BNB", prices={"BNB": price("5")})
    assert "fee_coin_basis_unknown" in coin(history, evidence, "BTC")["reasons"]


def test_withdrawal_removes_quantity_and_fee_at_moving_average():
    history, evidence = material()
    event(evidence, "withdrawal", coin="BNB", qty="2", fee_coin="BNB", fee_quantity="1")
    result = replay_cost_basis(history, evidence)
    assert result["coins"]["BNB"]["quantity"] == "7"
    assert result["coins"]["BNB"]["total_cost"] == "14"
    assert [r["removed_cost"] for r in result["disposals"]] == ["4", "2"]


@pytest.mark.parametrize("mode", ["apply", "write", "rebuild", None, True])
def test_write_guard_runs_before_reading_history_or_evidence(mode):
    with pytest.raises(HistoryReadError, match="write_forbidden"):
        replay_cost_basis(None, None, mode=mode)


def test_replay_has_no_io_and_preserves_input(monkeypatch):
    history, evidence = material()
    fill(history, evidence)
    before = copy.deepcopy((history, evidence))

    def forbidden(*args, **kwargs):
        pytest.fail("Cost replay attempted I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    result = replay_cost_basis(history, evidence)
    assert (history, evidence) == before
    assert result["cost_writes_allowed"] is False
    for key in ("store", "ledger", "settings"):
        with pytest.raises(TypeError):
            replay_cost_basis(history, evidence, **{key: object()})


def test_basis_replay_cannot_import_runtime_or_storage():
    path = Path(__file__).resolve().parents[1] / "application/cost_history_basis.py"
    allowed = {"__future__", "collections", "decimal", "application.cost_history_collector",
               "application.cost_history_coverage"}
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.module in allowed
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)


def test_cli_cost_preview_preserves_store_ledger_and_settings(tmp_path):
    history, evidence = material()
    fill(history, evidence)
    for name, data in (("history", history), ("evidence", evidence),
                       ("store", {}), ("ledger", {}), ("settings", {})):
        (tmp_path / f"{name}.json").write_text(json.dumps(data))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    script = Path(__file__).resolve().parents[1] / "scripts/replay_cost_history_coverage.py"
    args = [sys.executable, str(script), "--input", str(tmp_path / "history.json"),
            "--basis-evidence", str(tmp_path / "evidence.json")]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["coins"]["BTC"]["total_cost"] == "20"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    rejected = subprocess.run([*args, "--mode", "apply"], capture_output=True, text=True)
    assert rejected.returncode == 2
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
