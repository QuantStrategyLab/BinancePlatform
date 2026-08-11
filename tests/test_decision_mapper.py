import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(QPK_SRC) not in sys.path:
    sys.path.insert(0, str(QPK_SRC))

from decision_mapper import (
    build_execution_authority,
    is_execution_authority_valid,
    map_strategy_decision_to_allocation,
    map_strategy_decision_to_rotation_plan,
)
from quant_platform_kit import PortfolioSnapshot, Position
from quant_platform_kit.risk.contracts import CandidateRiskIdentity
from quant_platform_kit.strategy_contracts import BudgetIntent, PositionTarget, StrategyDecision


def _candidate_identity(**overrides):
    values = {
        "strategy_profile": "crypto_live_pool_rotation",
        "account_mode": "single_strategy_account_v1",
        "strategy_revision": "1" * 40,
        "runner_revision": "2" * 40,
        "config_sha256": "3" * 64,
        "input_manifest_sha256": "4" * 64,
        "authority_receipt_sha256": "5" * 64,
    }
    values.update(overrides)
    return CandidateRiskIdentity(**values)


def _approved_authority(decision, *, expired=False):
    now = datetime.now(timezone.utc)
    candidate = _candidate_identity()
    expires_at = now - timedelta(minutes=1) if expired else now + timedelta(minutes=5)
    mandate = {
        "mandate_id": "binance_crypto_research_only_v1",
        "mandate_version": "v1",
        "authority_receipt_sha256": candidate.authority_receipt_sha256,
        "authority_scope": "RESEARCH_ONLY",
        "strategy_profile": candidate.strategy_profile,
        "account_mode": candidate.account_mode,
        "strategy_revision": candidate.strategy_revision,
        "runner_revision": candidate.runner_revision,
        "config_sha256": candidate.config_sha256,
        "input_manifest_sha256": candidate.input_manifest_sha256,
        "candidate_identity_sha256": candidate.candidate_sha256,
        "effective_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "max_snapshot_age_seconds": 300,
        "effective_exposure_cap": 1.0,
        "loss_budget": 10_000.0,
        "product_caps": 1.0,
        "nominal_caps": 1.0,
        "product_leverage_factors": {"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
        "allowed_nonzero_assets": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "source_revision": "6" * 40,
    }
    snapshot = PortfolioSnapshot(
        as_of=now,
        total_equity=1_000.0,
        buying_power=900.0,
        cash_balance=900.0,
        positions=(Position(symbol="SOLUSDT", quantity=1.0, market_value=100.0),),
        metadata={"observed_effective_exposure": 0.1},
    )
    return build_execution_authority(
        decision,
        portfolio_snapshot=snapshot,
        mandate_provenance=mandate,
        candidate_identity=candidate,
        market_data={},
    )


class DecisionMapperTests(unittest.TestCase):
    def test_map_strategy_decision_to_allocation_uses_budgets_and_diagnostics(self):
        decision = StrategyDecision(
            positions=(
                PositionTarget(symbol="BTCUSDT", target_weight=0.3),
                PositionTarget(symbol="ETHUSDT", target_weight=0.4),
            ),
            budgets=(
                BudgetIntent(name="btc_core_dca_pool", symbol="BTCUSDT", amount=250.0),
                BudgetIntent(name="trend_rotation_pool", amount=400.0),
            ),
            diagnostics={
                "btc_target_ratio": 0.3,
                "trend_target_ratio": 0.7,
                "btc_base_order_usdt": 50.0,
            },
        )

        allocation = map_strategy_decision_to_allocation(
            decision,
            account_metrics={
                "total_equity": 10000.0,
                "trend_value": 3500.0,
                "dca_value": 1800.0,
            },
            execution_authority=_approved_authority(decision),
        )

        self.assertEqual(allocation["total_equity"], 10000.0)
        self.assertEqual(allocation["trend_usdt_pool"], 400.0)
        self.assertEqual(allocation["dca_usdt_pool"], 250.0)
        self.assertEqual(allocation["btc_base_order_usdt"], 50.0)
        self.assertEqual(allocation["btc_target_ratio"], 0.3)
        self.assertEqual(allocation["trend_target_ratio"], 0.4)

    def test_map_strategy_decision_to_rotation_plan_uses_unified_diagnostics(self):
        decision = StrategyDecision(
            positions=(PositionTarget(symbol="ETHUSDT", target_weight=0.4),),
            budgets=(BudgetIntent(name="trend_rotation_pool", amount=400.0),),
            diagnostics={
                "trend_pool": ("ETHUSDT", "SOLUSDT"),
                "metadata": {
                    "combo": {
                        "base_btc_weight": 0.50,
                        "base_trend_weight": 0.50,
                        "btc_weight": 0.25,
                        "trend_weight": 0.0,
                        "dynamic_regime_mode": "dual_leg",
                        "regime_tier": "hard",
                    },
                    "regime_off": True,
                    "btc_sma200_ratio": 0.94,
                    "ma200_slope": -0.01,
                    "gross_exposure": 0.25,
                },
                "rotation_candidates": {
                    "ETHUSDT": {"weight": 0.6, "relative_score": 1.2, "abs_momentum": 0.3},
                },
                "eligible_buy_symbols": ("ETHUSDT",),
                "planned_trend_buys": {"ETHUSDT": 320.0},
                "sell_reasons": {"SOLUSDT": "trend_sell_reason_rotated_out"},
                "artifact_contract": {"version": "v1"},
            },
            risk_flags=("regime_off",),
        )

        plan = map_strategy_decision_to_rotation_plan(
            decision,
            execution_authority=_approved_authority(decision),
        )

        self.assertEqual(plan["active_trend_pool"], ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(plan["eligible_buy_symbols"], ["ETHUSDT"])
        self.assertEqual(plan["planned_trend_buys"], {"ETHUSDT": 400.0})
        self.assertEqual(plan["sell_reasons"], {"SOLUSDT": "trend_sell_reason_rotated_out"})
        self.assertEqual(plan["artifact_contract"], {"version": "v1"})
        self.assertEqual(plan["risk_flags"], ("regime_off",))
        self.assertEqual(
            plan["combo_diagnostics"],
            {
                "base_btc_weight": 0.50,
                "base_trend_weight": 0.50,
                "effective_btc_weight": 0.25,
                "effective_trend_weight": 0.0,
                "dynamic_regime_mode": "dual_leg",
                "regime_tier": "hard",
                "regime_off": True,
                "btc_sma200_ratio": 0.94,
                "ma200_slope": -0.01,
                "gross_exposure": 0.25,
            },
        )

    def test_rejected_diagnostics_do_not_create_an_executable_plan(self):
        decision = StrategyDecision(
            diagnostics={
                "member_risk_assessment": {"outcome": "REJECT"},
                "eligible_buy_symbols": ("ETHUSDT",),
                "planned_trend_buys": {"ETHUSDT": 320.0},
                "sell_reasons": {"SOLUSDT": "stale_diagnostic"},
            }
        )

        plan = map_strategy_decision_to_rotation_plan(decision)

        self.assertEqual(plan["eligible_buy_symbols"], [])
        self.assertEqual(plan["planned_trend_buys"], {})
        self.assertEqual(plan["sell_reasons"], {})

    def test_authority_rejects_tampered_scope_identity_digests_payload_and_freshness(self):
        decision = StrategyDecision(positions=(PositionTarget(symbol="BTCUSDT", target_weight=0.1),))
        authority = _approved_authority(decision)
        self.assertIsNotNone(authority)
        self.assertTrue(is_execution_authority_valid(authority, decision=decision))

        member = authority.member_assessment
        mutations = (
            replace(authority, member_assessment=replace(member, outcome="REJECT", reason_codes=("rejected",))),
            replace(authority, member_assessment=replace(member, scope="ACCOUNT")),
            replace(authority, member_assessment=replace(member, decision_digest_sha256="a" * 64)),
            replace(authority, member_assessment=replace(member, portfolio_snapshot_digest_sha256="b" * 64)),
            replace(authority, member_assessment=replace(member, evaluated_at="2020-01-01T00:00:00Z")),
            replace(authority, candidate_identity=_candidate_identity(config_sha256="7" * 64)),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertFalse(is_execution_authority_valid(mutation, decision=decision))

        tampered_payload = replace(member)
        object.__setattr__(tampered_payload, "assessment_sha256", "0" * 64)
        self.assertFalse(
            is_execution_authority_valid(
                replace(authority, member_assessment=tampered_payload),
                decision=decision,
            )
        )
        non_finite = replace(member)
        object.__setattr__(non_finite, "effective_exposure_cap", float("nan"))
        self.assertFalse(
            is_execution_authority_valid(
                replace(authority, member_assessment=non_finite),
                decision=decision,
            )
        )

    def test_missing_or_expired_mandate_cannot_build_authority(self):
        decision = StrategyDecision(positions=(PositionTarget(symbol="BTCUSDT", target_weight=0.1),))
        self.assertIsNone(_approved_authority(decision, expired=True))
        self.assertIsNone(
            build_execution_authority(
                decision,
                portfolio_snapshot=PortfolioSnapshot(
                    as_of=datetime.now(timezone.utc),
                    total_equity=1_000.0,
                    metadata={"observed_effective_exposure": 0.0},
                ),
                mandate_provenance=None,
                candidate_identity=None,
                market_data={},
            )
        )


if __name__ == "__main__":
    unittest.main()
