"""Binance-only recovery accounting. No order or persistence ports.

The frozen snapshot is retained. Only a mathematically exact BONUS explanation
may reconcile balance drift; identity, orders, trades and the local ledger must
still match it. The caller verifies the designated workflow/storage provenance.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from quant_platform_kit.common.broker_reconciliation import (
    BrokerReconciliationFinding,
    build_broker_reconciliation_evidence,
    calculate_broker_observation_sha256 as digest,
)
from quant_platform_kit.common.broker_reconciliation_enrollment import (
    BrokerReconciliationBaselineCandidate,
    evaluate_broker_reconciliation_baseline_enrollment,
)
from quant_platform_kit.common.reconciliation_recovery import (
    ReconciliationRecoveryConfirmation,
    ReconciliationRecoverySourceSnapshot,
    build_reconciliation_recovery_record,
)
from application.broker_reconciliation import (
    build_reconciliation_candidate,
    collect_read_only_reconciliation_observations,
    diagnose_balance_flows,
    diagnose_balance_snapshot,
)


def _reader(expected):
    return lambda *_: json.dumps(expected)


def _validate_frozen_target(target):
    if (target.platform_id != "binance" or target.strategy_profile != "crypto_live_pool_rotation"
            or target.dry_run_only or target.live_continuity.state != "RECONCILE_ONLY"
            or target.live_continuity.baseline_kind != "legacy_authorized"):
        raise ValueError("recovery_target_not_frozen_legacy")


def collect_recovery_source(*, client, runtime_target, expected, ledger, symbols,
                            history_start, source_run, now=None):
    _validate_frozen_target(runtime_target)
    started_at = now or datetime.now(timezone.utc)
    account = client.get_account()
    observations = collect_read_only_reconciliation_observations(
        client, strategy_symbols=symbols, local_execution_ledger=ledger,
        now=started_at, account_snapshot=account,
    )
    original = build_reconciliation_candidate(
        observations=observations, runtime_target=runtime_target,
        env_reader=_reader(expected), observed_at=started_at,
    )
    allowed = {BrokerReconciliationFinding.POSITIONS_MISMATCH, BrokerReconciliationFinding.CASH_MISMATCH}
    if set(original.recovery_blockers) - allowed or observations.open_orders:
        raise ValueError("recovery_non_balance_state_mismatch")
    proof = diagnose_balance_flows(
        client, start=history_start, end=started_at, now=started_at,
        account=account, expected_digests=expected,
    )
    counts = proof.get("history_counts", {})
    bonus = proof.get("spot_bonus_reconciliation", {})
    if (proof.get("history_complete_for_requested_surfaces") is not True
            or bonus.get("historical_balance_hashes_match") is not True
            or any(value != 0 for name, value in counts.items() if name != "earn_rewards")):
        raise ValueError("recovery_balance_flow_unexplained")
    # A changing snapshot across the bounded history read cannot be enrolled.
    current = original.evidence.to_dict()
    observed_expected = {key: current[key] for key in expected}
    if diagnose_balance_snapshot(client.get_account(), expected_digests=observed_expected)["reason_code"] != "balance_snapshot_matches":
        raise ValueError("recovery_balance_changed_during_read")
    # Match here means exact accounting reconciliation to the frozen baseline,
    # not equality to a hash copied from the current observation.
    reconciled_fields = {key: value for key, value in current.items() if key not in {"schema_version", "evidence_sha256"}}
    reconciled_fields.update(positions_match=True, cash_match=True)
    reconciled = build_broker_reconciliation_evidence(**reconciled_fields)
    source = {
        "run": dict(source_run), "frozen_expected_sha256": digest(expected),
        "runtime_target_sha256": digest(runtime_target.to_dict()),
        "history_start": history_start.isoformat(), "symbols_sha256": digest(list(symbols)),
        "original_evidence": current, "reconciled_evidence": reconciled.to_dict(), "proof": proof,
    }
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        (reconciled,), source_receipts_sha256=digest(source), now=now or datetime.now(timezone.utc),
    )
    if evaluation.candidate is None:
        raise ValueError("recovery_enrollment_blocked")
    return {"candidate": evaluation.candidate.to_dict(), "source": source}


def validate_source(package, *, runtime_target, expected, now=None):
    """Validate content after the caller verifies trusted storage/workflow origin."""
    _validate_frozen_target(runtime_target)
    candidate = BrokerReconciliationBaselineCandidate.from_dict(package["candidate"])
    source = package["source"]
    if (source["frozen_expected_sha256"] != digest(expected)
            or source["runtime_target_sha256"] != digest(runtime_target.to_dict())
            or source["original_evidence"]["account_scope_sha256"] != expected["account_scope_sha256"]
            or source["proof"]["history_complete_for_requested_surfaces"] is not True
            or source["proof"]["spot_bonus_reconciliation"]["historical_balance_hashes_match"] is not True):
        raise ValueError("recovery_source_binding_mismatch")
    result = evaluate_broker_reconciliation_baseline_enrollment(
        (source["reconciled_evidence"],), source_receipts_sha256=digest(source), now=now,
    )
    if result.candidate is None or result.candidate.to_dict() != candidate.to_dict():
        raise ValueError("recovery_candidate_source_mismatch")
    for key in ("open_orders_sha256", "recent_executions_sha256", "local_execution_ledger_sha256"):
        if candidate.expected_digests[key] != expected[key]:
            raise ValueError("recovery_non_balance_source_mismatch")
    return candidate


def console_snapshot(candidate, *, recovery_id, now=None):
    now = now or datetime.now(timezone.utc)
    record = build_reconciliation_recovery_record(
        recovery_id=recovery_id, console_platform="binance", candidate=candidate, now=now,
    )
    return ReconciliationRecoverySourceSnapshot(
        source_id="binance.reconciliation_recovery", generated_at=now, computed_at=now, records=(record,),
    ).to_dict()


def verify_confirmation(payload, *, recovery_id, candidate):
    if (payload.get("ok") is not True
            or payload.get("schema_version") != "qsl_reconciliation_recovery_controller_read.v1"
            or payload.get("policy") != {"no_order": True, "execution_authority_granted": False, "controller_must_reverify": True}):
        raise ValueError("recovery_confirmation_policy_invalid")
    row = payload.get("recovery", {})
    required = {"recovery_id": recovery_id, "platform": "binance", "strategy_profile": candidate.strategy_profile,
                "environment": "live", "reconciliation_state": "RECONCILE_ONLY",
                "candidate_sha256": candidate.candidate_sha256,
                "dual_review_binding_sha256": candidate.candidate_sha256,
                "evidence_sample_count": len(candidate.source_evidence_sha256)}
    if any(row.get(key) != value for key, value in required.items()):
        raise ValueError("recovery_confirmation_binding_invalid")
    return ReconciliationRecoveryConfirmation.from_dict(payload.get("confirmation", {}))


def activated_target(target, control, *, expected):
    """Read a previously committed transition; never activate from a candidate alone."""
    from quant_platform_kit.common.reconciliation_recovery import ReconciliationRecoveryTransitionPlan
    from dataclasses import replace
    _validate_frozen_target(target)
    if control is None or control.get("state") == "RECONCILE_ONLY":
        return target
    if control.get("state") != "ACTIVE_LKG":
        raise ValueError("recovery_control_state_invalid")
    source = control["source"]
    if source.get("kind") == "post_rebase":
        from application.rebased_recovery import validate_post_rebase_source

        candidate = validate_post_rebase_source(
            control,
            runtime_target=target,
            legacy_expected=expected,
            require_fresh=False,
        )
    else:
        candidate = BrokerReconciliationBaselineCandidate.from_dict(control["candidate"])
    plan = ReconciliationRecoveryTransitionPlan.from_dict(control["transition_plan"])
    confirmation = ReconciliationRecoveryConfirmation.from_dict(control["confirmation"])
    if (source["runtime_target_sha256"] != digest(target.to_dict())
            or source["frozen_expected_sha256"] != digest(expected)
            or candidate.source_receipts_sha256 != digest(source)
            or candidate.account_scope_sha256 != expected["account_scope_sha256"]
            or plan.candidate_sha256 != candidate.candidate_sha256
            or confirmation.candidate_sha256 != candidate.candidate_sha256
            or confirmation.dual_review_binding_sha256 != candidate.candidate_sha256
            or plan.confirmation_sha256 != confirmation.confirmation_sha256
            or plan.recovery_id != control["recovery_id"] or confirmation.recovery_id != plan.recovery_id
            or plan.expected_digests != candidate.expected_digests
            or plan.baseline_id != target.live_continuity.baseline_id
            or plan.baseline_target_sha256 != target.live_continuity.baseline_target_sha256
            or candidate.baseline_id != plan.baseline_id or candidate.baseline_target_sha256 != plan.baseline_target_sha256
            or plan.verified_at <= confirmation.confirmed_at):
        raise ValueError("recovery_active_binding_invalid")
    # The target was already validated from the original deployed payload.
    # Retain every identity field and its legacy fingerprint serialization.
    return replace(target, live_continuity=replace(target.live_continuity, state="ACTIVE_LKG"))


def load_activated_target(target):
    """Opt-in consumer of the existing account's atomic recovery control document."""
    import os
    from application.broker_reconciliation import _expected_digests
    if os.getenv("BINANCE_RECOVERY_CONTROL_ENABLED", "false").lower() != "true":
        return target
    from live_services import get_firestore_client
    snapshot = get_firestore_client().collection("strategy").document("MULTI_ASSET_STATE__recovery").get(retry=None)
    return activated_target(target, snapshot.to_dict() if snapshot.exists else None, expected=_expected_digests())
