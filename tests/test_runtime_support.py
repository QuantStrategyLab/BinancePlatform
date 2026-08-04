import os
import sys
import unittest
from unittest.mock import Mock
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
if str(QPK_SRC) not in sys.path:
    sys.path.insert(0, str(QPK_SRC))

from runtime_support import (
    ExecutionRuntime,
    build_runtime_evidence_aggregate,
    build_execution_report,
    finalize_notification_delivery,
    record_gating_event,
    runtime_notify,
    validate_runtime_evidence_aggregate,
    runtime_call_client,
)
from quant_platform_kit.common.runtime_target import build_runtime_target


class TestBuildExecutionReport(unittest.TestCase):
    @staticmethod
    def risk_assessment(scope, assessment_sha256):
        return {
            "contract_version": "qsl.risk_gate_assessment.v1",
            "scope": scope,
            "evaluated_at": "2026-08-04T00:00:00Z",
            "policy_id": "qpk.risk_gate",
            "policy_version": "v1",
            "qpk_source_revision": "a" * 40,
            "mandate_id": "binance_crypto_research_only_v1",
            "mandate_version": "2026-08-04.1",
            "mandate_authority_receipt_sha256": "b" * 64,
            "mandate_scope": "RESEARCH_ONLY",
            "decision_digest_sha256": "c" * 64,
            "portfolio_snapshot_digest_sha256": "d" * 64,
            "effective_exposure_cap": 0.0,
            "observed_effective_exposure": 0.0,
            "proposed_effective_exposure": 0.0,
            "outcome": "REJECT",
            "reason_codes": ("budget_authority_exceeded",),
            "assessment_sha256": assessment_sha256,
        }

    @staticmethod
    def v2_release_identity():
        return {
            "strategy_profile": "crypto_live_pool_rotation",
            "mode": "core_major",
            "source_revision": "e" * 40,
            "input_timestamp": "2026-08-04T00:00:00Z",
            "artifact_contract": "qsl.crypto_live_pool.artifact_manifest.v1",
            "artifact_version": "2026-08-04-core_major",
            "artifacts": {
                name: {"sha256": character * 64}
                for name, character in zip(
                    ("live_pool", "live_pool_legacy", "latest_ranking", "latest_universe"),
                    "1234",
                )
            },
        }

    def test_v2_aggregate_builds_deterministic_redacted_missing_receipt(self):
        from runtime_support import build_runtime_evidence_aggregate_v2, validate_runtime_evidence_aggregate_v2

        cap = {
            "outcome": "REJECT",
            "decision_digest_sha256": "c" * 64,
            "release_identity_sha256": "5" * 64,
            "account_snapshot_sha256": "d" * 64,
            "account_assessment_sha256": "2" * 64,
            "mandate_authority_receipt_sha256": "b" * 64,
            "order_authorization_sha256": "6" * 64,
        }
        kwargs = {
            "produced_at": "2026-08-04T00:00:00Z",
            "run_id": "synthetic",
            "producer_revision": "f" * 40,
            "release_identity": self.v2_release_identity(),
            "member_risk_assessment": self.risk_assessment("MEMBER", "1" * 64),
            "account_risk_assessment": self.risk_assessment("ACCOUNT", "2" * 64),
            "cap_assessment": cap,
            "strategy_stop_evaluation": {"evaluated": True, "outcome": "CLEAR"},
            "account_breaker_evaluation": {"evaluated": True, "outcome": "CLEAR"},
            "execution_gate_outcome": "REJECT",
            "reconciliation": {"status": "MISSING"},
        }

        first = build_runtime_evidence_aggregate_v2(**kwargs)
        second = build_runtime_evidence_aggregate_v2(**kwargs)

        self.assertEqual(first, second)
        self.assertTrue(validate_runtime_evidence_aggregate_v2(first)["ok"])
        self.assertEqual(first["reconciliation"], {"status": "MISSING"})
        self.assertNotIn("positions", str(first))

    def test_runtime_call_client_blocks_missing_current_account_binding(self):
        client = Mock()
        runtime = ExecutionRuntime(client=client, dry_run=False)
        report = build_execution_report(runtime)

        with self.assertRaises(RuntimeError):
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quoteOrderQty": 1.0},
                effect_type="order_buy",
            )

        client.order_market_buy.assert_not_called()

    def test_v2_aggregate_is_redacted_missing_only_and_rejects_matched(self):
        from runtime_support import build_runtime_evidence_aggregate_v2

        with self.assertRaises(ValueError):
            build_runtime_evidence_aggregate_v2(
                produced_at="2026-08-04T00:00:00Z",
                run_id="synthetic",
                producer_revision="a" * 40,
                release_identity=self.v2_release_identity(),
                member_risk_assessment={"scope": "MEMBER", "outcome": "REJECT", "assessment_sha256": "1" * 64},
                account_risk_assessment={"scope": "ACCOUNT", "outcome": "REJECT", "assessment_sha256": "2" * 64},
                cap_assessment={"outcome": "REJECT", "positions": []},
                strategy_stop_evaluation={"evaluated": True, "outcome": "CLEAR"},
                account_breaker_evaluation={"evaluated": True, "outcome": "CLEAR"},
                execution_gate_outcome="REJECT",
                reconciliation={"status": "MATCHED"},
            )
    @staticmethod
    def runtime_evidence_inputs():
        return {
            "release_identity": {
                "strategy_profile": "crypto_live_pool_rotation",
                "mode": "core_major",
                "source_revision": "a" * 40,
                "input_timestamp": "2026-03-13T00:00:00Z",
                "artifact_contract": "crypto_live_pool_rotation.live_pool.v1",
                "artifact_version": "2026-03-13-core_major",
                "artifacts": {"live_pool": {"sha256": "b" * 64}},
            },
            "risk_engine": {"outcome": "APPROVE", "policy_version": "bootstrap_small_account_v2"},
            "effective_exposure_cap": {
                "value": 0.5,
                "mandate_version": "bootstrap_small_account_v2",
                "source": "approved_risk_mandate",
            },
            "stop_breaker_evaluation": {
                "stop_evaluated": True,
                "breaker_evaluated": True,
                "outcome": "CLEAR",
                "policy_version": "bootstrap_small_account_v2",
            },
            "reconciliation": {"status": "MISSING"},
        }

    def test_runtime_evidence_aggregate_is_redacted_and_static_only(self):
        aggregate = build_runtime_evidence_aggregate(**self.runtime_evidence_inputs())

        self.assertTrue(validate_runtime_evidence_aggregate(aggregate)["ok"])
        self.assertFalse(aggregate["verified_active"])
        self.assertFalse(aggregate["fills_verified"])
        self.assertFalse(aggregate["capital_use_verified"])
        self.assertNotIn("orders", str(aggregate))

    def test_runtime_evidence_aggregate_fails_closed_for_risk_reconciliation_and_sensitive_fields(self):
        aggregate = build_runtime_evidence_aggregate(**self.runtime_evidence_inputs())
        aggregate["risk_engine"]["outcome"] = "REJECT"
        aggregate["reconciliation"] = {"status": "MATCHED"}
        aggregate["positions"] = [{"symbol": "BTCUSDT"}]
        aggregate["release_identity"]["headers"] = {"authorization": "redacted"}

        validation = validate_runtime_evidence_aggregate(aggregate)

        self.assertFalse(validation["ok"])
        self.assertIn("runtime_evidence_aggregate risk_engine.outcome must be APPROVE", validation["errors"])
        self.assertIn(
            "runtime_evidence_aggregate reconciliation.MATCHED requires durable_receipt_sha256",
            validation["errors"],
        )
        self.assertIn("runtime_evidence_aggregate contains forbidden field: positions", validation["errors"])
        self.assertIn("runtime_evidence_aggregate contains forbidden field: headers", validation["errors"])

    def test_runtime_evidence_aggregate_rejects_static_matched_reconciliation(self):
        matched_inputs = self.runtime_evidence_inputs()
        matched_inputs["reconciliation"] = {
            "status": "MATCHED",
            "durable_receipt_sha256": "c" * 64,
            "identity_sha256": "d" * 64,
        }
        mismatched_inputs = self.runtime_evidence_inputs()
        mismatched_inputs["reconciliation"] = {
            "status": "MISMATCHED",
            "durable_receipt_sha256": "c" * 64,
            "identity_sha256": "d" * 64,
            "observed_identity_sha256": "e" * 64,
        }

        with self.assertRaisesRegex(ValueError, "MATCHED is not valid for static acceptance"):
            build_runtime_evidence_aggregate(**matched_inputs)
        self.assertTrue(validate_runtime_evidence_aggregate(build_runtime_evidence_aggregate(**mismatched_inputs))["ok"])

    def test_report_contains_enrichment_fields(self):
        runtime = ExecutionRuntime(dry_run=True, run_id="test-001")
        report = build_execution_report(runtime)
        self.assertIsNone(report["total_equity_usdt"])
        self.assertIsNone(report["trend_equity_usdt"])
        self.assertFalse(report["circuit_breaker_triggered"])
        self.assertIsNone(report["degraded_mode_level"])
        self.assertEqual(report["upstream_pool_symbols"], [])
        self.assertEqual(report["gating_summary"], {})
        self.assertEqual(report["gating_events"], [])

    def test_report_preserves_existing_fields(self):
        runtime = ExecutionRuntime(dry_run=False, run_id="test-002")
        with patch.dict(
            os.environ,
            {
                "STRATEGY_PROFILE": "crypto_live_pool_rotation",
                "SERVICE_NAME": "binance-runtime",
                "LOG_DEPLOY_TARGET": "vps",
            },
            clear=False,
        ):
            report = build_execution_report(runtime)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["run_id"], "test-002")
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["schema_version"], "runtime_report.v1")
        self.assertEqual(report["platform"], "binance")
        self.assertEqual(report["strategy_profile"], "crypto_live_pool_rotation")
        self.assertIn("buy_sell_intents", report)
        self.assertIn("log_lines", report)

    def test_report_uses_runtime_target_service_identity(self):
        runtime_target = build_runtime_target(
            platform_id="binance",
            strategy_profile="crypto_live_pool_rotation",
            dry_run_only=False,
            service_name="binance-platform",
        )
        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="test-runtime-target",
            runtime_target=runtime_target,
        )

        report = build_execution_report(runtime)

        self.assertEqual(report["service_name"], "binance-platform")
        self.assertEqual(report["runtime_target"]["service_name"], "binance-platform")

    def test_record_gating_event_updates_summary_and_events(self):
        report = {}

        record_gating_event(
            report,
            gate="trend_buy_below_min_budget",
            category="trend",
            symbol="ETHUSDT",
            detail={"budget_usdt": 12.0},
        )
        record_gating_event(
            report,
            gate="trend_buy_below_min_budget",
            category="trend",
        )

        self.assertEqual(report["gating_summary"]["trend_buy_below_min_budget"], 2)
        self.assertEqual(report["gating_events"][0]["symbol"], "ETHUSDT")
        self.assertEqual(report["gating_events"][0]["detail"]["budget_usdt"], 12.0)

    def test_runtime_notify_persists_only_safe_failed_delivery_receipt(self):
        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="test-notification",
            tg_token="secret-token",
            tg_chat_id="private-chat",
            notifier=lambda **_kwargs: {
                "sink": "telegram",
                "delivery_status": "failed",
                "transport_acknowledged": False,
                "error_type": "telegram_rejected",
            },
        )
        report = build_execution_report(runtime)

        acknowledged = runtime_notify(runtime, report, "sensitive notification body")
        finalize_notification_delivery(report)

        serialized = str(report)
        self.assertFalse(acknowledged)
        self.assertEqual(report["status"], "error")
        self.assertFalse(
            report["summary"]["notification_delivery_summary"]["all_acknowledged"]
        )
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("private-chat", serialized)
        self.assertNotIn("sensitive notification body", serialized)
