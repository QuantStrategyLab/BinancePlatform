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
import time
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
    BinanceReconciliationReadError,
    _expected_digests,
    calculate_broker_observation_sha256 as digest,
    collect_read_only_reconciliation_observations,
    collect_spot_usdt_external_cash_flows,
    diagnose_balance_flows,
    diagnose_bnb_wallet_activity,
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
# Operator-approved current control from read-only Runtime 35501649014. One exact downgrade.
QUIESCE_CONTROL_SHA256 = "3ba1f2535a439960a43a46ea99f28911842b5dcf2c77cce30f55e6303947872b"
QUIESCE_CONTROL_UPDATE_TIME = "2026-09-12T13:29:06.395700Z"
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
    except (BinanceReconciliationReadError, ValueError, TypeError, KeyError):
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


# Operator approved the complete encrypted proposal from Runtime 34690028846.
APPROVED_PROSPECTIVE_SHA256 = "cad5cd02ebc554835492cc99f78b0639656a4261617dc8492fbc65c16f1d56a2"
PROSPECTIVE_ARCHIVE_DOCUMENT = "MULTI_ASSET_STATE__before_rebase_34690028846"


def _load_prospective_approval():
    try:
        raw = os.environ.pop("BINANCE_APPROVED_PROSPECTIVE_OPENING", "")
        if not 0 < len(raw) <= 48000:
            raise ValueError
        approved = json.loads(raw)
        _validate_prospective_approval(approved)
        return approved
    except Exception:
        raise MigrationBlocked("prospective_approval_unavailable") from None


def _validate_prospective_approval(approved):
    if (not isinstance(approved, Mapping) or digest(approved) != APPROVED_PROSPECTIVE_SHA256
            or approved.get("source_sha") != "c625413dcc601412358efbb5373e38788da95d0e"
            or approved.get("historical_difference_unresolved") is not True
            or set(approved.get("proposed_fields", {})) != _ACCOUNTING_FIELDS | {
                "earn_accrual_checkpoint", "earn_accounted_net_changes", "external_cash_flow_cursor"}):
        raise MigrationBlocked("prospective_approval_mismatch")


def _prospective_preflight(*, refs, client, expected, approved, now, clock=None):
    from application.earn_accrual import compare_earn_checkpoints, _time
    clock = clock or (lambda: datetime.now(timezone.utc))
    _validate_prospective_approval(approved)
    snapshot, ledger, control = _read_source(refs)
    if (digest(ledger) != approved["ledger_sha256"] or digest(control) != approved["control_sha256"]
            or _timestamp(snapshot.update_time) != approved["ledger_update_time"]
            or _finite(ledger.get("daily_external_principal_usdt", 0)) != 0):
        raise MigrationBlocked("prospective_source_changed")
    fields = approved["proposed_fields"]
    previous = fields["earn_accrual_checkpoint"]
    opened = _time(previous["observed_at"])
    if (not opened < now <= opened + timedelta(hours=24) or opened.date() != now.date()
            or previous["account_scope_sha256"] != expected["account_scope_sha256"]
            or previous["account_scope_sha256"] != control["source"]["original_evidence"]["account_scope_sha256"]):
        raise MigrationBlocked("prospective_opening_identity_or_time_changed")
    fresh = collect_prospective_opening(client, ledger=ledger, expected=expected, now=now, clock=clock)
    try:
        end = _time(fresh["observation_completed_at"])
        compare_earn_checkpoints(previous, fresh["earn_accrual_checkpoint"],
                                verified_net_changes=fields["earn_accounted_net_changes"])
        if (set(fields["earn_accounted_net_changes"]) != set(previous["assets"])
                or any(v != "0" for v in fields["earn_accounted_net_changes"].values())
                or fresh["external_cash_flow_cursor"]["records"] != fields["external_cash_flow_cursor"]["records"]):
            raise ValueError
        observations = collect_read_only_reconciliation_observations(client,
            strategy_symbols=tuple(f'{a}USDT' for a in previous["assets"] if a != "USDT"),
            local_execution_ledger=ledger, now=end, lookback=end-opened)
        if (digest(observations.account_scope) != previous["account_scope_sha256"]
                or observations.open_orders or observations.recent_executions):
            raise ValueError
        decision = clock()
        if not end <= decision <= now + TTL or decision.date() != opened.date():
            raise ValueError
    except Exception:
        raise MigrationBlocked("prospective_continuity_unverified") from None
    return decision


def _prospective_transaction(transaction, *, refs, archive_ref, approved, now):
    _validate_prospective_approval(approved)
    owner = refs["owner_ref"].get(transaction=transaction, retry=None)
    current = refs["ledger_ref"].get(transaction=transaction, retry=None)
    control = refs["control_ref"].get(transaction=transaction, retry=None)
    archive = archive_ref.get(transaction=transaction, retry=None)
    if (owner.exists or archive.exists or not current.exists or not control.exists
            or digest(current.to_dict()) != approved["ledger_sha256"]
            or digest(control.to_dict()) != approved["control_sha256"]
            or _timestamp(current.update_time) != approved["ledger_update_time"]):
        raise MigrationAtomicPrecondition("prospective_atomic_precondition_changed")
    ledger, recovery = current.to_dict(), control.to_dict()
    _validate_safe_order_state(ledger)
    _validate_control(recovery)
    proposed = copy.deepcopy(approved["proposed_fields"])
    marker = {"archive_document": PROSPECTIVE_ARCHIVE_DOCUMENT, "started_at": now.isoformat(),
              "opening_balance_observed_at": proposed["earn_accrual_checkpoint"]["observed_at"],
              "historical_difference_unresolved": True, "approved_proposal_run_id": "34690028846",
              "approved_proposal_sha256": APPROVED_PROSPECTIVE_SHA256}
    patch = {**proposed, "accounting_rebase": marker}
    new_digest = digest({**ledger, **patch})
    backup = {"ledger": copy.deepcopy(ledger), "recovery_control": copy.deepcopy(recovery),
              "ledger_update_time": _timestamp(current.update_time), **marker,
              "approved_proposal": copy.deepcopy(approved), "new_ledger_sha256": new_digest,
              "valuation_price_source": "binance_get_avg_price_estimate"}
    transaction.create(archive_ref, backup)
    transaction.update(refs["ledger_ref"], patch)
    return {"new_ledger_sha256": new_digest, "archive_sha256": digest(backup)}


def _apply_prospective_rebase(refs, *, client, expected, now, fixed_now):
    from google.cloud import firestore
    approved = _load_prospective_approval()
    archive_ref = refs["ledger_ref"].parent.document(PROSPECTIVE_ARCHIVE_DOCUMENT)
    if archive_ref.get(retry=None).exists:
        raise MigrationBlocked("prospective_archive_already_exists")
    decision = _prospective_preflight(refs=refs, client=client, expected=expected, approved=approved,
                                     now=now, clock=(lambda: now + timedelta(seconds=1)) if fixed_now else None)

    @firestore.transactional
    def apply(transaction):
        return _prospective_transaction(transaction, refs=refs, archive_ref=archive_ref,
                                        approved=approved, now=decision)
    try:
        written = apply(get_firestore_client().transaction(max_attempts=1))
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationApplyUncertain("prospective_write_uncertain") from None
    try:
        after_snapshot, after_ledger, after_control = _read_source(refs)
        archive = archive_ref.get(retry=None)
        if (digest(after_ledger) != written["new_ledger_sha256"]
                or digest(after_control) != approved["control_sha256"] or not archive.exists
                or digest(archive.to_dict()) != written["archive_sha256"]):
            raise ValueError
        updated_at = _timestamp(after_snapshot.update_time)
    except Exception:
        raise MigrationApplyUncertain("prospective_readback_uncertain") from None
    return {"status": "rebased", "stage": "prospective_accounting_apply",
            "approved_proposal_run_id": "34690028846", "archive_document": PROSPECTIVE_ARCHIVE_DOCUMENT,
            "ledger_update_time": updated_at, **written, "old_ledger_archived": True,
            "historical_difference_unresolved": True, "control_unchanged": True, "no_order": True,
            "write_performed": True, "execution_authority_granted": False}


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


def _snapshot_marker(snapshot):
    return {
        "exists": bool(snapshot.exists),
        "update_time": _timestamp(snapshot.update_time) if snapshot.exists else None,
        "value": snapshot.to_dict() if snapshot.exists else None,
    }


def _snapshot_matches(snapshot, marker):
    return _snapshot_marker(snapshot) == marker


def _validate_stale_owner_control(target, control, *, expected):
    if not isinstance(control, Mapping) or control.get("state") != "ACTIVE_LKG":
        raise MigrationBlocked("recovery_control_not_active_lkg")
    try:
        from application.reconciliation_recovery import activated_target

        activated = activated_target(target, control, expected=expected)
        if str(getattr(getattr(activated, "live_continuity", None), "state", "")).upper() != "ACTIVE_LKG":
            raise MigrationBlocked("recovery_control_not_active_lkg")
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationBlocked("recovery_control_invalid") from None


def _release_stale_owner_transaction(transaction, *, refs, markers):
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


def release_stale_owner(refs, *, client, target, expected):
    """Delete one confirmed stale owner document and nothing else."""
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
    _validate_stale_owner_control(target, control, expected=expected)
    if client is None:
        raise MigrationBlocked("account_scope_unverified")
    try:
        account = client.get_account()
    except Exception:
        raise MigrationBlocked("account_read_failed") from None
    _private_spot_account(
        account, expected_account_scope_sha256=expected["account_scope_sha256"]
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
        return _release_stale_owner_transaction(transaction, refs=refs, markers=markers)

    try:
        apply(get_firestore_client().transaction(max_attempts=1))
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


def _read_earn_diagnosis_source(refs):
    """Read the three accounting documents without treating an owner as a blocker."""
    owner = refs["owner_ref"].get(retry=None)
    ledger_snapshot = refs["ledger_ref"].get(retry=None)
    control_snapshot = refs["control_ref"].get(retry=None)
    if not ledger_snapshot.exists:
        raise MigrationBlocked("earn_diagnosis_ledger_missing")
    ledger = ledger_snapshot.to_dict()
    control = control_snapshot.to_dict() if control_snapshot.exists else None
    if control is not None and (
        not isinstance(control, Mapping)
        or control.get("state") not in {
            "RECONCILE_ONLY", "ACTIVE_LKG", "ROLLBACK_LKG", "PAUSED", "REDUCE_ONLY",
        }
    ):
        raise MigrationBlocked("earn_diagnosis_control_invalid")

    def marker(snapshot):
        return {
            "exists": snapshot.exists,
            "update_time": (
                _timestamp(snapshot.update_time) if snapshot.exists else None
            ),
            "value": snapshot.to_dict() if snapshot.exists else None,
        }

    return {
        "owner_exists": owner.exists,
        "owner_marker": marker(owner),
        "ledger": ledger,
        "ledger_marker": marker(ledger_snapshot),
        "control": control,
        "control_marker": marker(control_snapshot),
    }


def _diagnosis_decimal(value, *, signed=False):
    from application.earn_accrual import _amount

    try:
        return _amount(str(value), signed=signed)
    except (TypeError, ValueError, InvalidOperation):
        raise MigrationBlocked("earn_diagnosis_numeric_input_invalid") from None


def _direction(value):
    return "INCREASE" if value > 0 else "DECREASE" if value < 0 else "UNCHANGED"


_BNB_DIAGNOSTIC_QUANTUM = Decimal("0.00000001")


def _validate_bnb_dividend_rows(rows, *, start_ms, end_ms):
    if not isinstance(rows, list):
        return None
    row_keys = set()
    direction_values = set()
    direction_missing = False
    total = Decimal(0)
    try:
        for row in rows:
            if not isinstance(row, Mapping) or row.get("asset") != "BNB":
                return None
            div_time = row.get("divTime")
            if type(div_time) is not int or not start_ms <= div_time <= end_ms:
                return None
            row_id = row.get("id")
            tran_id = row.get("tranId")
            if type(row_id) is not int or row_id < 0 or type(tran_id) is not int or tran_id < 0:
                return None
            key = (row_id, tran_id, div_time)
            if key in row_keys:
                return None
            row_keys.add(key)
            total += _diagnosis_decimal(row.get("amount"), signed=False)
            direction = row.get("direction")
            if type(direction) is int:
                direction_values.add(direction)
            else:
                direction_missing = True
    except (MigrationBlocked, TypeError, ValueError, InvalidOperation):
        return None
    return {
        "count": len(rows),
        "total": total,
        "direction_summary": {
            "integer_values": sorted(direction_values),
            "missing": direction_missing,
            "mixed": len(direction_values) > 1,
        },
    }


def _summarize_bnb_wallet_activity(report, *, residual, start, end):
    """Compare private wallet rows to BNB residual without exposing source data."""
    private_rows = report.get("_private_rows") if isinstance(report, Mapping) else None
    summary = {
        "status": "CHECK_FAILED",
        "complete": False,
        "source_reason_code": None,
        "source_failed_surface": None,
        "source_failure_stage": None,
        "source_response_shape": None,
        "dividend_count": None,
        "dividend_surface_complete": False,
        "dividend_direction_summary": {"integer_values": [], "missing": False, "mixed": False},
        "dust_record_count": None,
        "dust_bnb_detail_count": None,
        "dust_non_bnb_target_count": None,
        "dividend_residual_matches": None,
        "dust_transfer_residual_matches": None,
        "dust_after_fee_residual_matches": None,
        "combined_transfer_residual_matches": None,
        "combined_after_fee_residual_matches": None,
        "residual_within_one_eight_decimal_unit": abs(residual) <= _BNB_DIAGNOSTIC_QUANTUM,
        "dividend_net_semantics_verified": False,
        "dust_net_semantics_verified": False,
        "causal_reconciliation": False,
    }
    if not isinstance(report, Mapping) or report.get("requested_surfaces_complete") is not True:
        if isinstance(report, Mapping):
            counts = report.get("counts")
            if isinstance(counts, Mapping):
                if type(counts.get("bnb_dividends")) is int:
                    summary["dividend_count"] = counts["bnb_dividends"]
                if type(counts.get("spot_dust_conversions")) is int:
                    summary["dust_record_count"] = counts["spot_dust_conversions"]
            summary["source_reason_code"] = report.get("reason_code") if report.get("reason_code") in {
                "bnb_wallet_history_unverified",
            } else None
            summary["source_failed_surface"] = report.get("failed_surface") if report.get("failed_surface") in {
                "bnb_dividends", "spot_dust_conversions",
            } else None
            summary["source_failure_stage"] = report.get("failure_stage") if report.get("failure_stage") in {
                "request", "response_validation",
            } else None
            shape = report.get("response_shape")
            if isinstance(shape, Mapping):
                summary["source_response_shape"] = {
                    key: shape[key]
                    for key in (
                        "rows_present", "rows_is_list", "total_is_integer",
                        "total_is_decimal_string", "total_is_zero", "total_matches_rows",
                        "page_full", "row_time_and_asset_valid", "total_valid",
                        "rows_readable", "visible_row_count", "window_complete",
                    )
                    if type(shape.get(key)) is bool or type(shape.get(key)) is int
                }
            surface_diagnostics = report.get("surface_diagnostics")
            dividend_shape = (
                surface_diagnostics.get("bnb_dividends")
                if isinstance(surface_diagnostics, Mapping) else None
            )
            if (
                isinstance(dividend_shape, Mapping)
                and dividend_shape.get("window_complete") is True
                and isinstance(private_rows, Mapping)
            ):
                validated = _validate_bnb_dividend_rows(
                    private_rows.get("bnb_dividends"),
                    start_ms=int(start.timestamp() * 1000),
                    end_ms=int(end.timestamp() * 1000),
                )
                if validated is not None:
                    summary["dividend_count"] = validated["count"]
                    summary["dividend_surface_complete"] = True
                    summary["dividend_residual_matches"] = validated["total"] == residual
                    summary["dividend_direction_summary"] = validated["direction_summary"]
        return summary
    summary["status"] = "UNVERIFIED"
    if (
        not isinstance(private_rows, Mapping)
        or not isinstance(private_rows.get("bnb_dividends"), list)
        or not isinstance(private_rows.get("spot_dust_conversions"), list)
    ):
        return summary
    summary["dust_bnb_detail_count"] = 0
    summary["dust_non_bnb_target_count"] = 0
    dust_diagnostics = report.get("surface_diagnostics") if isinstance(report, Mapping) else None
    dust_shape = dust_diagnostics.get("spot_dust_conversions") if isinstance(dust_diagnostics, Mapping) else None
    dust_surface_exempt = isinstance(dust_shape, Mapping) and dust_shape.get("empty_missing_total_exempt") is True

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    dust_transfer_total = Decimal(0)
    dust_after_fee_total = Decimal(0)
    dust_keys = set()
    try:
        validated_dividends = _validate_bnb_dividend_rows(
            private_rows["bnb_dividends"], start_ms=start_ms, end_ms=end_ms,
        )
        if validated_dividends is None:
            return summary
        dividend_total = validated_dividends["total"]
        summary["dividend_count"] = validated_dividends["count"]
        summary["dividend_surface_complete"] = True
        summary["dividend_residual_matches"] = dividend_total == residual
        summary["dividend_direction_summary"] = validated_dividends["direction_summary"]

        for record in private_rows["spot_dust_conversions"]:
            if not isinstance(record, Mapping):
                return summary
            operate_time = record.get("operateTime")
            trans_id = record.get("transId")
            if (
                type(operate_time) is not int
                or not start_ms <= operate_time <= end_ms
                or type(trans_id) is not int
                or trans_id < 0
                or trans_id in dust_keys
            ):
                return summary
            dust_keys.add(trans_id)
            details = record.get("userAssetDribbletDetails")
            if not isinstance(details, list):
                return summary
            total_transfer = _diagnosis_decimal(record.get("totalTransferedAmount"), signed=False)
            total_fee = _diagnosis_decimal(record.get("totalServiceChargeAmount"), signed=False)
            detail_transfer_total = Decimal(0)
            detail_fee_total = Decimal(0)
            detail_keys = set()
            for detail in details:
                if not isinstance(detail, Mapping):
                    return summary
                detail_time = detail.get("operateTime")
                detail_trans_id = detail.get("transId")
                from_asset = detail.get("fromAsset")
                target_asset = detail.get("targetAsset")
                if (
                    type(detail_time) is not int
                    or not start_ms <= detail_time <= end_ms
                    or type(detail_trans_id) is not int
                    or detail_trans_id < 0
                    or not isinstance(from_asset, str)
                    or not isinstance(target_asset, str)
                ):
                    return summary
                detail_key = (detail_trans_id, detail_time, from_asset, target_asset)
                if detail_key in detail_keys:
                    return summary
                detail_keys.add(detail_key)
                transfer = _diagnosis_decimal(detail.get("transferedAmount"), signed=False)
                fee = _diagnosis_decimal(detail.get("serviceChargeAmount"), signed=False)
                if fee > transfer:
                    return summary
                detail_transfer_total += transfer
                detail_fee_total += fee
                if target_asset == "BNB":
                    summary["dust_bnb_detail_count"] += 1
                    dust_transfer_total += transfer
                    dust_after_fee_total += transfer - fee
                else:
                    summary["dust_non_bnb_target_count"] += 1
            if detail_transfer_total != total_transfer or detail_fee_total != total_fee:
                return summary

        summary["dust_record_count"] = len(private_rows["spot_dust_conversions"])
        if not dust_surface_exempt:
            summary["dust_transfer_residual_matches"] = dust_transfer_total == residual
            summary["dust_after_fee_residual_matches"] = dust_after_fee_total == residual
            combined_transfer = dividend_total + dust_transfer_total
            combined_after_fee = dividend_total + dust_after_fee_total
            summary["combined_transfer_residual_matches"] = combined_transfer == residual
            summary["combined_after_fee_residual_matches"] = combined_after_fee == residual
    except (MigrationBlocked, TypeError, ValueError, InvalidOperation):
        return summary
    summary["status"] = "COMPLETE"
    summary["complete"] = True
    return summary


def _trade_net_diagnosis(observations, *, assets):
    """Reconstruct signed Spot deltas from normalized, bounded myTrades rows."""
    net = {asset: Decimal(0) for asset in assets}
    counts = {asset: 0 for asset in assets}
    for trade in observations.recent_executions:
        symbol = str(trade.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or len(symbol) <= 4:
            raise MigrationBlocked("earn_diagnosis_trade_symbol_invalid")
        asset = symbol[:-4]
        if asset not in assets or "USDT" not in assets:
            raise MigrationBlocked("earn_diagnosis_trade_asset_out_of_scope")
        quantity = _diagnosis_decimal(trade.get("qty"))
        price = _diagnosis_decimal(trade.get("price"))
        commission = _diagnosis_decimal(trade.get("commission"))
        commission_asset = str(trade.get("commission_asset") or "").upper()
        if not commission_asset or commission_asset not in assets:
            raise MigrationBlocked("earn_diagnosis_trade_asset_out_of_scope")
        sign = Decimal(1) if trade.get("is_buyer") is True else Decimal(-1)
        net[asset] += sign * quantity
        net["USDT"] -= sign * quantity * price
        net[commission_asset] -= commission
        counts[asset] += 1
    return net, counts


def _forward_accounting_eligibility(*, result, asset_results):
    """Return whether the stable observation may feed forward accounting.

    This is deliberately narrower than causal reconciliation and never grants
    execution authority.  It only admits a stable Earn/checkpoint interval
    with no unsupported external flow and a separately verified BNB dividend
    surface when BNB needs that explanation.
    """
    if (
        result.get("owner_exists")
        or result.get("order_state_known") is not True
        or result.get("open_order_count") != 0
        or result.get("sampling_stable") is not True
        or result.get("account_scope_verified") is not True
        or result.get("control_unchanged") is not True
        or result.get("ledger_unchanged") is not True
        or result.get("unsupported_external_flow_count") != 0
        or result.get("changed_withdrawal_count") != 0
    ):
        return False
    for asset, check in asset_results.items():
        if (
            check.get("product_status") != "STABLE"
            or check.get("trade_net_matches_persisted") is not True
            or check.get("second_sample_residual_matches_first") is not True
            or check.get("classification") not in {
                "residual_zero_after_realtime_counter",
                "residual_matches_bonus_records",
            }
        ):
            return False
        if asset == "USDT" and check.get("external_flow_count", 0) > 0:
            if check.get("external_flow_matches_residual") is not True:
                return False
        if asset == "BNB" and check.get("residual_matches_bonus_records") is True:
            if (
                check.get("wallet_activity_complete") is not True
                or check.get("wallet_dividend_residual_matches") is not True
            ):
                return False
    return True


def diagnose_earn_forward(refs, *, client, expected, now):
    """Diagnose the current checkpoint window without changing any durable state."""
    from application.earn_accrual import (
        _time,
        _validate,
        collect_earn_checkpoint,
    )

    source = _read_earn_diagnosis_source(refs)
    ledger = source["ledger"]
    order_record = ledger.get("order_submission")
    order_state_known = isinstance(order_record, Mapping) and order_record.get("state") in {
        "RESERVED", "TERMINAL",
    }
    checkpoint = ledger.get("earn_accrual_checkpoint")
    if not isinstance(checkpoint, Mapping):
        raise MigrationBlocked("earn_diagnosis_checkpoint_missing")
    try:
        _validate(checkpoint)
        checkpoint_at = _time(checkpoint["observed_at"])
    except (TypeError, ValueError):
        raise MigrationBlocked("earn_diagnosis_checkpoint_invalid") from None
    expected_scope = expected.get("account_scope_sha256")
    if not isinstance(expected_scope, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_scope):
        raise MigrationBlocked("earn_diagnosis_account_scope_missing")
    if checkpoint.get("account_scope_sha256") != expected_scope:
        raise MigrationBlocked("earn_diagnosis_checkpoint_account_scope_mismatch")
    if not isinstance(now, datetime) or now.tzinfo is None or not checkpoint_at < now:
        raise MigrationBlocked("earn_diagnosis_checkpoint_window_invalid")
    if now - checkpoint_at > timedelta(days=7):
        raise MigrationBlocked("earn_diagnosis_checkpoint_window_invalid")

    assets = tuple(sorted(checkpoint["assets"]))
    if "USDT" not in assets or not 0 < len(assets) <= 32:
        raise MigrationBlocked("earn_diagnosis_asset_scope_invalid")
    net_values = ledger.get("earn_accounted_net_changes")
    if not isinstance(net_values, Mapping) or set(net_values) != set(assets):
        raise MigrationBlocked("earn_diagnosis_net_changes_missing")
    stored_net = {
        asset: _diagnosis_decimal(net_values[asset], signed=True) for asset in assets
    }
    cursor = ledger.get("external_cash_flow_cursor")
    if not isinstance(cursor, Mapping):
        raise MigrationBlocked("earn_diagnosis_cursor_missing")
    try:
        if _time(cursor.get("observed_at")) != checkpoint_at:
            raise ValueError
    except (TypeError, ValueError):
        raise MigrationBlocked("earn_diagnosis_cursor_mismatch") from None

    required_symbols = tuple(f"{asset}USDT" for asset in assets if asset != "USDT")
    configured_symbols = set(_symbols_from_env())
    if any(symbol not in configured_symbols for symbol in required_symbols):
        raise MigrationBlocked("earn_diagnosis_symbols_missing")

    sampling_requests = []

    class _SamplingClient:
        def __init__(self, wrapped, sample_label):
            self._wrapped = wrapped
            self._sample_label = sample_label

        def _read(self, surface, method, **kwargs):
            started = time.monotonic()
            started_at = datetime.now(timezone.utc)
            try:
                return method(**kwargs)
            finally:
                sampling_requests.append({
                    "label": f"{surface}_{self._sample_label}",
                    "started": started,
                    "finished": time.monotonic(),
                    "started_at": started_at,
                })

        def get_account(self):
            return self._read("spot", self._wrapped.get_account)

        def get_simple_earn_flexible_product_position(self, **kwargs):
            return self._read(
                "earn",
                self._wrapped.get_simple_earn_flexible_product_position,
                **kwargs,
            )

    try:
        current = collect_earn_checkpoint(
            _SamplingClient(client, "1"),
            assets=assets,
            observed_at=now,
            expected_account_scope_sha256=expected_scope,
        )
    except Exception:
        raise MigrationBlocked("earn_diagnosis_checkpoint_unavailable") from None

    # Each checkpoint performs Spot first and Flexible Earn second. Keep both
    # samples private and compare the complete normalized shape; Firestore
    # markers below cannot prove that broker reads came from one stable window.
    try:
        second_sample = collect_earn_checkpoint(
            _SamplingClient(client, "2"),
            assets=assets,
            observed_at=now,
            expected_account_scope_sha256=expected_scope,
        )
    except Exception:
        second_sample = None

    def _sampling_shape(value):
        if not isinstance(value, Mapping):
            return None
        assets_value = value.get("assets")
        if not isinstance(assets_value, Mapping):
            return None
        shape = {}
        for asset, row in assets_value.items():
            if not isinstance(row, Mapping) or not isinstance(row.get("products"), Mapping):
                return None
            products = {}
            for product, position in row["products"].items():
                if not isinstance(position, Mapping):
                    return None
                products[product] = (
                    position.get("auto_subscribe"),
                    position.get("can_redeem"),
                )
            shape[asset] = (
                products,
            )
        return shape

    sampling_shape_stable = (
        second_sample is not None
        and _sampling_shape(current) is not None
        and _sampling_shape(current) == _sampling_shape(second_sample)
    )

    def _sampling_components_stable(first, second):
        if not isinstance(first, Mapping) or not isinstance(second, Mapping):
            return False
        first_assets, second_assets = first.get("assets"), second.get("assets")
        if not isinstance(first_assets, Mapping) or not isinstance(second_assets, Mapping):
            return False
        if set(first_assets) != set(second_assets):
            return False
        try:
            for asset in first_assets:
                before, after = first_assets[asset], second_assets[asset]
                if not isinstance(before, Mapping) or not isinstance(after, Mapping):
                    return False
                if (
                    _diagnosis_decimal(before.get("spot_free"), signed=True)
                    != _diagnosis_decimal(after.get("spot_free"), signed=True)
                    or _diagnosis_decimal(before.get("spot_locked"), signed=True)
                    != _diagnosis_decimal(after.get("spot_locked"), signed=True)
                ):
                    return False
                first_products, second_products = before.get("products"), after.get("products")
                if not isinstance(first_products, Mapping) or not isinstance(second_products, Mapping):
                    return False
                if set(first_products) != set(second_products):
                    return False
                for product in first_products:
                    old_row, new_row = first_products[product], second_products[product]
                    if not isinstance(old_row, Mapping) or not isinstance(new_row, Mapping):
                        return False
                    quantity_delta = _diagnosis_decimal(new_row.get("total"), signed=True) - _diagnosis_decimal(
                        old_row.get("total"), signed=True
                    )
                    reward_delta = _diagnosis_decimal(
                        new_row.get("realtime_rewards"), signed=True
                    ) - _diagnosis_decimal(old_row.get("realtime_rewards"), signed=True)
                    if quantity_delta != reward_delta:
                        return False
        except (MigrationBlocked, TypeError, ValueError, InvalidOperation):
            return False
        return True

    sampling_components_stable = _sampling_components_stable(current, second_sample)
    expected_sampling_labels = ["spot_1", "earn_1", "spot_2", "earn_2"]
    observed_sampling_labels = [row["label"] for row in sampling_requests]
    sampling_timing_stable = (
        observed_sampling_labels == expected_sampling_labels
        and all(row["finished"] >= row["started"] for row in sampling_requests)
        and all(
            sampling_requests[index]["finished"] <= sampling_requests[index + 1]["started"]
            for index in range(len(sampling_requests) - 1)
        )
    )

    try:
        flows = collect_spot_usdt_external_cash_flows(
            client, now=now, cursor=copy.deepcopy(cursor)
        )
    except Exception:
        raise MigrationBlocked("earn_diagnosis_external_flow_unavailable") from None

    try:
        observations = collect_read_only_reconciliation_observations(
            client,
            strategy_symbols=required_symbols,
            local_execution_ledger=ledger,
            now=now,
            lookback=now - checkpoint_at,
        )
    except Exception:
        raise MigrationBlocked("earn_diagnosis_trade_history_unavailable") from None
    if digest(observations.account_scope) != expected_scope:
        raise MigrationBlocked("earn_diagnosis_account_scope_mismatch")
    trade_net, trade_counts = _trade_net_diagnosis(observations, assets=assets)

    external_principal = _diagnosis_decimal(
        flows.get("new_deposit_principal_usdt"), signed=False
    )
    if flows.get("new_unsupported_deposit_count", 0) or flows.get(
        "new_or_changed_withdrawal_count", 0
    ):
        external_status = "UNSUPPORTED_ACTIVITY"
    else:
        external_status = "OBSERVED"

    def _sample_residual_after_realtime(sample):
        if not isinstance(sample, Mapping) or not isinstance(sample.get("assets"), Mapping):
            return None
        residuals = {}
        for asset in assets:
            before = checkpoint["assets"].get(asset)
            after = sample["assets"].get(asset)
            if not isinstance(before, Mapping) or not isinstance(after, Mapping):
                return None
            before_products, after_products = before.get("products"), after.get("products")
            if not isinstance(before_products, Mapping) or not isinstance(after_products, Mapping):
                return None
            if set(before_products) != set(after_products):
                return None
            reward = Decimal(0)
            for product in before_products:
                old_row, new_row = before["products"][product], after["products"][product]
                if (
                    not isinstance(old_row, Mapping)
                    or not isinstance(new_row, Mapping)
                    or old_row.get("auto_subscribe") != new_row.get("auto_subscribe")
                ):
                    return None
                delta = _diagnosis_decimal(new_row.get("realtime_rewards"), signed=True) - _diagnosis_decimal(
                    old_row.get("realtime_rewards"), signed=True
                )
                if delta < 0:
                    return None
                reward += delta
            observed = _diagnosis_decimal(after.get("quantity"), signed=True) - _diagnosis_decimal(
                before.get("quantity"), signed=True
            )
            residuals[asset] = observed - stored_net[asset] - (
                external_principal if asset == "USDT" else Decimal(0)
            ) - reward
        return residuals

    second_residual_after_realtime = _sample_residual_after_realtime(second_sample)

    observed_delta = {}
    residual_before_realtime = {}
    residual_after_realtime = {}
    product_status = {}
    realtime_delta = {}
    for asset in assets:
        before = checkpoint["assets"][asset]
        after = current["assets"].get(asset)
        if not isinstance(after, Mapping):
            raise MigrationBlocked("earn_diagnosis_checkpoint_asset_missing")
        observed_delta[asset] = _diagnosis_decimal(after["quantity"], signed=True) - _diagnosis_decimal(
            before["quantity"], signed=True
        )
        before_products, after_products = before["products"], after["products"]
        if set(before_products) != set(after_products):
            product_status[asset] = "PRODUCT_LIFECYCLE_CHANGED"
            realtime_delta[asset] = None
        else:
            counter_delta = Decimal(0)
            status = "STABLE"
            for product in before_products:
                old_row, new_row = before_products[product], after_products[product]
                if old_row["auto_subscribe"] != new_row["auto_subscribe"]:
                    status = "PRODUCT_LIFECYCLE_CHANGED"
                    break
                delta = _diagnosis_decimal(new_row["realtime_rewards"], signed=True) - _diagnosis_decimal(
                    old_row["realtime_rewards"], signed=True
                )
                if delta < 0:
                    status = "COUNTER_RESET"
                    break
                counter_delta += delta
            product_status[asset] = status
            realtime_delta[asset] = counter_delta if status == "STABLE" else None
        residual_before_realtime[asset] = observed_delta[asset] - stored_net[asset] - (
            external_principal if asset == "USDT" else Decimal(0)
        )
        residual_after_realtime[asset] = residual_before_realtime[asset] - (
            realtime_delta[asset] if realtime_delta[asset] is not None else Decimal(0)
        )

    history = diagnose_balance_flows(
        client,
        start=checkpoint_at,
        end=now,
        now=now,
        reward_quantity_changes=residual_after_realtime,
    )
    if history.get("history_complete_for_requested_surfaces") is not True:
        reason = history.get("reason_code")
        if reason == "balance_history_reward_validation_failed":
            raise MigrationBlocked("earn_diagnosis_reward_history_invalid")
        raise MigrationBlocked("earn_diagnosis_history_incomplete")
    reward_checks = history.get("reward_quantity_checks")
    if not isinstance(reward_checks, Mapping) or set(reward_checks) != set(assets):
        raise MigrationBlocked("earn_diagnosis_reward_history_invalid")

    bnb_wallet_activity = {
        "status": "NOT_CHECKED",
        "complete": False,
        "dividend_surface_complete": False,
        "dividend_direction_summary": {"integer_values": [], "missing": False, "mixed": False},
        "source_reason_code": None,
        "source_failed_surface": None,
        "source_failure_stage": None,
        "source_response_shape": None,
        "dividend_count": None,
        "dust_record_count": None,
        "dust_bnb_detail_count": None,
        "dust_non_bnb_target_count": None,
        "dividend_residual_matches": None,
        "dust_transfer_residual_matches": None,
        "dust_after_fee_residual_matches": None,
        "combined_transfer_residual_matches": None,
        "combined_after_fee_residual_matches": None,
        "residual_within_one_eight_decimal_unit": False,
        "dividend_net_semantics_verified": False,
        "dust_net_semantics_verified": False,
        "causal_reconciliation": False,
    }
    if "BNB" in assets:
        bnb_wallet_activity["residual_within_one_eight_decimal_unit"] = (
            abs(residual_after_realtime["BNB"]) <= _BNB_DIAGNOSTIC_QUANTUM
        )
        other_assets_normal = all(
            product_status[asset] == "STABLE"
            and residual_after_realtime[asset] == 0
            and trade_net[asset] == stored_net[asset]
            for asset in assets
            if asset != "BNB"
        )
        bnb_preconditions = (
            product_status["BNB"] == "STABLE"
            and residual_after_realtime["BNB"] > 0
            and trade_net["BNB"] == stored_net["BNB"]
            and external_status != "UNSUPPORTED_ACTIVITY"
            and not observations.open_orders
            and other_assets_normal
        )
        if bnb_preconditions:
            try:
                bnb_wallet_report = diagnose_bnb_wallet_activity(
                    client,
                    start=checkpoint_at,
                    end=now,
                    include_rows=True,
                )
            except Exception:
                bnb_wallet_report = {
                    "requested_surfaces_complete": False,
                    "reason_code": "bnb_wallet_history_unverified",
                    "failure_stage": "request",
                }
            bnb_wallet_activity = _summarize_bnb_wallet_activity(
                bnb_wallet_report,
                residual=residual_after_realtime["BNB"],
                start=checkpoint_at,
                end=now,
            )

    asset_results = {}
    for asset in assets:
        check = reward_checks.get(asset)
        if not isinstance(check, Mapping) or not isinstance(check.get("reward_counts"), Mapping):
            raise MigrationBlocked("earn_diagnosis_reward_history_invalid")
        counts = check["reward_counts"]
        if any(type(counts.get(kind)) is not int or counts[kind] < 0 for kind in ("BONUS", "REALTIME")):
            raise MigrationBlocked("earn_diagnosis_reward_history_invalid")
        if product_status[asset] == "PRODUCT_LIFECYCLE_CHANGED":
            classification = "product_lifecycle_changed"
        elif product_status[asset] == "COUNTER_RESET":
            classification = "counter_reset"
        elif trade_net[asset] != stored_net[asset]:
            classification = "trade_net_unmatched"
        elif external_status == "UNSUPPORTED_ACTIVITY" and asset == "USDT":
            classification = "external_flow_unsupported"
        elif product_status[asset] == "STABLE" and residual_after_realtime[asset] == 0:
            classification = "residual_zero_after_realtime_counter"
        elif (
            product_status[asset] == "STABLE"
            and residual_after_realtime[asset] > 0
            and counts["BONUS"] > 0
            and check.get("delta_matches_bonus")
        ):
            classification = "residual_matches_bonus_records"
        else:
            classification = "quantity_unexplained"
        realtime_status = product_status[asset]
        if realtime_delta[asset] is not None:
            realtime_status = "INCREASE" if realtime_delta[asset] > 0 else "UNCHANGED"
        external_matches = None
        if asset == "USDT" and realtime_delta[asset] is not None:
            external_matches = (
                observed_delta[asset] - stored_net[asset] - realtime_delta[asset]
                == external_principal
            )
        asset_results[asset] = {
            "quantity_direction": _direction(observed_delta[asset]),
            "residual_before_realtime_direction": _direction(residual_before_realtime[asset]),
            "residual_direction": _direction(residual_after_realtime[asset]),
            "stored_net_change_direction": _direction(stored_net[asset]),
            "trade_net_difference_direction": _direction(trade_net[asset] - stored_net[asset]),
            "external_flow_direction": _direction(
                external_principal if asset == "USDT" else Decimal(0)
            ),
            "realtime_reward_direction": _direction(
                realtime_delta[asset] if realtime_delta[asset] is not None else Decimal(0)
            ),
            "second_sample_residual_direction": (
                _direction(second_residual_after_realtime[asset])
                if second_residual_after_realtime is not None
                else None
            ),
            "second_sample_residual_matches_first": (
                second_residual_after_realtime is not None
                and second_residual_after_realtime[asset] == residual_after_realtime[asset]
            ),
            "product_status": product_status[asset],
            "product_count_before": len(checkpoint["assets"][asset]["products"]),
            "product_count_current": len(current["assets"][asset]["products"]),
            "realtime_counter_status": realtime_status,
            "bonus_record_count": counts["BONUS"],
            "realtime_record_count": counts["REALTIME"],
            "residual_matches_bonus_records": (
                None
                if product_status[asset] != "STABLE"
                else (
                    residual_after_realtime[asset] > 0
                    and counts["BONUS"] > 0
                    and check.get("delta_matches_bonus") is True
                )
            ),
            "trade_count": trade_counts[asset],
            "trade_net_matches_persisted": trade_net[asset] == stored_net[asset],
            "trade_net_diagnostic_only": True,
            "external_flow_matches_residual": external_matches,
            "external_flow_count": flows.get("new_confirmed_deposit_count", 0) if asset == "USDT" else 0,
            "classification": classification,
            "causal_reconciliation": False,
        }
    if "BNB" in asset_results:
        asset_results["BNB"].update({
            "wallet_activity_status": bnb_wallet_activity["status"],
            "wallet_activity_complete": bnb_wallet_activity["complete"],
            "wallet_dividend_surface_complete": bnb_wallet_activity["dividend_surface_complete"],
            "wallet_dividend_direction_summary": bnb_wallet_activity["dividend_direction_summary"],
            "wallet_dividend_record_count": bnb_wallet_activity["dividend_count"],
            "wallet_dust_record_count": bnb_wallet_activity["dust_record_count"],
            "wallet_dust_bnb_detail_count": bnb_wallet_activity["dust_bnb_detail_count"],
            "wallet_dust_non_bnb_target_count": bnb_wallet_activity["dust_non_bnb_target_count"],
            "wallet_dividend_residual_matches": bnb_wallet_activity["dividend_residual_matches"],
            "wallet_dust_transfer_residual_matches": bnb_wallet_activity["dust_transfer_residual_matches"],
            "wallet_dust_after_fee_residual_matches": bnb_wallet_activity["dust_after_fee_residual_matches"],
            "wallet_combined_transfer_residual_matches": bnb_wallet_activity["combined_transfer_residual_matches"],
            "wallet_combined_after_fee_residual_matches": bnb_wallet_activity["combined_after_fee_residual_matches"],
            "residual_within_one_eight_decimal_unit": bnb_wallet_activity["residual_within_one_eight_decimal_unit"],
            "wallet_dividend_net_semantics_verified": bnb_wallet_activity["dividend_net_semantics_verified"],
            "wallet_dust_net_semantics_verified": bnb_wallet_activity["dust_net_semantics_verified"],
            "wallet_causal_reconciliation": bnb_wallet_activity["causal_reconciliation"],
            "wallet_source_reason_code": bnb_wallet_activity["source_reason_code"],
            "wallet_source_failed_surface": bnb_wallet_activity["source_failed_surface"],
            "wallet_source_failure_stage": bnb_wallet_activity["source_failure_stage"],
            "wallet_source_response_shape": bnb_wallet_activity["source_response_shape"],
        })

    sampling_residual_stable = (
        second_residual_after_realtime is not None
        and all(
            second_residual_after_realtime[asset] == residual_after_realtime[asset]
            for asset in assets
        )
    )
    sampling_stable = (
        sampling_shape_stable
        and sampling_components_stable
        and sampling_timing_stable
        and sampling_residual_stable
    )

    after = _read_earn_diagnosis_source(refs)
    if any(
        source[key] != after[key]
        for key in ("owner_marker", "ledger_marker", "control_marker")
    ):
        raise MigrationBlocked("earn_diagnosis_state_changed_during_read")
    result = {
        "status": "diagnosed",
        "stage": "earn_forward_accounting_diagnosis",
        "owner_exists": source["owner_exists"],
        "owner_unchanged": True,
        "ledger_unchanged": True,
        "control_unchanged": True,
        "sampling_stable": sampling_stable,
        "sampling_sequence": ["spot_1", "earn_1", "spot_2", "earn_2"],
        "sampling_second_read_available": second_sample is not None,
        "sampling_request_timing_stable": sampling_timing_stable,
        "sampling_components_stable": sampling_components_stable,
        "sampling_residual_stable": sampling_residual_stable,
        "sampling_observed_at": current.get("observed_at"),
        "account_scope_verified": True,
        "checkpoint_window_within_seven_days": True,
        "order_state_known": order_state_known,
        "diagnostic_restricted": not order_state_known,
        "assets": asset_results,
        "history_counts": history["history_counts"],
        "open_order_count": len(observations.open_orders),
        "unsupported_external_flow_count": flows.get("new_unsupported_deposit_count", 0),
        "changed_withdrawal_count": flows.get("new_or_changed_withdrawal_count", 0),
        "external_flow_status": external_status,
        "bnb_wallet_activity_checked": bnb_wallet_activity["status"] != "NOT_CHECKED",
        "bnb_wallet_activity_status": bnb_wallet_activity["status"],
        "bnb_wallet_activity_complete": bnb_wallet_activity["complete"],
        "causal_reconciliation": False,
        "activation_allowed": False,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }
    result["forward_accounting_eligible"] = _forward_accounting_eligibility(
        result=result, asset_results=asset_results
    )
    result["forward_accounting_write_permitted"] = False
    return result


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
    history = diagnose_balance_flows(
        client,
        start=now - timedelta(days=7),
        end=now,
        now=now,
        account=account,
        expected_digests=expected,
    )
    recent_execution_count = None
    try:
        observations = collect_read_only_reconciliation_observations(
            client,
            strategy_symbols=_symbols_from_env(),
            local_execution_ledger=ledger,
            now=now,
            lookback=timedelta(days=7),
            account_snapshot=account,
        )
        recent_execution_count = len(observations.recent_executions)
    except (BinanceReconciliationReadError, ValueError, TypeError, KeyError):
        # The category remains explicitly unverified; do not infer zero trades.
        pass
    earn_diagnosis = None
    try:
        earn_diagnosis = diagnose_earn_forward(refs, client=client, expected=expected, now=now)
    except (MigrationBlocked, ValueError, TypeError, KeyError):
        # Earn remains observed but unsupported unless its dedicated proof passes.
        pass
    activity = classify_activity_evidence(
        history_counts=history.get("history_counts") if isinstance(history, Mapping) else None,
        recent_execution_count=recent_execution_count,
        flow_summary=flows,
        earn_forward_eligible=(
            earn_diagnosis.get("forward_accounting_eligible")
            if isinstance(earn_diagnosis, Mapping) else None
        ),
    )
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
        "activity_by_type": activity,
        "activity_history_complete": history.get("history_complete_for_requested_surfaces") is True,
        "activity_history_reason_code": history.get("reason_code"),
        "earn_forward_accounting_eligible": (
            earn_diagnosis.get("forward_accounting_eligible")
            if isinstance(earn_diagnosis, Mapping) else False
        ),
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


def classify_activity_evidence(
    *, history_counts, recent_execution_count, flow_summary, earn_forward_eligible=None
):
    """Summarize activity by accounting category without inferring causality."""
    counts = history_counts if isinstance(history_counts, Mapping) else {}
    flows = flow_summary if isinstance(flow_summary, Mapping) else None

    def _nonnegative_int(value):
        return type(value) is int and value >= 0

    trade_count = recent_execution_count if _nonnegative_int(recent_execution_count) else None
    if flows is None:
        deposit_count = withdrawal_count = None
        deposit_supported = withdrawal_supported = False
        deposit_status = withdrawal_status = "unverified"
    else:
        deposit_count = flows.get("new_confirmed_deposit_count")
        withdrawal_count = flows.get("new_or_changed_withdrawal_count")
        deposit_count = deposit_count if _nonnegative_int(deposit_count) else None
        withdrawal_count = withdrawal_count if _nonnegative_int(withdrawal_count) else None
        unsupported = flows.get("new_unsupported_deposit_count")
        deposit_supported = deposit_count is not None and unsupported == 0
        withdrawal_supported = withdrawal_count == 0
        deposit_status = "observed" if deposit_count is not None else "unverified"
        withdrawal_status = "observed" if withdrawal_count is not None else "unverified"

    transfer_count = sum(
        value for key, value in counts.items()
        if isinstance(key, str) and key.startswith("transfer_") and _nonnegative_int(value)
    ) if counts else None
    earn_count = sum(
        value for key, value in counts.items()
        if key in {"earn_rewards", "earn_subscriptions", "earn_redemptions"}
        and _nonnegative_int(value)
    ) if counts else None
    categories = {
        "trade": {
            "count": trade_count,
            "status": "observed" if trade_count is not None else "unverified",
            "supported": trade_count == 0,
        },
        "deposit": {
            "count": deposit_count,
            "status": deposit_status,
            "supported": deposit_supported,
        },
        "withdrawal": {
            "count": withdrawal_count,
            "status": withdrawal_status,
            "supported": withdrawal_supported,
        },
        "internal_transfer": {
            "count": transfer_count,
            "status": "observed" if transfer_count is not None else "unverified",
            "supported": transfer_count == 0,
        },
        "earn": {
            "count": earn_count,
            "status": (
                "forward_eligible" if earn_count and earn_forward_eligible is True
                else "observed" if earn_count is not None else "unverified"
            ),
            "supported": earn_count == 0 or (earn_count is not None and earn_forward_eligible is True),
        },
    }
    return {**categories, "all_supported": all(item["supported"] for item in categories.values())}


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
    rebase_marker = ledger.to_dict().get("accounting_rebase") if ledger.exists else None
    rebase_document = (
        rebase_marker.get("archive_document")
        if isinstance(rebase_marker, Mapping)
        and isinstance(rebase_marker.get("archive_document"), str)
        else None
    )
    rebase_family = (
        "prospective_rebase"
        if rebase_document == PROSPECTIVE_ARCHIVE_DOCUMENT
        else "post_rebase"
        if rebase_document == REBASE_ARCHIVE_DOCUMENT
        else "unknown"
        if rebase_document is not None
        else None
    )
    archive_exists = None
    material_status = None
    ledger_digest_match = None
    marker_consistent = None
    order_state_safe = None
    if rebase_document is not None and hasattr(refs["ledger_ref"], "parent"):
        archive_snapshot = refs["ledger_ref"].parent.document(rebase_document).get(retry=None)
        archive_exists = archive_snapshot.exists
        if archive_exists and ledger.exists:
            ledger_value = ledger.to_dict()
            order_state = ledger_value.get("order_submission", {}).get("state")
            order_state_safe = order_state in {"RESERVED", "TERMINAL"}
            if rebase_family == "prospective_rebase":
                from application.rebased_recovery import PROSPECTIVE_LEDGER_SHA256

                ledger_digest_match = digest(ledger_value) == PROSPECTIVE_LEDGER_SHA256
                marker = ledger_value.get("accounting_rebase")
                marker_consistent = isinstance(marker, Mapping) and marker == {
                    key: archive_snapshot.to_dict().get(key)
                    for key in (
                        "archive_document",
                        "started_at",
                        "opening_balance_observed_at",
                        "historical_difference_unresolved",
                        "approved_proposal_run_id",
                        "approved_proposal_sha256",
                    )
                }
            elif rebase_family == "post_rebase":
                ledger_digest_match = digest(ledger_value) == archive_snapshot.to_dict().get("new_ledger_sha256")
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
                    from application.rebased_recovery import validate_prospective_rebase_material

                    validate_prospective_rebase_material(ledger_value, archive_snapshot.to_dict())
                elif rebase_family == "post_rebase":
                    from application.rebased_recovery import validate_post_rebase_material

                    validate_post_rebase_material(ledger_value, archive_snapshot.to_dict())
                material_status = "valid"
            except ValueError as exc:
                material_status = str(exc) if str(exc) in {
                    "prospective_rebase_archive_invalid",
                    "prospective_rebase_ledger_invalid",
                    "post_rebase_archive_invalid",
                    "post_rebase_ledger_invalid",
                    "post_rebase_order_state_unsafe",
                } else "rebase_material_invalid"
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
        "rebase_family": rebase_family,
        "rebase_archive_exists": archive_exists,
        "rebase_material_status": material_status,
        "rebase_ledger_digest_match": ledger_digest_match,
        "rebase_marker_consistent": marker_consistent,
        "rebase_order_state_safe": order_state_safe,
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
    if action == "earn-forward-diagnose":
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
        client = connect_client(
            os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30
        )
        return diagnose_earn_forward(refs, client=client, expected=expected, now=now)
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
    if action == "release-stale-owner":
        owner_snapshot = refs["owner_ref"].get(retry=None)
        client = None
        if owner_snapshot.exists:
            client = connect_client(
                os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30
            )
        return release_stale_owner(
            refs, client=client, target=target, expected=expected
        )
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
    if action == "prospective-rebase-apply":
        return _apply_prospective_rebase(refs, client=client, expected=expected, now=now, fixed_now=fixed_now)
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
            "earn-forward-diagnose",
            "audit",
            "preview",
            "scope-preview",
            "cash-flow-preview",
            "rebase-proposal",
            "rebase-apply",
            "prospective-rebase-apply",
            "release-stale-owner",
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
                    "stage": (
                        "private_scope_publication"
                        if args.action == "scope-preview"
                        else "stale_owner_release"
                        if args.action == "release-stale-owner"
                        else "accounting_migration_apply"
                    ),
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
