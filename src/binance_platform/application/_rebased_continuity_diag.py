"""Historical continuity diagnosis and source validation."""
from __future__ import annotations

from binance_platform.application._rebased_shared import (
    BrokerReconciliationBaselineCandidate,
    BrokerReconciliationEvidence,
    HISTORICAL_CONTINUITY_KIND,
    Mapping,
    PROSPECTIVE_ARCHIVE_DOCUMENT,
    PROSPECTIVE_ARCHIVE_SHA256,
    PROSPECTIVE_MIGRATION_RUN_ID,
    PROSPECTIVE_MIGRATION_RUN_SHA,
    Sequence,
    _evidence,
    _facade_const,
    _facade_digest,
    _utc,
    _valid_run,
    _validate_frozen_target,
    datetime,
    evaluate_broker_reconciliation_baseline_enrollment,
    is_prospective_archive_document,
    prospective_archive_document,
    prospective_migration_binding,
    re,
    timedelta,
    timezone,
)
from binance_platform.application._rebased_continuity_proof import (
    _build_continuity_proof,
    _collect_and_validate_continuity_history,
    _read_continuity_broker_state,
    _validate_continuity_inputs,
    _verify_continuity_conservation,
    validate_historical_continuity_material,
)

def _collect_historical_continuity_private(
    *,
    client: object,
    runtime_target: object,
    legacy_expected: Mapping[str, str],
    ledger: Mapping[str, object],
    archive: Mapping[str, object],
    symbols: Sequence[str],
    source_run: Mapping[str, object],
    migration_run: Mapping[str, object],
    now: datetime | None = None,
    clock=None,
) -> dict[str, object]:
    """Chunked Binance history from approved opening; ledger is verification target only."""
    ctx = _validate_continuity_inputs(
        runtime_target=runtime_target,
        ledger=ledger,
        archive=archive,
        source_run=source_run,
        migration_run=migration_run,
        now=now,
        clock=clock,
    )
    material = ctx["material"]
    managed = material["managed_assets"]
    history, counts, earn_rewards, earn_subscriptions, earn_redemptions = (
        _collect_and_validate_continuity_history(
            client,
            opening_at=ctx["opening_at"],
            final_at=ctx["final_at"],
            managed_assets=managed,
            approved_reward_products=ctx["approved_reward_products"],
            opening_reward_product_totals=ctx["opening_reward_product_totals"],
        )
    )
    observations, broker_checkpoint, observed_count = _read_continuity_broker_state(
        client,
        symbols=symbols,
        ledger=ledger,
        opening_at=ctx["opening_at"],
        final_at=ctx["final_at"],
        managed=managed,
        expected_scope=legacy_expected.get("account_scope_sha256"),
    )
    _verify_continuity_conservation(
        ledger=ledger,
        archive=archive,
        material=material,
        history=history,
        current_checkpoint=ctx["current_checkpoint"],
        current_snapshot=ctx["current_snapshot"],
        broker_checkpoint=broker_checkpoint,
        final_at=ctx["final_at"],
        earn_rewards=earn_rewards,
        earn_subscriptions=earn_subscriptions,
        earn_redemptions=earn_redemptions,
    )
    proof = _build_continuity_proof(
        history=history,
        counts=counts,
        managed=managed,
        observed_count=observed_count,
        earn_rewards=earn_rewards,
        earn_subscriptions=earn_subscriptions,
    )
    return {
        "observed_at": ctx["final_at"],
        "observations": observations,
        "current_ledger_sha256": material["current_ledger_sha256"],
        "archive_sha256": material["archive_sha256"],
        "proof": proof,
    }


def collect_historical_continuity_diagnosis(**kwargs) -> dict[str, object]:
    """Sanitized read-only diagnosis for opening→now continuity via chunked history."""
    private = _collect_historical_continuity_private(**kwargs)
    result = {
        "status": "diagnosed",
        "source_kind": HISTORICAL_CONTINUITY_KIND,
        "current_ledger_sha256": private["current_ledger_sha256"],
        "historical_difference_unresolved": True,
        "chunk_count": private["proof"]["chunk_count"],
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }
    counts = private["proof"].get("reward_product_classification_counts")
    if isinstance(counts, Mapping):
        result["reward_product_classification_counts"] = dict(counts)
    return result


def collect_historical_continuity_source(**kwargs) -> dict[str, object]:
    """Enroll a candidate bound to the verified current ledger digest."""
    private = _collect_historical_continuity_private(**kwargs)
    runtime_target = kwargs["runtime_target"]
    legacy_expected = kwargs["legacy_expected"]
    ledger = kwargs["ledger"]
    archive = kwargs["archive"]
    symbols = kwargs["symbols"]
    source_run = kwargs["source_run"]
    migration_run = kwargs["migration_run"]
    observed_at = private["observed_at"]
    observations = private["observations"]
    if _facade_digest(ledger) != private["current_ledger_sha256"]:
        raise ValueError("historical_continuity_ledger_changed")
    binding = prospective_migration_binding(archive)
    if (
        migration_run["id"] != binding["run_id"]
        or migration_run["head_sha"] != binding["head_sha"]
        or private["archive_sha256"] != binding["archive_sha256"]
    ):
        raise ValueError("historical_continuity_migration_run_invalid")
    reconciled = _evidence(
        runtime_target=runtime_target,
        account_scope_sha256=legacy_expected["account_scope_sha256"],
        observations=observations,
        observed_at=observed_at,
    )
    source = {
        "kind": HISTORICAL_CONTINUITY_KIND,
        "run": dict(source_run),
        "migration_run": dict(migration_run),
        "archive_document": binding["archive_document"],
        "archive_sha256": private["archive_sha256"],
        "current_ledger_sha256": private["current_ledger_sha256"],
        "new_ledger_sha256": private["current_ledger_sha256"],
        "frozen_expected_sha256": _facade_digest(legacy_expected),
        "runtime_target_sha256": _facade_digest(runtime_target.to_dict()),
        "symbols_sha256": _facade_digest(list(symbols)),
        "historical_difference_unresolved": True,
        "opening_balance_observed_at": validate_historical_continuity_material(
            ledger, archive
        )["opening_at"].isoformat(),
        "reconciled_evidence": reconciled.to_dict(),
        "proof": private["proof"],
    }
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        (reconciled,), source_receipts_sha256=_facade_digest(source), now=observed_at
    )
    if evaluation.candidate is None:
        raise ValueError("historical_continuity_enrollment_blocked")
    return {"candidate": evaluation.candidate.to_dict(), "source": source}


def validate_historical_continuity_source(
    package: Mapping[str, object],
    *,
    runtime_target: object,
    legacy_expected: Mapping[str, str],
    now: datetime | None = None,
    require_fresh: bool = True,
) -> BrokerReconciliationBaselineCandidate:
    """Revalidate a stored historical-continuity package without private values."""
    from quant_platform_kit.common.broker_reconciliation import (
        calculate_broker_reconciliation_evidence_sha256,
    )

    _validate_frozen_target(runtime_target)
    try:
        source = package["source"]
        proof = source["proof"]
        candidate = BrokerReconciliationBaselineCandidate.from_dict(package["candidate"])
        evidence = BrokerReconciliationEvidence.from_dict(source["reconciled_evidence"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("historical_continuity_source_binding_mismatch") from None
    if not isinstance(source, Mapping) or not isinstance(proof, Mapping):
        raise ValueError("historical_continuity_source_binding_mismatch")
    expected_digests = {
        "positions_sha256": evidence.positions_sha256,
        "cash_sha256": evidence.cash_sha256,
        "open_orders_sha256": evidence.open_orders_sha256,
        "recent_executions_sha256": evidence.recent_executions_sha256,
        "local_execution_ledger_sha256": evidence.local_execution_ledger_sha256,
    }
    observed = evidence.observed_at
    if isinstance(observed, datetime):
        observed_at = observed.astimezone(timezone.utc) if observed.tzinfo else observed.replace(tzinfo=timezone.utc)
    else:
        observed_at = _utc(observed)
    candidate_first = candidate.first_observed_at
    candidate_last = candidate.last_observed_at
    if isinstance(candidate_first, datetime):
        first_at = candidate_first.astimezone(timezone.utc) if candidate_first.tzinfo else candidate_first.replace(tzinfo=timezone.utc)
    else:
        first_at = _utc(candidate_first)
    if isinstance(candidate_last, datetime):
        last_at = candidate_last.astimezone(timezone.utc) if candidate_last.tzinfo else candidate_last.replace(tzinfo=timezone.utc)
    else:
        last_at = _utc(candidate_last)
    evidence_body = dict(source["reconciled_evidence"])
    try:
        evidence_digest_ok = (
            calculate_broker_reconciliation_evidence_sha256(evidence_body)
            == evidence.evidence_sha256
        )
    except (TypeError, ValueError):
        evidence_digest_ok = False
    if (
        source.get("kind") != HISTORICAL_CONTINUITY_KIND
        or not _valid_run(source.get("migration_run"))
        or not _valid_run(source.get("run"))
        or source.get("frozen_expected_sha256") != _facade_digest(legacy_expected)
        or source.get("runtime_target_sha256") != _facade_digest(runtime_target.to_dict())
        or source.get("historical_difference_unresolved") is not True
        or not isinstance(source.get("current_ledger_sha256"), str)
        or len(source["current_ledger_sha256"]) != 64
        or source.get("new_ledger_sha256") != source["current_ledger_sha256"]
        or proof.get("history_complete_for_requested_surfaces") is not True
        or proof.get("opening_checkpoint_used_as_bridge") is not False
        or proof.get("current_ledger_bound") is not True
        or type(proof.get("chunk_count")) is not int
        or proof["chunk_count"] < 1
        or candidate.source_receipts_sha256 != _facade_digest(source)
        or candidate.source_evidence_sha256 != (evidence.evidence_sha256,)
        or candidate.expected_digests != expected_digests
        or candidate.account_scope_sha256 != legacy_expected.get("account_scope_sha256")
        or candidate.local_execution_ledger_sha256 != source.get("current_ledger_sha256")
        or candidate.platform_id != runtime_target.platform_id
        or candidate.strategy_profile != runtime_target.strategy_profile
        or candidate.baseline_id != runtime_target.live_continuity.baseline_id
        or candidate.baseline_target_sha256
        != runtime_target.live_continuity.baseline_target_sha256
        or evidence.account_scope_sha256 != legacy_expected.get("account_scope_sha256")
        or evidence.platform_id != runtime_target.platform_id
        or evidence.strategy_profile != runtime_target.strategy_profile
        or evidence.baseline_id != runtime_target.live_continuity.baseline_id
        or evidence.baseline_target_sha256
        != runtime_target.live_continuity.baseline_target_sha256
        or evidence.runtime_target_sha256
        != runtime_target.live_continuity.baseline_target_sha256
        or evidence.broker_connected is not True
        or evidence.account_identity_match is not True
        or evidence.positions_match is not True
        or evidence.cash_match is not True
        or evidence.open_orders_match is not True
        or evidence.recent_executions_match is not True
        or evidence.local_execution_ledger_match is not True
        or first_at != observed_at
        or last_at != observed_at
        or not evidence_digest_ok
    ):
        raise ValueError("historical_continuity_source_binding_mismatch")
    archive_document = source.get("archive_document")
    migration_run = source.get("migration_run")
    if archive_document == PROSPECTIVE_ARCHIVE_DOCUMENT:
        if (
            migration_run.get("id") != _facade_const('PROSPECTIVE_MIGRATION_RUN_ID', PROSPECTIVE_MIGRATION_RUN_ID)
            or migration_run.get("head_sha") != _facade_const('PROSPECTIVE_MIGRATION_RUN_SHA', PROSPECTIVE_MIGRATION_RUN_SHA)
            or source.get("archive_sha256") != _facade_const('PROSPECTIVE_ARCHIVE_SHA256', PROSPECTIVE_ARCHIVE_SHA256)
        ):
            raise ValueError("historical_continuity_source_binding_mismatch")
    elif is_prospective_archive_document(archive_document):
        try:
            expected_document = prospective_archive_document(str(migration_run["id"]))
        except Exception as exc:
            raise ValueError("historical_continuity_source_binding_mismatch") from exc
        if (
            expected_document != archive_document
            or not isinstance(migration_run.get("head_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", migration_run["head_sha"])
        ):
            raise ValueError("historical_continuity_source_binding_mismatch")
    else:
        raise ValueError("historical_continuity_source_binding_mismatch")
    if require_fresh:
        reference = now or datetime.now(timezone.utc)
        if not (observed_at <= reference <= observed_at + timedelta(minutes=30)):
            raise ValueError("historical_continuity_candidate_stale")
        evaluation = evaluate_broker_reconciliation_baseline_enrollment(
            (evidence,), source_receipts_sha256=_facade_digest(source), now=reference
        )
        if evaluation.candidate is None or evaluation.candidate.to_dict() != candidate.to_dict():
            raise ValueError("historical_continuity_candidate_source_mismatch")
    else:
        if (
            candidate.platform_id != evidence.platform_id
            or candidate.strategy_profile != evidence.strategy_profile
            or candidate.baseline_id != evidence.baseline_id
            or candidate.baseline_target_sha256 != evidence.baseline_target_sha256
            or candidate.account_scope_sha256 != evidence.account_scope_sha256
        ):
            raise ValueError("historical_continuity_candidate_source_mismatch")
    return candidate

