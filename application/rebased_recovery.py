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
    collect_bnb_dividend_quantity,
    collect_read_only_reconciliation_observations,
    collect_spot_usdt_external_cash_flows,
    diagnose_balance_flows,
    diagnose_chunked_balance_flows,
    _add_reward_quantity,
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
HISTORICAL_CONTINUITY_KIND = "historical_continuity"
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
_DAILY_LOSS_SEMANTIC_FIELDS = frozenset({
    "daily_equity_base",
    "daily_trend_equity_base",
    "daily_trend_pnl_basis",
    "daily_trend_cash_flow_usdt",
    "daily_trend_net_invested_usdt",
    "daily_trend_risk_base_usdt",
    "daily_trend_third_fee_usdt",
    "last_reset_date",
})
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


def _exact_decimal_sum(values: Sequence[Decimal]) -> Decimal:
    """Sum Decimals by integer scaling; never rounds under a Decimal context cap."""
    parts: list[tuple[int, int]] = []
    for value in values:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("post_rebase_balance_amount_invalid")
        sign, digits, exp = value.as_tuple()
        if not isinstance(exp, int):
            raise ValueError("post_rebase_balance_amount_invalid")
        coeff = 0
        for digit in digits:
            coeff = coeff * 10 + int(digit)
        if sign:
            coeff = -coeff
        parts.append((coeff, exp))
    if not parts:
        return Decimal(0)
    min_exp = min(exp for _coeff, exp in parts)
    total = 0
    for coeff, exp in parts:
        total += coeff * (10 ** (exp - min_exp))
    if total == 0:
        return Decimal(0)
    negative = total < 0
    digit_tuple = tuple(int(char) for char in str(abs(total)))
    return Decimal((1 if negative else 0, digit_tuple, min_exp))


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
    raw_opening = ledger.get("last_balance_snapshot")
    if (
        not isinstance(raw_opening, Mapping)
        or not raw_opening
        or any(not isinstance(asset, str) or not asset for asset in raw_opening)
    ):
        raise ValueError("prospective_rebase_ledger_invalid")
    opening_quantities = {
        asset.upper(): _quantity(_amount(value)) for asset, value in raw_opening.items()
    }
    if len(opening_quantities) != len(raw_opening):
        raise ValueError("prospective_rebase_ledger_invalid")
    return {
        "opening_at": _utc(marker["opening_balance_observed_at"]),
        "opening_quantities": opening_quantities,
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
    try:
        first_dividends = collect_bnb_dividend_quantity(
            client, start=opening_at, end=observed_at
        ) if "BNB" in assets else {"quantity": Decimal(0), "identities": ()}
    except ValueError:
        raise ValueError("prospective_rebase_dividend_unverified") from None

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
    try:
        second_dividends = collect_bnb_dividend_quantity(
            client, start=opening_at, end=final_at
        ) if "BNB" in assets else {"quantity": Decimal(0), "identities": ()}
    except ValueError:
        raise ValueError("prospective_rebase_dividend_unverified") from None
    if not _same_cash_flow_slice(first_flows, second_flows):
        raise ValueError("prospective_rebase_cash_flow_changed_during_read")
    if (
        first_dividends.get("identities") != second_dividends.get("identities")
        or first_dividends.get("quantity") != second_dividends.get("quantity")
    ):
        raise ValueError("prospective_rebase_dividend_changed_during_read")
    first_flows = {**first_flows, "bnb_dividend_quantity": format(first_dividends["quantity"], "f")}
    second_flows = {**second_flows, "bnb_dividend_quantity": format(second_dividends["quantity"], "f")}

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


def validate_historical_continuity_material(
    ledger: Mapping[str, object],
    archive: Mapping[str, object],
) -> dict[str, object]:
    """Validate immutable opening archive; current ledger is verification target only."""
    if (
        not isinstance(ledger, Mapping)
        or not isinstance(archive, Mapping)
        or digest(archive) != PROSPECTIVE_ARCHIVE_SHA256
    ):
        raise ValueError("historical_continuity_archive_invalid")
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
        or dict(marker) != expected_marker
        or ledger.get("order_submission", {}).get("state") not in {"RESERVED", "TERMINAL"}
    ):
        raise ValueError("historical_continuity_archive_invalid")
    opening_checkpoint = proposed.get("earn_accrual_checkpoint")
    opening_snapshot = proposed.get("last_balance_snapshot")
    if not isinstance(opening_checkpoint, Mapping) or not isinstance(opening_snapshot, Mapping):
        raise ValueError("historical_continuity_archive_invalid")
    # Quantity-bearing ledger surfaces must still match the opening unless history explains them.
    # The earn checkpoint inside the current ledger is never a continuity bridge by itself.
    return {
        "opening_at": _utc(marker["opening_balance_observed_at"]),
        "opening_checkpoint": opening_checkpoint,
        "opening_snapshot": opening_snapshot,
        "opening_quantities": {
            asset.upper(): _amount(value) for asset, value in opening_snapshot.items()
        },
        "managed_assets": tuple(opening_checkpoint["assets"]),
        "current_ledger_sha256": digest(ledger),
        "archive_sha256": digest(archive),
        "archived_control": archived_control,
    }


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
    _validate_frozen_target(runtime_target)
    material = validate_historical_continuity_material(ledger, archive)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    opening_at = material["opening_at"]
    if not opening_at < observed_at:
        raise ValueError("historical_continuity_window_invalid")
    if not _valid_run(source_run):
        raise ValueError("historical_continuity_source_run_invalid")
    if not _valid_run(migration_run) or (
        migration_run["id"] != PROSPECTIVE_MIGRATION_RUN_ID
        or migration_run["head_sha"] != PROSPECTIVE_MIGRATION_RUN_SHA
    ):
        raise ValueError("historical_continuity_migration_run_invalid")

    # Current ledger earn checkpoint cannot substitute for archive-bound history.
    current_checkpoint = ledger.get("earn_accrual_checkpoint")
    current_snapshot = ledger.get("last_balance_snapshot")
    if (
        not isinstance(current_checkpoint, Mapping)
        or not isinstance(current_snapshot, Mapping)
    ):
        raise ValueError("historical_continuity_conservation_unverified")

    final_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if not observed_at < final_at <= observed_at + timedelta(minutes=2):
        raise ValueError("historical_continuity_observation_window_invalid")

    try:
        current_checkpoint_at = _utc(current_checkpoint.get("observed_at"))
        opening_checkpoint_at = _utc(material["opening_checkpoint"].get("observed_at"))
    except ValueError:
        raise ValueError("historical_continuity_observation_timeline_invalid") from None
    if not (
        opening_at <= opening_checkpoint_at <= current_checkpoint_at <= final_at
        and opening_at <= current_checkpoint_at
    ):
        raise ValueError("historical_continuity_observation_timeline_invalid")

    approved_reward_products = frozenset(
        (asset, product_id)
        for asset, row in material["opening_checkpoint"].get("assets", {}).items()
        if isinstance(row, Mapping)
        for product_id in (row.get("products") or {})
        if isinstance(product_id, str) and product_id
    )

    # History must cover through final_at before integrity / conservation checks.
    history = diagnose_chunked_balance_flows(
        client,
        start=opening_at,
        end=final_at,
        now=final_at,
        managed_reward_assets=material["managed_assets"],
        approved_reward_products=approved_reward_products,
    )
    if history.get("history_complete_for_requested_surfaces") is not True:
        reason = history.get("reason_code")
        if reason == "balance_history_duplicate_event":
            raise ValueError("historical_continuity_duplicate_event")
        if reason == "balance_history_unapproved_reward_product":
            raise ValueError("historical_continuity_unapproved_reward_product")
        raise ValueError("historical_continuity_history_incomplete")
    counts = history.get("history_counts")
    if not isinstance(counts, Mapping):
        raise ValueError("historical_continuity_history_incomplete")
    lifecycle_surfaces = {"earn_rewards", "earn_subscriptions", "earn_redemptions"}
    unsupported = [
        name for name, value in counts.items()
        if name not in lifecycle_surfaces and value not in (0, None)
    ]
    if unsupported:
        raise ValueError("historical_continuity_unsupported_activity")
    if counts.get("earn_rewards") not in (0, None) and type(counts.get("earn_rewards")) is not int:
        raise ValueError("historical_continuity_history_incomplete")
    for surface in ("earn_subscriptions", "earn_redemptions"):
        if counts.get(surface) not in (0, None) and type(counts.get(surface)) is not int:
            raise ValueError("historical_continuity_history_incomplete")

    earn_rewards = int(counts.get("earn_rewards") or 0)
    earn_subscriptions = int(counts.get("earn_subscriptions") or 0)
    earn_redemptions = int(counts.get("earn_redemptions") or 0)
    managed = material["managed_assets"]
    expected_scope = legacy_expected.get("account_scope_sha256")
    proposed = archive["approved_proposal"]["proposed_fields"]
    opening_checkpoint = material["opening_checkpoint"]
    opening_snapshot = material["opening_snapshot"]
    opening_cursor = proposed.get("external_cash_flow_cursor")
    opening_nets = proposed.get("earn_accounted_net_changes")
    opening_principal = proposed.get("daily_external_principal_usdt", 0)
    current_principal = ledger.get("daily_external_principal_usdt", opening_principal)
    current_cursor = ledger.get("external_cash_flow_cursor")
    current_nets = ledger.get("earn_accounted_net_changes")
    classification_counts = history.get("reward_product_classification_counts")

    try:
        account = client.get_account()
        observations = collect_read_only_reconciliation_observations(
            client,
            strategy_symbols=symbols,
            local_execution_ledger=ledger,
            now=final_at,
            lookback=final_at - opening_at,
            account_snapshot=account,
        )
        after = client.get_account()
    except Exception:
        raise ValueError("historical_continuity_broker_read_failed") from None
    if (
        digest(observations.account_scope) != expected_scope
        or digest({"account_uid": str(after.get("uid") or "")}) != expected_scope
    ):
        raise ValueError("historical_continuity_account_identity_mismatch")
    if observations.open_orders:
        raise ValueError("historical_continuity_open_orders_present")
    if observations.recent_executions:
        raise ValueError("historical_continuity_unsupported_activity")

    first_spot = _historical_spot_digest(account, managed)
    second_spot = _historical_spot_digest(after, managed)
    if first_spot["digest"] != second_spot["digest"]:
        raise ValueError("historical_continuity_quantity_changed_during_read")
    observed_count = second_spot["observed_non_managed_asset_count"]

    try:
        broker_checkpoint = collect_earn_checkpoint(
            client,
            assets=managed,
            observed_at=final_at,
            expected_account_scope_sha256=expected_scope,
        )
    except EarnCheckpointUnavailable:
        raise ValueError("historical_continuity_checkpoint_unavailable") from None
    if broker_checkpoint["account_scope_sha256"] != expected_scope:
        raise ValueError("historical_continuity_account_identity_mismatch")
    try:
        if _utc(broker_checkpoint.get("observed_at")) != final_at:
            raise ValueError("historical_continuity_observation_timeline_invalid")
    except ValueError as exc:
        if str(exc) == "historical_continuity_observation_timeline_invalid":
            raise
        raise ValueError("historical_continuity_observation_timeline_invalid") from None
    for asset, row in broker_checkpoint["assets"].items():
        spot = second_spot["balances"].get(asset)
        if spot is None or spot != (_amount(row["spot_free"]), _amount(row["spot_locked"])):
            raise ValueError("historical_continuity_quantity_changed_during_read")

    broker_quantities = {
        asset: _amount(row["quantity"])
        for asset, row in broker_checkpoint["assets"].items()
    }
    ledger_snapshot_quantities = {
        str(asset).upper(): _amount(value)
        for asset, value in current_snapshot.items()
    }
    try:
        if not _daily_loss_fields_unchanged(proposed, ledger):
            raise ValueError("daily_loss")
        if _amount(current_principal) != _amount(opening_principal):
            raise ValueError("principal")
        if not _cursor_evolution_allowed(opening_cursor, current_cursor, final_at=final_at):
            raise ValueError("cursor")
        if not _nets_evolution_allowed(opening_nets, current_nets, managed):
            raise ValueError("nets")
        if set(current_snapshot) != set(opening_snapshot):
            raise ValueError("scope")
        if set(current_checkpoint.get("assets", ())) != set(opening_checkpoint["assets"]):
            raise ValueError("scope")
        if set(broker_checkpoint["assets"]) != set(opening_checkpoint["assets"]):
            raise ValueError("scope")
        if ledger_snapshot_quantities != broker_quantities:
            raise ValueError("snapshot")
        if current_checkpoint.get("assets") != broker_checkpoint["assets"]:
            raise ValueError("checkpoint")
        if not _checkpoint_nontime_metadata_equal(opening_checkpoint, current_checkpoint):
            raise ValueError("checkpoint")
        if earn_rewards == 0 and earn_subscriptions == 0 and earn_redemptions == 0:
            if broker_quantities != material["opening_quantities"]:
                raise ValueError("unchanged")
            if ledger_snapshot_quantities != material["opening_quantities"]:
                raise ValueError("snapshot")
            if not _checkpoint_assets_equal(opening_checkpoint, current_checkpoint):
                raise ValueError("unchanged")
            if not _checkpoint_assets_equal(opening_checkpoint, broker_checkpoint):
                raise ValueError("unchanged")
            compare_earn_checkpoints(
                opening_checkpoint,
                broker_checkpoint,
                verified_net_changes={asset: "0" for asset in managed},
            )
        else:
            totals = history.get("reward_quantity_totals")
            if earn_rewards > 0 and not isinstance(totals, Mapping):
                raise ValueError("rewards")
            bonus_by_asset = {}
            realtime_by_asset = {}
            expected_quantities = {}
            for asset, opening_qty in material["opening_quantities"].items():
                if earn_rewards == 0:
                    bonus = Decimal(0)
                    realtime = Decimal(0)
                else:
                    row = totals.get(asset)
                    if not isinstance(row, Mapping):
                        raise ValueError("rewards")
                    bonus = _amount(row.get("BONUS"))
                    realtime = _amount(row.get("REALTIME"))
                bonus_by_asset[asset] = bonus
                realtime_by_asset[asset] = realtime
                expected_quantities[asset] = _add_reward_quantity(
                    _add_reward_quantity(_amount(opening_qty), bonus), realtime
                )
            if broker_quantities != expected_quantities:
                raise ValueError("rewards")
            if ledger_snapshot_quantities != expected_quantities:
                raise ValueError("snapshot")
            opening_products = {
                (asset, product_id)
                for asset, row in opening_checkpoint.get("assets", {}).items()
                if isinstance(row, Mapping)
                for product_id in (row.get("products") or {})
            }
            current_products = {
                (asset, product_id)
                for asset, row in broker_checkpoint.get("assets", {}).items()
                if isinstance(row, Mapping)
                for product_id in (row.get("products") or {})
            }
            added_products = current_products - opening_products
            removed_products = opening_products - current_products
            subscription_facts = history.get("_private_lifecycle_subscription_facts") or []
            redemption_facts = history.get("_private_lifecycle_redemption_facts") or []
            if not isinstance(subscription_facts, list) or not isinstance(redemption_facts, list):
                raise ValueError("lifecycle")
            for asset, product_id in added_products:
                matches = [
                    fact
                    for fact in subscription_facts
                    if (
                        isinstance(fact, Mapping)
                        and fact.get("asset") == asset
                        and fact.get("product_id") == product_id
                    )
                ]
                if not matches:
                    raise ValueError("lifecycle")
                sub_amount = sum((_amount(fact.get("amount")) for fact in matches), Decimal(0))
                product = broker_checkpoint["assets"][asset]["products"][product_id]
                if _amount(product["total"]) != _add_reward_quantity(
                    sub_amount, _amount(product["realtime_rewards"])
                ):
                    raise ValueError("lifecycle")
            for asset, product_id in removed_products:
                matches = [
                    fact
                    for fact in redemption_facts
                    if (
                        isinstance(fact, Mapping)
                        and fact.get("asset") == asset
                        and fact.get("product_id") == product_id
                    )
                ]
                if not matches:
                    raise ValueError("lifecycle")
                redeem_amount = _exact_decimal_sum(
                    [_amount(fact.get("amount")) for fact in matches]
                )
                opening_product = opening_checkpoint["assets"][asset]["products"][product_id]
                if redeem_amount != _amount(opening_product["total"]):
                    raise ValueError("lifecycle")
            lifecycle_mutated = bool(added_products or removed_products) or any(
                isinstance(fact, Mapping) and fact.get("asset") in managed
                for fact in (*subscription_facts, *redemption_facts)
            )
            for asset in managed:
                opening_row = opening_checkpoint["assets"][asset]
                current_row = broker_checkpoint["assets"][asset]
                asset_subs = sum(
                    (
                        _amount(fact.get("amount"))
                        for fact in subscription_facts
                        if isinstance(fact, Mapping) and fact.get("asset") == asset
                    ),
                    Decimal(0),
                )
                asset_redeems = sum(
                    (
                        _amount(fact.get("amount"))
                        for fact in redemption_facts
                        if isinstance(fact, Mapping) and fact.get("asset") == asset
                    ),
                    Decimal(0),
                )
                spot_delta = _amount(current_row["spot_free"]) - _amount(opening_row["spot_free"])
                if spot_delta != bonus_by_asset[asset] + asset_redeems - asset_subs:
                    raise ValueError("bonus_spot")
                shared = set(opening_row["products"]) & set(current_row["products"])
                counter_delta = Decimal(0)
                for product_id in shared:
                    before = opening_row["products"][product_id]
                    after = current_row["products"][product_id]
                    if before.get("auto_subscribe") != after.get("auto_subscribe"):
                        raise ValueError("lifecycle")
                    delta = _amount(after["realtime_rewards"]) - _amount(before["realtime_rewards"])
                    if delta < 0:
                        raise ValueError("lifecycle")
                    counter_delta = _add_reward_quantity(counter_delta, delta)
                    product_subs = sum(
                        (
                            _amount(fact.get("amount"))
                            for fact in subscription_facts
                            if (
                                isinstance(fact, Mapping)
                                and fact.get("asset") == asset
                                and fact.get("product_id") == product_id
                            )
                        ),
                        Decimal(0),
                    )
                    product_redeems = sum(
                        (
                            _amount(fact.get("amount"))
                            for fact in redemption_facts
                            if (
                                isinstance(fact, Mapping)
                                and fact.get("asset") == asset
                                and fact.get("product_id") == product_id
                            )
                        ),
                        Decimal(0),
                    )
                    principal_before = _amount(before["total"]) - _amount(before["realtime_rewards"])
                    principal_after = _amount(after["total"]) - _amount(after["realtime_rewards"])
                    if principal_after != principal_before + product_subs - product_redeems:
                        raise ValueError("lifecycle")
                for product_id, after in current_row["products"].items():
                    if product_id in shared:
                        continue
                    counter_delta = _add_reward_quantity(
                        counter_delta, _amount(after["realtime_rewards"])
                    )
                if counter_delta != realtime_by_asset[asset]:
                    raise ValueError("realtime_earn")
            if not lifecycle_mutated:
                compare_earn_checkpoints(
                    opening_checkpoint,
                    current_checkpoint,
                    verified_net_changes={
                        asset: format(bonus_by_asset[asset], "f") for asset in managed
                    },
                )
                compare_earn_checkpoints(
                    opening_checkpoint,
                    broker_checkpoint,
                    verified_net_changes={
                        asset: format(bonus_by_asset[asset], "f") for asset in managed
                    },
                )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ValueError("historical_continuity_conservation_unverified") from None

    proof = {
        "history_complete_for_requested_surfaces": True,
        "history_counts": dict(counts),
        "chunk_count": history.get("chunk_count"),
        "quantity_double_read_match": True,
        "managed_asset_count": len(managed),
        "non_managed_spot_policy": "observe_only",
        "observed_non_managed_asset_count": observed_count,
        "earn_rewards_observed": earn_rewards,
        "earn_reward_conservation_verified": earn_rewards > 0 or earn_subscriptions > 0,
        "current_ledger_bound": True,
        "opening_checkpoint_used_as_bridge": False,
    }
    if isinstance(classification_counts, Mapping):
        proof["reward_product_classification_counts"] = {
            str(key): int(value)
            for key, value in classification_counts.items()
            if key in {
                "opening_approved",
                "lifecycle_subscription_proven",
                "out_of_scope_asset",
                "lifecycle_evidence_incomplete",
                "product_mapping_ambiguous",
                "unexplained_managed_impact",
            }
            and type(value) is int
        }
    return {
        "observed_at": final_at,
        "observations": observations,
        "current_ledger_sha256": material["current_ledger_sha256"],
        "archive_sha256": material["archive_sha256"],
        "proof": proof,
    }


def _cursor_evolution_allowed(
    opening_cursor: object,
    current_cursor: object,
    *,
    final_at: datetime,
) -> bool:
    """Allow evidenced cursor time advancement; never invent or copy records."""
    if opening_cursor == current_cursor:
        return True
    if not isinstance(opening_cursor, Mapping) or not isinstance(current_cursor, Mapping):
        return False
    if current_cursor.get("version") != opening_cursor.get("version"):
        return False
    opening_records = opening_cursor.get("records")
    current_records = current_cursor.get("records")
    if opening_records != current_records:
        return False
    try:
        opening_at = _utc(opening_cursor.get("observed_at"))
        current_at = _utc(current_cursor.get("observed_at"))
    except ValueError:
        return False
    return opening_at <= current_at <= final_at


def _nets_evolution_allowed(
    opening_nets: object,
    current_nets: object,
    managed: Sequence[str],
) -> bool:
    """Allow accounted nets to remain zero without requiring byte-identical opening."""
    if opening_nets == current_nets:
        return True
    if not isinstance(current_nets, Mapping):
        return False
    if set(current_nets) != set(managed):
        return False
    try:
        return all(_amount(current_nets[asset]) == 0 for asset in managed)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def _daily_loss_fields_unchanged(
    opening_fields: Mapping[str, object],
    ledger: Mapping[str, object],
) -> bool:
    """Daily-loss semantics stay frozen until independently recomputed."""
    for field in _DAILY_LOSS_SEMANTIC_FIELDS:
        if ledger.get(field) != opening_fields.get(field):
            return False
    return True


def _checkpoint_nontime_metadata_equal(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    """Authority/scope metadata must match; observed_at and assets may evolve."""
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    ignore = {"observed_at", "assets"}
    left_keys = set(left) - ignore
    right_keys = set(right) - ignore
    if left_keys != right_keys:
        return False
    for key in left_keys:
        left_val = left[key]
        right_val = right[key]
        if key == "execution_authority_granted":
            if left_val is not False or right_val is not False:
                return False
            continue
        if key == "account_scope_sha256":
            if (
                type(left_val) is not str
                or type(right_val) is not str
                or left_val != right_val
            ):
                return False
            continue
        if left_val != right_val:
            return False
    return True


def _checkpoint_assets_equal(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    """No-activity checkpoints may advance observed_at only; other fields stay fixed."""
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    left_keys = set(left) - {"observed_at"}
    right_keys = set(right) - {"observed_at"}
    if left_keys != right_keys:
        return False
    if not _checkpoint_nontime_metadata_equal(left, right):
        return False
    return left.get("assets") == right.get("assets")


def _historical_spot_digest(
    account: Mapping[str, object], managed_assets: Sequence[str]
) -> dict[str, object]:
    raw_rows = account.get("balances") if isinstance(account, Mapping) else None
    if not isinstance(raw_rows, list) or any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("historical_continuity_broker_read_failed")
    managed = tuple(dict.fromkeys(str(asset).upper() for asset in managed_assets))
    managed_set = set(managed)
    balances: dict[str, tuple[Decimal, Decimal]] = {}
    canonical = []
    for row in raw_rows:
        asset = str(row.get("asset") or "").strip().upper()
        if not asset or asset in balances:
            raise ValueError("historical_continuity_broker_read_failed")
        free, locked = _amount(row.get("free")), _amount(row.get("locked"))
        if locked != 0:
            raise ValueError("historical_continuity_conservation_unverified")
        balances[asset] = (free, locked)
        canonical.append({"asset": asset, "free": str(free), "locked": str(locked)})
    if any(asset not in balances for asset in managed):
        raise ValueError("historical_continuity_conservation_unverified")
    observed_count = sum(
        asset not in managed_set and free != 0 for asset, (free, _locked) in balances.items()
    )
    return {
        "digest": digest(sorted(canonical, key=lambda row: row["asset"])),
        "balances": balances,
        "observed_non_managed_asset_count": observed_count,
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
    if digest(ledger) != private["current_ledger_sha256"]:
        raise ValueError("historical_continuity_ledger_changed")
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
        "archive_document": PROSPECTIVE_ARCHIVE_DOCUMENT,
        "archive_sha256": private["archive_sha256"],
        "current_ledger_sha256": private["current_ledger_sha256"],
        "new_ledger_sha256": private["current_ledger_sha256"],
        "frozen_expected_sha256": digest(legacy_expected),
        "runtime_target_sha256": digest(runtime_target.to_dict()),
        "symbols_sha256": digest(list(symbols)),
        "historical_difference_unresolved": True,
        "opening_balance_observed_at": validate_historical_continuity_material(
            ledger, archive
        )["opening_at"].isoformat(),
        "reconciled_evidence": reconciled.to_dict(),
        "proof": private["proof"],
    }
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        (reconciled,), source_receipts_sha256=digest(source), now=observed_at
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
        or source.get("migration_run", {}).get("id") != PROSPECTIVE_MIGRATION_RUN_ID
        or source.get("migration_run", {}).get("head_sha") != PROSPECTIVE_MIGRATION_RUN_SHA
        or not _valid_run(source.get("migration_run"))
        or not _valid_run(source.get("run"))
        or source.get("archive_document") != PROSPECTIVE_ARCHIVE_DOCUMENT
        or source.get("frozen_expected_sha256") != digest(legacy_expected)
        or source.get("runtime_target_sha256") != digest(runtime_target.to_dict())
        or source.get("historical_difference_unresolved") is not True
        or not isinstance(source.get("current_ledger_sha256"), str)
        or len(source["current_ledger_sha256"]) != 64
        or source.get("new_ledger_sha256") != source["current_ledger_sha256"]
        or proof.get("history_complete_for_requested_surfaces") is not True
        or proof.get("opening_checkpoint_used_as_bridge") is not False
        or proof.get("current_ledger_bound") is not True
        or type(proof.get("chunk_count")) is not int
        or proof["chunk_count"] < 1
        or candidate.source_receipts_sha256 != digest(source)
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
    if require_fresh:
        reference = now or datetime.now(timezone.utc)
        if not (observed_at <= reference <= observed_at + timedelta(minutes=30)):
            raise ValueError("historical_continuity_candidate_stale")
        evaluation = evaluate_broker_reconciliation_baseline_enrollment(
            (evidence,), source_receipts_sha256=digest(source), now=reference
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
