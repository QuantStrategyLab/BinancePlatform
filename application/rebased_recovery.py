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

from application.broker_reconciliation import (
    collect_read_only_reconciliation_observations,
    collect_spot_usdt_external_cash_flows,
    diagnose_balance_flows,
)
from application.earn_accrual import (
    EarnCheckpointUnavailable,
    collect_earn_checkpoint,
    compare_earn_checkpoints,
    prepare_forward_earn_state,
)
from application.reconciliation_recovery import _validate_frozen_target
from scripts.migrate_daily_accounting_state import (
    APPROVED_REBASE_BALANCES_SHA256 as APPROVED_OPENING_BALANCES_SHA256,
    APPROVED_REBASE_CONTROL_SHA256 as APPROVED_OLD_CONTROL_SHA256,
    APPROVED_REBASE_LEDGER_SHA256 as APPROVED_OLD_LEDGER_SHA256,
    APPROVED_PROSPECTIVE_SHA256,
    PROSPECTIVE_ARCHIVE_DOCUMENT,
    REBASE_ARCHIVE_DOCUMENT as ARCHIVE_DOCUMENT,
)


SOURCE_KIND = "post_rebase"
APPROVED_PROPOSAL_RUN_ID = "34601984051"
MIGRATION_RUN_ID = 34606795875
MIGRATION_RUN_SHA = "ed7ee6e96cea0addb292f3f45338652095d0da58"
QUANTITY_PRECISION = 8
MAX_HISTORY = timedelta(days=7)

# Exact roots from the user-approved prospective migration.  This separate
# path is diagnosis-only; the existing recovery candidate and activation path
# remains bound to the older rebase above.
PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID = "34690028846"
PROSPECTIVE_MIGRATION_RUN_ID = 34690695663
PROSPECTIVE_MIGRATION_RUN_SHA = "a3ef5660e6d25fcfd5a7dedd10536a32eedde203"
PROSPECTIVE_LEDGER_SHA256 = "8c02ec9b5aa716edbffed7e98eb67bbdbf2b951d09588652478564d5e82c63b1"
PROSPECTIVE_ARCHIVE_SHA256 = "375a38c47bf47e64ae16f6a7ba0ea6e97a70ec48d9a361264ce9ea27693fcce5"

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


def validate_prospective_rebase_material(
    ledger: Mapping[str, object],
    archive: Mapping[str, object],
) -> dict[str, object]:
    """Validate the exact new opening without enrolling it for recovery."""
    if (
        not isinstance(ledger, Mapping)
        or not isinstance(archive, Mapping)
        or digest(archive) != PROSPECTIVE_ARCHIVE_SHA256
    ):
        raise ValueError("prospective_rebase_archive_invalid")
    if digest(ledger) != PROSPECTIVE_LEDGER_SHA256:
        raise ValueError("prospective_rebase_ledger_invalid")

    old_ledger = archive.get("ledger")
    archived_control = archive.get("recovery_control")
    approved = archive.get("approved_proposal")
    proposed = approved.get("proposed_fields") if isinstance(approved, Mapping) else None
    marker = ledger.get("accounting_rebase")
    expected_marker = {
        key: archive.get(key)
        for key in (
            "archive_document",
            "started_at",
            "opening_balance_observed_at",
            "historical_difference_unresolved",
            "approved_proposal_run_id",
            "approved_proposal_sha256",
        )
    }
    if (
        not isinstance(old_ledger, Mapping)
        or not isinstance(archived_control, Mapping)
        or not isinstance(approved, Mapping)
        or not isinstance(proposed, Mapping)
        or not isinstance(marker, Mapping)
        or digest(approved) != APPROVED_PROSPECTIVE_SHA256
        or approved.get("historical_difference_unresolved") is not True
        or approved.get("ledger_sha256") != digest(old_ledger)
        or approved.get("control_sha256") != digest(archived_control)
        or archive.get("archive_document") != PROSPECTIVE_ARCHIVE_DOCUMENT
        or archive.get("approved_proposal_run_id") != PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID
        or archive.get("approved_proposal_sha256") != APPROVED_PROSPECTIVE_SHA256
        or archive.get("historical_difference_unresolved") is not True
        or archive.get("new_ledger_sha256") != PROSPECTIVE_LEDGER_SHA256
        or archive.get("valuation_price_source") != "binance_get_avg_price_estimate"
    ):
        raise ValueError("prospective_rebase_archive_invalid")
    if (
        dict(marker) != expected_marker
        or marker.get("opening_balance_observed_at")
        != proposed.get("earn_accrual_checkpoint", {}).get("observed_at")
        or ledger != {**old_ledger, **proposed, "accounting_rebase": expected_marker}
        or ledger.get("order_submission", {}).get("state") not in {"RESERVED", "TERMINAL"}
    ):
        raise ValueError("prospective_rebase_ledger_invalid")
    return {
        "opening_at": _utc(marker["opening_balance_observed_at"]),
        "managed_assets": tuple(proposed["earn_accrual_checkpoint"]["assets"]),
        "archived_control": archived_control,
    }


def _same_cash_flow_slice(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    keys = (
        "new_deposit_principal_usdt",
        "new_confirmed_deposit_count",
        "new_deposit_completed_at",
        "new_unsupported_deposit_count",
        "new_or_changed_withdrawal_count",
    )
    return all(first.get(key) == second.get(key) for key in keys) and (
        first.get("cursor", {}).get("records") == second.get("cursor", {}).get("records")
    )


def _prospective_spot_snapshot(
    account: Mapping[str, object],
) -> tuple[str, dict[str, tuple[Decimal, Decimal]], str]:
    try:
        uid = account["uid"]
        rows = account["balances"]
        if (
            not isinstance(uid, (str, int))
            or isinstance(uid, bool)
            or not str(uid)
            or not isinstance(rows, list)
            or len(rows) > 5000
        ):
            raise ValueError
        spot = {}
        canonical = []
        for row in rows:
            asset = row["asset"]
            if (
                not isinstance(row, Mapping)
                or not isinstance(asset, str)
                or not 0 < len(asset) <= 128
                or not asset.isprintable()
                or any(char.isspace() for char in asset)
                or asset in spot
            ):
                raise ValueError
            free, locked = _amount(row["free"]), _amount(row["locked"])
            if locked:
                raise ValueError
            spot[asset] = (free, locked)
            canonical.append(
                {"asset": asset, "free": format(free, "f"), "locked": format(locked, "f")}
            )
    except (KeyError, TypeError, ValueError):
        raise ValueError("prospective_rebase_spot_unverified") from None
    return (
        digest({"account_uid": str(uid)}),
        spot,
        digest(sorted(canonical, key=lambda row: row["asset"])),
    )


def _collect_prospective_rebase_private(
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
    """Collect private forward proof and stable Spot evidence in memory."""
    _validate_frozen_target(runtime_target)
    material = validate_prospective_rebase_material(ledger, archive)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    opening_at = material["opening_at"]
    if not opening_at < observed_at or observed_at - opening_at > MAX_HISTORY:
        raise ValueError("prospective_rebase_history_window_invalid")
    if not _valid_run(source_run):
        raise ValueError("prospective_rebase_source_run_invalid")
    if not _valid_run(migration_run) or (
        migration_run["id"] != PROSPECTIVE_MIGRATION_RUN_ID
        or migration_run["head_sha"] != PROSPECTIVE_MIGRATION_RUN_SHA
    ):
        raise ValueError("prospective_rebase_migration_run_invalid")

    assets = material["managed_assets"]
    expected_scope = legacy_expected.get("account_scope_sha256")
    try:
        first = collect_earn_checkpoint(
            client,
            assets=assets,
            observed_at=observed_at,
            expected_account_scope_sha256=expected_scope,
        )
    except EarnCheckpointUnavailable:
        raise ValueError("prospective_rebase_checkpoint_unavailable") from None
    try:
        first_flows = collect_spot_usdt_external_cash_flows(
            client,
            now=observed_at,
            cursor=ledger.get("external_cash_flow_cursor"),
        )
    except ValueError:
        raise ValueError("prospective_rebase_cash_flow_unverified") from None

    final_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if not observed_at < final_at <= observed_at + timedelta(minutes=2):
        raise ValueError("prospective_rebase_observation_window_invalid")
    try:
        second = collect_earn_checkpoint(
            client,
            assets=assets,
            observed_at=final_at,
            expected_account_scope_sha256=expected_scope,
        )
        second_flows = collect_spot_usdt_external_cash_flows(
            client,
            now=final_at,
            cursor=ledger.get("external_cash_flow_cursor"),
        )
    except EarnCheckpointUnavailable:
        raise ValueError("prospective_rebase_checkpoint_unavailable") from None
    except ValueError:
        raise ValueError("prospective_rebase_cash_flow_unverified") from None
    if not _same_cash_flow_slice(first_flows, second_flows):
        raise ValueError("prospective_rebase_cash_flow_changed_during_read")

    try:
        first_state = prepare_forward_earn_state(ledger, first, first_flows)
        second_state = prepare_forward_earn_state(ledger, second, second_flows)
        compare_earn_checkpoints(
            first,
            second,
            verified_net_changes={asset: "0" for asset in assets},
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("prospective_rebase_conservation_unverified") from None
    if (
        first_state.get("earn_accrual_checkpoint") != first
        or second_state.get("earn_accrual_checkpoint") != second
        or digest(ledger) != PROSPECTIVE_LEDGER_SHA256
    ):
        raise ValueError("prospective_rebase_conservation_unverified")

    try:
        final_account = client.get_account()
        final_scope, final_spot, final_spot_sha256 = _prospective_spot_snapshot(final_account)
        for asset, row in second["assets"].items():
            if asset not in final_spot or final_spot[asset] != (
                _amount(row["spot_free"]),
                _amount(row["spot_locked"]),
            ):
                raise ValueError("spot changed")
        observations = collect_read_only_reconciliation_observations(
            client,
            strategy_symbols=symbols,
            local_execution_ledger=ledger,
            now=final_at,
            lookback=final_at - opening_at,
            account_snapshot=final_account,
        )
        after_scope, _after_spot, after_spot_sha256 = _prospective_spot_snapshot(
            client.get_account()
        )
    except Exception:
        raise ValueError("prospective_rebase_spot_changed_during_read") from None
    if (
        final_scope != expected_scope
        or after_scope != expected_scope
        or digest(observations.account_scope) != expected_scope
    ):
        raise ValueError("prospective_rebase_account_identity_mismatch")
    if final_spot_sha256 != after_spot_sha256:
        raise ValueError("prospective_rebase_spot_changed_during_read")
    if observations.open_orders:
        raise ValueError("prospective_rebase_open_orders_present")
    if observations.recent_executions:
        raise ValueError("prospective_rebase_recent_executions_present")
    return {
        "observed_at": final_at,
        "observations": observations,
        "proof": {
            "forward_accounting_conserved": True,
            "checkpoint_samples": 2,
            "whole_spot_double_read_match": True,
            "spot_sha256": digest(observations.positions),
            "first_checkpoint_sha256": digest(first),
            "final_checkpoint_sha256": digest(second),
            "cash_flow_slice_sha256": digest(
                {
                    key: second_flows[key]
                    for key in (
                        "new_deposit_principal_usdt",
                        "new_confirmed_deposit_count",
                        "new_deposit_completed_at",
                        "new_unsupported_deposit_count",
                        "new_or_changed_withdrawal_count",
                        "cursor",
                    )
                }
            ),
        },
    }


def collect_prospective_rebase_diagnosis(**kwargs) -> dict[str, object]:
    """Return only a sanitized summary of the private forward proof."""
    _collect_prospective_rebase_private(**kwargs)
    return {
        "status": "diagnosed",
        "source_kind": "prospective_rebase",
        "historical_difference_unresolved": True,
        "forward_accounting_conserved": True,
        "checkpoint_samples": 2,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }


def collect_prospective_rebase_source(**kwargs) -> dict[str, object]:
    """Enroll the exact prospective opening after independent forward proof."""
    private = _collect_prospective_rebase_private(**kwargs)
    runtime_target = kwargs["runtime_target"]
    legacy_expected = kwargs["legacy_expected"]
    ledger = kwargs["ledger"]
    archive = kwargs["archive"]
    symbols = kwargs["symbols"]
    source_run = kwargs["source_run"]
    migration_run = kwargs["migration_run"]
    observed_at = private["observed_at"]
    observations = private["observations"]
    reconciled = _evidence(
        runtime_target=runtime_target,
        account_scope_sha256=legacy_expected["account_scope_sha256"],
        observations=observations,
        observed_at=observed_at,
    )
    source = {
        "kind": "prospective_rebase",
        "run": dict(source_run),
        "migration_run": dict(migration_run),
        "archive_document": PROSPECTIVE_ARCHIVE_DOCUMENT,
        "archive_sha256": digest(archive),
        "new_ledger_sha256": digest(ledger),
        "frozen_expected_sha256": digest(legacy_expected),
        "runtime_target_sha256": digest(runtime_target.to_dict()),
        "symbols_sha256": digest(list(symbols)),
        "historical_difference_unresolved": True,
        "reconciled_evidence": reconciled.to_dict(),
        "proof": private["proof"],
    }
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        (reconciled,), source_receipts_sha256=digest(source), now=observed_at
    )
    if evaluation.candidate is None:
        raise ValueError("prospective_rebase_enrollment_blocked")
    return {"candidate": evaluation.candidate.to_dict(), "source": source}


def validate_prospective_rebase_source(
    package: Mapping[str, object],
    *,
    runtime_target: object,
    legacy_expected: Mapping[str, str],
    now: datetime | None = None,
    require_fresh: bool = True,
) -> BrokerReconciliationBaselineCandidate:
    """Strictly validate a stored prospective package without private values."""
    _validate_frozen_target(runtime_target)
    try:
        source = package["source"]
        proof = source["proof"]
        candidate = BrokerReconciliationBaselineCandidate.from_dict(package["candidate"])
        evidence = BrokerReconciliationEvidence.from_dict(source["reconciled_evidence"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("prospective_rebase_source_binding_mismatch") from None
    if not isinstance(source, Mapping) or not isinstance(proof, Mapping):
        raise ValueError("prospective_rebase_source_binding_mismatch")
    source_keys = {
        "kind", "run", "migration_run", "archive_document", "archive_sha256",
        "new_ledger_sha256", "frozen_expected_sha256", "runtime_target_sha256",
        "symbols_sha256", "historical_difference_unresolved", "reconciled_evidence", "proof",
    }
    proof_keys = {
        "forward_accounting_conserved", "checkpoint_samples",
        "whole_spot_double_read_match", "spot_sha256", "first_checkpoint_sha256",
        "final_checkpoint_sha256", "cash_flow_slice_sha256",
    }
    sha_values = (
        source.get("archive_sha256"), source.get("new_ledger_sha256"),
        source.get("frozen_expected_sha256"), source.get("runtime_target_sha256"),
        source.get("symbols_sha256"), proof.get("spot_sha256"),
        proof.get("first_checkpoint_sha256"), proof.get("final_checkpoint_sha256"),
        proof.get("cash_flow_slice_sha256"),
    )
    expected_digests = {
        "positions_sha256": evidence.positions_sha256,
        "cash_sha256": evidence.cash_sha256,
        "open_orders_sha256": evidence.open_orders_sha256,
        "recent_executions_sha256": evidence.recent_executions_sha256,
        "local_execution_ledger_sha256": evidence.local_execution_ledger_sha256,
    }
    if (
        set(source) != source_keys
        or set(proof) != proof_keys
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in sha_values
        )
        or source.get("kind") != "prospective_rebase"
        or not _valid_run(source.get("run"))
        or not _valid_run(source.get("migration_run"))
        or source["migration_run"].get("id") != PROSPECTIVE_MIGRATION_RUN_ID
        or source["migration_run"].get("head_sha") != PROSPECTIVE_MIGRATION_RUN_SHA
        or source.get("archive_document") != PROSPECTIVE_ARCHIVE_DOCUMENT
        or source.get("archive_sha256") != PROSPECTIVE_ARCHIVE_SHA256
        or source.get("new_ledger_sha256") != PROSPECTIVE_LEDGER_SHA256
        or source.get("frozen_expected_sha256") != digest(legacy_expected)
        or source.get("runtime_target_sha256") != digest(runtime_target.to_dict())
        or source.get("historical_difference_unresolved") is not True
        or proof.get("forward_accounting_conserved") is not True
        or proof.get("checkpoint_samples") != 2
        or proof.get("whole_spot_double_read_match") is not True
        or proof.get("spot_sha256") != evidence.positions_sha256
        or candidate.source_receipts_sha256 != digest(source)
        or candidate.source_evidence_sha256 != (evidence.evidence_sha256,)
        or candidate.expected_digests != expected_digests
        or candidate.account_scope_sha256 != legacy_expected.get("account_scope_sha256")
        or candidate.local_execution_ledger_sha256 != PROSPECTIVE_LEDGER_SHA256
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
        or evidence.positions_match is not True
        or evidence.cash_match is not True
        or evidence.open_orders_match is not True
        or evidence.recent_executions_match is not True
        or evidence.local_execution_ledger_match is not True
    ):
        raise ValueError("prospective_rebase_source_binding_mismatch")
    if require_fresh:
        evaluated = evaluate_broker_reconciliation_baseline_enrollment(
            (evidence,), source_receipts_sha256=digest(source), now=now
        )
        if evaluated.candidate is None or evaluated.candidate.to_dict() != candidate.to_dict():
            raise ValueError("prospective_rebase_candidate_source_mismatch")
    return candidate


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
