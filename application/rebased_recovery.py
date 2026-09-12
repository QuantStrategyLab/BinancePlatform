"""Private, read-only evidence for the approved Binance accounting rebase.

The module validates the immutable archive and the current ledger in memory,
then enrolls only an exact, stable managed-asset quantity snapshot.  Returned
packages contain hashes and bounded counters, never account rows or amounts.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

from quant_platform_kit.common.broker_reconciliation import (
    BrokerReconciliationEvidence,
    build_broker_reconciliation_evidence,
    calculate_broker_observation_sha256 as digest,
)
from quant_platform_kit.common.broker_reconciliation_enrollment import (
    BrokerReconciliationBaselineCandidate,
    evaluate_broker_reconciliation_baseline_enrollment,
)

from application.broker_reconciliation import diagnose_balance_flows
from application.reconciliation_recovery import _validate_frozen_target
from scripts.migrate_daily_accounting_state import (
    APPROVED_REBASE_BALANCES_SHA256 as APPROVED_OPENING_BALANCES_SHA256,
    APPROVED_REBASE_CONTROL_SHA256 as APPROVED_OLD_CONTROL_SHA256,
    APPROVED_REBASE_LEDGER_SHA256 as APPROVED_OLD_LEDGER_SHA256,
    REBASE_ARCHIVE_DOCUMENT as ARCHIVE_DOCUMENT,
)


SOURCE_KIND = "post_rebase"
APPROVED_PROPOSAL_RUN_ID = "34601984051"
MIGRATION_RUN_ID = 34606795875
MIGRATION_RUN_SHA = "ed7ee6e96cea0addb292f3f45338652095d0da58"
QUANTITY_PRECISION = 8
MAX_HISTORY = timedelta(days=7)

_REBASED_LEDGER_FIELDS = {
    "daily_trend_pnl_basis",
    "daily_trend_cash_flow_usdt",
    "daily_trend_net_invested_usdt",
    "daily_trend_risk_base_usdt",
    "daily_trend_third_fee_usdt",
    "last_balance_snapshot",
    "daily_equity_base",
    "daily_trend_equity_base",
    "last_reset_date",
    "accounting_rebase",
}
_RUN_KEYS = {"id", "head_sha", "head_branch", "event", "path"}


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("post_rebase_ledger_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("post_rebase_ledger_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("post_rebase_ledger_invalid")
    return parsed.astimezone(timezone.utc)


def _amount(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("post_rebase_balance_amount_invalid") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("post_rebase_balance_amount_invalid")
    return number


def _quantity(value: Decimal) -> float:
    with localcontext() as context:
        context.prec = 100
        return round(float(value), QUANTITY_PRECISION)


def _valid_run(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _RUN_KEYS
        and type(value.get("id")) is int
        and value.get("id", 0) > 0
        and isinstance(value.get("head_sha"), str)
        and len(value["head_sha"]) == 40
        and all(char in "0123456789abcdef" for char in value["head_sha"])
        and value.get("head_branch") == "main"
        and value.get("event") == "workflow_dispatch"
        and value.get("path") == ".github/workflows/main.yml"
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
        or digest(old_ledger) != APPROVED_OLD_LEDGER_SHA256
        or digest(old_control) != APPROVED_OLD_CONTROL_SHA256
        or archive.get("archive_document") != ARCHIVE_DOCUMENT
        or archive.get("approved_proposal_run_id") != APPROVED_PROPOSAL_RUN_ID
        or archive.get("historical_difference_unresolved") is not True
        or archive.get("valuation_price_source") != "binance_get_avg_price_estimate"
    ):
        raise ValueError("post_rebase_archive_invalid")

    expected_new_digest = archive.get("new_ledger_sha256")
    marker = ledger.get("accounting_rebase")
    if (
        not isinstance(expected_new_digest, str)
        or len(expected_new_digest) != 64
        or digest(ledger) != expected_new_digest
        or not isinstance(marker, Mapping)
        or marker.get("archive_document") != ARCHIVE_DOCUMENT
        or marker.get("approved_proposal_run_id") != APPROVED_PROPOSAL_RUN_ID
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
    if len(opening) != len(raw_opening) or digest(opening) != APPROVED_OPENING_BALANCES_SHA256:
        raise ValueError("post_rebase_ledger_invalid")
    return {
        "new_ledger_sha256": expected_new_digest,
        "archive_sha256": digest(archive),
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
    return totals, digest(sorted(canonical_spot, key=lambda row: row["asset"])), observed_count


def _evidence(
    *, runtime_target: object, account_scope_sha256: str, observations: object, observed_at: datetime
) -> BrokerReconciliationEvidence:
    continuity = runtime_target.live_continuity
    return build_broker_reconciliation_evidence(
        platform_id=runtime_target.platform_id,
        strategy_profile=runtime_target.strategy_profile,
        account_scope_sha256=account_scope_sha256,
        baseline_id=continuity.baseline_id,
        baseline_target_sha256=continuity.baseline_target_sha256,
        runtime_target_sha256=continuity.baseline_target_sha256,
        observed_at=observed_at,
        broker_connected=True,
        account_identity_match=True,
        positions_match=True,
        cash_match=True,
        open_orders_match=True,
        recent_executions_match=True,
        local_execution_ledger_match=True,
        positions_sha256=digest(observations.positions),
        cash_sha256=digest(observations.cash),
        open_orders_sha256=digest(observations.open_orders),
        recent_executions_sha256=digest(observations.recent_executions),
        local_execution_ledger_sha256=digest(observations.local_execution_ledger),
    )


def collect_post_rebase_source(
    *, client: object, runtime_target: object, legacy_expected: Mapping[str, str],
    ledger: Mapping[str, object], archive: Mapping[str, object], symbols: Sequence[str],
    source_run: Mapping[str, object], migration_run: Mapping[str, object], now: datetime | None = None,
    observe_only_non_managed_spot: bool = False,
) -> dict[str, object]:
    """Collect a fresh, zero-activity candidate for the approved new opening."""
    from application.broker_reconciliation import collect_read_only_reconciliation_observations

    _validate_frozen_target(runtime_target)
    material = validate_post_rebase_material(ledger, archive)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    opening_at = material["opening_balance_observed_at"]
    if not opening_at < observed_at or observed_at - opening_at > MAX_HISTORY:
        raise ValueError("post_rebase_history_window_invalid")
    if not _valid_run(source_run):
        raise ValueError("post_rebase_source_run_invalid")
    if not _valid_run(migration_run) or (
        migration_run["id"] != MIGRATION_RUN_ID or migration_run["head_sha"] != MIGRATION_RUN_SHA
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
    if digest(observations.account_scope) != legacy_expected.get("account_scope_sha256"):
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
    if digest({"account_uid": str(second.get("uid") or "")}) != legacy_expected.get("account_scope_sha256"):
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
        "frozen_expected_sha256": digest(legacy_expected),
        "runtime_target_sha256": digest(runtime_target.to_dict()),
        "symbols_sha256": digest(list(symbols)),
        "quantity_precision": QUANTITY_PRECISION,
        "historical_difference_unresolved": True,
        "opening_balance_observed_at": opening_at.isoformat(),
        "reconciled_evidence": reconciled.to_dict(),
        "proof": safe_proof,
    }
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        (reconciled,), source_receipts_sha256=digest(source), now=observed_at
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
        or source.get("migration_run", {}).get("id") != MIGRATION_RUN_ID
        or source.get("migration_run", {}).get("head_sha") != MIGRATION_RUN_SHA
        or not _valid_run(source.get("migration_run"))
        or not _valid_run(source.get("run"))
        or source.get("archive_document") != ARCHIVE_DOCUMENT
        or source.get("historical_difference_unresolved") is not True
        or source.get("quantity_precision") != QUANTITY_PRECISION
        or source.get("frozen_expected_sha256") != digest(legacy_expected)
        or source.get("runtime_target_sha256") != digest(runtime_target.to_dict())
        or candidate.source_receipts_sha256 != digest(source)
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
            (evidence,), source_receipts_sha256=digest(source), now=now
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
