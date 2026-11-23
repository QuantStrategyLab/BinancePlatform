"""Post-rebase material validation and enrollment collection."""
from __future__ import annotations

from binance_platform.application._rebased_shared import (
    ARCHIVE_DOCUMENT,
    APPROVED_OLD_CONTROL_SHA256,
    APPROVED_OLD_LEDGER_SHA256,
    APPROVED_OPENING_BALANCES_SHA256,
    APPROVED_PROPOSAL_RUN_ID,
    BrokerReconciliationBaselineCandidate,
    BrokerReconciliationEvidence,
    Decimal,
    MAX_HISTORY,
    MIGRATION_RUN_ID,
    MIGRATION_RUN_SHA,
    Mapping,
    QUANTITY_PRECISION,
    SOURCE_KIND,
    Sequence,
    _REBASED_LEDGER_FIELDS,
    _amount,
    _evidence,
    _facade_const,
    _facade_digest,
    _quantity,
    _utc,
    _valid_run,
    _validate_frozen_target,
    datetime,
    diagnose_balance_flows,
    evaluate_broker_reconciliation_baseline_enrollment,
    timezone,
)

def validate_post_rebase_material(
    ledger: Mapping[str, object], archive: Mapping[str, object]
) -> dict[str, object]:
    """Validate private archive/current-ledger material against approved roots."""
    if not isinstance(ledger, Mapping) or not isinstance(archive, Mapping):
        raise ValueError("post_rebase_archive_invalid")
    old_ledger = archive.get("ledger")
    old_control = archive.get("recovery_control")
    if (
        not isinstance(old_ledger, Mapping)
        or not isinstance(old_control, Mapping)
        or _facade_digest(old_ledger) != _facade_const('APPROVED_OLD_LEDGER_SHA256', APPROVED_OLD_LEDGER_SHA256)
        or _facade_digest(old_control) != _facade_const('APPROVED_OLD_CONTROL_SHA256', APPROVED_OLD_CONTROL_SHA256)
        or archive.get("archive_document") != ARCHIVE_DOCUMENT
        or archive.get("approved_proposal_run_id") != _facade_const('APPROVED_PROPOSAL_RUN_ID', APPROVED_PROPOSAL_RUN_ID)
        or archive.get("historical_difference_unresolved") is not True
        or archive.get("valuation_price_source") != "binance_get_avg_price_estimate"
    ):
        raise ValueError("post_rebase_archive_invalid")

    expected_new_digest = archive.get("new_ledger_sha256")
    marker = ledger.get("accounting_rebase")
    if (
        not isinstance(expected_new_digest, str)
        or len(expected_new_digest) != 64
        or _facade_digest(ledger) != expected_new_digest
        or not isinstance(marker, Mapping)
        or marker.get("archive_document") != ARCHIVE_DOCUMENT
        or marker.get("approved_proposal_run_id") != _facade_const('APPROVED_PROPOSAL_RUN_ID', APPROVED_PROPOSAL_RUN_ID)
        or marker.get("historical_difference_unresolved") is not True
        or any(marker.get(key) != archive.get(key) for key in (
            "archive_document",
            "started_at",
            "opening_balance_observed_at",
            "historical_difference_unresolved",
            "approved_proposal_run_id",
        ))
    ):
        raise ValueError("post_rebase_ledger_invalid")
    if ledger.get("order_submission", {}).get("state") not in {"RESERVED", "TERMINAL"}:
        raise ValueError("post_rebase_order_state_unsafe")
    if {
        key: value for key, value in ledger.items() if key not in _REBASED_LEDGER_FIELDS
    } != {
        key: value for key, value in old_ledger.items() if key not in _REBASED_LEDGER_FIELDS
    }:
        raise ValueError("post_rebase_ledger_invalid")

    raw_opening = ledger.get("last_balance_snapshot")
    if (
        not isinstance(raw_opening, Mapping)
        or not raw_opening
        or any(not isinstance(asset, str) or not asset for asset in raw_opening)
    ):
        raise ValueError("post_rebase_ledger_invalid")
    opening = {asset.upper(): _quantity(_amount(value)) for asset, value in raw_opening.items()}
    if len(opening) != len(raw_opening) or _facade_digest(opening) != _facade_const('APPROVED_OPENING_BALANCES_SHA256', APPROVED_OPENING_BALANCES_SHA256):
        raise ValueError("post_rebase_ledger_invalid")
    return {
        "new_ledger_sha256": expected_new_digest,
        "archive_sha256": _facade_digest(archive),
        "opening_balance_observed_at": _utc(marker["opening_balance_observed_at"]),
        "opening_quantities": opening,
    }


def _strict_managed_quantities(
    client: object,
    account: Mapping[str, object],
    managed_assets: Sequence[str],
    *, observe_only_non_managed_spot: bool = False,
) -> tuple[dict[str, float], str, int]:
    raw_rows = account.get("balances") if isinstance(account, Mapping) else None
    if not isinstance(raw_rows, list) or any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("post_rebase_balance_amount_invalid")
    spot: dict[str, tuple[Decimal, Decimal]] = {}
    canonical_spot = []
    for row in raw_rows:
        asset = str(row.get("asset") or "").strip().upper()
        if not asset or asset in spot:
            raise ValueError("post_rebase_balance_amount_invalid")
        free, locked = _amount(row.get("free")), _amount(row.get("locked"))
        if locked != 0:
            raise ValueError("post_rebase_locked_balance_present")
        spot[asset] = (free, locked)
        canonical_spot.append({"asset": asset, "free": str(free), "locked": str(locked)})
    managed = tuple(dict.fromkeys(str(asset).upper() for asset in managed_assets))
    observed_count = sum(asset not in managed and free != 0 for asset, (free, _locked) in spot.items())
    if observed_count and not observe_only_non_managed_spot:
        raise ValueError("post_rebase_unknown_spot_balance")
    if any(asset not in spot for asset in managed):
        raise ValueError("post_rebase_quantity_mismatch")

    totals = {}
    for asset in managed:
        try:
            response = client.get_simple_earn_flexible_product_position(asset=asset, current=1, size=100)
        except Exception as exc:
            raise ValueError("post_rebase_earn_read_failed") from exc
        rows = response.get("rows") if isinstance(response, Mapping) else None
        total = response.get("total") if isinstance(response, Mapping) else None
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, Mapping) for row in rows)
            or type(total) is not int
            or total != len(rows)
            or len(rows) >= 100
        ):
            raise ValueError("post_rebase_earn_page_incomplete")
        earn = Decimal(0)
        for row in rows:
            if str(row.get("asset") or "").strip().upper() != asset:
                raise ValueError("post_rebase_earn_page_incomplete")
            earn += _amount(row.get("totalAmount"))
        totals[asset] = _quantity(spot[asset][0] + earn)
    return totals, _facade_digest(sorted(canonical_spot, key=lambda row: row["asset"])), observed_count


def collect_post_rebase_source(
    *, client: object, runtime_target: object, legacy_expected: Mapping[str, str],
    ledger: Mapping[str, object], archive: Mapping[str, object], symbols: Sequence[str],
    source_run: Mapping[str, object], migration_run: Mapping[str, object], now: datetime | None = None,
    observe_only_non_managed_spot: bool = False,
) -> dict[str, object]:
    """Collect a fresh, zero-activity candidate for the approved new opening."""
    from binance_platform.application.broker_reconciliation import collect_read_only_reconciliation_observations

    _validate_frozen_target(runtime_target)
    material = validate_post_rebase_material(ledger, archive)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    opening_at = material["opening_balance_observed_at"]
    if not opening_at < observed_at or observed_at - opening_at > _facade_const('MAX_HISTORY', MAX_HISTORY):
        raise ValueError("post_rebase_history_window_invalid")
    if not _valid_run(source_run):
        raise ValueError("post_rebase_source_run_invalid")
    if not _valid_run(migration_run) or (
        migration_run["id"] != _facade_const('MIGRATION_RUN_ID', MIGRATION_RUN_ID) or migration_run["head_sha"] != _facade_const('MIGRATION_RUN_SHA', MIGRATION_RUN_SHA)
    ):
        raise ValueError("post_rebase_migration_run_invalid")

    account = client.get_account()
    lookback = observed_at - opening_at
    observations = collect_read_only_reconciliation_observations(
        client,
        strategy_symbols=symbols,
        local_execution_ledger=ledger,
        now=observed_at,
        lookback=lookback,
        account_snapshot=account,
    )
    if _facade_digest(observations.account_scope) != legacy_expected.get("account_scope_sha256"):
        raise ValueError("post_rebase_account_identity_mismatch")
    if observations.open_orders:
        raise ValueError("post_rebase_open_orders_present")
    if observations.recent_executions:
        raise ValueError("post_rebase_recent_executions_present")
    proof = diagnose_balance_flows(client, start=opening_at, end=observed_at, now=observed_at)
    if proof.get("history_complete_for_requested_surfaces") is not True:
        raise ValueError("post_rebase_history_incomplete")
    counts = proof.get("history_counts")
    if not isinstance(counts, Mapping) or any(
        value != 0 for name, value in counts.items() if name != "earn_rewards"
    ):
        raise ValueError("post_rebase_non_reward_activity_present")

    managed_assets = tuple(material["opening_quantities"])
    first_quantities, first_spot_sha256, observed_count = _strict_managed_quantities(
        client, account, managed_assets, observe_only_non_managed_spot=observe_only_non_managed_spot,
    )
    second = client.get_account()
    if _facade_digest({"account_uid": str(second.get("uid") or "")}) != legacy_expected.get("account_scope_sha256"):
        raise ValueError("post_rebase_account_identity_mismatch")
    second_quantities, second_spot_sha256, _ = _strict_managed_quantities(
        client, second, managed_assets, observe_only_non_managed_spot=observe_only_non_managed_spot,
    )
    if first_quantities != second_quantities or first_spot_sha256 != second_spot_sha256:
        raise ValueError("post_rebase_quantity_changed_during_read")
    if first_quantities != material["opening_quantities"]:
        raise ValueError("post_rebase_quantity_mismatch")

    reconciled = _evidence(
        runtime_target=runtime_target,
        account_scope_sha256=legacy_expected["account_scope_sha256"],
        observations=observations,
        observed_at=observed_at,
    )
    safe_proof = {
        "history_complete_for_requested_surfaces": True,
        "history_counts": dict(counts),
        "quantity_double_read_match": True,
        "managed_asset_count": len(managed_assets),
        "non_managed_spot_policy": "observe_only" if observe_only_non_managed_spot else "reject",
        "observed_non_managed_asset_count": observed_count,
        "earn_rewards_observed": counts.get("earn_rewards", 0),
    }
    source = {
        "kind": SOURCE_KIND,
        "run": dict(source_run),
        "migration_run": dict(migration_run),
        "archive_document": ARCHIVE_DOCUMENT,
        "archive_sha256": material["archive_sha256"],
        "new_ledger_sha256": material["new_ledger_sha256"],
        "frozen_expected_sha256": _facade_digest(legacy_expected),
        "runtime_target_sha256": _facade_digest(runtime_target.to_dict()),
        "symbols_sha256": _facade_digest(list(symbols)),
        "quantity_precision": QUANTITY_PRECISION,
        "historical_difference_unresolved": True,
        "opening_balance_observed_at": opening_at.isoformat(),
        "reconciled_evidence": reconciled.to_dict(),
        "proof": safe_proof,
    }
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        (reconciled,), source_receipts_sha256=_facade_digest(source), now=observed_at
    )
    if evaluation.candidate is None:
        raise ValueError("post_rebase_enrollment_blocked")
    return {"candidate": evaluation.candidate.to_dict(), "source": source}


def validate_post_rebase_source(
    package: Mapping[str, object], *, runtime_target: object,
    legacy_expected: Mapping[str, str], now: datetime | None = None, require_fresh: bool = True,
) -> BrokerReconciliationBaselineCandidate:
    """Revalidate a sanitized stored package; private archive checks happen separately."""
    _validate_frozen_target(runtime_target)
    try:
        source = package["source"]
        candidate = BrokerReconciliationBaselineCandidate.from_dict(package["candidate"])
        evidence = BrokerReconciliationEvidence.from_dict(source["reconciled_evidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("post_rebase_source_binding_mismatch") from exc
    if (
        not isinstance(source, Mapping)
        or source.get("kind") != SOURCE_KIND
        or source.get("migration_run", {}).get("id") != _facade_const('MIGRATION_RUN_ID', MIGRATION_RUN_ID)
        or source.get("migration_run", {}).get("head_sha") != _facade_const('MIGRATION_RUN_SHA', MIGRATION_RUN_SHA)
        or not _valid_run(source.get("migration_run"))
        or not _valid_run(source.get("run"))
        or source.get("archive_document") != ARCHIVE_DOCUMENT
        or source.get("historical_difference_unresolved") is not True
        or source.get("quantity_precision") != QUANTITY_PRECISION
        or source.get("frozen_expected_sha256") != _facade_digest(legacy_expected)
        or source.get("runtime_target_sha256") != _facade_digest(runtime_target.to_dict())
        or candidate.source_receipts_sha256 != _facade_digest(source)
        or candidate.account_scope_sha256 != legacy_expected.get("account_scope_sha256")
        or candidate.local_execution_ledger_sha256 != source.get("new_ledger_sha256")
        or source.get("proof", {}).get("history_complete_for_requested_surfaces") is not True
        or source.get("proof", {}).get("quantity_double_read_match") is not True
        or source.get("proof", {}).get("non_managed_spot_policy", "reject") not in {"reject", "observe_only"}
        or type(source.get("proof", {}).get("observed_non_managed_asset_count", 0)) is not int
        or source.get("proof", {}).get("observed_non_managed_asset_count", 0) < 0
        or (
            source.get("proof", {}).get("non_managed_spot_policy", "reject") == "reject"
            and source.get("proof", {}).get("observed_non_managed_asset_count", 0) != 0
        )
        or not isinstance(source.get("proof", {}).get("history_counts"), Mapping)
        or any(
            type(value) is not int or value < 0
            for value in source.get("proof", {}).get("history_counts", {}).values()
        )
        or any(
            value != 0
            for name, value in source.get("proof", {}).get("history_counts", {}).items()
            if name != "earn_rewards"
        )
        or candidate.platform_id != runtime_target.platform_id
        or candidate.strategy_profile != runtime_target.strategy_profile
        or candidate.baseline_id != runtime_target.live_continuity.baseline_id
        or candidate.baseline_target_sha256 != runtime_target.live_continuity.baseline_target_sha256
        or evidence.platform_id != runtime_target.platform_id
        or evidence.strategy_profile != runtime_target.strategy_profile
        or evidence.account_scope_sha256 != legacy_expected.get("account_scope_sha256")
        or evidence.baseline_id != runtime_target.live_continuity.baseline_id
        or evidence.baseline_target_sha256 != runtime_target.live_continuity.baseline_target_sha256
        or evidence.runtime_target_sha256 != runtime_target.live_continuity.baseline_target_sha256
    ):
        raise ValueError("post_rebase_source_binding_mismatch")
    if require_fresh:
        result = evaluate_broker_reconciliation_baseline_enrollment(
            (evidence,), source_receipts_sha256=_facade_digest(source), now=now
        )
        if result.candidate is None or result.candidate.to_dict() != candidate.to_dict():
            raise ValueError("post_rebase_candidate_source_mismatch")
    else:
        expected = {
            "positions_sha256": evidence.positions_sha256,
            "cash_sha256": evidence.cash_sha256,
            "open_orders_sha256": evidence.open_orders_sha256,
            "recent_executions_sha256": evidence.recent_executions_sha256,
            "local_execution_ledger_sha256": evidence.local_execution_ledger_sha256,
        }
        if (
            candidate.source_evidence_sha256 != (evidence.evidence_sha256,)
            or candidate.expected_digests != expected
            or candidate.platform_id != evidence.platform_id
            or candidate.strategy_profile != evidence.strategy_profile
            or candidate.baseline_id != evidence.baseline_id
            or candidate.baseline_target_sha256 != evidence.baseline_target_sha256
        ):
            raise ValueError("post_rebase_candidate_source_mismatch")
    return candidate

