"""LOCAL prerequisite: validate supplied receipts and prepare an in-memory preview.

No backend enumeration, store call, delivery, ACK or cursor advancement exists.
Contiguity describes only the bounded supplied batch, not archive completeness.
The producer remains the single payload/identity validation authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from application.interval_source_receipt_candidate import (
    IntervalReceiptBlocked,
    IntervalReceiptConflict,
    _FIELDS,
    _SCHEMA,
    _digest,
    _json,
    _payload_material,
    _time,
    _validate_path,
)


@dataclass(frozen=True)
class IntervalReceiptPreview:
    """Detached output for inspection/calculation, never trusted delivery evidence."""

    _records_json: tuple[str, ...] = field(repr=False)
    interval_ids_sha256: tuple[str, ...] = field(repr=False)
    payload_sha256s: tuple[str, ...] = field(repr=False)
    input_count: int
    duplicate_count: int
    encoded_bytes: int

    @property
    def delivery_status(self):
        return "blocked_archive_enumeration_and_durable_ack_unqualified"

    @property
    def delivery_acknowledged(self):
        return False

    def live_run_records(self):
        """Return fresh copies in the existing consumer envelope; never persist."""
        return [json.loads(record) for record in self._records_json]


def _read_receipt(entry, *, ledger_path, account_scope_sha256):
    if (type(entry) not in (tuple, list) or len(entry) != 2
            or not isinstance(entry[0], str) or type(entry[1]) is not dict):
        raise IntervalReceiptBlocked("interval_reader_receipt_invalid")
    path, receipt = entry
    if (len(receipt) != 5
            or set(receipt) != {"schema_version", "interval_id_sha256", "payload_sha256", "source", "interval"}
            or receipt["schema_version"] != _SCHEMA
            or type(receipt["source"]) is not dict or type(receipt["interval"]) is not dict):
        raise IntervalReceiptBlocked("interval_reader_receipt_invalid")
    # Resource checks precede the producer's deepcopy/Decimal work. These use
    # its exact fields and string bound; they do not define another payload.
    interval = receipt["interval"]
    if (len(interval) != len(_FIELDS) or set(interval) != _FIELDS
            or any(type(value) is not str or len(value) > 80 for value in interval.values())):
        raise IntervalReceiptBlocked("interval_reader_receipt_invalid")
    payload, start, end, logical_id = _payload_material(receipt["interval"])
    if (receipt["interval_id_sha256"] != logical_id
            or receipt["payload_sha256"] != _digest(payload)):
        raise IntervalReceiptBlocked("interval_reader_receipt_invalid")
    source = receipt["source"]
    if len(source) != 5 or set(source) != {"ledger_path", "ledger_before_version", "ledger_before_sha256",
                       "ledger_after_sha256", "patch_sha256"}:
        raise IntervalReceiptBlocked("interval_reader_receipt_invalid")
    if (source["ledger_path"] != ledger_path or payload["account_scope_sha256"] != account_scope_sha256
            or path != ledger_path + "__interval_receipt_" + logical_id):
        raise IntervalReceiptBlocked("interval_reader_binding_mismatch")
    version = source["ledger_before_version"]
    if (not isinstance(version, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z", version)
            or any(not isinstance(source[key], str) or not re.fullmatch(r"[0-9a-f]{64}", source[key])
                   for key in ("ledger_before_sha256", "ledger_after_sha256", "patch_sha256"))):
        raise IntervalReceiptBlocked("interval_reader_receipt_invalid")
    # Validate calendar seconds through the producer; preserve the separately
    # checked nine-digit nanos verbatim for chronological source-order checking.
    _time(version[:19] + "Z")
    serialized = _json(receipt)
    encoded_bytes = len(path.encode("utf-8")) + len(serialized.encode("utf-8"))
    return payload, start, end, logical_id, version, serialized, encoded_bytes


def prepare_retained_interval_preview(receipts, *, ledger_path, account_scope_sha256,
                                      cutover_start_at, cutover_end_at, strategy_profile,
                                      lifecycle_stream_id, max_receipts, max_bytes):
    """Validate a bounded batch, not an archive query, and prepare ordered records.

    Every supplied interval must lie within the explicit cutover and together
    span it without a gap or overlap. Exact receipt duplicates are no-ops; the
    same normalized logical ID with changed payload/source metadata conflicts.
    Metadata hashes are structurally checked, not proven against source state.
    Identical relative ledger paths cannot establish physical backend identity.
    No supplied receipt or this preview can establish a durable consumer ACK.
    """
    try:
        return _prepare(
            receipts, ledger_path=ledger_path, account_scope_sha256=account_scope_sha256,
            cutover_start_at=cutover_start_at, cutover_end_at=cutover_end_at,
            strategy_profile=strategy_profile, lifecycle_stream_id=lifecycle_stream_id,
            max_receipts=max_receipts, max_bytes=max_bytes,
        )
    except IntervalReceiptConflict:
        raise IntervalReceiptConflict("interval_reader_existing_conflict") from None
    except IntervalReceiptBlocked as error:
        # Only fixed reasons created in this module leave this boundary. Never
        # echo supplied financial material, labels, paths or backend exceptions.
        reason = str(error)
        if not re.fullmatch(r"interval_reader_[a-z_]+", reason):
            reason = "interval_reader_receipt_invalid"
        raise IntervalReceiptBlocked(reason) from None
    except Exception:
        raise IntervalReceiptBlocked("interval_reader_material_invalid") from None


def _prepare(receipts, *, ledger_path, account_scope_sha256, cutover_start_at, cutover_end_at,
             strategy_profile, lifecycle_stream_id, max_receipts, max_bytes):
    if (type(max_receipts) is not int or not 1 <= max_receipts <= 1000
            or type(max_bytes) is not int or not 1 <= max_bytes <= 1_048_576
            or type(receipts) not in (tuple, list)):
        raise IntervalReceiptBlocked("interval_reader_limits_invalid")
    if len(receipts) > max_receipts:
        raise IntervalReceiptBlocked("interval_reader_count_limit")
    _validate_path(ledger_path)
    if (not isinstance(account_scope_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", account_scope_sha256)
            or any(not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", label)
                   for label in (strategy_profile, lifecycle_stream_id))):
        raise IntervalReceiptBlocked("interval_reader_binding_invalid")
    start, end = _time(cutover_start_at), _time(cutover_end_at)
    if not start < end:
        raise IntervalReceiptBlocked("interval_reader_cutover_invalid")
    by_id, by_end = {}, {}
    total_bytes, duplicates = 0, 0
    for entry in receipts:
        material = _read_receipt(entry, ledger_path=ledger_path, account_scope_sha256=account_scope_sha256)
        payload, interval_start, interval_end, logical_id, version, serialized, size = material
        total_bytes += size
        if total_bytes > max_bytes:
            raise IntervalReceiptBlocked("interval_reader_bytes_limit")
        if interval_start < start or interval_end > end:
            raise IntervalReceiptBlocked("interval_reader_coverage_outside_cutover")
        previous = by_id.get(logical_id)
        if previous is not None:
            if previous[5] != serialized:
                raise IntervalReceiptConflict("interval_reader_existing_conflict")
            duplicates += 1
            continue
        if interval_end in by_end and by_end[interval_end] != interval_start:
            raise IntervalReceiptConflict("interval_reader_existing_conflict")
        by_end[interval_end] = interval_start
        by_id[logical_id] = material
    ordered = sorted(by_id.values(), key=lambda material: (material[1], material[2]))
    cursor, last_version = start, None
    for _, interval_start, interval_end, _, version, _, _ in ordered:
        if interval_start < cursor:
            raise IntervalReceiptBlocked("interval_reader_coverage_overlap")
        if interval_start != cursor:
            raise IntervalReceiptBlocked("interval_reader_coverage_gap")
        if last_version is not None and version <= last_version:
            raise IntervalReceiptBlocked("interval_reader_source_order_invalid")
        cursor, last_version = interval_end, version
    if cursor != end:
        raise IntervalReceiptBlocked("interval_reader_coverage_gap")
    records = tuple(_json({
        "strategy_profile": strategy_profile, "domain": "crypto", "record_kind": "execution",
        "recorded_at": interval_end.isoformat(), "lifecycle_stream_id": lifecycle_stream_id,
        "execution_result": {"external_cash_flow_interval": payload},
    }) for payload, _, interval_end, _, _, _, _ in ordered)
    return IntervalReceiptPreview(records, tuple(item[3] for item in ordered),
                                  tuple(_digest(item[0]) for item in ordered),
                                  len(receipts), duplicates, total_bytes)
