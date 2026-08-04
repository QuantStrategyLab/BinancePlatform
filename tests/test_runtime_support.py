import hashlib
import json
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
    build_runtime_evidence_aggregate_v2,
    build_execution_report,
    finalize_notification_delivery,
    record_gating_event,
    runtime_notify,
    validate_runtime_evidence_aggregate,
    validate_runtime_evidence_aggregate_v2,
    runtime_call_client,
)
from quant_platform_kit.common.runtime_target import build_runtime_target


class TestBuildExecutionReport(unittest.TestCase):
    @staticmethod
    def action_authorization_report(runtime, *, payload, method_name="order_market_buy", effect_type="order_buy"):
        runtime.authorization_sequence = 1
        report = build_execution_report(runtime)
        report.update({
            "release_identity_sha256": "5" * 64,
            "member_risk_assessment": {
                "scope": "MEMBER",
                "outcome": "APPROVE",
                "decision_digest_sha256": "c" * 64,
                "assessment_sha256": "1" * 64,
            },
            "account_risk_assessment": {
                "scope": "ACCOUNT",
                "outcome": "APPROVE",
                "decision_digest_sha256": "c" * 64,
                "portfolio_snapshot_digest_sha256": "d" * 64,
                "assessment_sha256": "2" * 64,
            },
        })
        authorization = {
            "contract_version": "qsl.binance_order_authorization.v2",
            "outcome": "APPROVE",
            "run_id": str(runtime.run_id),
            "authorization_kind": "ACTION",
            "action_sequence": 1,
            "action_class": "btc_dca_buy",
            "method_name": method_name,
            "effect_type": effect_type,
            "canonical_payload_sha256": hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            ).hexdigest(),
            "decision_digest_sha256": "c" * 64,
            "release_identity_sha256": "5" * 64,
            "account_snapshot_sha256": "d" * 64,
            "member_assessment_sha256": "1" * 64,
            "account_assessment_sha256": "2" * 64,
            "mandate_authority_receipt_sha256": "b" * 64,
            "mandate_scope": "PAPER",
        }
        authorization["authorization_sha256"] = hashlib.sha256(
            json.dumps(authorization, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        report["order_authorization"] = authorization
        return report

    def test_runtime_call_client_consumes_exact_action_authorization_once(self):
        payload = {"symbol": "BTCUSDT", "quantity": 0.001}
        runtime = ExecutionRuntime(dry_run=True, run_id="synthetic-run")
        report = self.action_authorization_report(runtime, payload=payload)

        result = runtime_call_client(
            runtime,
            report,
            method_name="order_market_buy",
            payload=payload,
            effect_type="order_buy",
        )

        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(report["order_authorization"]["authorization_kind"], "ACTION")
        with self.assertRaises(RuntimeError):
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload=payload,
                effect_type="order_buy",
            )

    def test_runtime_call_client_rejects_action_binding_mismatches(self):
        payload = {"symbol": "BTCUSDT", "quantity": 0.001}
        variants = (
            ("method", "order_market_sell", payload, "order_buy"),
            ("effect", "order_market_buy", payload, "order_sell"),
            ("payload", "order_market_buy", {"symbol": "BTCUSDT", "quantity": 0.002}, "order_buy"),
        )
        for label, method_name, call_payload, effect_type in variants:
            with self.subTest(label=label):
                runtime = ExecutionRuntime(dry_run=True, run_id=f"synthetic-{label}")
                report = self.action_authorization_report(runtime, payload=payload)
                with self.assertRaises(RuntimeError):
                    runtime_call_client(
                        runtime,
                        report,
                        method_name=method_name,
                        payload=call_payload,
                        effect_type=effect_type,
                    )

    def test_runtime_call_client_rejects_stale_decision_and_snapshot_bindings(self):
        payload = {"symbol": "BTCUSDT", "quantity": 0.001}
        variants = (
            ("stale_sequence", lambda runtime, _report: setattr(runtime, "authorization_sequence", 2)),
            ("decision", lambda _runtime, report: report["account_risk_assessment"].update(
                decision_digest_sha256="e" * 64
            )),
            ("snapshot", lambda _runtime, report: report["account_risk_assessment"].update(
                portfolio_snapshot_digest_sha256="e" * 64
            )),
        )
        for label, mutate in variants:
            with self.subTest(label=label):
                runtime = ExecutionRuntime(dry_run=True, run_id=f"synthetic-{label}")
                report = self.action_authorization_report(runtime, payload=payload)
                mutate(runtime, report)
                with self.assertRaises(RuntimeError):
                    runtime_call_client(
                        runtime,
                        report,
                        method_name="order_market_buy",
                        payload=payload,
                        effect_type="order_buy",
                    )

    @staticmethod
    def canonical_sha256(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()

    @classmethod
    def risk_assessment(
        cls,
        scope,
        *,
        outcome="REJECT",
        mandate_scope="RESEARCH_ONLY",
        effective_exposure_cap=0.0,
        decision_digest_sha256="c" * 64,
        portfolio_snapshot_digest_sha256="d" * 64,
    ):
        assessment = {
            "contract_version": "qsl.risk_gate_assessment.v1",
            "scope": scope,
            "evaluated_at": "2026-08-04T00:00:00Z",
            "policy_id": "qpk.risk_gate",
            "policy_version": "v1",
            "qpk_source_revision": "a" * 40,
            "mandate_id": "binance_crypto_research_only_v1",
            "mandate_version": "2026-08-04.1",
            "mandate_authority_receipt_sha256": "b" * 64,
            "mandate_scope": mandate_scope,
            "decision_digest_sha256": decision_digest_sha256,
            "portfolio_snapshot_digest_sha256": portfolio_snapshot_digest_sha256,
            "effective_exposure_cap": effective_exposure_cap,
            "observed_effective_exposure": 0.0,
            "proposed_effective_exposure": 0.25 if outcome == "APPROVE" else 0.0,
            "outcome": outcome,
            "reason_codes": () if outcome == "APPROVE" else ("budget_authority_exceeded",),
        }
        assessment["assessment_sha256"] = cls.canonical_sha256(assessment)
        return assessment

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

    @classmethod
    def v2_chain_inputs(cls, *, outcome="REJECT"):
        mandate_scope = "PAPER" if outcome == "APPROVE" else "RESEARCH_ONLY"
        effective_exposure_cap = 0.5 if outcome == "APPROVE" else 0.0
        member = cls.risk_assessment(
            "MEMBER",
            outcome=outcome,
            mandate_scope=mandate_scope,
            effective_exposure_cap=effective_exposure_cap,
        )
        account = cls.risk_assessment(
            "ACCOUNT",
            outcome=outcome,
            mandate_scope=mandate_scope,
            effective_exposure_cap=effective_exposure_cap,
        )
        release_identity = cls.v2_release_identity()
        authorization = {
            "contract_version": "qsl.binance_order_authorization.v2",
            "outcome": outcome,
            "run_id": "synthetic",
            "authorization_kind": "ACTION",
            "action_sequence": 1,
            "action_class": "btc_dca_buy",
            "method_name": "order_market_buy",
            "effect_type": "order_buy",
            "canonical_payload_sha256": "9" * 64,
            "decision_digest_sha256": account["decision_digest_sha256"],
            "release_identity_sha256": cls.canonical_sha256(release_identity),
            "account_snapshot_sha256": account["portfolio_snapshot_digest_sha256"],
            "member_assessment_sha256": member["assessment_sha256"],
            "account_assessment_sha256": account["assessment_sha256"],
            "mandate_authority_receipt_sha256": account["mandate_authority_receipt_sha256"],
            "mandate_scope": account["mandate_scope"],
        }
        authorization["authorization_sha256"] = cls.canonical_sha256(authorization)
        cap = {
            "outcome": outcome,
            "mandate_id": account["mandate_id"],
            "mandate_version": account["mandate_version"],
            "mandate_authority_receipt_sha256": account["mandate_authority_receipt_sha256"],
            "mandate_scope": account["mandate_scope"],
            "effective_exposure_cap": account["effective_exposure_cap"],
            "decision_digest_sha256": account["decision_digest_sha256"],
            "release_identity_sha256": cls.canonical_sha256(release_identity),
            "account_snapshot_sha256": account["portfolio_snapshot_digest_sha256"],
            "account_assessment_sha256": account["assessment_sha256"],
            "qpk_source_revision": account["qpk_source_revision"],
            "order_authorization_sha256": authorization["authorization_sha256"],
        }
        return {
            "produced_at": "2026-08-04T00:00:00Z",
            "run_id": "synthetic",
            "producer_revision": "f" * 40,
            "release_identity": release_identity,
            "member_risk_assessment": member,
            "account_risk_assessment": account,
            "cap_assessment": cap,
            "order_authorization": authorization,
            "strategy_stop_evaluation": {"evaluated": True, "outcome": "CLEAR"},
            "account_breaker_evaluation": {"evaluated": True, "outcome": "CLEAR"},
            "execution_gate_outcome": outcome,
            "reconciliation": {"status": "MISSING"},
        }

    @classmethod
    def resign_aggregate(cls, aggregate):
        aggregate["aggregate_sha256"] = cls.canonical_sha256(
            {key: value for key, value in aggregate.items() if key != "aggregate_sha256"}
        )

    @classmethod
    def resign_assessment(cls, assessment):
        assessment["assessment_sha256"] = cls.canonical_sha256(
            {key: value for key, value in assessment.items() if key != "assessment_sha256"}
        )

    @classmethod
    def resign_authorization(cls, aggregate):
        authorization = aggregate["order_authorization"]
        authorization["authorization_sha256"] = cls.canonical_sha256(
            {key: value for key, value in authorization.items() if key != "authorization_sha256"}
        )
        aggregate["cap_assessment"]["order_authorization_sha256"] = authorization["authorization_sha256"]

    def test_v2_aggregate_builds_deterministic_redacted_missing_receipt(self):
        kwargs = self.v2_chain_inputs()

        first = build_runtime_evidence_aggregate_v2(**kwargs)
        second = build_runtime_evidence_aggregate_v2(**kwargs)

        self.assertEqual(first, second)
        self.assertTrue(validate_runtime_evidence_aggregate_v2(first)["ok"])
        self.assertEqual(first["reconciliation"], {"status": "MISSING"})
        self.assertEqual(first["order_authorization"], kwargs["order_authorization"])
        self.assertNotIn("positions", str(first))

    def test_v2_aggregate_validates_synthetic_future_authority_approve_chain(self):
        aggregate = build_runtime_evidence_aggregate_v2(**self.v2_chain_inputs(outcome="APPROVE"))

        self.assertTrue(validate_runtime_evidence_aggregate_v2(aggregate)["ok"])
        self.assertEqual(aggregate["execution_gate_outcome"], "APPROVE")

    def test_v2_aggregate_validates_preliminary_reject_and_fail_closed_action_reject(self):
        preliminary = self.v2_chain_inputs()
        preliminary_authorization = preliminary["order_authorization"]
        preliminary_authorization.update(
            authorization_kind="PRELIMINARY",
            action_sequence=0,
            action_class="",
            method_name="",
            effect_type="",
            canonical_payload_sha256="",
        )
        preliminary_authorization["authorization_sha256"] = self.canonical_sha256(
            {
                key: value
                for key, value in preliminary_authorization.items()
                if key != "authorization_sha256"
            }
        )
        preliminary["cap_assessment"]["order_authorization_sha256"] = preliminary_authorization[
            "authorization_sha256"
        ]

        approved_risk = self.v2_chain_inputs(outcome="APPROVE")
        approved_risk["order_authorization"]["outcome"] = "REJECT"
        approved_risk["order_authorization"]["authorization_sha256"] = self.canonical_sha256(
            {
                key: value
                for key, value in approved_risk["order_authorization"].items()
                if key != "authorization_sha256"
            }
        )
        approved_risk["cap_assessment"]["order_authorization_sha256"] = approved_risk["order_authorization"][
            "authorization_sha256"
        ]
        approved_risk["execution_gate_outcome"] = "REJECT"

        self.assertTrue(validate_runtime_evidence_aggregate_v2(build_runtime_evidence_aggregate_v2(**preliminary))["ok"])
        self.assertTrue(validate_runtime_evidence_aggregate_v2(build_runtime_evidence_aggregate_v2(**approved_risk))["ok"])

    def test_v2_aggregate_rejects_contradictory_execution_gate_before_signing(self):
        kwargs = self.v2_chain_inputs()
        kwargs["execution_gate_outcome"] = "APPROVE"

        with self.assertRaises(ValueError):
            build_runtime_evidence_aggregate_v2(**kwargs)

    def test_v2_aggregate_rejects_stale_risk_assessment_digest(self):
        aggregate = build_runtime_evidence_aggregate_v2(**self.v2_chain_inputs())
        aggregate["member_risk_assessment"]["decision_digest_sha256"] = "e" * 64
        self.resign_aggregate(aggregate)

        self.assertFalse(validate_runtime_evidence_aggregate_v2(aggregate)["ok"])

    def test_v2_aggregate_rejects_member_account_cross_assessment_splice(self):
        shared_fields = (
            "policy_id",
            "policy_version",
            "qpk_source_revision",
            "mandate_id",
            "mandate_version",
            "mandate_authority_receipt_sha256",
            "mandate_scope",
            "decision_digest_sha256",
            "portfolio_snapshot_digest_sha256",
            "effective_exposure_cap",
        )
        for field_name in shared_fields:
            with self.subTest(field_name=field_name):
                aggregate = build_runtime_evidence_aggregate_v2(**self.v2_chain_inputs())
                member = aggregate["member_risk_assessment"]
                member[field_name] = 0.25 if field_name == "effective_exposure_cap" else (
                    "e" * 64 if field_name.endswith("sha256") else "changed"
                )
                self.resign_assessment(member)
                aggregate["order_authorization"]["member_assessment_sha256"] = member["assessment_sha256"]
                self.resign_authorization(aggregate)
                self.resign_aggregate(aggregate)

                self.assertFalse(validate_runtime_evidence_aggregate_v2(aggregate)["ok"])

    def test_v2_aggregate_rejects_cap_chain_mismatches(self):
        cap_fields = (
            "decision_digest_sha256",
            "release_identity_sha256",
            "account_snapshot_sha256",
            "account_assessment_sha256",
            "mandate_id",
            "mandate_version",
            "mandate_authority_receipt_sha256",
            "mandate_scope",
            "effective_exposure_cap",
            "qpk_source_revision",
            "order_authorization_sha256",
        )
        for field_name in cap_fields:
            with self.subTest(field_name=field_name):
                aggregate = build_runtime_evidence_aggregate_v2(**self.v2_chain_inputs())
                aggregate["cap_assessment"][field_name] = (
                    0.25 if field_name == "effective_exposure_cap" else (
                        "e" * 64 if field_name.endswith("sha256") else "changed"
                    )
                )
                self.resign_aggregate(aggregate)

                self.assertFalse(validate_runtime_evidence_aggregate_v2(aggregate)["ok"])

    def test_v2_aggregate_rejects_authorization_chain_mismatches(self):
        authorization_fields = (
            "run_id",
            "decision_digest_sha256",
            "release_identity_sha256",
            "account_snapshot_sha256",
            "member_assessment_sha256",
            "account_assessment_sha256",
            "mandate_authority_receipt_sha256",
            "mandate_scope",
        )
        for field_name in authorization_fields:
            with self.subTest(field_name=field_name):
                aggregate = build_runtime_evidence_aggregate_v2(**self.v2_chain_inputs())
                aggregate["order_authorization"][field_name] = (
                    "e" * 64 if field_name.endswith("sha256") else "changed"
                )
                self.resign_authorization(aggregate)
                self.resign_aggregate(aggregate)

                self.assertFalse(validate_runtime_evidence_aggregate_v2(aggregate)["ok"])

    def test_v2_aggregate_rejects_stale_action_binding_digest(self):
        action_fields = (
            "action_sequence",
            "action_class",
            "method_name",
            "effect_type",
            "canonical_payload_sha256",
        )
        for field_name in action_fields:
            with self.subTest(field_name=field_name):
                aggregate = build_runtime_evidence_aggregate_v2(**self.v2_chain_inputs())
                aggregate["order_authorization"][field_name] = (
                    2 if field_name == "action_sequence" else (
                        "e" * 64 if field_name.endswith("sha256") else "changed"
                    )
                )
                self.resign_aggregate(aggregate)

                self.assertFalse(validate_runtime_evidence_aggregate_v2(aggregate)["ok"])

    def test_v2_aggregate_rejects_missing_or_aliased_singletons(self):
        variants = (
            ("missing_member", lambda value: value.pop("member_risk_assessment")),
            ("missing_authorization", lambda value: value.pop("order_authorization")),
            ("member_alias", lambda value: value.update(member_risk_assessments=[])),
            ("authorization_alias", lambda value: value.update(authorization=value["order_authorization"])),
        )
        for label, mutate in variants:
            with self.subTest(label=label):
                aggregate = build_runtime_evidence_aggregate_v2(**self.v2_chain_inputs())
                mutate(aggregate)
                self.resign_aggregate(aggregate)

                self.assertFalse(validate_runtime_evidence_aggregate_v2(aggregate)["ok"])

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
        with self.assertRaises(ValueError):
            build_runtime_evidence_aggregate_v2(
                produced_at="2026-08-04T00:00:00Z",
                run_id="synthetic",
                producer_revision="a" * 40,
                release_identity=self.v2_release_identity(),
                member_risk_assessment={"scope": "MEMBER", "outcome": "REJECT", "assessment_sha256": "1" * 64},
                account_risk_assessment={"scope": "ACCOUNT", "outcome": "REJECT", "assessment_sha256": "2" * 64},
                cap_assessment={"outcome": "REJECT", "positions": []},
                order_authorization={},
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
