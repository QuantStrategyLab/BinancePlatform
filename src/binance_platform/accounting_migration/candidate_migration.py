"""Core daily accounting migration, control quiesce, inspection, and owner release."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from binance_platform.application import rebased_recovery as _rebased_recovery
from binance_platform.application import reconciliation_recovery as _reconciliation_recovery
from binance_platform.accounting_migration.common import (
    _ACCOUNTING_FIELDS,
    _EARN_PAGE_SIZE,
    NEW_BASIS,
    REBASE_ARCHIVE_DOCUMENT,
    SCHEMA_VERSION,
    TTL,
    MigrationApplyUncertain,
    MigrationAtomicPrecondition,
    MigrationBlocked,
    MigrationHooks,
    _candidate_digest,
    _canonical_sha,
    _finite,
    _private_spot_account,
    _same_balance,
    _snapshot_marker,
    _snapshot_matches,
    _timestamp,
    _validate_control,
    _validate_safe_order_state,
    _zero_activity,
    is_prospective_archive_document,
    ledger_scope,
    resolve_hooks,
)


def build_candidate(
    *,
    ledger: Mapping[str, object],
    ledger_update_time,
    control: Mapping[str, object] | None,
    evidence: Mapping[str, object],
    source_sha: str,
    observed_at: datetime,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    if observed_at.tzinfo is None or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise MigrationBlocked("migration_identity_invalid")
    observed_at = observed_at.astimezone(timezone.utc)
    _validate_control(control)
    order_state = _validate_safe_order_state(ledger)
    _zero_activity(evidence)
    account_scope = str(evidence.get("account_scope_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", account_scope):
        raise MigrationBlocked("account_scope_unverified")
    balances = evidence.get("balance_snapshot")
    if not isinstance(balances, Mapping) or "BNB" not in balances:
        raise MigrationBlocked("bnb_balance_missing")
    normalized_balances = {
        str(asset): round(_finite(value), 8) for asset, value in balances.items()
    }
    if not {"USDT", "BTC", "BNB"}.issubset(normalized_balances):
        raise MigrationBlocked("balance_snapshot_incomplete")

    today = observed_at.date().isoformat()
    last_reset = str(ledger.get("last_reset_date") or "")
    try:
        last_reset_date = active_hooks.datetime_cls.strptime(last_reset, "%Y-%m-%d").date()
    except ValueError:
        raise MigrationBlocked("last_reset_date_invalid") from None
    if last_reset_date > observed_at.date():
        raise MigrationBlocked("last_reset_date_invalid")

    patch: dict[str, object] = {
        "daily_trend_pnl_basis": NEW_BASIS,
        "daily_trend_cash_flow_usdt": 0.0,
        "daily_trend_net_invested_usdt": 0.0,
        "daily_trend_third_fee_usdt": 0.0,
    }
    if last_reset == today:
        if ledger.get("daily_trend_pnl_basis") != "trend_val":
            raise MigrationBlocked("same_day_legacy_basis_invalid")
        old_snapshot = ledger.get("last_balance_snapshot")
        if not isinstance(old_snapshot, Mapping) or not old_snapshot:
            raise MigrationBlocked("same_day_snapshot_missing")
        for asset, old_value in old_snapshot.items():
            if asset not in normalized_balances or not _same_balance(
                old_value, normalized_balances[asset], asset=str(asset)
            ):
                raise MigrationBlocked("same_day_balance_changed")
        equity_base = _finite(ledger.get("daily_equity_base"))
        trend_base = _finite(ledger.get("daily_trend_equity_base"))
        if equity_base <= 0 or (
            trend_base <= 0
            and any(
                value > 0
                for asset, value in normalized_balances.items()
                if asset not in {"USDT", "BTC", "BNB"}
            )
        ):
            raise MigrationBlocked("same_day_basis_invalid")
        mode = "same_utc_day_zero_activity"
        patch["daily_trend_risk_base_usdt"] = trend_base
    else:
        prices = evidence.get("prices")
        if not isinstance(prices, Mapping):
            raise MigrationBlocked("price_snapshot_incomplete")
        asset_values = {}
        for asset, quantity in normalized_balances.items():
            if asset == "USDT":
                asset_values[asset] = quantity
                continue
            if quantity == 0:
                asset_values[asset] = 0.0
                continue
            symbol = f"{asset}USDT"
            if symbol not in prices:
                raise MigrationBlocked("price_snapshot_incomplete")
            price = _finite(prices[symbol])
            if quantity > 0 and price <= 0:
                raise MigrationBlocked("price_snapshot_incomplete")
            asset_values[asset] = quantity * price
        trend_equity = sum(
            value
            for asset, value in asset_values.items()
            if asset not in {"USDT", "BTC", "BNB"}
        )
        total_equity = sum(asset_values.values())
        if total_equity <= 0:
            raise MigrationBlocked("rollover_equity_invalid")
        mode = "new_utc_day_zero_activity"
        patch.update(
            {
                "daily_equity_base": total_equity,
                "daily_trend_equity_base": trend_equity,
                "daily_trend_risk_base_usdt": trend_equity,
                "last_reset_date": today,
            }
        )

    expires_at = observed_at + TTL
    candidate: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "source_sha": source_sha,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "account_scope_sha256": account_scope,
        "ledger_update_time": _timestamp(ledger_update_time),
        "ledger_sha256": _canonical_sha(ledger, digest_fn=digest_fn),
        "preserved_ledger_sha256": _canonical_sha(
            {
                key: value
                for key, value in ledger.items()
                if key not in _ACCOUNTING_FIELDS
            },
            digest_fn=digest_fn,
        ),
        "control_sha256": _canonical_sha(control, digest_fn=digest_fn),
        "broker_snapshot_sha256": str(evidence.get("broker_snapshot_sha256") or ""),
        "activity_sha256": str(evidence.get("activity_sha256") or ""),
        "balance_snapshot_sha256": _canonical_sha(normalized_balances, digest_fn=digest_fn),
        "balance_asset_count": len(normalized_balances),
        "order_state": order_state,
        "preserved_circuit_breaker_latch": ledger.get("is_circuit_broken"),
        "patch": patch,
        "no_order": True,
        "execution_authority_granted": False,
    }
    for name in ("broker_snapshot_sha256", "activity_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(candidate[name])):
            raise MigrationBlocked("broker_evidence_digest_invalid")
    candidate["candidate_sha256"] = _candidate_digest(candidate, digest_fn=digest_fn)
    candidate["public_preview"] = {
        key: copy.deepcopy(candidate[key])
        for key in (
            "schema_version",
            "mode",
            "source_sha",
            "observed_at",
            "expires_at",
            "ledger_update_time",
            "ledger_sha256",
            "preserved_ledger_sha256",
            "control_sha256",
            "broker_snapshot_sha256",
            "activity_sha256",
            "balance_snapshot_sha256",
            "balance_asset_count",
            "preserved_circuit_breaker_latch",
            "patch",
            "candidate_sha256",
            "no_order",
            "execution_authority_granted",
        )
    }
    return candidate


def validate_candidate(
    candidate: Mapping[str, object],
    *,
    expected_digest: str,
    now: datetime,
    hooks: MigrationHooks | None = None,
) -> None:
    active_hooks = resolve_hooks(hooks)
    if candidate.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("candidate_schema_invalid")
    actual = _candidate_digest(candidate, digest_fn=active_hooks.digest_fn)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        or actual != expected_digest
        or candidate.get("candidate_sha256") != actual
    ):
        raise ValueError("candidate_digest_mismatch")
    try:
        observed = active_hooks.datetime_cls.fromisoformat(
            str(candidate["observed_at"]).replace("Z", "+00:00")
        )
        expires = active_hooks.datetime_cls.fromisoformat(
            str(candidate["expires_at"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError):
        raise ValueError("candidate_time_invalid") from None
    now = now.astimezone(timezone.utc)
    if (
        observed.tzinfo is None
        or expires.tzinfo is None
        or expires - observed != TTL
        or not observed <= now <= expires
    ):
        raise ValueError("candidate_expired")
    if observed.astimezone(timezone.utc).date() != now.date():
        raise ValueError("candidate_expired")
    patch = candidate.get("patch")
    if not isinstance(patch, Mapping) or not set(patch).issubset(
        _ACCOUNTING_FIELDS - {"last_balance_snapshot"}
    ):
        raise ValueError("candidate_patch_invalid")
    if candidate.get("preserved_circuit_breaker_latch") not in {True, False}:
        raise ValueError("candidate_latch_invalid")


def compare_and_apply(
    transaction,
    *,
    ledger_ref,
    owner_ref,
    control_ref,
    candidate,
    balance_snapshot,
    hooks: MigrationHooks | None = None,
) -> None:
    digest_fn = resolve_hooks(hooks).digest_fn
    owner = owner_ref.get(transaction=transaction, retry=None)
    ledger = ledger_ref.get(transaction=transaction, retry=None)
    control = control_ref.get(transaction=transaction, retry=None)
    ledger_value = ledger.to_dict() if ledger.exists else None
    control_value = control.to_dict() if control.exists else None
    if (
        owner.exists
        or not ledger.exists
        or _timestamp(getattr(ledger, "update_time", None))
        != candidate.get("ledger_update_time")
        or _canonical_sha(ledger_value, digest_fn=digest_fn) != candidate.get("ledger_sha256")
        or _canonical_sha(control_value, digest_fn=digest_fn) != candidate.get("control_sha256")
        or (
            control_value is not None and control_value.get("state") != "RECONCILE_ONLY"
        )
    ):
        raise MigrationAtomicPrecondition("migration_atomic_precondition_changed")
    _validate_safe_order_state(ledger_value)
    if ledger_value.get("is_circuit_broken") != candidate.get(
        "preserved_circuit_breaker_latch"
    ):
        raise MigrationAtomicPrecondition("migration_atomic_precondition_changed")
    normalized_snapshot = {
        str(asset): round(_finite(value), 8)
        for asset, value in balance_snapshot.items()
    }
    if _canonical_sha(normalized_snapshot, digest_fn=digest_fn) != candidate.get(
        "balance_snapshot_sha256"
    ):
        raise MigrationAtomicPrecondition("migration_atomic_precondition_changed")
    patch = {**candidate["patch"], "last_balance_snapshot": normalized_snapshot}
    if not set(patch).issubset(_ACCOUNTING_FIELDS):
        raise ValueError("candidate_patch_invalid")
    transaction.update(ledger_ref, patch)


def strict_balance_snapshot(client, positions, assets) -> dict[str, float]:
    try:
        spot = {}
        for row in positions:
            asset = str(row.get("asset") or "")
            if not asset or asset in spot:
                raise MigrationBlocked("spot_balance_rows_invalid")
            amount = Decimal(str(row["free"])) + Decimal(str(row["locked"]))
            if not amount.is_finite() or amount < 0:
                raise MigrationBlocked("balance_amount_invalid")
            spot[asset] = amount
        result = {}
        for asset in sorted(assets):
            if asset not in spot:
                raise MigrationBlocked("spot_balance_missing")
            earn = client.get_simple_earn_flexible_product_position(
                asset=asset, current=1, size=_EARN_PAGE_SIZE
            )
            earn_rows = earn.get("rows") if isinstance(earn, Mapping) else None
            response_total = earn.get("total") if isinstance(earn, Mapping) else None
            total_valid = type(response_total) is int or (
                isinstance(response_total, str) and response_total.isdecimal()
            )
            if (
                not isinstance(earn_rows, list)
                or any(
                    not isinstance(row, Mapping) or row.get("asset") != asset
                    for row in earn_rows
                )
                or not total_valid
                or int(response_total) < 0
            ):
                raise MigrationBlocked("earn_balance_rows_invalid")
            if (
                len(earn_rows) != int(response_total)
                or len(earn_rows) >= _EARN_PAGE_SIZE
            ):
                raise MigrationBlocked("earn_balance_rows_incomplete")
            total = spot[asset] + sum(
                (Decimal(str(row["totalAmount"])) for row in earn_rows), Decimal(0)
            )
            if not total.is_finite() or total < 0:
                raise MigrationBlocked("balance_amount_invalid")
            result[asset] = round(float(total), 8)
    except MigrationBlocked:
        raise
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise MigrationBlocked("balance_amount_invalid") from None
    return result


def collect_evidence(
    client,
    *,
    ledger,
    now,
    expected,
    configured_symbols: tuple[str, ...],
    require_prices: bool = False,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    assets, symbols = ledger_scope(ledger, configured_symbols=configured_symbols)
    midnight = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    account = client.get_account()
    observations = active_hooks.collect_observations_fn(
        client,
        strategy_symbols=symbols,
        local_execution_ledger=ledger,
        now=now,
        lookback=now - midnight,
        account_snapshot=account,
    )
    if digest_fn(observations.account_scope) != expected.get("account_scope_sha256"):
        raise MigrationBlocked("account_scope_unverified")
    history = active_hooks.diagnose_flows_fn(client, start=midnight, end=now, now=now)
    raw_positions = account.get("balances") if isinstance(account, Mapping) else None
    if not isinstance(raw_positions, list):
        raise MigrationBlocked("spot_balance_rows_invalid")
    snapshot = strict_balance_snapshot(client, raw_positions, assets)
    prices = {}
    if require_prices or str(ledger.get("last_reset_date") or "") != now.date().isoformat():
        for asset, quantity in snapshot.items():
            if asset != "USDT" and quantity > 0:
                try:
                    price = _finite(
                        client.get_avg_price(symbol=f"{asset}USDT")["price"]
                    )
                except Exception:
                    raise MigrationBlocked("price_snapshot_incomplete") from None
                if price <= 0:
                    raise MigrationBlocked("price_snapshot_incomplete")
                prices[f"{asset}USDT"] = price
    activity = {
        "recent_execution_count": len(observations.recent_executions),
        "history_counts": history.get("history_counts"),
        "history_complete": history.get("history_complete_for_requested_surfaces"),
    }
    return {
        "account_scope_sha256": digest_fn(observations.account_scope),
        "balance_snapshot": snapshot,
        "prices": prices,
        "open_order_count": len(observations.open_orders),
        **activity,
        "broker_snapshot_sha256": digest_fn(
            {
                "positions": observations.positions,
                "open_orders": observations.open_orders,
                "recent_executions": observations.recent_executions,
            }
        ),
        "activity_sha256": digest_fn(activity),
        "utc_date": now.date().isoformat(),
    }


def verify_applied(
    refs,
    *,
    candidate,
    balance_snapshot,
    hooks: MigrationHooks | None = None,
) -> None:
    digest_fn = resolve_hooks(hooks).digest_fn
    owner = refs["owner_ref"].get(retry=None)
    ledger = refs["ledger_ref"].get(retry=None)
    control = refs["control_ref"].get(retry=None)
    if owner.exists or not ledger.exists:
        raise MigrationApplyUncertain("migration_readback_mismatch")
    ledger_value = ledger.to_dict()
    control_value = control.to_dict() if control.exists else None
    expected_patch = {
        **candidate["patch"],
        "last_balance_snapshot": {
            str(asset): round(_finite(value), 8)
            for asset, value in balance_snapshot.items()
        },
    }
    if (
        any(ledger_value.get(key) != value for key, value in expected_patch.items())
        or _canonical_sha(
            {
                key: value
                for key, value in ledger_value.items()
                if key not in _ACCOUNTING_FIELDS
            },
            digest_fn=digest_fn,
        )
        != candidate.get("preserved_ledger_sha256")
        or _canonical_sha(control_value, digest_fn=digest_fn) != candidate.get("control_sha256")
    ):
        raise MigrationApplyUncertain("migration_readback_mismatch")


def read_source(refs, *, proposal_target=None, expected=None):
    owner = refs["owner_ref"].get(retry=None)
    ledger = refs["ledger_ref"].get(retry=None)
    control = refs["control_ref"].get(retry=None)
    if owner.exists or not ledger.exists:
        raise MigrationBlocked("ledger_unavailable_or_owned")
    ledger_value = ledger.to_dict()
    control_value = control.to_dict() if control.exists else None
    if (
        proposal_target is not None
        and isinstance(control_value, Mapping)
        and control_value.get("state") == "ACTIVE_LKG"
    ):
        try:
            _reconciliation_recovery.activated_target(
                proposal_target, control_value, expected=expected
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MigrationBlocked("prospective_control_identity_unverified") from None
    else:
        _validate_control(control_value)
    _validate_safe_order_state(ledger_value)
    return ledger, ledger_value, control_value


def validate_stale_owner_control(target, control, *, expected) -> None:
    if not isinstance(control, Mapping) or control.get("state") != "ACTIVE_LKG":
        raise MigrationBlocked("recovery_control_not_active_lkg")
    try:
        activated = _reconciliation_recovery.activated_target(
            target, control, expected=expected
        )
        if (
            str(getattr(getattr(activated, "live_continuity", None), "state", "")).upper()
            != "ACTIVE_LKG"
        ):
            raise MigrationBlocked("recovery_control_not_active_lkg")
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationBlocked("recovery_control_invalid") from None


def release_stale_owner_transaction(transaction, *, refs, markers):
    owner = refs["owner_ref"].get(transaction=transaction, retry=None)
    ledger = refs["ledger_ref"].get(transaction=transaction, retry=None)
    control = refs["control_ref"].get(transaction=transaction, retry=None)
    if (
        not _snapshot_matches(owner, markers["owner"])
        or not _snapshot_matches(ledger, markers["ledger"])
        or not _snapshot_matches(control, markers["control"])
    ):
        raise MigrationAtomicPrecondition("stale_owner_release_precondition_changed")
    owner_value = owner.to_dict()
    if not isinstance(owner_value, Mapping):
        raise MigrationAtomicPrecondition("stale_owner_release_precondition_changed")
    owner_id = owner_value.get("owner_id")
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise MigrationAtomicPrecondition("stale_owner_release_precondition_changed")
    transaction.delete(refs["owner_ref"])
    return None


def release_stale_owner(
    refs,
    *,
    client,
    target,
    expected,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    """Delete one confirmed stale owner document and nothing else."""
    active_hooks = resolve_hooks(hooks)
    owner_snapshot = refs["owner_ref"].get(retry=None)
    if not owner_snapshot.exists:
        return {
            "status": "already_absent",
            "stage": "stale_owner_release",
            "owner_exists": False,
            "no_order": True,
            "write_performed": False,
        }
    owner_value = owner_snapshot.to_dict()
    if not isinstance(owner_value, Mapping):
        raise MigrationBlocked("owner_document_invalid")
    owner_id = owner_value.get("owner_id")
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise MigrationBlocked("owner_id_invalid")
    ledger_snapshot = refs["ledger_ref"].get(retry=None)
    control_snapshot = refs["control_ref"].get(retry=None)
    if not ledger_snapshot.exists or not control_snapshot.exists:
        raise MigrationBlocked("stale_owner_release_source_missing")
    ledger = ledger_snapshot.to_dict()
    control = control_snapshot.to_dict()
    if not isinstance(ledger, Mapping):
        raise MigrationBlocked("ledger_unavailable")
    _validate_safe_order_state(ledger)
    validate_control_fn = (
        active_hooks.validate_stale_owner_control_fn or validate_stale_owner_control
    )
    validate_control_fn(target, control, expected=expected)
    if client is None:
        raise MigrationBlocked("account_scope_unverified")
    try:
        account = client.get_account()
    except Exception:
        raise MigrationBlocked("account_read_failed") from None
    if active_hooks.private_spot_account_fn is not None:
        active_hooks.private_spot_account_fn(
            account, expected_account_scope_sha256=expected["account_scope_sha256"]
        )
    else:
        _private_spot_account(
            account,
            expected_account_scope_sha256=expected["account_scope_sha256"],
            digest_fn=active_hooks.digest_fn,
        )
    try:
        open_orders = client.get_open_orders()
    except Exception:
        raise MigrationBlocked("open_orders_read_failed") from None
    if not isinstance(open_orders, list):
        raise MigrationBlocked("open_orders_unverified")
    if open_orders:
        raise MigrationBlocked("open_orders_present")
    markers = {
        "owner": _snapshot_marker(owner_snapshot),
        "ledger": _snapshot_marker(ledger_snapshot),
        "control": _snapshot_marker(control_snapshot),
    }
    from google.cloud import firestore

    @firestore.transactional
    def apply(transaction):
        return release_stale_owner_transaction(transaction, refs=refs, markers=markers)

    try:
        apply(active_hooks.firestore_client_fn().transaction(max_attempts=1))
    except MigrationAtomicPrecondition:
        raise
    except Exception:
        raise MigrationApplyUncertain("stale_owner_release_outcome_uncertain") from None
    try:
        owner_after = refs["owner_ref"].get(retry=None)
        ledger_after = refs["ledger_ref"].get(retry=None)
        control_after = refs["control_ref"].get(retry=None)
        if (
            owner_after.exists
            or not _snapshot_matches(ledger_after, markers["ledger"])
            or not _snapshot_matches(control_after, markers["control"])
        ):
            raise MigrationApplyUncertain("stale_owner_release_readback_uncertain")
    except MigrationApplyUncertain:
        raise
    except Exception:
        raise MigrationApplyUncertain("stale_owner_release_readback_uncertain") from None
    return {
        "status": "released",
        "stage": "stale_owner_release",
        "owner_exists": False,
        "owner_released": True,
        "ledger_unchanged": True,
        "control_unchanged": True,
        "no_order": True,
        "write_performed": True,
        "execution_authority_granted": False,
    }


def inspect_control(refs, *, hooks: MigrationHooks | None = None) -> dict[str, object]:
    """Read only control provenance and existence flags; never enter migration."""
    digest_fn = resolve_hooks(hooks).digest_fn
    snapshot = refs["control_ref"].get(retry=None)
    owner = refs["owner_ref"].get(retry=None)
    ledger = refs["ledger_ref"].get(retry=None)
    value = snapshot.to_dict() if snapshot.exists else None
    control = value if isinstance(value, Mapping) else {}
    state = control.get("state")
    if not isinstance(state, str) or state not in {
        "RECONCILE_ONLY",
        "ACTIVE_LKG",
        "ROLLBACK_LKG",
        "PAUSED",
        "REDUCE_ONLY",
    }:
        state = "INVALID" if snapshot.exists else "ABSENT"
    recovery_id = control.get("recovery_id")
    if not isinstance(recovery_id, str) or not re.fullmatch(
        r"binance-[0-9]{1,20}-[0-9]{1,5}", recovery_id
    ):
        recovery_id = None
    source = control.get("source")
    source_run = source.get("run") if isinstance(source, Mapping) else None
    if not (
        isinstance(source_run, Mapping)
        and type(source_run.get("id")) is int
        and 0 < source_run["id"] < 10**20
        and isinstance(source_run.get("head_sha"), str)
        and re.fullmatch(r"[0-9a-f]{40}", source_run["head_sha"])
    ):
        source_run = None
    else:
        source_run = {key: source_run[key] for key in ("id", "head_sha")}
    rebase_marker = ledger.to_dict().get("accounting_rebase") if ledger.exists else None
    rebase_document = (
        rebase_marker.get("archive_document")
        if isinstance(rebase_marker, Mapping)
        and isinstance(rebase_marker.get("archive_document"), str)
        else None
    )
    rebase_family = (
        "post_rebase"
        if rebase_document == REBASE_ARCHIVE_DOCUMENT
        else "prospective_rebase"
        if is_prospective_archive_document(rebase_document)
        else "unknown"
        if rebase_document is not None
        else None
    )
    archive_exists = None
    material_status = None
    ledger_digest_match = None
    marker_consistent = None
    order_state_safe = None
    changed_top_level_fields = None
    if rebase_document is not None and hasattr(refs["ledger_ref"], "parent"):
        archive_snapshot = refs["ledger_ref"].parent.document(rebase_document).get(retry=None)
        archive_exists = archive_snapshot.exists
        if archive_exists and ledger.exists:
            ledger_value = ledger.to_dict()
            archive_value = archive_snapshot.to_dict()
            order_state = ledger_value.get("order_submission", {}).get("state")
            order_state_safe = order_state in {"RESERVED", "TERMINAL"}
            if rebase_family == "prospective_rebase":
                expected_ledger_digest = archive_value.get("new_ledger_sha256")
                ledger_digest_match = (
                    isinstance(expected_ledger_digest, str)
                    and digest_fn(ledger_value) == expected_ledger_digest
                )
                expected_marker = {
                    key: archive_value.get(key)
                    for key in (
                        "archive_document",
                        "started_at",
                        "opening_balance_observed_at",
                        "historical_difference_unresolved",
                        "approved_proposal_run_id",
                        "approved_proposal_sha256",
                    )
                }
                marker = ledger_value.get("accounting_rebase")
                marker_consistent = isinstance(marker, Mapping) and marker == expected_marker
                approved = archive_value.get("approved_proposal")
                old_ledger = archive_value.get("ledger")
                proposed = (
                    approved.get("proposed_fields")
                    if isinstance(approved, Mapping)
                    else None
                )
                if isinstance(old_ledger, Mapping) and isinstance(proposed, Mapping):
                    expected_ledger = {
                        **old_ledger,
                        **proposed,
                        "accounting_rebase": expected_marker,
                    }
                    changed_top_level_fields = sorted(
                        key
                        for key in set(ledger_value) | set(expected_ledger)
                        if ledger_value.get(key) != expected_ledger.get(key)
                    )
            elif rebase_family == "post_rebase":
                ledger_digest_match = (
                    digest_fn(ledger_value)
                    == archive_snapshot.to_dict().get("new_ledger_sha256")
                )
                marker = ledger_value.get("accounting_rebase")
                marker_consistent = isinstance(marker, Mapping) and all(
                    marker.get(key) == archive_snapshot.to_dict().get(key)
                    for key in (
                        "archive_document",
                        "started_at",
                        "opening_balance_observed_at",
                        "historical_difference_unresolved",
                        "approved_proposal_run_id",
                    )
                )
            try:
                if rebase_family == "prospective_rebase":
                    _rebased_recovery.validate_prospective_rebase_material(
                        ledger_value, archive_value
                    )
                elif rebase_family == "post_rebase":
                    _rebased_recovery.validate_post_rebase_material(
                        ledger_value, archive_value
                    )
                material_status = "valid"
            except ValueError as exc:
                material_status = (
                    str(exc)
                    if str(exc)
                    in {
                        "prospective_rebase_archive_invalid",
                        "prospective_rebase_ledger_invalid",
                        "post_rebase_archive_invalid",
                        "post_rebase_ledger_invalid",
                        "post_rebase_order_state_unsafe",
                    }
                    else "rebase_material_invalid"
                )
    return {
        "status": "inspected",
        "stage": "accounting_migration_control_inspection",
        "control_exists": snapshot.exists,
        "state": state,
        "control_sha256": _canonical_sha(value, digest_fn=digest_fn),
        "control_update_time": _timestamp(snapshot.update_time)
        if snapshot.exists
        else None,
        "control_create_time": _timestamp(snapshot.create_time)
        if getattr(snapshot, "create_time", None)
        else None,
        "recovery_id": recovery_id,
        "source_run": source_run,
        "confirmation_present": isinstance(control.get("confirmation"), Mapping),
        "transition_plan_present": isinstance(control.get("transition_plan"), Mapping),
        "owner_exists": owner.exists,
        "ledger_exists": ledger.exists,
        "rebase_family": rebase_family,
        "rebase_archive_exists": archive_exists,
        "rebase_material_status": material_status,
        "rebase_ledger_digest_match": ledger_digest_match,
        "rebase_marker_consistent": marker_consistent,
        "rebase_order_state_safe": order_state_safe,
        "rebase_changed_top_level_fields": changed_top_level_fields,
        "no_order": True,
        "write_performed": False,
    }


def quiesce_control_transaction(
    transaction, refs, *, hooks: MigrationHooks | None = None
) -> tuple[str, str]:
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    owner = refs["owner_ref"].get(transaction=transaction, retry=None)
    ledger = refs["ledger_ref"].get(transaction=transaction, retry=None)
    snapshot = refs["control_ref"].get(transaction=transaction, retry=None)
    control = snapshot.to_dict() if snapshot.exists else None
    if (
        owner.exists
        or not ledger.exists
        or not isinstance(control, Mapping)
        or control.get("state") != "ACTIVE_LKG"
        or _timestamp(snapshot.update_time) != active_hooks.quiesce_control_update_time
        or _canonical_sha(control, digest_fn=digest_fn) != active_hooks.quiesce_control_sha256
    ):
        raise MigrationBlocked("control_quiesce_precondition_changed")
    next_control_sha = _canonical_sha({**control, "state": "RECONCILE_ONLY"}, digest_fn=digest_fn)
    ledger_sha = _canonical_sha(ledger.to_dict(), digest_fn=digest_fn)
    transaction.update(refs["control_ref"], {"state": "RECONCILE_ONLY"})
    return next_control_sha, ledger_sha


def quiesce_control(refs, *, hooks: MigrationHooks | None = None) -> dict[str, object]:
    from google.cloud import firestore

    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn

    @firestore.transactional
    def apply(transaction):
        return quiesce_control_transaction(transaction, refs, hooks=active_hooks)

    try:
        expected_control_sha, ledger_sha = apply(
            active_hooks.firestore_client_fn().transaction(max_attempts=1)
        )
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationApplyUncertain("control_quiesce_outcome_uncertain") from None
    try:
        snapshot = refs["control_ref"].get(retry=None)
        ledger = refs["ledger_ref"].get(retry=None)
        owner = refs["owner_ref"].get(retry=None)
        if (
            not snapshot.exists
            or not ledger.exists
            or owner.exists
            or _canonical_sha(snapshot.to_dict(), digest_fn=digest_fn) != expected_control_sha
            or _canonical_sha(ledger.to_dict(), digest_fn=digest_fn) != ledger_sha
        ):
            raise ValueError("readback_mismatch")
        return {
            "status": "quiesced",
            "stage": "accounting_migration_control_quiesce",
            "state": "RECONCILE_ONLY",
            "control_sha256": expected_control_sha,
            "control_update_time": _timestamp(snapshot.update_time),
            "ledger_unchanged": True,
            "no_order": True,
            "write_performed": True,
        }
    except Exception:
        raise MigrationApplyUncertain("control_quiesce_readback_uncertain") from None
