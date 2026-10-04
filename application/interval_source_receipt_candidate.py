"""Opt-in LOCAL CANDIDATE: retain an interval in the checkpoint transaction.

No client, backend defaults, runtime registration, receipt query, delivery, or
deletion is provided. The caller must supply references and a transaction runner
from the SAME already-bound state backend. Tests use only an in-memory fake.
This does not qualify backend durability or a native PerformanceStore ACK.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


_FIELDS = {
    "account_scope_sha256", "start_at", "end_at", "end_equity_usdt",
    "net_external_cash_flow", "currency", "valuation_basis",
}
_PATCH_FIELDS = {
    "earn_accrual_checkpoint", "external_cash_flow_cursor",
    "earn_accounted_net_changes", "last_balance_snapshot",
    "daily_external_principal_usdt",
}
_SCHEMA = "binance_interval_source_receipt_candidate.v1"
_BASIS = "checkpoint_quantities_sampled_prices"


class IntervalReceiptBlocked(ValueError):
    """Fixed local reason; no write was requested after failed preconditions."""


class IntervalReceiptConflict(IntervalReceiptBlocked):
    """An existing create-once receipt differs; this API never replaces it."""


class IntervalReceiptUncertain(RuntimeError):
    """Commit/readback was not established; never interpret as an ACK/rollback."""


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise IntervalReceiptBlocked("interval_receipt_material_invalid") from None


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value):
    # Actual producer datetime strings have microsecond precision. Reject a
    # finer payload timestamp rather than collapse distinct logical intervals.
    if (not isinstance(value, str) or not 0 < len(value) <= 80
            or re.search(r"[.,][0-9]{7}", value)):
        raise IntervalReceiptBlocked("interval_receipt_time_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise IntervalReceiptBlocked("interval_receipt_time_invalid") from None


def _version(value):
    """Exact UTC seconds+nanos from snapshot.update_time, never isoformat CAS."""
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise IntervalReceiptBlocked("interval_receipt_version_invalid")
    nanos = getattr(value, "nanosecond", 0) or value.microsecond * 1000
    if type(nanos) is not int or not 0 <= nanos < 1_000_000_000 or nanos // 1000 != value.microsecond:
        raise IntervalReceiptBlocked("interval_receipt_version_invalid")
    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def _amount(value, *, positive=False):
    if not isinstance(value, str) or not 0 < len(value) <= 80:
        raise IntervalReceiptBlocked("interval_receipt_amount_invalid")
    try:
        amount = Decimal(value)
        if (not amount.is_finite() or amount.copy_abs() > Decimal("1e30")
                or amount.as_tuple().exponent < -30 or (positive and amount <= 0)):
            raise ValueError
        return amount
    except (InvalidOperation, ValueError):
        raise IntervalReceiptBlocked("interval_receipt_amount_invalid") from None


@dataclass(frozen=True)
class IntervalReceiptPlan:
    """Frozen serialized material, detached from the caller's mutable state."""

    ledger_path: str
    receipt_path: str
    source_ledger_version: str
    ledger_before_sha256: str
    ledger_after_sha256: str
    payload_sha256: str
    patch_json: str
    receipt_json: str


def _validate_path(value):
    if (not isinstance(value, str) or len(value) > 512
            or len(value.split("/")) % 2
            or any(not part or part in {".", ".."} for part in value.split("/"))):
        raise IntervalReceiptBlocked("interval_receipt_reference_invalid")


def _payload_material(interval):
    if not isinstance(interval, Mapping) or set(interval) != _FIELDS:
        raise IntervalReceiptBlocked("interval_receipt_payload_invalid")
    payload = copy.deepcopy(dict(interval))
    scope = payload["account_scope_sha256"]
    if (not isinstance(scope, str) or not re.fullmatch(r"[0-9a-f]{64}", scope)
            or payload["currency"] != "USDT" or payload["valuation_basis"] != _BASIS):
        raise IntervalReceiptBlocked("interval_receipt_payload_invalid")
    start, end = _time(payload["start_at"]), _time(payload["end_at"])
    if not start < end or end.date() - start.date() > timedelta(days=1):
        raise IntervalReceiptBlocked("interval_receipt_time_invalid")
    _amount(payload["end_equity_usdt"], positive=True)
    _amount(payload["net_external_cash_flow"])
    identity = {"account_scope_sha256": scope, "start_at": start.isoformat(), "end_at": end.isoformat()}
    return payload, start, end, _digest(identity)


def _receipt_material(*, ledger_path, source_version, before_digest, after_digest, patch, payload):
    _, _, _, logical_id = _payload_material(payload)
    return {
        "schema_version": _SCHEMA,
        "interval_id_sha256": logical_id,
        "payload_sha256": _digest(payload),
        "source": {
            "ledger_path": ledger_path,
            "ledger_before_version": source_version,
            "ledger_before_sha256": before_digest,
            "ledger_after_sha256": after_digest,
            "patch_sha256": _digest(patch),
        },
        "interval": payload,
    }


def _validate_plan(plan):
    """A frozen dataclass is not a trust boundary; check all serialized bindings."""
    try:
        _validate_path(plan.ledger_path)
        patch, receipt = json.loads(plan.patch_json), json.loads(plan.receipt_json)
        if (not isinstance(patch, dict) or set(patch) != _PATCH_FIELDS
                or not isinstance(receipt, dict) or _json(patch) != plan.patch_json
                or not isinstance(plan.source_ledger_version, str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z", plan.source_ledger_version)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in (plan.ledger_before_sha256, plan.ledger_after_sha256, plan.payload_sha256))):
            raise ValueError
        _time(plan.source_ledger_version[:19] + "Z")
        payload, _, end, logical_id = _payload_material(receipt["interval"])
        checkpoint = patch["earn_accrual_checkpoint"]
        assets = checkpoint["assets"]
        if (checkpoint["account_scope_sha256"] != payload["account_scope_sha256"]
                or checkpoint["execution_authority_granted"] is not False
                or _time(checkpoint["observed_at"]) != end
                or _time(patch["external_cash_flow_cursor"]["observed_at"]) != end
                or not isinstance(assets, Mapping) or not assets
                or patch["earn_accounted_net_changes"] != {asset: "0" for asset in assets}
                or plan.receipt_path != plan.ledger_path + "__interval_receipt_" + logical_id
                or _digest(payload) != plan.payload_sha256):
            raise ValueError
        expected = _receipt_material(
            ledger_path=plan.ledger_path, source_version=plan.source_ledger_version,
            before_digest=plan.ledger_before_sha256, after_digest=plan.ledger_after_sha256,
            patch=patch, payload=payload,
        )
        if _json(expected) != plan.receipt_json:
            raise ValueError
        return patch, payload
    except (AttributeError, KeyError, TypeError, ValueError):
        raise IntervalReceiptBlocked("interval_receipt_plan_invalid") from None


def build_interval_receipt_plan(*, ledger_path, source_ledger_update_time,
                                previous_state, next_state, interval):
    """Bind an ALREADY verified forward transition to its exact interval.

    Caller retains responsibility for the existing conservation, cursor history,
    fill provenance, price sampling and no-order gates. This helper does not
    reconstruct evidence or establish those facts from balances/daily totals.
    """
    _validate_path(ledger_path)
    payload, start, end, logical_id = _payload_material(interval)
    scope = payload["account_scope_sha256"]
    if not isinstance(previous_state, Mapping) or not isinstance(next_state, Mapping):
        raise IntervalReceiptBlocked("interval_receipt_state_invalid")
    old, new = copy.deepcopy(dict(previous_state)), copy.deepcopy(dict(next_state))
    try:
        before, after = old["earn_accrual_checkpoint"], new["earn_accrual_checkpoint"]
        if (before["account_scope_sha256"] != scope or after["account_scope_sha256"] != scope
                or before["execution_authority_granted"] is not False
                or after["execution_authority_granted"] is not False
                or _time(before["observed_at"]) != start or _time(after["observed_at"]) != end
                or _time(old["external_cash_flow_cursor"]["observed_at"]) != start
                or _time(new["external_cash_flow_cursor"]["observed_at"]) != end
                or not isinstance(before["assets"], Mapping) or not before["assets"]
                or set(before["assets"]) != set(after["assets"])
                or new["earn_accounted_net_changes"] != {asset: "0" for asset in before["assets"]}
                or old["order_submission"]["state"] not in {"RESERVED", "TERMINAL"}):
            raise ValueError
        changed = {key for key in set(old) | set(new) if key not in old or key not in new
                   or _json(old[key]) != _json(new[key])}
        if not changed <= _PATCH_FIELDS or any(key not in new for key in _PATCH_FIELDS):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise IntervalReceiptBlocked("interval_receipt_transition_invalid") from None
    patch = {key: new[key] for key in _PATCH_FIELDS}
    # This patch preserves all unrelated fields, including future audit/fill keys.
    if _json({**old, **patch}) != _json(new):
        raise IntervalReceiptBlocked("interval_receipt_transition_invalid")
    receipt_path = ledger_path + "__interval_receipt_" + logical_id
    source_version = _version(source_ledger_update_time)
    before_digest, after_digest, payload_digest = _digest(old), _digest(new), _digest(payload)
    receipt = _receipt_material(ledger_path=ledger_path, source_version=source_version,
                                before_digest=before_digest, after_digest=after_digest,
                                patch=patch, payload=payload)
    return IntervalReceiptPlan(ledger_path, receipt_path, source_version, before_digest,
                               after_digest, payload_digest, _json(patch), _json(receipt))


def _read(transaction, *, ledger_ref, owner_ref, receipt_ref, owner_id):
    # Firestore requires all reads before writes; all three are transaction reads.
    owner = owner_ref.get(transaction=transaction, retry=None)
    ledger = ledger_ref.get(transaction=transaction, retry=None)
    receipt = receipt_ref.get(transaction=transaction, retry=None)
    if (not owner.exists or not isinstance(owner.to_dict(), Mapping)
            or owner.to_dict().get("owner_id") != owner_id or not ledger.exists):
        raise IntervalReceiptBlocked("interval_receipt_owner_or_ledger_changed")
    return ledger, receipt


def _same_backend(*, bound_client, database, refs, transaction=None):
    """Minimal pinned Firestore-native identity check; never opens a client."""
    if (bound_client is None or not isinstance(database, str)
            or not re.fullmatch(r"projects/[^/]+/databases/[^/]+", database)
            or getattr(bound_client, "_database_string", None) != database
            or (transaction is not None and getattr(transaction, "_client", None) is not bound_client)):
        raise IntervalReceiptBlocked("interval_receipt_backend_mismatch")
    for ref in refs:
        if (getattr(ref, "_client", None) is not bound_client
                or getattr(ref, "_document_path", None) != database + "/documents/" + ref.path):
            raise IntervalReceiptBlocked("interval_receipt_backend_mismatch")
    if transaction is not None and (
        type(getattr(transaction, "_max_attempts", None)) is not int
        or transaction._max_attempts != 1
    ):
        raise IntervalReceiptBlocked("interval_receipt_transaction_attempts_invalid")


def persist_interval_source_receipt(*, bound_client, transaction_runner, ledger_ref, owner_ref,
                                    owner_id, expected_ledger_update_time, plan):
    """Stage receipt create + checkpoint/cursor patch, then exact read-only readback.

    bound_client must be the existing state store's native client. The pinned
    native _client / _database_string / _document_path seam is checked before
    document reads. transaction_runner(callback) must execute one transaction,
    with max_attempts=1, using that client. No implicit
    client construction or backend selection is allowed. Unknown commit/readback
    raises uncertainty; it is not a delivery ACK or proof of rollback.

    Retry uses the original frozen plan and a freshly observed ledger version.
    Identical receipt plus exact planned poststate is a no-write replay. A later
    progressed ledger fails closed here; archive queries remain a separate proposal.
    """
    if (not isinstance(plan, IntervalReceiptPlan) or not callable(transaction_runner)
            or not isinstance(owner_id, str) or not owner_id or owner_id != owner_id.strip()
            or getattr(ledger_ref, "path", None) != plan.ledger_path
            or getattr(owner_ref, "path", None) != plan.ledger_path + "__owner"):
        raise IntervalReceiptBlocked("interval_receipt_reference_or_owner_invalid")
    patch, payload = _validate_plan(plan)
    expected_version = _version(expected_ledger_update_time)
    database = getattr(bound_client, "_database_string", None)
    _same_backend(bound_client=bound_client, database=database, refs=(ledger_ref, owner_ref))
    receipt_ref = ledger_ref.parent.document(plan.receipt_path.rsplit("/", 1)[1])
    if getattr(receipt_ref, "path", None) != plan.receipt_path:
        raise IntervalReceiptBlocked("interval_receipt_reference_invalid")
    refs = (ledger_ref, owner_ref, receipt_ref)
    _same_backend(bound_client=bound_client, database=database, refs=refs)

    def stage(transaction):
        _same_backend(bound_client=bound_client, database=database, refs=refs, transaction=transaction)
        ledger, receipt = _read(transaction, ledger_ref=ledger_ref, owner_ref=owner_ref,
                                receipt_ref=receipt_ref, owner_id=owner_id)
        if _version(ledger.update_time) != expected_version:
            raise IntervalReceiptBlocked("interval_receipt_ledger_version_changed")
        if receipt.exists:
            if _json(receipt.to_dict()) != plan.receipt_json:
                raise IntervalReceiptConflict("interval_receipt_existing_conflict")
            if _digest(ledger.to_dict()) != plan.ledger_after_sha256:
                raise IntervalReceiptBlocked("interval_receipt_replay_poststate_unverified")
            if _json({key: ledger.to_dict()[key] for key in _PATCH_FIELDS}) != plan.patch_json:
                raise IntervalReceiptBlocked("interval_receipt_replay_patch_unverified")
            return "identical"
        if (expected_version != plan.source_ledger_version
                or _digest(ledger.to_dict()) != plan.ledger_before_sha256):
            raise IntervalReceiptBlocked("interval_receipt_ledger_source_changed")
        actual = ledger.to_dict()
        regenerated = build_interval_receipt_plan(
            ledger_path=plan.ledger_path, source_ledger_update_time=ledger.update_time,
            previous_state=actual, next_state={**actual, **patch}, interval=payload,
        )
        if regenerated != plan:
            raise IntervalReceiptBlocked("interval_receipt_plan_source_mismatch")
        transaction.create(receipt_ref, json.loads(plan.receipt_json))
        transaction.update(ledger_ref, patch)
        return "created"

    try:
        mode = transaction_runner(stage)
        if mode not in {"created", "identical"}:
            raise IntervalReceiptUncertain("interval_receipt_commit_uncertain")
    except IntervalReceiptBlocked:
        raise
    except Exception:
        raise IntervalReceiptUncertain("interval_receipt_commit_uncertain") from None

    def readback(transaction):
        _same_backend(bound_client=bound_client, database=database, refs=refs, transaction=transaction)
        ledger, receipt = _read(transaction, ledger_ref=ledger_ref, owner_ref=owner_ref,
                                receipt_ref=receipt_ref, owner_id=owner_id)
        if (not receipt.exists or _json(receipt.to_dict()) != plan.receipt_json
                or _digest(ledger.to_dict()) != plan.ledger_after_sha256):
            raise IntervalReceiptBlocked("interval_receipt_readback_mismatch")
        return True

    try:
        if transaction_runner(readback) is not True:
            raise IntervalReceiptUncertain("interval_receipt_readback_uncertain")
    except Exception:
        raise IntervalReceiptUncertain("interval_receipt_readback_uncertain") from None
    return {
        "status": "source_receipt_readback_verified",
        "write_mode": mode,
        "receipt_path": plan.receipt_path,
        "payload_sha256": plan.payload_sha256,
        "performance_delivery_acknowledged": False,
    }
