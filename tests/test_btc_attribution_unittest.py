import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from infra.btc_no_submit import approved_exit_proposal

from infra.btc_attribution import AttributionBlocked, read_btc_balance_with_parts, reconstruct

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def ledger():
    return {
        "contract_version": "btc-attribution/v1", "account_scope": "fixture", "account_selector": "account",
        "strategy_profile": "crypto_btc_dca", "source": "synthetic", "source_version": "v1",
        "opening_at": "2026-10-01T00:00:00Z", "observed_through": "2026-10-30T00:00:00Z",
        "available_at": "2026-10-30T00:00:00Z", "complete": True,
        "opening_spot_btc": "0.50", "opening_earn_btc": "0.10",
        "fills": [{"id": "fill1", "at": "2026-10-02T00:00:00Z", "side": "BUY",
                   "btc_qty": "0.20", "fee_asset": "BTC", "fee_qty": "0.01"},
                  {"id": "fill2", "at": "2026-10-03T00:00:00Z", "side": "SELL",
                   "btc_qty": "0.10", "fee_asset": "USDT", "fee_qty": "2"}],
        "transfers": [{"id": "transfer1", "at": "2026-10-04T00:00:00Z", "direction": "spot_to_earn",
                       "btc_qty": "0.05"}],
        "unsettled_orders": [], "observed_spot_free_btc": "0.54", "observed_earn_btc": "0.15",
    }


def calculate(value):
    return reconstruct(value, as_of=NOW, account_scope="fixture", account_selector="account")


class AttributionTests(unittest.TestCase):
    def test_reconstructs_fees_and_transfers(self):
        result = calculate(ledger())
        self.assertEqual(str(result.spot), "0.54")
        self.assertEqual(str(result.earn), "0.15")
        self.assertEqual(str(result.fees["USDT"]), "2")
        self.assertEqual(str(result.fees["BTC"]), "0.01")

    def test_existing_reads_capture_spot_earn_without_extra_calls(self):
        calls = []
        class Broker:
            def get_asset_balance(self, **kwargs):
                calls.append("spot")
                return {"free": "0", "locked": "0"}
            def get_simple_earn_flexible_product_position(self, **kwargs):
                calls.append("earn")
                return {"rows": [{"totalAmount": "0.69"}]}
        def existing_read(client, asset, *, log_buffer):
            client.get_asset_balance(asset=asset)
            client.get_simple_earn_flexible_product_position(asset=asset, current=1, size=100)
            return 0.69
        parts = {}
        total = read_btc_balance_with_parts(existing_read, Broker(), "BTC", log_buffer=[], output=parts)
        self.assertEqual(total, 0.69)
        self.assertEqual(calls, ["spot", "earn"])
        self.assertEqual(parts, {"spot_free": "0", "spot_locked": "0", "earn": "0.69"})

    def test_usdt_parts_use_existing_reads_and_preserve_locked_and_earn(self):
        calls = []
        class Broker:
            def get_asset_balance(self, **kwargs):
                calls.append(("spot", kwargs["asset"]))
                return {"free": "50", "locked": "25"}
            def get_simple_earn_flexible_product_position(self, **kwargs):
                calls.append(("earn", kwargs["asset"]))
                return {"rows": [{"totalAmount": "125"}]}
        def existing_read(client, asset, *, log_buffer):
            client.get_asset_balance(asset=asset)
            client.get_simple_earn_flexible_product_position(asset=asset, current=1, size=100)
            return 200.0
        parts = {}
        self.assertEqual(read_btc_balance_with_parts(existing_read, Broker(), "USDT", log_buffer=[], output=parts), 200)
        self.assertEqual(calls, [("spot", "USDT"), ("earn", "USDT")])
        self.assertEqual(parts, {"spot_free": "50", "spot_locked": "25", "earn": "125", "managed_total": 200.0})

    def test_missing_duplicate_unknown_and_conservation_block(self):
        for mutation in ({"complete": False}, {"unsettled_orders": [{"id": "unknown"}]},
                         {"opening_spot_btc": None}, {"observed_spot_free_btc": "0.55"},
                         {"account_scope": "other"}, {"available_at": "2026-10-31T00:00:00Z"},
                         {"observed_through": "2026-10-29T00:00:00Z"},
                         {"fills": [ledger()["fills"][0], ledger()["fills"][0]]}):
            value = {**ledger(), **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(AttributionBlocked):
                calculate(value)


class ExitProposalTests(unittest.TestCase):
    def decision(self):
        return SimpleNamespace(diagnostics={"planned_investment_usd": 0, "btc_target_ratio": .345,
            "btc_value_usd": 690, "target_btc_value_usd": 345,
            "zscore_exit": {"applied": True, "route": "risk_reduced"}},
            positions=(SimpleNamespace(symbol="BTCUSDT", target_weight=.345),),
            budgets=(SimpleNamespace(name="btc_dca_pool", symbol="BTCUSDT", amount=0),), risk_flags=())

    def test_exit_is_only_a_preview_and_must_match_account(self):
        params = dict(price=1000, account_btc="0.69", evidence=ledger(), as_of=NOW,
                      account_scope="fixture", account_selector="account",
                      broker_parts={"spot_free": "0.54", "spot_locked": "0", "earn": "0.15"})
        proposal = approved_exit_proposal(self.decision(), {"total_equity": 1000, "dca_value": 690}, **params)
        self.assertEqual(proposal["quantity"], "0.345")
        self.assertFalse(proposal["execution_ready"])
        self.assertTrue(proposal["proposal_only"])
        oversized_exit = self.decision()
        oversized_exit.diagnostics["target_btc_value_usd"] = 100
        oversized_exit.diagnostics["btc_target_ratio"] = .1
        oversized_exit.positions[0].target_weight = .1
        with self.assertRaises(ValueError):
            approved_exit_proposal(oversized_exit, {"total_equity": 1000, "dca_value": 690}, **params)
        for update in ({"evidence": None}, {"account_btc": "0.70"}, {"price": 999},
                       {"broker_parts": None},
                       {"broker_parts": {"spot_free": "0", "spot_locked": "0", "earn": "0.69"}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                approved_exit_proposal(self.decision(), {"total_equity": 1000, "dca_value": 690},
                                       **{**params, **update})

    def test_non_terminating_quantity_rounds_down(self):
        candidate = self.decision()
        candidate.diagnostics["target_btc_value_usd"] = 344
        candidate.diagnostics["btc_target_ratio"] = .344
        candidate.positions[0].target_weight = .344
        params = dict(price=1000, account_btc="0.69", evidence=ledger(), as_of=NOW,
                      account_scope="fixture", account_selector="account",
                      broker_parts={"spot_free": "0.54", "spot_locked": "0", "earn": "0.15"})
        # Non-divisible value using a different account price must not round upward.
        from decimal import Decimal
        params["price"] = 999
        candidate.diagnostics["btc_value_usd"] = 689.31
        candidate.diagnostics["target_btc_value_usd"] = 344
        candidate.diagnostics["btc_target_ratio"] = .344
        proposal = approved_exit_proposal(candidate, {"total_equity": 1000, "dca_value": 689.31}, **params)
        self.assertLessEqual(Decimal(proposal["quantity"]) * Decimal(999), Decimal("345.31"))
