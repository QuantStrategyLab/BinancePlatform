"""Shared constants, exceptions, and pure validation helpers for accounting migration."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant_platform_kit.binance import connect_client
from quant_platform_kit.common.broker_reconciliation import (
    calculate_broker_observation_sha256 as digest,
)

from binance_platform.console_origin import resolve_console_origin

from binance_platform.application.broker_reconciliation import (
    collect_read_only_reconciliation_observations,
    collect_spot_usdt_external_cash_flows,
    diagnose_balance_flows,
    diagnose_bnb_wallet_activity,
)
from binance_platform.application.earn_accrual import _amount as _earn_amount
from binance_platform.application.portfolio_service import (
    maybe_rebase_daily_state_for_balance_change,
)
from binance_platform.live_services import get_firestore_client


REPOSITORY = "QuantStrategyLab/BinancePlatform"
LEDGER_DOCUMENT = "MULTI_ASSET_STATE"
OWNER_DOCUMENT = "MULTI_ASSET_STATE__owner"
CONTROL_DOCUMENT = "MULTI_ASSET_STATE__recovery"
SCHEMA_VERSION = "binance_daily_accounting_migration_candidate.v1"
NEW_BASIS = "trend_mark_plus_cash_flow_v1"
PREVIEW_PATH = Path("reports/binance-accounting-migration-preview/candidate.json")
REBASE_PROPOSAL_PATH = Path("reports/binance-accounting-rebase-proposal/proposal.cms")
PRIVATE_SCOPE_CONSOLE_PATH = "/api/internal/binance-private-scope"
PRIVATE_SCOPE_CONSOLE_URL: str | None = None


def resolve_private_scope_console_url(env: Mapping[str, str] | None = None) -> str:
    """Resolve private-scope receiver URL from monkeypatched override or protected config."""
    if PRIVATE_SCOPE_CONSOLE_URL is not None:
        return PRIVATE_SCOPE_CONSOLE_URL
    return resolve_console_origin(os.environ if env is None else env) + PRIVATE_SCOPE_CONSOLE_PATH
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

# Historical completed prospective migration (Runtime 34690028846). Kept only so
# recovery/inspect can still bind that already-applied archive; new proposals and
# apply no longer use these fixed roots.
PROSPECTIVE_ARCHIVE_DOCUMENT = "MULTI_ASSET_STATE__before_rebase_34690028846"
APPROVED_PROSPECTIVE_SHA256 = "cad5cd02ebc554835492cc99f78b0639656a4261617dc8492fbc65c16f1d56a2"
_PROSPECTIVE_ARCHIVE_PREFIX = "MULTI_ASSET_STATE__before_rebase_"
_PROPOSAL_RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_PROSPECTIVE_PROPOSED_FIELD_KEYS = _ACCOUNTING_FIELDS | {
    "earn_accrual_checkpoint",
    "earn_accounted_net_changes",
    "external_cash_flow_cursor",
}
_BNB_DIAGNOSTIC_QUANTUM = Decimal("0.00000001")


class MigrationBlocked(ValueError):
    """A required proof is absent; no migration may be written."""


class MigrationApplyUncertain(RuntimeError):
    """The single write attempt may have committed and must not be retried."""


class MigrationAtomicPrecondition(MigrationBlocked):
    """The transaction proved the preview stale before scheduling a write."""


@dataclass(frozen=True)
class MigrationHooks:
    """Explicit runtime bindings so core modules stay free of global env/monkeypatch state."""

    digest_fn: Callable[[Any], str] = digest
    connect_client_fn: Callable[..., Any] = connect_client
    firestore_client_fn: Callable[[], Any] = get_firestore_client
    collect_observations_fn: Callable[..., Any] = collect_read_only_reconciliation_observations
    collect_cash_flows_fn: Callable[..., Any] = collect_spot_usdt_external_cash_flows
    diagnose_flows_fn: Callable[..., Any] = diagnose_balance_flows
    diagnose_bnb_wallet_fn: Callable[..., Any] = diagnose_bnb_wallet_activity
    maybe_rebase_daily_state_fn: Callable[..., Any] = maybe_rebase_daily_state_for_balance_change
    quiesce_control_sha256: str = QUIESCE_CONTROL_SHA256
    quiesce_control_update_time: str = QUIESCE_CONTROL_UPDATE_TIME
    approved_rebase_ledger_sha256: str = APPROVED_REBASE_LEDGER_SHA256
    approved_rebase_control_sha256: str = APPROVED_REBASE_CONTROL_SHA256
    approved_rebase_balances_sha256: str = APPROVED_REBASE_BALANCES_SHA256
    approved_prospective_sha256: str = APPROVED_PROSPECTIVE_SHA256
    datetime_cls: Any = datetime
    read_source_fn: Callable[..., Any] | None = None
    collect_evidence_fn: Callable[..., Any] | None = None
    validate_stale_owner_control_fn: Callable[..., Any] | None = None
    private_spot_account_fn: Callable[..., Any] | None = None
    prospective_preflight_fn: Callable[..., Any] | None = None
    verified_control_account_scope_fn: Callable[..., Any] | None = None
    validated_private_scope_source_fn: Callable[..., Any] | None = None
    collect_prospective_opening_fn: Callable[..., Any] | None = None
    build_rebase_proposal_fn: Callable[..., Any] | None = None
    encrypt_rebase_proposal_fn: Callable[..., Any] | None = None
    build_candidate_fn: Callable[..., Any] | None = None
    compare_and_apply_fn: Callable[..., Any] | None = None
    verify_applied_fn: Callable[..., Any] | None = None
    publish_private_spot_scope_preview_fn: Callable[..., Any] | None = None
    request_json_fn: Callable[..., Any] | None = None


DEFAULT_HOOKS = MigrationHooks()


def resolve_hooks(hooks: MigrationHooks | None) -> MigrationHooks:
    return hooks if hooks is not None else DEFAULT_HOOKS


def parse_symbols(raw_symbols: str | None) -> tuple[str, ...]:
    raw = str(raw_symbols or "").strip()
    symbols = tuple(
        dict.fromkeys(item.strip().upper() for item in raw.split(",") if item.strip())
    )
    if not symbols:
        raise MigrationBlocked("reconciliation_symbols_missing")
    return symbols


def _finite(value, *, nonnegative: bool = True) -> float:
    import math

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


def _canonical_sha(value, *, digest_fn: Callable[[Any], str] = digest) -> str:
    return digest_fn(value)


def _candidate_digest(
    candidate: Mapping[str, object], *, digest_fn: Callable[[Any], str] = digest
) -> str:
    return _canonical_sha(
        {
            key: value
            for key, value in candidate.items()
            if key not in {"candidate_sha256", "public_preview"}
        },
        digest_fn=digest_fn,
    )


def validate_runtime_context(env: Mapping[str, str]) -> None:
    if (
        env.get("GITHUB_REPOSITORY") != REPOSITORY
        or env.get("GITHUB_REF") != "refs/heads/main"
        or env.get("GITHUB_WORKFLOW_REF")
        != f"{REPOSITORY}/.github/workflows/main.yml@refs/heads/main"
        or env.get("RUNTIME_TARGET_ENABLED", "").lower() != "false"
        or env.get("RECONCILE_ONLY", "").lower() != "true"
        or not re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", ""))
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


def _proposal_run_id(raw: object) -> str:
    """Validate a workflow run id that is bound into a one-shot proposal."""
    text = str(raw or "").strip()
    if not _PROPOSAL_RUN_ID_RE.fullmatch(text):
        raise MigrationBlocked("proposal_run_id_invalid")
    return text


def prospective_archive_document(proposal_run_id: object) -> str:
    """Archive name for one prospective apply; existence is the one-shot guard."""
    run_id = _proposal_run_id(proposal_run_id)
    return f"{_PROSPECTIVE_ARCHIVE_PREFIX}{run_id}"


def is_prospective_archive_document(name: object) -> bool:
    if not isinstance(name, str):
        return False
    # Legacy approved rebase uses the same before_rebase_* prefix; keep it post_rebase.
    if name == REBASE_ARCHIVE_DOCUMENT:
        return False
    if name == PROSPECTIVE_ARCHIVE_DOCUMENT:
        return True
    prefix = _PROSPECTIVE_ARCHIVE_PREFIX
    return name.startswith(prefix) and _PROPOSAL_RUN_ID_RE.fullmatch(name[len(prefix):]) is not None


def _snapshot_marker(snapshot) -> dict[str, object]:
    return {
        "exists": bool(snapshot.exists),
        "update_time": _timestamp(snapshot.update_time) if snapshot.exists else None,
        "value": snapshot.to_dict() if snapshot.exists else None,
    }


def _snapshot_matches(snapshot, marker) -> bool:
    return _snapshot_marker(snapshot) == marker


def _diagnosis_decimal(value, *, signed: bool = False) -> Decimal:
    try:
        return _earn_amount(str(value), signed=signed)
    except (TypeError, ValueError, InvalidOperation):
        raise MigrationBlocked("earn_diagnosis_numeric_input_invalid") from None


def _direction(value) -> str:
    return "INCREASE" if value > 0 else "DECREASE" if value < 0 else "UNCHANGED"


def _private_spot_account(
    account,
    *,
    expected_account_scope_sha256: str,
    digest_fn: Callable[[Any], str] = digest,
):
    if not isinstance(account, Mapping):
        raise MigrationBlocked("private_scope_balance_invalid")
    uid = str(account.get("uid") or "")
    if not uid or digest_fn({"account_uid": uid}) != expected_account_scope_sha256:
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


def ledger_scope(
    ledger: Mapping[str, object], *, configured_symbols: tuple[str, ...]
) -> tuple[set[str], tuple[str, ...]]:
    configured = tuple(dict.fromkeys((*configured_symbols, "BTCUSDT", "BNBUSDT")))
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


def firestore_refs(firestore_client_fn: Callable[[], Any] = get_firestore_client) -> dict[str, Any]:
    collection = firestore_client_fn().collection("strategy")
    return {
        "ledger_ref": collection.document(LEDGER_DOCUMENT),
        "owner_ref": collection.document(OWNER_DOCUMENT),
        "control_ref": collection.document(CONTROL_DOCUMENT),
    }


def _now_utc(hooks: MigrationHooks) -> datetime:
    return hooks.datetime_cls.now(timezone.utc)
