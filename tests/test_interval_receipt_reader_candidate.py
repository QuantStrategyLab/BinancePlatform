"""Bounded synthetic receipts only; no archive, broker or delivery operations."""

import copy
import json
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from application.interval_source_receipt_candidate import (
    IntervalReceiptBlocked,
    IntervalReceiptConflict,
    build_interval_receipt_plan,
)
from application.interval_receipt_reader_candidate import prepare_retained_interval_preview


LEDGER = "strategy/SYNTHETIC_STATE"
SCOPE = "a" * 64
TIMES = [f"2026-10-0{day}T00:00:00+00:00" for day in range(1, 5)]


def source_receipt(index=0, *, start=None, end=None, equity="1000", flow="0",
                   scope=SCOPE, ledger=LEDGER, version=None):
    start, end = start or TIMES[index], end or TIMES[index + 1]
    previous = {
        "earn_accrual_checkpoint": {
            "account_scope_sha256": scope, "observed_at": start,
            "assets": {"USDT": {"quantity": "1000"}},
            "execution_authority_granted": False,
        },
        "external_cash_flow_cursor": {"observed_at": start, "records": []},
        "earn_accounted_net_changes": {"USDT": "0"},
        "last_balance_snapshot": {"USDT": 1000.0},
        "daily_external_principal_usdt": 0.0,
        "order_submission": {"state": "TERMINAL"},
        "synthetic_private_field": "not_in_preview",
    }
    following = copy.deepcopy(previous)
    following["earn_accrual_checkpoint"]["observed_at"] = end
    following["earn_accrual_checkpoint"]["assets"]["USDT"]["quantity"] = equity
    following["external_cash_flow_cursor"]["observed_at"] = end
    following["last_balance_snapshot"] = {"USDT": float(equity)}
    following["daily_external_principal_usdt"] = float(flow)
    interval = {
        "account_scope_sha256": scope, "start_at": start, "end_at": end,
        "end_equity_usdt": equity, "net_external_cash_flow": flow,
        "currency": "USDT", "valuation_basis": "checkpoint_quantities_sampled_prices",
    }
    version = version or datetime(2026, 10, index + 1, tzinfo=timezone.utc)
    plan = build_interval_receipt_plan(
        ledger_path=ledger, source_ledger_update_time=version,
        previous_state=previous, next_state=following, interval=interval,
    )
    return plan.receipt_path, json.loads(plan.receipt_json)


class RetainedIntervalPreviewTests(unittest.TestCase):
    def setUp(self):
        self.receipts = [source_receipt(0), source_receipt(1, equity="1150", flow="100"),
                         source_receipt(2, equity="1265")]

    def prepare(self, receipts=None, **changes):
        args = {
            "ledger_path": LEDGER, "account_scope_sha256": SCOPE,
            "cutover_start_at": TIMES[0], "cutover_end_at": TIMES[3],
            "strategy_profile": "crypto_synthetic", "lifecycle_stream_id": "synthetic_receipts",
            "max_receipts": 10, "max_bytes": 100_000,
        }
        args.update(changes)
        return prepare_retained_interval_preview(self.receipts if receipts is None else receipts, **args)

    def test_out_of_order_receipts_prepare_ordered_existing_envelope(self):
        preview = self.prepare(list(reversed(self.receipts)))
        records = preview.live_run_records()
        self.assertEqual([r["recorded_at"] for r in records],
                         ["2026-10-02T00:00:00+00:00", "2026-10-03T00:00:00+00:00",
                          "2026-10-04T00:00:00+00:00"])
        for record, (_, receipt) in zip(records, self.receipts):
            self.assertEqual(record["execution_result"], {"external_cash_flow_interval": receipt["interval"]})
            self.assertEqual(record["strategy_profile"], "crypto_synthetic")
            self.assertEqual(record["domain"], "crypto")
            self.assertEqual(record["record_kind"], "execution")
            self.assertEqual(record["lifecycle_stream_id"], "synthetic_receipts")

    def test_native_archive_and_ack_are_explicitly_blocked(self):
        preview = self.prepare()
        self.assertEqual(preview.delivery_status, "blocked_archive_enumeration_and_durable_ack_unqualified")
        self.assertFalse(preview.delivery_acknowledged)

    def test_exact_duplicates_are_idempotent_without_counting_flow_twice(self):
        preview = self.prepare([*self.receipts, copy.deepcopy(self.receipts[1])])
        self.assertEqual(len(preview.live_run_records()), 3)
        self.assertEqual(preview.duplicate_count, 1)
        self.assertEqual(preview.input_count, 4)
        self.assertEqual(sum(float(r["execution_result"]["external_cash_flow_interval"]["net_external_cash_flow"])
                             for r in preview.live_run_records()), 100)

    def test_dictionary_key_order_is_not_a_receipt_conflict(self):
        duplicate = copy.deepcopy(self.receipts[1])
        duplicate[1]["interval"] = dict(reversed(list(duplicate[1]["interval"].items())))
        duplicate = (duplicate[0], dict(reversed(list(duplicate[1].items()))))
        self.assertEqual(self.prepare([*self.receipts, duplicate]).duplicate_count, 1)

    def test_reader_retry_is_identical_and_cannot_be_an_ack(self):
        first, retry = self.prepare(), self.prepare(copy.deepcopy(self.receipts))
        self.assertEqual(first, retry)
        self.assertFalse(retry.delivery_acknowledged)

    def test_material_is_detached_and_repr_does_not_expose_financial_payload(self):
        preview = self.prepare()
        original = preview.live_run_records()
        self.receipts[0][1]["interval"]["end_equity_usdt"] = "987654321"
        changed = preview.live_run_records()
        changed[0]["execution_result"].clear()
        self.assertEqual(preview.live_run_records(), original)
        self.assertNotIn("end_equity", repr(preview))
        self.assertNotIn(LEDGER, repr(preview))
        serialized = json.dumps(original)
        self.assertNotIn("source", serialized)
        self.assertNotIn("not_in_preview", serialized)
        self.assertNotIn(LEDGER, serialized)

    def test_missing_middle_interval_is_blocked(self):
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_coverage_gap"):
            self.prepare([self.receipts[0], self.receipts[2]])

    def test_missing_leading_interval_is_blocked(self):
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_coverage_gap"):
            self.prepare(self.receipts[1:])

    def test_missing_trailing_interval_is_blocked(self):
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_coverage_gap"):
            self.prepare(self.receipts[:-1])

    def test_empty_batch_is_not_coverage(self):
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_coverage_gap"):
            self.prepare([])

    def test_interval_outside_explicit_cutover_is_not_silently_filtered(self):
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_coverage_outside_cutover"):
            self.prepare(cutover_end_at=TIMES[2])

    def test_overlapping_intervals_are_blocked(self):
        overlap = source_receipt(1, start="2026-10-01T12:00:00Z", end="2026-10-02T12:00:00Z")
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_coverage_overlap"):
            self.prepare([self.receipts[0], overlap, self.receipts[2]])

    def test_two_different_starts_at_same_end_are_blocked(self):
        alternative = source_receipt(0, start="2026-10-01T12:00:00Z")
        with self.assertRaises(IntervalReceiptConflict):
            self.prepare([*self.receipts, alternative])

    def test_same_logical_interval_changed_amount_is_a_conflict(self):
        changed = source_receipt(1, equity="1151", flow="100")
        with self.assertRaises(IntervalReceiptConflict):
            self.prepare([*self.receipts, changed])

    def test_same_logical_interval_changed_source_metadata_is_a_conflict(self):
        changed = copy.deepcopy(self.receipts[1])
        changed[1]["source"]["ledger_after_sha256"] = "b" * 64
        with self.assertRaises(IntervalReceiptConflict):
            self.prepare([*self.receipts, changed])

    def test_timezone_equivalence_dedupes_identity_but_changed_raw_payload_conflicts(self):
        equivalent = source_receipt(1, start="2026-10-01T20:00:00-04:00", end="2026-10-02T20:00:00-04:00")
        self.assertEqual(equivalent[0], self.receipts[1][0])
        with self.assertRaises(IntervalReceiptConflict):
            self.prepare([*self.receipts, equivalent])

    def test_equivalent_cutover_timestamps_work_without_rewriting_payload(self):
        preview = self.prepare(cutover_start_at="2026-09-30T20:00:00-04:00",
                               cutover_end_at="2026-10-03T20:00:00-04:00")
        self.assertEqual(preview.live_run_records()[0]["execution_result"]["external_cash_flow_interval"],
                         self.receipts[0][1]["interval"])

    def test_cross_scope_is_blocked(self):
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_binding_mismatch"):
            self.prepare([self.receipts[0], source_receipt(1, scope="b" * 64), self.receipts[2]])

    def test_cross_ledger_is_blocked(self):
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_binding_mismatch"):
            self.prepare([self.receipts[0], source_receipt(1, ledger="strategy/OTHER"), self.receipts[2]])

    def test_source_path_mixing_and_reused_versions_are_blocked(self):
        wrong = copy.deepcopy(self.receipts)
        wrong[1][1]["source"]["ledger_path"] = "strategy/OTHER"
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_binding_mismatch"):
            self.prepare(wrong)
        wrong = copy.deepcopy(self.receipts)
        wrong[1][1]["source"]["ledger_before_version"] = wrong[0][1]["source"]["ledger_before_version"]
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_source_order_invalid"):
            self.prepare(wrong)

    def test_source_versions_must_progress_with_intervals(self):
        wrong = copy.deepcopy(self.receipts)
        wrong[2][1]["source"]["ledger_before_version"] = "2026-09-30T00:00:00.000000001Z"
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_source_order_invalid"):
            self.prepare(wrong)

    def test_receipt_identity_and_payload_digest_tampering_are_blocked(self):
        for key in ("interval_id_sha256", "payload_sha256"):
            wrong = copy.deepcopy(self.receipts)
            wrong[1][1][key] = "b" * 64
            with self.subTest(key=key), self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_receipt_invalid"):
                self.prepare(wrong)

    def test_receipt_path_tampering_is_blocked(self):
        wrong = copy.deepcopy(self.receipts)
        wrong[1] = ("strategy/private_credential_token", wrong[1][1])
        with self.assertRaisesRegex(IntervalReceiptBlocked, "^interval_reader_binding_mismatch$"):
            self.prepare(wrong)

    def test_unknown_receipt_source_or_payload_keys_are_blocked_without_echo(self):
        for area in (None, "source", "interval"):
            wrong = copy.deepcopy(self.receipts)
            target = wrong[1][1] if area is None else wrong[1][1][area]
            target["private_token"] = "secret-do-not-log"
            with self.subTest(area=area), self.assertRaises(IntervalReceiptBlocked) as caught:
                self.prepare(wrong)
            self.assertNotIn("secret", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_source_version_date_and_precision_are_validated(self):
        for version in ("2026-02-30T00:00:00.000000001Z", "2026-10-02T00:00:00Z",
                        "2026-10-02T00:00:00.0000000001Z", "private-token"):
            wrong = copy.deepcopy(self.receipts)
            wrong[1][1]["source"]["ledger_before_version"] = version
            with self.subTest(version=version), self.assertRaises(IntervalReceiptBlocked):
                self.prepare(wrong)

    def test_nanosecond_source_versions_are_not_collapsed(self):
        receipts = copy.deepcopy(self.receipts)
        for index, (_, receipt) in enumerate(receipts):
            receipt["source"]["ledger_before_version"] = f"2026-10-01T00:00:00.00000000{index + 1}Z"
        self.assertEqual(len(self.prepare(receipts).live_run_records()), 3)

    def test_count_limit_counts_duplicates(self):
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_count_limit"):
            self.prepare([*self.receipts, self.receipts[0]], max_receipts=3)

    def test_bytes_limit_is_exact_and_counts_paths_and_duplicates(self):
        preview = self.prepare()
        self.assertEqual(self.prepare(max_bytes=preview.encoded_bytes), preview)
        with self.assertRaisesRegex(IntervalReceiptBlocked, "interval_reader_bytes_limit"):
            self.prepare(max_bytes=preview.encoded_bytes - 1)
        duplicate = self.prepare([*self.receipts, self.receipts[0]])
        self.assertGreater(duplicate.encoded_bytes, preview.encoded_bytes)

    def test_explicit_resource_limits_and_safe_labels_are_required(self):
        for changes in ({"max_receipts": 1001}, {"max_receipts": True}, {"max_receipts": 0},
                        {"max_bytes": 1_048_577}, {"max_bytes": True}, {"max_bytes": 0},
                        {"strategy_profile": "../private"}, {"lifecycle_stream_id": ""},
                        {"account_scope_sha256": "private-token"}, {"ledger_path": "../private"},
                        {"cutover_start_at": TIMES[3]}, {"cutover_end_at": "2026-10-04"}):
            with self.subTest(changes=changes), self.assertRaises(IntervalReceiptBlocked):
                self.prepare(**changes)

    def test_non_sequence_and_malformed_entries_are_blocked(self):
        for receipts in (iter(self.receipts), "secret", [None], [("path",)], [("path", {})]):
            with self.subTest(kind=type(receipts).__name__), self.assertRaises(IntervalReceiptBlocked):
                self.prepare(receipts)

    def test_producer_numeric_and_timestamp_bounds_are_not_weakened(self):
        for value in ("NaN", "Infinity", "1e31", "1e-31", "0", "-1", "9" * 81):
            wrong = copy.deepcopy(self.receipts)
            wrong[1][1]["interval"]["end_equity_usdt"] = value
            with self.subTest(value=value), self.assertRaises(IntervalReceiptBlocked):
                self.prepare(wrong)
        wrong = copy.deepcopy(self.receipts)
        wrong[1][1]["interval"]["end_at"] = "2026-10-03T00:00:00.0000001Z"
        with self.assertRaises(IntervalReceiptBlocked):
            self.prepare(wrong)

    def test_nested_or_oversized_payload_is_rejected_before_producer_deepcopy(self):
        class PrivateBomb:
            def __deepcopy__(self, memo):
                raise AssertionError("private-data-must-not-be-copied")

        for value in (PrivateBomb(), {"private": "secret"}, "9" * 10_000):
            wrong = copy.deepcopy(self.receipts)
            wrong[1][1]["interval"]["end_equity_usdt"] = value
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(
                    IntervalReceiptBlocked, "^interval_reader_receipt_invalid$"):
                self.prepare(wrong)

    def test_prepare_has_no_network_or_client_construction(self):
        with patch.object(socket, "create_connection", side_effect=AssertionError("network_forbidden")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("network_forbidden")):
            self.assertEqual(len(self.prepare().live_run_records()), 3)

    def test_actual_pinned_calculator_reads_existing_envelope_with_end_flow_convention(self):
        from quant_platform_kit.strategy_lifecycle.live_equity import live_run_records_to_return_series_result
        records = self.prepare().live_run_records()
        result = live_run_records_to_return_series_result(records, domain="crypto")
        self.assertEqual((result.status, result.detail), ("ok", "interval"))
        self.assertEqual(len(result.series), 2)
        self.assertAlmostEqual(result.series.iloc[0], 0.05)
        self.assertAlmostEqual(result.series.iloc[1], 0.10)
        duplicate_preview = self.prepare([*reversed(self.receipts), self.receipts[1]])
        duplicate_result = live_run_records_to_return_series_result(duplicate_preview.live_run_records(), domain="crypto")
        self.assertTrue(result.series.equals(duplicate_result.series))

    def test_actual_store_local_before_cloud_failure_does_not_qualify_delivery_or_retry(self):
        from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore

        class FailCloudStore(PerformanceStore):
            def _write_cloud_json(self, key, payload):
                raise RuntimeError("synthetic_private_endpoint_failure")

        preview = self.prepare()
        record = preview.live_run_records()[0]
        with tempfile.TemporaryDirectory() as directory:
            store = FailCloudStore(local_root=Path(directory), cloud_bucket="synthetic_no_backend")
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "synthetic_private_endpoint_failure"):
                    store.save_live_run_record("crypto_synthetic", "crypto", record,
                                               stream_id="synthetic_receipts")
                # Inspect partial local evidence directly; never call the store's
                # optional cloud list/read API or use a local read as a cloud ACK.
                local_paths = list(Path(directory).rglob("*.json"))
                self.assertEqual(len(local_paths), 1)
                self.assertEqual(json.loads(local_paths[0].read_text())["execution_result"],
                                 record["execution_result"])
            self.assertFalse(preview.delivery_acknowledged)


if __name__ == "__main__":
    unittest.main()
