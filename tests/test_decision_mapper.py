import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(QPK_SRC) not in sys.path:
    sys.path.insert(0, str(QPK_SRC))

from decision_mapper import map_strategy_decision_to_allocation, map_strategy_decision_to_rotation_plan
from quant_platform_kit.risk.contracts import CandidateRiskIdentity
from quant_platform_kit.risk.gate import _canonical_digest, _decision_metrics
from quant_platform_kit.strategy_contracts import BudgetIntent, PositionTarget, StrategyDecision


_CANDIDATE_IDENTITY = CandidateRiskIdentity(
    strategy_profile="crypto_live_pool_rotation",
    account_mode="single_strategy_account_v1",
    strategy_revision="1" * 40,
    runner_revision="2" * 40,
    config_sha256="3" * 64,
    input_manifest_sha256="4" * 64,
    authority_receipt_sha256="5" * 64,
)


def _utc_text(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _risk_assessment(
    scope,
    *,
    outcome="APPROVE",
    candidate_sha=None,
    decision_sha,
    evaluated_at=None,
):
    return {
        "contract_version": "qsl.risk_gate_assessment.v1",
        "scope": scope,
        "evaluated_at": evaluated_at or _utc_text(datetime.now(timezone.utc)),
        "policy_id": "qpk.risk_gate",
        "policy_version": "v1",
        "mandate_authority_receipt_sha256": "7" * 64,
        "candidate_identity_sha256": candidate_sha or _CANDIDATE_IDENTITY.candidate_sha256,
        "decision_digest_sha256": decision_sha,
        "portfolio_snapshot_digest_sha256": "8" * 64,
        "outcome": outcome,
        "reason_codes": () if outcome == "APPROVE" else ("rejected",),
        "assessment_sha256": "9" * 64,
    }


def _decision_digest(decision):
    payload, _, _ = _decision_metrics(decision, total_equity=None)
    return _canonical_digest(payload)


def _with_authority(
    decision,
    *,
    risk_gate="APPROVE",
    member="APPROVE",
    account="APPROVE",
    member_candidate_sha=None,
    account_candidate_sha=None,
    member_decision_sha=None,
    account_decision_sha=None,
    evaluated_at=None,
):
    digest = _decision_digest(decision)
    return StrategyDecision(
        positions=decision.positions,
        budgets=decision.budgets,
        risk_flags=decision.risk_flags,
        diagnostics={
            **dict(decision.diagnostics),
            "risk_gate": risk_gate,
            "candidate_risk_identity": _CANDIDATE_IDENTITY,
            "member_risk_assessment": _risk_assessment(
                "MEMBER",
                outcome=member,
                candidate_sha=member_candidate_sha,
                decision_sha=member_decision_sha or digest,
                evaluated_at=evaluated_at,
            ),
            "account_risk_assessment": _risk_assessment(
                "ACCOUNT",
                outcome=account,
                candidate_sha=account_candidate_sha,
                decision_sha=account_decision_sha or digest,
                evaluated_at=evaluated_at,
            ),
        },
    )


class DecisionMapperTests(unittest.TestCase):
    def test_map_strategy_decision_to_allocation_uses_budgets_and_diagnostics(self):
        decision = _with_authority(StrategyDecision(
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
        ))

        allocation = map_strategy_decision_to_allocation(
            decision,
            account_metrics={
                "total_equity": 10000.0,
                "trend_value": 3500.0,
                "dca_value": 1800.0,
            },
        )

        self.assertEqual(allocation["total_equity"], 10000.0)
        self.assertEqual(allocation["trend_usdt_pool"], 400.0)
        self.assertEqual(allocation["dca_usdt_pool"], 250.0)
        self.assertEqual(allocation["btc_base_order_usdt"], 50.0)
        self.assertEqual(allocation["btc_target_ratio"], 0.3)
        self.assertEqual(allocation["trend_target_ratio"], 0.7)

    def test_map_strategy_decision_to_rotation_plan_uses_unified_diagnostics(self):
        decision = _with_authority(StrategyDecision(
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
        ))

        plan = map_strategy_decision_to_rotation_plan(decision)

        self.assertEqual(plan["active_trend_pool"], ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(plan["eligible_buy_symbols"], ["ETHUSDT"])
        self.assertEqual(plan["planned_trend_buys"], {"ETHUSDT": 320.0})
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

    def test_execution_intents_fail_closed_without_matching_scoped_approvals(self):
        approved = _with_authority(
            StrategyDecision(
                positions=(PositionTarget(symbol="ETHUSDT", target_weight=0.4),),
                budgets=(BudgetIntent(name="trend_rotation_pool", amount=400.0),),
                diagnostics={
                    "btc_target_ratio": 0.3,
                    "trend_target_ratio": 0.7,
                    "btc_base_order_usdt": 50.0,
                    "trend_pool": ("ETHUSDT", "SOLUSDT"),
                    "rotation_candidates": {
                        "ETHUSDT": {"weight": 0.6, "relative_score": 1.2, "abs_momentum": 0.3},
                    },
                    "eligible_buy_symbols": ("ETHUSDT",),
                    "planned_trend_buys": {"ETHUSDT": 320.0},
                    "sell_reasons": {"SOLUSDT": "stale_rotated_out"},
                },
            )
        )
        base_diagnostics = dict(approved.diagnostics)
        decision_sha = base_diagnostics["member_risk_assessment"]["decision_digest_sha256"]
        cases = {
            "risk_engine_reject_with_stale_diagnostics": {
                **base_diagnostics,
                "risk_gate": "REJECT",
            },
            "risk_engine_missing": {
                key: value for key, value in base_diagnostics.items() if key != "risk_gate"
            },
            "member_reject": {
                **base_diagnostics,
                "member_risk_assessment": _risk_assessment(
                    "MEMBER", outcome="REJECT", decision_sha=decision_sha
                ),
            },
            "member_missing": {
                key: value for key, value in base_diagnostics.items() if key != "member_risk_assessment"
            },
            "account_reject": {
                **base_diagnostics,
                "account_risk_assessment": _risk_assessment(
                    "ACCOUNT", outcome="REJECT", decision_sha=decision_sha
                ),
            },
            "account_missing": {
                key: value for key, value in base_diagnostics.items() if key != "account_risk_assessment"
            },
            "candidate_identity_mismatch": {
                **base_diagnostics,
                "account_risk_assessment": _risk_assessment(
                    "ACCOUNT", candidate_sha="f" * 64, decision_sha=decision_sha
                ),
            },
            "decision_identity_mismatch": {
                **base_diagnostics,
                "account_risk_assessment": _risk_assessment("ACCOUNT", decision_sha="f" * 64),
            },
            "matching_foreign_candidate_identity": {
                **base_diagnostics,
                "member_risk_assessment": _risk_assessment(
                    "MEMBER", candidate_sha="f" * 64, decision_sha=decision_sha
                ),
                "account_risk_assessment": _risk_assessment(
                    "ACCOUNT", candidate_sha="f" * 64, decision_sha=decision_sha
                ),
            },
            "matching_foreign_decision_identity": {
                **base_diagnostics,
                "member_risk_assessment": _risk_assessment("MEMBER", decision_sha="f" * 64),
                "account_risk_assessment": _risk_assessment("ACCOUNT", decision_sha="f" * 64),
            },
        }

        for name, diagnostics in cases.items():
            with self.subTest(name=name):
                decision = StrategyDecision(
                    positions=(PositionTarget(symbol="ETHUSDT", target_weight=0.4),),
                    budgets=(BudgetIntent(name="trend_rotation_pool", amount=400.0),),
                    diagnostics=diagnostics,
                )
                allocation = map_strategy_decision_to_allocation(
                    decision,
                    account_metrics={
                        "total_equity": 10000.0,
                        "trend_value": 3500.0,
                        "dca_value": 1800.0,
                    },
                )
                plan = map_strategy_decision_to_rotation_plan(decision)

                self.assertEqual(allocation["btc_target_ratio"], 0.0)
                self.assertEqual(allocation["trend_target_ratio"], 0.0)
                self.assertEqual(allocation["trend_usdt_pool"], 0.0)
                self.assertEqual(allocation["dca_usdt_pool"], 0.0)
                self.assertEqual(allocation["btc_base_order_usdt"], 0.0)
                self.assertEqual(plan["selected_candidates"], {})
                self.assertEqual(plan["eligible_buy_symbols"], [])
                self.assertEqual(plan["planned_trend_buys"], {})
                self.assertEqual(plan["sell_reasons"], {})

    def test_execution_intents_fail_closed_for_invalid_stale_or_future_assessments(self):
        now = datetime.now(timezone.utc)
        evaluated_at_cases = {
            "invalid": "not-a-timestamp",
            "timezone_missing": now.replace(tzinfo=None).isoformat(),
            "stale": _utc_text(now - timedelta(seconds=301)),
            "future": _utc_text(now + timedelta(seconds=60)),
        }

        for name, evaluated_at in evaluated_at_cases.items():
            with self.subTest(name=name):
                decision = _with_authority(
                    StrategyDecision(
                        positions=(PositionTarget(symbol="ETHUSDT", target_weight=0.4),),
                        budgets=(BudgetIntent(name="trend_rotation_pool", amount=400.0),),
                        diagnostics={
                            "trend_target_ratio": 0.4,
                            "rotation_candidates": {
                                "ETHUSDT": {
                                    "weight": 1.0,
                                    "relative_score": 1.2,
                                    "abs_momentum": 0.3,
                                },
                            },
                            "eligible_buy_symbols": ("ETHUSDT",),
                            "planned_trend_buys": {"ETHUSDT": 320.0},
                        },
                    ),
                    evaluated_at=evaluated_at,
                )

                allocation = map_strategy_decision_to_allocation(
                    decision,
                    account_metrics={
                        "total_equity": 10000.0,
                        "trend_value": 3500.0,
                        "dca_value": 1800.0,
                    },
                )
                plan = map_strategy_decision_to_rotation_plan(decision)

                self.assertEqual(allocation["trend_target_ratio"], 0.0)
                self.assertEqual(allocation["trend_usdt_pool"], 0.0)
                self.assertEqual(plan["selected_candidates"], {})
                self.assertEqual(plan["eligible_buy_symbols"], [])
                self.assertEqual(plan["planned_trend_buys"], {})


if __name__ == "__main__":
    unittest.main()
