"""Shared constants, helpers, digest/evidence builders, and archive binding."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

from quant_platform_kit.common.broker_reconciliation import (  # noqa: F401
    BrokerReconciliationEvidence,
    build_broker_reconciliation_evidence,
    calculate_broker_observation_sha256 as digest,
)
from quant_platform_kit.common.broker_reconciliation_enrollment import (  # noqa: F401
    BrokerReconciliationBaselineCandidate,
    evaluate_broker_reconciliation_baseline_enrollment,
)

from binance_platform.application.broker_reconciliation import (  # noqa: F401
    collect_bnb_dividend_quantity,
    collect_read_only_reconciliation_observations,
    collect_spot_usdt_external_cash_flows,
    diagnose_balance_flows,
    diagnose_chunked_balance_flows,
    _add_reward_quantity,
)
from binance_platform.application.earn_accrual import (  # noqa: F401
    EarnCheckpointUnavailable,
    collect_earn_checkpoint,
    compare_earn_checkpoints,
    prepare_forward_earn_state,
)
from binance_platform.application.reconciliation_recovery import _validate_frozen_target  # noqa: F401
from binance_platform.accounting_migration.common import (  # noqa: F401
    APPROVED_REBASE_BALANCES_SHA256 as APPROVED_OPENING_BALANCES_SHA256,
    APPROVED_REBASE_CONTROL_SHA256 as APPROVED_OLD_CONTROL_SHA256,
    APPROVED_REBASE_LEDGER_SHA256 as APPROVED_OLD_LEDGER_SHA256,
    APPROVED_PROSPECTIVE_SHA256,
    PROSPECTIVE_ARCHIVE_DOCUMENT,
    REBASE_ARCHIVE_DOCUMENT as ARCHIVE_DOCUMENT,
    is_prospective_archive_document,
    prospective_archive_document,
)

def _facade_const(name: str, default: object) -> object:
    import sys
    mod = sys.modules.get("binance_platform.application.rebased_recovery")
    return getattr(mod, name, default) if mod is not None else default

def _facade_digest(value: object) -> str:
    return _facade_const("digest", digest)(value)  # type: ignore[operator]

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



def prospective_migration_binding(archive: Mapping[str, object]) -> dict[str, object]:
    """Bind diagnose/prepare/verify to archive+proposal identity; no self-auth hash."""
    if not isinstance(archive, Mapping):
        raise ValueError("prospective_rebase_archive_invalid")
    archive_document = archive.get("archive_document")
    if archive_document == PROSPECTIVE_ARCHIVE_DOCUMENT:
        if _facade_digest(archive) != _facade_const('PROSPECTIVE_ARCHIVE_SHA256', PROSPECTIVE_ARCHIVE_SHA256):
            raise ValueError("prospective_rebase_archive_invalid")
        return {
            "run_id": _facade_const('PROSPECTIVE_MIGRATION_RUN_ID', PROSPECTIVE_MIGRATION_RUN_ID),
            "head_sha": _facade_const('PROSPECTIVE_MIGRATION_RUN_SHA', PROSPECTIVE_MIGRATION_RUN_SHA),
            "archive_document": PROSPECTIVE_ARCHIVE_DOCUMENT,
            "archive_sha256": _facade_const('PROSPECTIVE_ARCHIVE_SHA256', PROSPECTIVE_ARCHIVE_SHA256),
            "ledger_sha256": _facade_const('PROSPECTIVE_LEDGER_SHA256', PROSPECTIVE_LEDGER_SHA256),
            "approval_sha256": _facade_const('APPROVED_PROSPECTIVE_SHA256', APPROVED_PROSPECTIVE_SHA256),
            "proposal_run_id": _facade_const('PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID', PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID),
            "historical": True,
        }
    if not is_prospective_archive_document(archive_document):
        raise ValueError("prospective_rebase_archive_invalid")
    approved = archive.get("approved_proposal")
    if not isinstance(approved, Mapping):
        raise ValueError("prospective_rebase_archive_invalid")
    try:
        proposal_run_id = str(approved.get("proposal_run_id") or "").strip()
        expected_document = prospective_archive_document(proposal_run_id)
        source_sha = approved.get("source_sha")
        approval_digest = _facade_digest(approved)
        ledger_digest = archive.get("new_ledger_sha256")
    except Exception as exc:
        raise ValueError("prospective_rebase_archive_invalid") from exc
    if (
        expected_document != archive_document
        or archive.get("approved_proposal_run_id") != proposal_run_id
        or archive.get("approved_proposal_sha256") != approval_digest
        or approved.get("archive_document") != archive_document
        or not isinstance(source_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
        or not isinstance(ledger_digest, str)
        or len(ledger_digest) != 64
        or approval_digest != archive.get("approved_proposal_sha256")
    ):
        raise ValueError("prospective_rebase_archive_invalid")
    return {
        "run_id": int(proposal_run_id),
        "head_sha": source_sha,
        "archive_document": archive_document,
        "archive_sha256": _facade_digest(archive),
        "ledger_sha256": ledger_digest,
        "approval_sha256": approval_digest,
        "proposal_run_id": proposal_run_id,
        "historical": False,
    }

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
        "digest": _facade_digest(sorted(canonical, key=lambda row: row["asset"])),
        "balances": balances,
        "observed_non_managed_asset_count": observed_count,
    }


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
        positions_sha256=_facade_digest(observations.positions),
        cash_sha256=_facade_digest(observations.cash),
        open_orders_sha256=_facade_digest(observations.open_orders),
        recent_executions_sha256=_facade_digest(observations.recent_executions),
        local_execution_ledger_sha256=_facade_digest(observations.local_execution_ledger),
    )

