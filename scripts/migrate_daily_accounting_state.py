#!/usr/bin/env python3
"""Inspect, preview, or atomically apply bounded Binance accounting operations.

The command has no order, transfer, redemption, subscription, cancellation, or
notification path.  Preview writes one short-lived redacted candidate file.
Apply accepts only that fixed artifact path, repeats the read-only evidence, and
updates an allowlist of fields in the existing Firestore ledger transaction.
The private Spot scope preview publishes once to the authenticated console.
"""

# ruff: noqa: E402

from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
import subprocess
import tempfile
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application.broker_reconciliation import (
    _expected_digests,
    calculate_broker_observation_sha256 as digest,
    collect_read_only_reconciliation_observations,
    collect_spot_usdt_external_cash_flows,
    diagnose_balance_flows,
)
from application.portfolio_service import maybe_rebase_daily_state_for_balance_change
from runtime_support import ExecutionIntegrityError
from live_services import get_firestore_client
from quant_platform_kit.binance import connect_client
from quant_platform_kit.common.broker_reconciliation_enrollment import BrokerReconciliationBaselineCandidate
from quant_platform_kit.common.runtime_target import resolve_runtime_target_from_env


REPOSITORY = "QuantStrategyLab/BinancePlatform"
LEDGER_DOCUMENT = "MULTI_ASSET_STATE"
OWNER_DOCUMENT = "MULTI_ASSET_STATE__owner"
CONTROL_DOCUMENT = "MULTI_ASSET_STATE__recovery"
SCHEMA_VERSION = "binance_daily_accounting_migration_candidate.v1"
NEW_BASIS = "trend_mark_plus_cash_flow_v1"
PREVIEW_PATH = Path("reports/binance-accounting-migration-preview/candidate.json")
REBASE_PROPOSAL_PATH = Path("reports/binance-accounting-rebase-proposal/proposal.cms")
PRIVATE_SCOPE_CONSOLE_URL = (
    "https://qsl-strategy-switch-console.pigbibi.workers.dev"
    "/api/internal/binance-private-scope"
)
# Operator-approved control from read-only Runtime 34586531344. One exact downgrade.
QUIESCE_CONTROL_SHA256 = "68318adcf853fe755409814c8707d3338f0af7927da396a60547c8375e9af5c4"
QUIESCE_CONTROL_UPDATE_TIME = "2026-09-08T18:31:35.004893Z"
# User approved the concrete new-start proposal from Runtime 34601984051.
# One exact ledger/control/quantity set; prices are freshly observed at execution.
APPROVED_REBASE_LEDGER_SHA256 = "6171bd4d33196b25da6b2a9b23311f46dd1dc0e8a324aaec83f657d6a5e2a042"
APPROVED_REBASE_CONTROL_SHA256 = "b590cdaa2f251eaff966c351dd40ba18af1d53c5742f7ac57b5f1ffd593b1e51"
APPROVED_REBASE_BALANCES_SHA256 = "e048d378056180b41c0ebf215a1a229748abb0f6983ed3f6dd481057713383f6"
REBASE_ARCHIVE_DOCUMENT = "MULTI_ASSET_STATE__before_rebase_34601984051"
TTL = timedelta(minutes=10)
_EARN_PAGE_SIZE = 100
_MAX_SPOT_BALANCE_ROWS = 5000
_MAX_PRIVATE_SCOPE_BODY_BYTES = 256 * 1024
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



def collect_prospective_opening(client, *, ledger, expected, now, clock=None,
                                collect_cash_flows=collect_spot_usdt_external_cash_flows):
    """Capture a current opening proposal; historical mismatches remain archived.

    No historical reward sum becomes a starting principal or new-period profit.
    The external cursor fingerprints prior events solely to prevent replay.
    """
    from application.earn_accrual import EarnCheckpointUnavailable, collect_earn_checkpoint, compare_earn_checkpoints
    clock = clock or (lambda: datetime.now(timezone.utc))
    assets = tuple(sorted(ledger.get('last_balance_snapshot', {})))
    if not assets or now.tzinfo is None:
        raise MigrationBlocked('prospective_opening_scope_missing')
    def read_orders():
        orders = client.get_open_orders()
        if not isinstance(orders, list) or orders:
            raise MigrationBlocked('prospective_opening_orders_unsettled')
    stage = 'initial_orders'
    try:
        read_orders()
        first = collect_earn_checkpoint(client, assets=assets, observed_at=now,
                                        expected_account_scope_sha256=expected['account_scope_sha256'])
        stage = 'opening_cash_cursor'
        cash = collect_cash_flows(client, now=now, cursor=None)
        if not isinstance(cash.get('cursor'), dict):
            raise MigrationBlocked('prospective_opening_cursor_unavailable')
        stage = 'prices'
        prices = {}
        for asset, row in first['assets'].items():
            if asset != 'USDT' and Decimal(row['quantity']) > 0:
                price = _finite(client.get_avg_price(symbol=f'{asset}USDT')['price'])
                if price <= 0:
                    raise MigrationBlocked('price_snapshot_incomplete')
                prices[f'{asset}USDT'] = price
        read_orders()
        second = collect_earn_checkpoint(client, assets=assets, observed_at=now,
                                         expected_account_scope_sha256=expected['account_scope_sha256'])
        ended_at = clock()
        if not now < ended_at <= now + timedelta(minutes=2):
            raise MigrationBlocked('prospective_opening_observation_expired')
        second['observed_at'] = ended_at.isoformat()
        try:
            compare_earn_checkpoints(first, second, verified_net_changes={asset: '0' for asset in assets})
        except ValueError:
            raise MigrationBlocked('prospective_opening_changed_during_read') from None
        stage = 'closing_cash_cursor'
        closing_cash = collect_cash_flows(client, now=ended_at, cursor=None)
        if (not isinstance(closing_cash.get('cursor'), dict)
                or cash['cursor'].get('records') != closing_cash['cursor'].get('records')):
            raise MigrationBlocked('prospective_opening_flows_changed_during_read')
        # Only the sampling interval is checked for trades; pre-start history is archived.
        stage = 'sampling_executions'
        observations = collect_read_only_reconciliation_observations(
            client, strategy_symbols=tuple(f'{a}USDT' for a in assets if a != 'USDT'),
            local_execution_ledger=ledger, now=ended_at, lookback=ended_at-now)
        if (digest(observations.account_scope) != expected['account_scope_sha256']
                or observations.open_orders or observations.recent_executions):
            raise MigrationBlocked('prospective_opening_activity_during_read')
        second['observed_at'] = ended_at.isoformat()
        return {
            'opening_mode': 'prospective', 'current_observation_complete': True,
            'account_scope_sha256': second['account_scope_sha256'],
            'balance_snapshot': {asset: float(Decimal(row['quantity'])) for asset, row in second['assets'].items()},
            'prices': prices, 'open_order_count': 0, 'history_complete': False,
            'history_counts': None, 'recent_execution_count': None,
            'observation_started_at': now.isoformat(), 'observation_completed_at': ended_at.isoformat(),
            'earn_accrual_checkpoint': second, 'external_cash_flow_cursor': closing_cash['cursor'],
            'utc_date': ended_at.astimezone(timezone.utc).date().isoformat(),
        }
    except MigrationBlocked:
        raise
    except EarnCheckpointUnavailable as error:
        raise MigrationBlocked(str(error)) from None
    except Exception:
        raise MigrationBlocked(f'prospective_opening_{stage}_unavailable') from None


def build_rebase_proposal(*, ledger, evidence, observed_at):
    """Informational new-start preview, deliberately not an apply candidate."""
    _validate_safe_order_state(ledger)
    prospective = evidence.get("opening_mode") == "prospective"
    if prospective:
        checkpoint = evidence.get("earn_accrual_checkpoint", {})
        if (evidence.get("current_observation_complete") is not True
                or checkpoint.get("account_scope_sha256") != evidence.get("account_scope_sha256")
                or not isinstance(evidence.get("external_cash_flow_cursor"), Mapping)):
            raise MigrationBlocked("rebase_proposal_current_state_unsettled")
    if (type(evidence.get("open_order_count")) is not int or evidence["open_order_count"] != 0
            or (not prospective and (evidence.get("history_complete") is not True
                or type(evidence.get("recent_execution_count")) is not int
                or evidence["recent_execution_count"] != 0))):
        raise MigrationBlocked("rebase_proposal_current_state_unsettled")
    balances = evidence.get("balance_snapshot")
    prices = evidence.get("prices")
    if (not isinstance(balances, Mapping) or not {"USDT", "BTC", "BNB"}.issubset(balances)
            or not isinstance(prices, Mapping)):
        raise MigrationBlocked("rebase_proposal_balances_incomplete")
    assets = []
    for asset, raw_quantity in sorted(balances.items()):
        if not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", asset):
            raise MigrationBlocked("rebase_proposal_asset_invalid")
        quantity = _finite(raw_quantity)
        price = 1.0 if asset == "USDT" else _finite(prices.get(f"{asset}USDT", 0.0))
        if quantity > 0 and price <= 0:
            raise MigrationBlocked("price_snapshot_incomplete")
        assets.append({"asset": asset, "quantity": quantity, "price_usdt": price,
                       "value_usdt": _finite(quantity * price)})
    total = _finite(sum(row["value_usdt"] for row in assets))
    trend = _finite(sum(row["value_usdt"] for row in assets if row["asset"] not in {"USDT", "BTC", "BNB"}))
    if total <= 0 or observed_at.tzinfo is None:
        raise MigrationBlocked("rebase_proposal_valuation_invalid")
    proposed = {
        "daily_trend_pnl_basis": NEW_BASIS,
        "daily_trend_cash_flow_usdt": 0.0, "daily_trend_net_invested_usdt": 0.0,
        "daily_trend_third_fee_usdt": 0.0, "daily_trend_risk_base_usdt": trend,
        "daily_equity_base": total, "daily_trend_equity_base": trend,
        "last_balance_snapshot": {row["asset"]: row["quantity"] for row in assets},
        "last_reset_date": observed_at.astimezone(timezone.utc).date().isoformat(),
    }
    if prospective:
        proposed["earn_accrual_checkpoint"] = copy.deepcopy(evidence["earn_accrual_checkpoint"])
        proposed["earn_accounted_net_changes"] = {a: "0" for a in evidence["earn_accrual_checkpoint"]["assets"]}
        proposed["external_cash_flow_cursor"] = copy.deepcopy(evidence["external_cash_flow_cursor"])
    return {
        "opening_mode": "prospective" if prospective else "legacy_preview",
        "automatic_accounting_ready": False,
        "recovery_ready": False,
        "observation_started_at": evidence.get("observation_started_at"),
        "observation_completed_at": evidence.get("observation_completed_at"),
        "status": "awaiting_operator_decision", "executable_candidate": False,
        "historical_difference_unresolved": True, "complete_balance_reconciliation": False,
        "observed_at": observed_at.isoformat(), "assets": assets,
        "valuation_scope": "managed_assets_spot_plus_flexible_earn",
        "valuation_price_source": "binance_get_avg_price_estimate",
        "old_fields": {key: copy.deepcopy(ledger[key]) for key in proposed if key in ledger},
        "old_missing_fields": sorted(set(proposed) - set(ledger)),
        "proposed_fields": proposed,
        "preserved_circuit_breaker_latch": ledger.get("is_circuit_broken"),
        "preserve_all_other_ledger_fields": True,
        "requires_full_old_ledger_archive_before_apply": True,
        "requires_fresh_preflight_and_separate_apply_approval": True,
        "pre_start_income_classification": "unreconstructed_history",
        "current_day_activity_counts": copy.deepcopy(evidence.get("history_counts")),
        "no_order": True, "write_performed": False, "execution_authority_granted": False,
    }


def encrypt_rebase_proposal(proposal, *, certificate):
    """Use standard CMS encryption; plaintext stays in memory on the runner."""
    if not isinstance(certificate, str) or not 100 <= len(certificate) <= 12000:
        raise MigrationBlocked("proposal_recipient_certificate_invalid")
    try:
        with tempfile.TemporaryDirectory() as directory:
            cert_path = Path(directory) / "recipient.pem"
            cert_path.write_text(certificate, encoding="utf-8")
            return subprocess.run(
                ["openssl", "cms", "-encrypt", "-aes-256-cbc", "-binary", "-outform", "DER", str(cert_path)],
                input=json.dumps(proposal, sort_keys=True, allow_nan=False).encode(),
                capture_output=True, check=True, timeout=20,
            ).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        raise MigrationBlocked("proposal_encryption_failed") from None


def _approved_rebase_fields(*, ledger, control, evidence, now):
    if (digest(ledger) != APPROVED_REBASE_LEDGER_SHA256
            or digest(control) != APPROVED_REBASE_CONTROL_SHA256
            or digest(evidence.get("balance_snapshot")) != APPROVED_REBASE_BALANCES_SHA256):
        raise MigrationBlocked("approved_rebase_source_changed")
    _validate_control(control)
    if evidence.get("account_scope_sha256") != control["source"]["original_evidence"]["account_scope_sha256"]:
        raise MigrationBlocked("account_scope_unverified")
    counts = evidence.get("history_counts")
    if (not isinstance(counts, Mapping) or not counts
            or any(type(value) is not int or value < 0 or (name != "earn_rewards" and value != 0)
                   for name, value in counts.items())):
        raise MigrationBlocked("approved_rebase_activity_changed")
    return build_rebase_proposal(ledger=ledger, evidence=evidence, observed_at=now)["proposed_fields"]


def _rebase_transaction(transaction, *, refs, archive_ref, ledger, control,
                        ledger_update_time, proposed_fields, observed_at, started_at):
    owner = refs["owner_ref"].get(transaction=transaction, retry=None)
    current = refs["ledger_ref"].get(transaction=transaction, retry=None)
    recovery = refs["control_ref"].get(transaction=transaction, retry=None)
    archive = archive_ref.get(transaction=transaction, retry=None)
    if (owner.exists or archive.exists or not current.exists or not recovery.exists
            or digest(current.to_dict()) != APPROVED_REBASE_LEDGER_SHA256
            or digest(ledger) != APPROVED_REBASE_LEDGER_SHA256
            or digest(recovery.to_dict()) != APPROVED_REBASE_CONTROL_SHA256
            or digest(control) != APPROVED_REBASE_CONTROL_SHA256
            or _timestamp(current.update_time) != _timestamp(ledger_update_time)):
        raise MigrationAtomicPrecondition("approved_rebase_atomic_precondition_changed")
    _validate_safe_order_state(current.to_dict())
    _validate_control(control)
    if (set(proposed_fields) != _ACCOUNTING_FIELDS
            or digest(proposed_fields.get("last_balance_snapshot")) != APPROVED_REBASE_BALANCES_SHA256):
        raise MigrationAtomicPrecondition("approved_rebase_patch_invalid")
    marker = {"archive_document": REBASE_ARCHIVE_DOCUMENT,
              "started_at": started_at.isoformat(), "opening_balance_observed_at": observed_at.isoformat(),
              "historical_difference_unresolved": True, "approved_proposal_run_id": "34601984051"}
    patch = {**proposed_fields, "accounting_rebase": marker}
    new_ledger_sha = digest({**ledger, **patch})
    backup = {"ledger": copy.deepcopy(ledger), "recovery_control": copy.deepcopy(control),
              "ledger_update_time": _timestamp(ledger_update_time), **marker,
              "new_ledger_sha256": new_ledger_sha, "valuation_price_source": "binance_get_avg_price_estimate"}
    # Both writes commit together; create prevents any archive replacement.
    transaction.create(archive_ref, backup)
    transaction.update(refs["ledger_ref"], patch)
    return {"new_ledger_sha256": new_ledger_sha, "archive_sha256": digest(backup)}


def _apply_approved_rebase(refs, *, client, expected, now, fixed_now):
    from google.cloud import firestore

    snapshot, ledger, control = _read_source(refs)
    archive_ref = refs["ledger_ref"].parent.document(REBASE_ARCHIVE_DOCUMENT)
    if (digest(ledger) != APPROVED_REBASE_LEDGER_SHA256
            or digest(control) != APPROVED_REBASE_CONTROL_SHA256 or archive_ref.get(retry=None).exists):
        raise MigrationBlocked("approved_rebase_source_changed")
    evidence = _collect_evidence(client, ledger=ledger, now=now, expected=expected, require_prices=True)
    decision_now = now if fixed_now else datetime.now(timezone.utc)
    if (decision_now - now > TTL or evidence["utc_date"] != decision_now.date().isoformat()):
        raise MigrationBlocked("approved_rebase_evidence_stale")
    proposed = _approved_rebase_fields(ledger=ledger, control=control, evidence=evidence, now=decision_now)

    @firestore.transactional
    def apply(transaction):
        return _rebase_transaction(transaction, refs=refs, archive_ref=archive_ref, ledger=ledger,
            control=control, ledger_update_time=snapshot.update_time, proposed_fields=proposed,
            observed_at=now, started_at=decision_now)

    try:
        written = apply(get_firestore_client().transaction(max_attempts=1))
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationApplyUncertain("approved_rebase_write_uncertain") from None
    try:
        after_snapshot, after_ledger, after_control = _read_source(refs)
        archive = archive_ref.get(retry=None)
        if (digest(after_ledger) != written["new_ledger_sha256"]
                or digest(after_control) != APPROVED_REBASE_CONTROL_SHA256
                or not archive.exists or digest(archive.to_dict()) != written["archive_sha256"]):
            raise ValueError("readback mismatch")
        updated_at = _timestamp(after_snapshot.update_time)
    except Exception:
        raise MigrationApplyUncertain("approved_rebase_readback_uncertain") from None
    return {"status": "rebased", "stage": "accounting_rebase_apply", "archive_document": REBASE_ARCHIVE_DOCUMENT,
            "ledger_update_time": updated_at, "historical_difference_unresolved": True,
            "old_ledger_archived": True, "control_unchanged": True, "no_order": True,
            "write_performed": True, "execution_authority_granted": False}


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


def _ledger_scope(ledger):
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
    return assets, symbols


def _collect_evidence(client, *, ledger, now, expected, require_prices=False):
    assets, symbols = _ledger_scope(ledger)
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


def _private_spot_account(account, *, expected_account_scope_sha256):
    if not isinstance(account, Mapping):
        raise MigrationBlocked("private_scope_balance_invalid")
    uid = str(account.get("uid") or "")
    if not uid or digest({"account_uid": uid}) != expected_account_scope_sha256:
        raise MigrationBlocked("account_scope_unverified")
    rows = account.get("balances")
    if not isinstance(rows, list) or len(rows) > _MAX_SPOT_BALANCE_ROWS:
        raise MigrationBlocked("private_scope_balance_invalid")
    normalized = {}
    try:
        for row in rows:
            if not isinstance(row, Mapping):
                raise MigrationBlocked("private_scope_balance_invalid")
            asset = str(row.get("asset") or "").strip().upper()
            if (
                not 1 <= len(asset) <= 20
                or not asset.isalnum()
                or asset in normalized
            ):
                raise MigrationBlocked("private_scope_balance_invalid")
            free = Decimal(str(row.get("free")))
            locked = Decimal(str(row.get("locked")))
            if (
                not free.is_finite()
                or not locked.is_finite()
                or free < 0
                or locked < 0
            ):
                raise MigrationBlocked("private_scope_balance_invalid")
            normalized[asset] = (free, locked)
    except (InvalidOperation, TypeError, ValueError):
        raise MigrationBlocked("private_scope_balance_invalid") from None
    return uid, tuple(sorted(normalized.items()))


def _validated_private_scope_source(refs, *, initial_source=None):
    # Lazy import avoids a module cycle: rebased_recovery aliases the approved
    # roots from this migration module.
    from application.rebased_recovery import validate_post_rebase_material

    ledger_snapshot, ledger, control = initial_source or _read_source(refs)
    archive_ref = refs["ledger_ref"].parent.document(REBASE_ARCHIVE_DOCUMENT)
    archive_snapshot = archive_ref.get(retry=None)
    if not archive_snapshot.exists:
        raise MigrationBlocked("private_scope_source_invalid")
    archive = archive_snapshot.to_dict()
    try:
        material = validate_post_rebase_material(ledger, archive)
        approved_account_scope = archive["recovery_control"]["source"][
            "original_evidence"
        ]["account_scope_sha256"]
        if not isinstance(approved_account_scope, str):
            raise ValueError("account scope")
    except (KeyError, TypeError, ValueError):
        raise MigrationBlocked("private_scope_source_invalid") from None
    return {
        "archive_ref": archive_ref,
        "material": material,
        "approved_account_scope_sha256": approved_account_scope,
        "binding": {
            "ledger_sha256": digest(ledger),
            "ledger_update_time": _timestamp(ledger_snapshot.update_time),
            "control_sha256": digest(control),
            "archive_sha256": digest(archive),
        },
    }


def collect_private_spot_scope_preview(
    refs,
    *,
    client,
    expected,
    observed_at,
    source_sha,
    initial_source=None,
):
    """Collect an encrypted-output-only view of non-managed Spot balances."""
    before = _validated_private_scope_source(refs, initial_source=initial_source)
    account_scope = expected.get("account_scope_sha256")
    if (
        not isinstance(account_scope, str)
        or len(account_scope) != 64
        or account_scope != before["approved_account_scope_sha256"]
    ):
        raise MigrationBlocked("account_scope_unverified")
    first = _private_spot_account(
        client.get_account(), expected_account_scope_sha256=account_scope
    )
    second = _private_spot_account(
        client.get_account(), expected_account_scope_sha256=account_scope
    )
    if first != second:
        raise MigrationBlocked("private_scope_snapshot_changed")

    after = _validated_private_scope_source(refs)
    if before["binding"] != after["binding"]:
        raise MigrationBlocked("private_scope_source_changed")

    managed_assets = set(before["material"]["opening_quantities"])
    non_managed = [
        {"asset": asset, "free": str(free), "locked": str(locked)}
        for asset, (free, locked) in first[1]
        if asset not in managed_assets and (free != 0 or locked != 0)
    ]
    return {
        "status": "observed",
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "source_kind": "post_rebase",
        "source": {
            "source_sha": source_sha,
            "archive_document": REBASE_ARCHIVE_DOCUMENT,
            "archive_sha256": before["binding"]["archive_sha256"],
            "ledger_sha256": before["binding"]["ledger_sha256"],
            "account_scope_sha256": account_scope,
        },
        "scope": "non_managed_nonzero_spot_assets",
        "non_managed_nonzero_spot_assets": non_managed,
        "historical_difference_unresolved": True,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }


def _private_scope_publication_context():
    token = os.environ.get("RECONCILIATION_RECOVERY_SYNC_TOKEN", "")
    source_run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not token or not re.fullmatch(r"[1-9][0-9]{0,19}", source_run_id):
        raise MigrationBlocked("private_scope_publication_config_invalid")
    return token, source_run_id


def build_private_spot_scope_payload(preview, *, source_run_id, source_sha):
    """Build the one private console request without adding account surfaces."""
    if (
        type(source_run_id) is not str
        or not re.fullmatch(r"[1-9][0-9]{0,19}", source_run_id)
        or type(source_sha) is not str
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
    ):
        raise MigrationBlocked("private_scope_payload_invalid")
    try:
        assets = [
            {
                "asset": row["asset"],
                "free": format(Decimal(row["free"]), "f"),
                "locked": format(Decimal(row["locked"]), "f"),
            }
            for row in preview["non_managed_nonzero_spot_assets"]
        ]
        payload = {
            "platform": "binance",
            "observed_at": preview["observed_at"],
            "source_run_id": source_run_id,
            "source_sha": source_sha,
            "account_scope_sha256": preview["source"]["account_scope_sha256"],
            "assets": assets,
            "historical_difference_unresolved": True,
            "no_order": True,
            "execution_authority_granted": False,
        }
        encoded = json.dumps(payload).encode()
    except (InvalidOperation, KeyError, TypeError, ValueError):
        raise MigrationBlocked("private_scope_payload_invalid") from None
    if len(encoded) > _MAX_PRIVATE_SCOPE_BODY_BYTES:
        raise MigrationBlocked("private_scope_payload_too_large")
    return payload


def publish_private_spot_scope_preview(preview, *, token, source_run_id, source_sha):
    """POST one bounded private report and require its exact acknowledgement."""
    from scripts.binance_recovery_controller import request_json

    payload = build_private_spot_scope_payload(
        preview, source_run_id=source_run_id, source_sha=source_sha
    )
    try:
        acknowledgement = request_json(
            PRIVATE_SCOPE_CONSOLE_URL, token, payload=payload
        )
        expected_keys = {"ok", "observed_at", "source_run_id", "asset_count"}
        if (
            set(acknowledgement) != expected_keys
            or acknowledgement.get("ok") is not True
            or type(acknowledgement.get("observed_at")) is not str
            or acknowledgement["observed_at"] != payload["observed_at"]
            or type(acknowledgement.get("source_run_id")) is not str
            or acknowledgement["source_run_id"] != source_run_id
            or type(acknowledgement.get("asset_count")) is not int
            or acknowledgement["asset_count"] != len(payload["assets"])
        ):
            raise ValueError("private scope acknowledgement mismatch")
    except Exception:
        raise MigrationApplyUncertain("private_scope_publication_unknown") from None


def audit_ledger(refs, *, client, expected, now):
    """Compare two distinct balance bases without granting migration authority."""
    ledger_snapshot, ledger, control = _read_source(refs)
    # Reuse the exact reviewed control, allowing only its approved downgrade.
    if (not isinstance(control, Mapping)
            or digest({**control, "state": "ACTIVE_LKG"}) != QUIESCE_CONTROL_SHA256):
        raise MigrationBlocked("audit_recovery_source_changed")
    try:
        source = control["source"]
        original = source["original_evidence"]
        baseline = BrokerReconciliationBaselineCandidate.from_dict(control["candidate"]).expected_digests
        recovered_expected = {**baseline, "account_scope_sha256": expected["account_scope_sha256"]}
        if (source["frozen_expected_sha256"] != digest(expected)
                or original["account_scope_sha256"] != expected["account_scope_sha256"]
                or any(baseline[key] != original[key] for key in ("positions_sha256", "cash_sha256"))):
            raise ValueError("source binding")
        start = datetime.fromisoformat(original["observed_at"].replace("Z", "+00:00"))
        if now.tzinfo is None or start.tzinfo is None:
            raise ValueError("window")
        old = ledger["last_balance_snapshot"]
        if not isinstance(old, Mapping) or not old:
            raise ValueError("snapshot")
        assets, symbols = _ledger_scope(ledger)
        if any(not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", asset) for asset in assets):
            raise ValueError("asset")
        reset_date = datetime.strptime(ledger["last_reset_date"], "%Y-%m-%d").date().isoformat()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MigrationBlocked("audit_source_invalid") from None
    post_rebase_source = None
    if "accounting_rebase" in ledger:
        post_rebase_source = _validated_private_scope_source(refs, initial_source=(ledger_snapshot, ledger, control))
        start = post_rebase_source["material"]["opening_balance_observed_at"]
    if not start < now or now - start > timedelta(days=7):
        raise MigrationBlocked("audit_source_invalid")
    account = client.get_account()
    observations = collect_read_only_reconciliation_observations(
        client, strategy_symbols=symbols, local_execution_ledger=ledger,
        now=now, lookback=now - start, account_snapshot=account,
    )
    if digest(observations.account_scope) != expected["account_scope_sha256"]:
        raise MigrationBlocked("account_scope_unverified")
    balances = _strict_balance_snapshot(client, account["balances"], assets)
    comparison = {
        asset: "MISSING_IN_LEDGER" if asset not in old else
        "MATCH" if _same_balance(old[asset], balances[asset], asset=asset) else "MISMATCH"
        for asset in sorted(assets)
    }
    execution_counts = {}
    for trade in observations.recent_executions:
        symbol = trade.get("symbol")
        if symbol not in symbols:
            raise MigrationBlocked("audit_execution_symbol_invalid")
        execution_counts[symbol] = execution_counts.get(symbol, 0) + 1
    btc_spot = next(
        float(Decimal(row["free"]) + Decimal(row["locked"]))
        for row in account["balances"] if row["asset"] == "BTC"
    )
    btc_change = "UNKNOWN"
    if "BTC" in old:
        btc_change = "UNCHANGED" if _same_balance(old["BTC"], balances["BTC"], asset="BTC") else (
            "INCREASE" if balances["BTC"] > _finite(old["BTC"]) else "DECREASE"
        )
    history = diagnose_balance_flows(
        client, start=start, end=now, now=now, account=account,
        expected_digests=recovered_expected,
        reward_quantity_changes={asset: Decimal(str(balances[asset])) - Decimal(str(old[asset]))
                                 for asset in assets if asset in old},
    )
    if history.get("history_complete_for_requested_surfaces") is not True:
        raise MigrationBlocked("audit_history_incomplete")
    bnb_wallet_activity = None
    if comparison.get("BNB") == "MISMATCH":
        from application.broker_reconciliation import diagnose_bnb_wallet_activity
        bnb_wallet_activity = diagnose_bnb_wallet_activity(client, start=start, end=now)
    after_account = client.get_account()
    after_balances = _strict_balance_snapshot(client, after_account["balances"], assets)
    after_snapshot, after_ledger, after_control = _read_source(refs)
    if (digest(account["balances"]) != digest(after_account["balances"])
            or any(not _same_balance(balances[a], after_balances[a], asset=a) for a in assets)
            or digest(after_ledger) != digest(ledger)
            or _timestamp(after_snapshot.update_time) != _timestamp(ledger_snapshot.update_time)
            or digest(after_control) != digest(control)):
        raise MigrationBlocked("audit_state_changed_during_read")
    if post_rebase_source is not None and _validated_private_scope_source(refs)["binding"] != post_rebase_source["binding"]:
        raise MigrationBlocked("audit_state_changed_during_read")
    return {
        "status": "audited", "stage": "accounting_ledger_audit",
        "observed_at": now.isoformat(),
        "ledger_update_time": _timestamp(ledger_snapshot.update_time),
        "ledger_last_reset_date": reset_date,
        "ledger_uses_new_accounting_basis": ledger.get("daily_trend_pnl_basis") == NEW_BASIS,
        "circuit_breaker_latched": ledger.get("is_circuit_broken") is True,
        "ledger_balance_scope": "managed_assets_spot_plus_flexible_earn",
        "ledger_balance_comparison": comparison,
        # Legacy snapshot stores quantities only. Document update/reset times
        # cannot establish when those quantities were observed.
        "ledger_snapshot_observation_time_available": post_rebase_source is not None,
        "missing_ledger_nonzero_assets": [a for a in sorted(assets) if a not in old and balances[a] > 0],
        "btc_total_balance_change": btc_change,
        "btc_ledger_matches_current_spot_only": "BTC" in old and _same_balance(old["BTC"], btc_spot, asset="BTC"),
        "btc_has_flexible_earn_balance": balances["BTC"] > btc_spot + 1e-8,
        "bnb_has_flexible_earn_balance": balances.get("BNB", 0) > next(
            (float(Decimal(row["free"]) + Decimal(row["locked"])) for row in account["balances"] if row["asset"] == "BNB"), 0.0) + 1e-8,
        "bnb_quantity_change": ("UNKNOWN" if "BNB" not in old else
                                "INCREASE" if balances["BNB"] > old["BNB"] else
                                "DECREASE" if balances["BNB"] < old["BNB"] else "UNCHANGED"),
        "bnb_wallet_activity": bnb_wallet_activity,
        "realtime_reward_interval_alignment": "UNVERIFIED_DAILY_HISTORY_VS_INTRADAY_BALANCES",
        "history_window_basis": "approved_opening" if post_rebase_source else "legacy_recovery",
        "recent_execution_window_start": start.isoformat(),
        "recent_execution_count": len(observations.recent_executions),
        "recent_execution_counts_by_symbol": execution_counts,
        "open_order_count": len(observations.open_orders),
        "recovered_spot_window_start": start.isoformat(),
        "recovered_spot_history": history,
        "complete_balance_reconciliation": False,
        "ledger_unchanged": True, "no_order": True, "write_performed": False,
        "execution_authority_granted": False,
    }


def preview_external_cash_flow(refs, *, client, expected, now, initial_source=None):
    """Replay the production deposit consumer on a copy, without ledger writes."""
    snapshot, ledger, control = initial_source or _read_source(refs)
    assets, _ = _ledger_scope(ledger)
    if len(assets) > 32 or any(not isinstance(a, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", a) for a in assets):
        raise MigrationBlocked("cash_flow_preview_scope_invalid")
    account = client.get_account()
    _, spot = _private_spot_account(account, expected_account_scope_sha256=expected["account_scope_sha256"])
    balances = _strict_balance_snapshot(client, account["balances"], assets)
    try:
        flows = collect_spot_usdt_external_cash_flows(
            client, now=now, cursor=copy.deepcopy(ledger.get("external_cash_flow_cursor")),
        )
    except ValueError as exc:
        safe_codes = {
            "external_cash_flow_cursor_invalid", "external_cash_flow_history_read_failed",
            "external_cash_flow_history_incomplete", "external_cash_flow_record_invalid",
            "external_cash_flow_record_changed", "external_cash_flow_cursor_capacity_exceeded",
            "external_cash_flow_window_invalid",
        }
        reason = str(exc) if str(exc) in safe_codes else "cash_flow_preview_read_failed"
        raise MigrationBlocked(reason) from None
    after_account = client.get_account()
    _, after_spot = _private_spot_account(after_account, expected_account_scope_sha256=expected["account_scope_sha256"])
    after_balances = _strict_balance_snapshot(client, after_account["balances"], assets)
    after_snapshot, after_ledger, after_control = _read_source(refs)
    if (spot != after_spot or balances != after_balances or ledger != after_ledger
            or control != after_control or snapshot.update_time != after_snapshot.update_time):
        raise MigrationBlocked("cash_flow_preview_state_changed")
    result = {
        "stage": "external_cash_flow_preview", "observed_at": now.isoformat(),
        "cash_flow_history_read": True, "cursor_present": ledger.get("external_cash_flow_cursor") is not None,
        "new_confirmed_deposit_count": flows["new_confirmed_deposit_count"],
        "unmanaged_spot_asset_count": sum(1 for a, (free, locked) in spot if a not in assets and free + locked > 0),
        "complete_balance_reconciliation": False, "write_performed": False,
        "ledger_unchanged": True, "no_order": True, "execution_authority_granted": False,
    }
    state, report = copy.deepcopy(ledger), {}
    try:
        reconciled = maybe_rebase_daily_state_for_balance_change(
            state, SimpleNamespace(client=client, now_utc=now), report,
            None, None, balances, [],
            collect_external_cash_flows_fn=lambda *_args, **_kwargs: copy.deepcopy(flows),
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            append_log_fn=lambda *_args: None, translate_fn=lambda key, **_kwargs: key,
        )
    except ExecutionIntegrityError:
        diagnostics = report.get("diagnostics", {})
        reason = diagnostics.get("external_cash_flow", {}).get("reason_code") or diagnostics.get("balance_change", {}).get("reason_code")
        safe_codes = {
            "external_cash_flow_cursor_missing", "external_cash_flow_late_completion",
            "external_cash_flow_balance_mismatch", "external_withdrawal_accounting_unsupported",
            "external_cash_flow_scope_unsupported",
        }
        return {**result, "status": "blocked", "reason_code": reason if reason in safe_codes else "cash_flow_preview_unverifiable"}
    return {**result, "status": "reconciled_preview" if reconciled else "baseline_preview" if flows["bootstrap"] else "no_new_deposit"}


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
    publication = (
        _private_scope_publication_context() if action == "scope-preview" else None
    )
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
    if action == "cash-flow-preview":
        return preview_external_cash_flow(
            refs, client=client, expected=expected, now=now,
            initial_source=(ledger_snapshot, ledger, control),
        )
    if action == "scope-preview":
        preview = collect_private_spot_scope_preview(
            refs,
            client=client,
            expected=expected,
            observed_at=now,
            source_sha=os.environ["GITHUB_SHA"],
            initial_source=(ledger_snapshot, ledger, control),
        )
        publish_private_spot_scope_preview(
            preview,
            token=publication[0],
            source_run_id=publication[1],
            source_sha=os.environ["GITHUB_SHA"],
        )
        return {
            "status": "private_scope_published",
            "stage": "private_scope_publication",
            "ledger_unchanged": True,
            "no_order": True,
            "execution_authority_granted": False,
            "report_write_performed": True,
        }
    if action == "audit":
        return audit_ledger(refs, client=client, expected=expected, now=now)
    if action == "rebase-apply":
        return _apply_approved_rebase(refs, client=client, expected=expected, now=now, fixed_now=fixed_now)
    evidence = (collect_prospective_opening(client, ledger=ledger, now=now, expected=expected)
                if action == "rebase-proposal" else
                _collect_evidence(client, ledger=ledger, now=now, expected=expected))
    decision_now = now if fixed_now else datetime.now(timezone.utc)
    if evidence["utc_date"] != decision_now.date().isoformat():
        raise MigrationBlocked("utc_day_changed_during_evidence")
    if action == "rebase-proposal":
        proposal = build_rebase_proposal(ledger=ledger, evidence=evidence, observed_at=decision_now)
        after_snapshot, after_ledger, after_control = _read_source(refs)
        if (digest(ledger) != digest(after_ledger) or digest(control) != digest(after_control)
                or _timestamp(ledger_snapshot.update_time) != _timestamp(after_snapshot.update_time)):
            raise MigrationBlocked("proposal_ledger_changed_during_read")
        proposal.update(source_sha=os.environ["GITHUB_SHA"], ledger_update_time=_timestamp(ledger_snapshot.update_time),
                        ledger_sha256=digest(ledger), control_sha256=digest(control))
        encrypted = encrypt_rebase_proposal(proposal, certificate=os.environ.get("PROPOSAL_RECIPIENT_CERTIFICATE", ""))
        REBASE_PROPOSAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        REBASE_PROPOSAL_PATH.write_bytes(encrypted)
        return {"status": "encrypted_proposal_ready", "stage": "accounting_rebase_proposal",
                "executable_candidate": False, "ledger_unchanged": True, "no_order": True,
                "write_performed": False, "execution_authority_granted": False}
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
    parser.add_argument(
        "action",
        choices=(
            "inspect",
            "quiesce",
            "audit",
            "preview",
            "scope-preview",
            "cash-flow-preview",
            "rebase-proposal",
            "rebase-apply",
            "apply",
        ),
    )
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
                    "stage": "private_scope_publication"
                    if args.action == "scope-preview"
                    else "accounting_migration_apply",
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
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
