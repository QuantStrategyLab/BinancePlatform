#!/usr/bin/env python3
"""Preview or atomically apply the bounded Binance daily-accounting migration.

The command has no order, transfer, redemption, subscription, cancellation, or
notification path.  Preview writes one short-lived redacted candidate file.
Apply accepts only that fixed artifact path, repeats the read-only evidence, and
updates an allowlist of fields in the existing Firestore ledger transaction.
"""

# ruff: noqa: E402

from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application.broker_reconciliation import (
    _expected_digests,
    calculate_broker_observation_sha256 as digest,
    collect_read_only_reconciliation_observations,
    diagnose_balance_flows,
)
from live_services import get_firestore_client
from quant_platform_kit.binance import connect_client
from quant_platform_kit.common.runtime_target import resolve_runtime_target_from_env


REPOSITORY = "QuantStrategyLab/BinancePlatform"
LEDGER_DOCUMENT = "MULTI_ASSET_STATE"
OWNER_DOCUMENT = "MULTI_ASSET_STATE__owner"
CONTROL_DOCUMENT = "MULTI_ASSET_STATE__recovery"
SCHEMA_VERSION = "binance_daily_accounting_migration_candidate.v1"
NEW_BASIS = "trend_mark_plus_cash_flow_v1"
PREVIEW_PATH = Path("reports/binance-accounting-migration-preview/candidate.json")
# Operator-approved control from read-only Runtime 34586531344. One exact downgrade.
QUIESCE_CONTROL_SHA256 = "68318adcf853fe755409814c8707d3338f0af7927da396a60547c8375e9af5c4"
QUIESCE_CONTROL_UPDATE_TIME = "2026-09-08T18:31:35.004893Z"
TTL = timedelta(minutes=10)
_EARN_PAGE_SIZE = 100
_ACCOUNTING_FIELDS = frozenset(
    {
        "daily_trend_pnl_basis",
        "daily_trend_cash_flow_usdt",
        "daily_trend_net_invested_usdt",
        "daily_trend_risk_base_usdt",
        "daily_trend_third_fee_usdt",
        "last_balance_snapshot",
        "daily_equity_base",
        "daily_trend_equity_base",
        "last_reset_date",
    }
)


class MigrationBlocked(ValueError):
    """A required proof is absent; no migration may be written."""


class MigrationApplyUncertain(RuntimeError):
    """The single write attempt may have committed and must not be retried."""


class MigrationAtomicPrecondition(MigrationBlocked):
    """The transaction proved the preview stale before scheduling a write."""


def _symbols_from_env() -> tuple[str, ...]:
    raw = str(os.environ.get("BINANCE_RECONCILIATION_SYMBOLS") or "").strip()
    symbols = tuple(
        dict.fromkeys(item.strip().upper() for item in raw.split(",") if item.strip())
    )
    if not symbols:
        raise MigrationBlocked("reconciliation_symbols_missing")
    return symbols


def _finite(value, *, nonnegative=True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MigrationBlocked("numeric_evidence_invalid") from None
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise MigrationBlocked("numeric_evidence_invalid")
    return number


def _timestamp(value) -> str:
    if isinstance(value, str):
        text = value
    elif hasattr(value, "isoformat"):
        text = value.isoformat()
    else:
        raise MigrationBlocked("ledger_update_time_missing")
    return text.replace("+00:00", "Z")


def _canonical_sha(value) -> str:
    return digest(value)


def _candidate_digest(candidate: Mapping[str, object]) -> str:
    return _canonical_sha(
        {
            key: value
            for key, value in candidate.items()
            if key not in {"candidate_sha256", "public_preview"}
        }
    )


def require_runtime_context() -> None:
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{REPOSITORY}/.github/workflows/main.yml@refs/heads/main"
        or os.environ.get("RUNTIME_TARGET_ENABLED", "").lower() != "false"
        or os.environ.get("RECONCILE_ONLY", "").lower() != "true"
        or not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("GITHUB_SHA", ""))
    ):
        raise ValueError("migration_requires_disabled_main_runtime")


def _validate_safe_order_state(ledger: Mapping[str, object]) -> str:
    record = ledger.get("order_submission")
    if not isinstance(record, Mapping) or set(record) != {"state"}:
        raise MigrationBlocked("unsafe_order_state")
    state = str(record.get("state") or "")
    if state not in {"RESERVED", "TERMINAL"}:
        raise MigrationBlocked("unsafe_order_state")
    return state


def _validate_control(control: Mapping[str, object] | None) -> None:
    if control is not None and (
        not isinstance(control, Mapping) or control.get("state") != "RECONCILE_ONLY"
    ):
        raise MigrationBlocked("recovery_control_not_reconcile_only")


def _zero_activity(evidence: Mapping[str, object]) -> None:
    if evidence.get("history_complete") is not True:
        raise MigrationBlocked("activity_evidence_incomplete")
    if int(evidence.get("open_order_count", -1)) != 0:
        raise MigrationBlocked("open_orders_present")
    counts = evidence.get("history_counts")
    if not isinstance(counts, Mapping):
        raise MigrationBlocked("activity_evidence_incomplete")
    values = [evidence.get("recent_execution_count"), *counts.values()]
    if any(type(value) is not int or value < 0 for value in values):
        raise MigrationBlocked("activity_evidence_incomplete")
    if any(value != 0 for value in values):
        raise MigrationBlocked("current_day_activity_present")


def _same_balance(left, right, *, asset: str) -> bool:
    tolerance = 1e-4 if asset == "USDT" else 1e-8
    return abs(_finite(left) - _finite(right)) <= tolerance


def build_candidate(
    *,
    ledger: Mapping[str, object],
    ledger_update_time,
    control: Mapping[str, object] | None,
    evidence: Mapping[str, object],
    source_sha: str,
    observed_at: datetime,
) -> dict[str, object]:
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
        last_reset_date = datetime.strptime(last_reset, "%Y-%m-%d").date()
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
        "ledger_sha256": _canonical_sha(ledger),
        "preserved_ledger_sha256": _canonical_sha(
            {
                key: value
                for key, value in ledger.items()
                if key not in _ACCOUNTING_FIELDS
            }
        ),
        "control_sha256": _canonical_sha(control),
        "broker_snapshot_sha256": str(evidence.get("broker_snapshot_sha256") or ""),
        "activity_sha256": str(evidence.get("activity_sha256") or ""),
        "balance_snapshot_sha256": _canonical_sha(normalized_balances),
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
    candidate["candidate_sha256"] = _candidate_digest(candidate)
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
    candidate: Mapping[str, object], *, expected_digest: str, now: datetime
) -> None:
    if candidate.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("candidate_schema_invalid")
    actual = _candidate_digest(candidate)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        or actual != expected_digest
        or candidate.get("candidate_sha256") != actual
    ):
        raise ValueError("candidate_digest_mismatch")
    try:
        observed = datetime.fromisoformat(
            str(candidate["observed_at"]).replace("Z", "+00:00")
        )
        expires = datetime.fromisoformat(
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
    transaction, *, ledger_ref, owner_ref, control_ref, candidate, balance_snapshot
):
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
        or _canonical_sha(ledger_value) != candidate.get("ledger_sha256")
        or _canonical_sha(control_value) != candidate.get("control_sha256")
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
    if _canonical_sha(normalized_snapshot) != candidate.get("balance_snapshot_sha256"):
        raise MigrationAtomicPrecondition("migration_atomic_precondition_changed")
    patch = {**candidate["patch"], "last_balance_snapshot": normalized_snapshot}
    if not set(patch).issubset(_ACCOUNTING_FIELDS):
        raise ValueError("candidate_patch_invalid")
    transaction.update(ledger_ref, patch)


def _strict_balance_snapshot(client, positions, assets):
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


def _collect_evidence(client, *, ledger, now, expected):
    configured = tuple(dict.fromkeys((*_symbols_from_env(), "BTCUSDT", "BNBUSDT")))
    old_snapshot = ledger.get("last_balance_snapshot")
    old_assets = set(old_snapshot) if isinstance(old_snapshot, Mapping) else set()
    assets = (
        old_assets
        | {symbol[:-4] for symbol in configured if symbol.endswith("USDT")}
        | {"USDT", "BTC", "BNB"}
    )
    symbols = tuple(
        dict.fromkeys(
            (*configured, *(f"{asset}USDT" for asset in old_assets if asset != "USDT"))
        )
    )
    midnight = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    account = client.get_account()
    observations = collect_read_only_reconciliation_observations(
        client,
        strategy_symbols=symbols,
        local_execution_ledger=ledger,
        now=now,
        lookback=now - midnight,
        account_snapshot=account,
    )
    if digest(observations.account_scope) != expected.get("account_scope_sha256"):
        raise MigrationBlocked("account_scope_unverified")
    history = diagnose_balance_flows(client, start=midnight, end=now, now=now)
    raw_positions = account.get("balances") if isinstance(account, Mapping) else None
    if not isinstance(raw_positions, list):
        raise MigrationBlocked("spot_balance_rows_invalid")
    snapshot = _strict_balance_snapshot(client, raw_positions, assets)
    prices = {}
    if str(ledger.get("last_reset_date") or "") != now.date().isoformat():
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
        "account_scope_sha256": digest(observations.account_scope),
        "balance_snapshot": snapshot,
        "prices": prices,
        "open_order_count": len(observations.open_orders),
        **activity,
        "broker_snapshot_sha256": digest(
            {
                "positions": observations.positions,
                "open_orders": observations.open_orders,
                "recent_executions": observations.recent_executions,
            }
        ),
        "activity_sha256": digest(activity),
        "utc_date": now.date().isoformat(),
    }


def _verify_applied(refs, *, candidate, balance_snapshot):
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
            }
        )
        != candidate.get("preserved_ledger_sha256")
        or _canonical_sha(control_value) != candidate.get("control_sha256")
    ):
        raise MigrationApplyUncertain("migration_readback_mismatch")


def _refs():
    collection = get_firestore_client().collection("strategy")
    return {
        "ledger_ref": collection.document(LEDGER_DOCUMENT),
        "owner_ref": collection.document(OWNER_DOCUMENT),
        "control_ref": collection.document(CONTROL_DOCUMENT),
    }


def _read_source(refs):
    owner = refs["owner_ref"].get(retry=None)
    ledger = refs["ledger_ref"].get(retry=None)
    control = refs["control_ref"].get(retry=None)
    if owner.exists or not ledger.exists:
        raise MigrationBlocked("ledger_unavailable_or_owned")
    ledger_value = ledger.to_dict()
    control_value = control.to_dict() if control.exists else None
    _validate_control(control_value)
    _validate_safe_order_state(ledger_value)
    return ledger, ledger_value, control_value


def inspect_control(refs):
    """Read only control provenance and existence flags; never enter migration."""
    snapshot = refs["control_ref"].get(retry=None)
    owner = refs["owner_ref"].get(retry=None)
    ledger = refs["ledger_ref"].get(retry=None)
    value = snapshot.to_dict() if snapshot.exists else None
    control = value if isinstance(value, Mapping) else {}
    state = control.get("state")
    if not isinstance(state, str) or state not in {"RECONCILE_ONLY", "ACTIVE_LKG", "ROLLBACK_LKG", "PAUSED", "REDUCE_ONLY"}:
        state = "INVALID" if snapshot.exists else "ABSENT"
    recovery_id = control.get("recovery_id")
    if not isinstance(recovery_id, str) or not re.fullmatch(r"binance-[0-9]{1,20}-[0-9]{1,5}", recovery_id):
        recovery_id = None
    source = control.get("source")
    source_run = source.get("run") if isinstance(source, Mapping) else None
    if not (isinstance(source_run, Mapping) and type(source_run.get("id")) is int
            and 0 < source_run["id"] < 10**20
            and isinstance(source_run.get("head_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", source_run["head_sha"])):
        source_run = None
    else:
        source_run = {key: source_run[key] for key in ("id", "head_sha")}
    return {
        "status": "inspected", "stage": "accounting_migration_control_inspection",
        "control_exists": snapshot.exists, "state": state,
        "control_sha256": _canonical_sha(value),
        "control_update_time": _timestamp(snapshot.update_time) if snapshot.exists else None,
        "control_create_time": _timestamp(snapshot.create_time) if getattr(snapshot, "create_time", None) else None,
        "recovery_id": recovery_id, "source_run": source_run,
        "confirmation_present": isinstance(control.get("confirmation"), Mapping),
        "transition_plan_present": isinstance(control.get("transition_plan"), Mapping),
        "owner_exists": owner.exists, "ledger_exists": ledger.exists,
        "no_order": True, "write_performed": False,
    }


def _quiesce_control_transaction(transaction, refs):
    owner = refs["owner_ref"].get(transaction=transaction, retry=None)
    ledger = refs["ledger_ref"].get(transaction=transaction, retry=None)
    snapshot = refs["control_ref"].get(transaction=transaction, retry=None)
    control = snapshot.to_dict() if snapshot.exists else None
    if (owner.exists or not ledger.exists or not isinstance(control, Mapping)
            or control.get("state") != "ACTIVE_LKG"
            or _timestamp(snapshot.update_time) != QUIESCE_CONTROL_UPDATE_TIME
            or _canonical_sha(control) != QUIESCE_CONTROL_SHA256):
        raise MigrationBlocked("control_quiesce_precondition_changed")
    next_control_sha = _canonical_sha({**control, "state": "RECONCILE_ONLY"})
    ledger_sha = _canonical_sha(ledger.to_dict())
    transaction.update(refs["control_ref"], {"state": "RECONCILE_ONLY"})
    return next_control_sha, ledger_sha


def _quiesce_control(refs):
    from google.cloud import firestore

    @firestore.transactional
    def apply(transaction):
        return _quiesce_control_transaction(transaction, refs)

    try:
        expected_control_sha, ledger_sha = apply(get_firestore_client().transaction(max_attempts=1))
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationApplyUncertain("control_quiesce_outcome_uncertain") from None
    try:
        snapshot = refs["control_ref"].get(retry=None)
        ledger = refs["ledger_ref"].get(retry=None)
        owner = refs["owner_ref"].get(retry=None)
        if (not snapshot.exists or not ledger.exists or owner.exists
                or _canonical_sha(snapshot.to_dict()) != expected_control_sha
                or _canonical_sha(ledger.to_dict()) != ledger_sha):
            raise ValueError("readback_mismatch")
        return {
            "status": "quiesced", "stage": "accounting_migration_control_quiesce",
            "state": "RECONCILE_ONLY", "control_sha256": expected_control_sha,
            "control_update_time": _timestamp(snapshot.update_time),
            "ledger_unchanged": True, "no_order": True, "write_performed": True,
        }
    except Exception:
        raise MigrationApplyUncertain("control_quiesce_readback_uncertain") from None


def run(action: str, *, expected_digest: str = "", now: datetime | None = None):
    require_runtime_context()
    if action == "inspect":
        return inspect_control(_refs())
    if action == "quiesce":
        return _quiesce_control(_refs())
    fixed_now = now is not None
    now = now or datetime.now(timezone.utc)
    target = resolve_runtime_target_from_env(
        env=os.environ, expected_platform_id="binance"
    )
    if (
        str(getattr(getattr(target, "live_continuity", None), "state", "")).upper()
        != "RECONCILE_ONLY"
    ):
        raise MigrationBlocked("runtime_target_not_reconcile_only")
    expected = _expected_digests()
    if not expected:
        raise MigrationBlocked("expected_account_scope_missing")
    refs = _refs()
    ledger_snapshot, ledger, control = _read_source(refs)
    client = connect_client(
        os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30
    )
    evidence = _collect_evidence(client, ledger=ledger, now=now, expected=expected)
    decision_now = now if fixed_now else datetime.now(timezone.utc)
    if evidence["utc_date"] != decision_now.date().isoformat():
        raise MigrationBlocked("utc_day_changed_during_evidence")
    if action == "preview":
        candidate = build_candidate(
            ledger=ledger,
            ledger_update_time=ledger_snapshot.update_time,
            control=control,
            evidence=evidence,
            source_sha=os.environ["GITHUB_SHA"],
            observed_at=decision_now,
        )
        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_PATH.write_text(
            json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8"
        )
        return candidate["public_preview"]

    candidate = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    validate_candidate(candidate, expected_digest=expected_digest, now=decision_now)
    if (
        candidate.get("source_sha") != os.environ["GITHUB_SHA"]
        or candidate.get("account_scope_sha256") != evidence["account_scope_sha256"]
        or candidate.get("broker_snapshot_sha256") != evidence["broker_snapshot_sha256"]
        or candidate.get("activity_sha256") != evidence["activity_sha256"]
    ):
        raise MigrationBlocked("migration_evidence_changed")
    from google.cloud import firestore

    @firestore.transactional
    def apply(transaction):
        compare_and_apply(
            transaction,
            **refs,
            candidate=candidate,
            balance_snapshot=evidence["balance_snapshot"],
        )

    try:
        apply(get_firestore_client().transaction(max_attempts=1))
    except MigrationAtomicPrecondition:
        raise
    except Exception:
        raise MigrationApplyUncertain("migration_apply_outcome_uncertain") from None
    try:
        _verify_applied(
            refs, candidate=candidate, balance_snapshot=evidence["balance_snapshot"]
        )
    except MigrationApplyUncertain:
        raise
    except Exception:
        raise MigrationApplyUncertain("migration_readback_mismatch") from None
    return {
        "status": "applied",
        "candidate_sha256": candidate["candidate_sha256"],
        "no_order": True,
        "execution_authority_granted": False,
    }


def main(argv=None) -> int:
    parser = ArgumentParser(
        description="Preview or apply the bounded Binance daily-accounting migration"
    )
    parser.add_argument("action", choices=("inspect", "quiesce", "preview", "apply"))
    parser.add_argument("--expected-digest", default="")
    args = parser.parse_args(argv)
    if args.action != "apply" and args.expected_digest:
        parser.error("only apply accepts an expected digest")
    if args.action == "apply" and not re.fullmatch(
        r"[0-9a-f]{64}", args.expected_digest
    ):
        parser.error("apply requires the exact preview candidate digest")
    try:
        result = run(args.action, expected_digest=args.expected_digest)
    except MigrationApplyUncertain as exc:
        print(
            json.dumps(
                {
                    "status": "uncertain",
                    "stage": "accounting_migration_apply",
                    "reason_code": str(exc),
                    "no_retry": True,
                    "no_order": True,
                }
            )
        )
        return 2
    except MigrationBlocked as exc:
        # These are fixed internal guard codes, never raw broker exceptions.
        print(json.dumps({
            "status": "blocked", "stage": "accounting_migration",
            "reason_code": str(exc), "no_order": True,
        }))
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "stage": "accounting_migration",
                    "reason_code": "migration_blocked",
                    "no_order": True,
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
