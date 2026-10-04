"""LOCAL CANDIDATE only: atomic source retention against an in-memory store."""

import copy
import json
import socket
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from application.interval_source_receipt_candidate import (
    IntervalReceiptBlocked,
    IntervalReceiptConflict,
    IntervalReceiptUncertain,
    build_interval_receipt_plan,
    persist_interval_source_receipt,
)


START = "2026-10-03T23:59:00+00:00"
END = "2026-10-04T00:00:00+00:00"
VERSION = datetime(2026, 10, 3, 23, 59, 1, tzinfo=timezone.utc)
LEDGER = "strategy/MULTI_ASSET_STATE"
OWNER = LEDGER + "__owner"


def material(start=START, end=END, quantity="1100", flow="100"):
    previous = {
        "earn_accrual_checkpoint": {
            "account_scope_sha256": "a" * 64,
            "observed_at": start,
            "assets": {"USDT": {"quantity": "1000"}},
            "execution_authority_granted": False,
        },
        "external_cash_flow_cursor": {"observed_at": start, "records": []},
        "earn_accounted_net_changes": {"USDT": "0"},
        "last_balance_snapshot": {"USDT": 1000.0},
        "last_reset_date": "2026-10-04",
        "daily_external_principal_usdt": 0.0,
        "order_submission": {"state": "TERMINAL", "fill_accounted": True},
        "daily_trend_third_fee_usdt": 0.5,
        "unknown_future_field": {"keep": True},
    }
    following = copy.deepcopy(previous)
    following["earn_accrual_checkpoint"]["observed_at"] = end
    following["earn_accrual_checkpoint"]["assets"]["USDT"]["quantity"] = quantity
    following["external_cash_flow_cursor"] = {
        "observed_at": end, "records": [{"synthetic_deposit_id": "fixed"}]
    }
    following["last_balance_snapshot"] = {"USDT": float(quantity)}
    following["daily_external_principal_usdt"] = float(flow)
    interval = {
        "account_scope_sha256": "a" * 64,
        "start_at": start,
        "end_at": end,
        "end_equity_usdt": quantity,
        "net_external_cash_flow": flow,
        "currency": "USDT",
        "valuation_basis": "checkpoint_quantities_sampled_prices",
    }
    return previous, following, interval


class Snapshot:
    def __init__(self, value, version):
        self.exists = value is not None
        self.update_time = version
        self.value = copy.deepcopy(value)

    def to_dict(self):
        return copy.deepcopy(self.value)


class NanosecondVersion(datetime):
    """Matches the SDK's separately stored nanos and datetime comparison trap."""

    def __new__(cls, *args, nanosecond=0, **kwargs):
        if nanosecond:
            kwargs["microsecond"] = nanosecond // 1000
        result = super().__new__(cls, *args, **kwargs)
        result.nanosecond = nanosecond
        return result


class Collection:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, name):
        return Ref(self.store, self.path + "/" + name)


class Ref:
    def __init__(self, store, path):
        self.store, self.path = store, path
        self._client = store
        self.id = path.rsplit("/", 1)[1]
        self.parent = Collection(store, path.rsplit("/", 1)[0])

    @property
    def _document_path(self):
        return self._client._database_string + "/documents/" + self.path

    def get(self, *, transaction, retry):
        self.store.read_calls += 1
        assert retry is None
        assert transaction.store is self.store
        if transaction.operations:
            raise AssertionError("reads must precede writes")
        transaction.reads.add(self.path)
        return Snapshot(transaction.values.get(self.path), transaction.versions.get(self.path))


class Transaction:
    def __init__(self, store):
        self.store = store
        self._client = store
        self._max_attempts = 1
        self.values = copy.deepcopy(store.values)
        # Native timestamp objects are treated as immutable version tokens.
        # datetime-subclass deepcopy can discard separately stored nanoseconds.
        self.versions = store.versions.copy()
        self.reads = set()
        self.operations = []

    def create(self, ref, value):
        self.operations.append(("create", ref.path, copy.deepcopy(value)))

    def update(self, ref, patch):
        self.operations.append(("update", ref.path, copy.deepcopy(patch)))


class AtomicStore:
    """Models all-or-nothing commit, read versions, and ambiguous transport."""

    def __init__(self, previous):
        self._database_string = "projects/synthetic-project/databases/synthetic-database"
        self.read_calls = 0
        self.values = {LEDGER: copy.deepcopy(previous), OWNER: {"owner_id": "owner-one"}}
        self.versions = {LEDGER: VERSION, OWNER: VERSION}
        self.transactions = []
        self.failure = None
        self.before_commit = None
        self.after_commit = None
        self.clock = VERSION

    def run(self, callback):
        transaction = Transaction(self)
        result = callback(transaction)
        self.transactions.append(transaction)
        if self.before_commit:
            hook, self.before_commit = self.before_commit, None
            hook(self)
        def version_identity(value):
            if value is None:
                return None
            return (value.year, value.month, value.day, value.hour, value.minute, value.second,
                    getattr(value, "nanosecond", 0) or value.microsecond * 1000)
        if any(version_identity(self.versions.get(path)) != version_identity(transaction.versions.get(path))
               for path in transaction.reads):
            raise RuntimeError("synthetic optimistic conflict")
        if transaction.operations and self.failure == "before_commit":
            self.failure = None
            raise RuntimeError("synthetic write transport failure")
        candidate = copy.deepcopy(self.values)
        for operation, path, value in transaction.operations:
            if operation == "create":
                if path in candidate:
                    raise RuntimeError("synthetic already exists")
                candidate[path] = value
            else:
                if path not in candidate:
                    raise RuntimeError("synthetic missing ledger")
                if self.failure == "during_update":
                    self.failure = None
                    raise RuntimeError("synthetic second write rejection")
                candidate[path].update(value)
        if transaction.operations:
            self.clock += timedelta(microseconds=1)
            self.values = candidate
            for _, path, _ in transaction.operations:
                self.versions[path] = self.clock
            if self.after_commit:
                hook, self.after_commit = self.after_commit, None
                hook(self)
            if self.failure == "after_commit":
                self.failure = None
                raise RuntimeError("synthetic lost commit response")
        elif self.failure == "readback":
            self.failure = None
            raise RuntimeError("synthetic readback failure")
        return result

    def ref(self, path):
        return Ref(self, path)

    def external_change(self, path, value):
        self.clock += timedelta(microseconds=1)
        self.values[path] = copy.deepcopy(value)
        self.versions[path] = self.clock


class IntervalSourceReceiptTests(unittest.TestCase):
    def setUp(self):
        self.previous, self.following, self.interval = material()
        self.store = AtomicStore(self.previous)
        self.plan = self.build()

    def build(self, **changes):
        args = dict(
            ledger_path=LEDGER,
            source_ledger_update_time=VERSION,
            previous_state=self.previous,
            next_state=self.following,
            interval=self.interval,
        )
        args.update(changes)
        return build_interval_receipt_plan(**args)

    def persist(self, *, plan=None, owner="owner-one", version=None):
        return persist_interval_source_receipt(
            bound_client=self.store,
            transaction_runner=self.store.run,
            ledger_ref=self.store.ref(LEDGER),
            owner_ref=self.store.ref(OWNER),
            owner_id=owner,
            expected_ledger_update_time=version or self.store.versions[LEDGER],
            plan=plan or self.plan,
        )

    def receipt(self):
        return self.store.values[self.plan.receipt_path]

    def test_receipt_checkpoint_and_cursor_commit_in_one_transaction(self):
        result = self.persist()
        operations = self.store.transactions[0].operations
        self.assertEqual([op[0] for op in operations], ["create", "update"])
        self.assertEqual(operations[0][1], self.plan.receipt_path)
        self.assertEqual(self.store.values[LEDGER], self.following)
        self.assertEqual(self.receipt()["interval"], self.interval)
        self.assertEqual(len(self.receipt()["interval"]), 7)
        self.assertEqual(result["status"], "source_receipt_readback_verified")
        self.assertIs(result["performance_delivery_acknowledged"], False)

    def test_write_failure_rolls_back_both_records(self):
        for failure in ("before_commit", "during_update"):
            with self.subTest(failure=failure):
                self.store.failure = failure
                with self.assertRaises(IntervalReceiptUncertain):
                    self.persist()
                self.assertEqual(self.store.values[LEDGER], self.previous)
                self.assertNotIn(self.plan.receipt_path, self.store.values)

    def test_exact_duplicate_retry_never_reaccounts_principal_or_fee(self):
        self.persist()
        ledger_version = self.store.versions[LEDGER]
        original_receipt = copy.deepcopy(self.receipt())
        result = self.persist()
        self.assertEqual(result["write_mode"], "identical")
        self.assertEqual(self.store.transactions[-2].operations, [])
        self.assertEqual(self.store.versions[LEDGER], ledger_version)
        self.assertEqual(self.store.values[LEDGER]["daily_external_principal_usdt"], 100.0)
        self.assertEqual(self.store.values[LEDGER]["daily_trend_third_fee_usdt"], 0.5)
        self.assertEqual(self.receipt(), original_receipt)

    def test_lost_commit_response_is_uncertain_then_fresh_version_retry_is_identical(self):
        self.store.failure = "after_commit"
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist()
        self.assertEqual(self.store.values[LEDGER], self.following)
        self.assertEqual(self.receipt()["interval"], self.interval)
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist(version=VERSION)
        self.assertEqual(self.persist()["write_mode"], "identical")

    def test_failed_readback_is_uncertain_and_source_is_retained(self):
        self.store.failure = "readback"
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist()
        self.assertEqual(self.receipt()["interval"], self.interval)
        self.assertEqual(self.persist()["write_mode"], "identical")

    def test_conflicting_payload_has_same_identity_and_cannot_overwrite(self):
        self.persist()
        changed_interval = {**self.interval, "end_equity_usdt": "1101"}
        conflict = self.build(interval=changed_interval)
        self.assertEqual(conflict.receipt_path, self.plan.receipt_path)
        original = copy.deepcopy(self.receipt())
        with self.assertRaises(IntervalReceiptConflict):
            self.persist(plan=conflict)
        self.assertEqual(self.receipt(), original)
        self.assertEqual(self.store.values[LEDGER], self.following)

    def test_readback_payload_conflict_never_returns_confirmation(self):
        def corrupt(store):
            receipt = copy.deepcopy(store.values[self.plan.receipt_path])
            receipt["interval"]["end_equity_usdt"] = "9999"
            store.external_change(self.plan.receipt_path, receipt)
        self.store.after_commit = corrupt
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist()
        with self.assertRaises(IntervalReceiptConflict):
            self.persist()
        self.assertEqual(self.receipt()["interval"]["end_equity_usdt"], "9999")

    def test_readback_ledger_conflict_never_claims_retention_pair_confirmed(self):
        self.store.after_commit = lambda store: store.external_change(
            LEDGER, {**self.following, "daily_external_principal_usdt": 999.0}
        )
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist()
        self.assertEqual(self.receipt()["interval"], self.interval)
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist()

    def test_owner_missing_or_wrong_blocks_before_writes(self):
        for owner_value in (None, {"owner_id": "other-owner"}):
            with self.subTest(owner=owner_value):
                self.store.values[OWNER] = owner_value
                with self.assertRaises(IntervalReceiptBlocked):
                    self.persist()
                self.assertEqual(self.store.values[LEDGER], self.previous)
                self.assertNotIn(self.plan.receipt_path, self.store.values)

    def test_owner_changes_before_commit_atomically_abort(self):
        self.store.before_commit = lambda store: store.external_change(OWNER, {"owner_id": "other-owner"})
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist()
        self.assertEqual(self.store.values[LEDGER], self.previous)
        self.assertNotIn(self.plan.receipt_path, self.store.values)

    def test_owner_changes_after_commit_readback_is_uncertain(self):
        self.store.after_commit = lambda store: store.external_change(OWNER, {"owner_id": "other-owner"})
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist()
        self.assertEqual(self.receipt()["interval"], self.interval)

    def test_stale_version_or_same_version_changed_digest_blocks(self):
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist(version=VERSION - timedelta(seconds=1))
        self.store.values[LEDGER]["unknown_future_field"] = {"keep": False}
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist()
        self.assertNotIn(self.plan.receipt_path, self.store.values)

    def test_version_cas_preserves_submicrosecond_firestore_precision(self):
        before = NanosecondVersion(2026, 10, 3, 23, 59, 1, nanosecond=123456701, tzinfo=timezone.utc)
        changed = NanosecondVersion(2026, 10, 3, 23, 59, 1, nanosecond=123456702, tzinfo=timezone.utc)
        self.assertEqual(before, changed)  # Ordinary datetime equality drops the tail.
        self.assertEqual(before.isoformat(), changed.isoformat())
        self.store.versions[LEDGER] = before
        plan = self.build(source_ledger_update_time=before)
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist(plan=plan, version=changed)
        self.assertNotIn(plan.receipt_path, self.store.values)
        self.store.clock = before
        self.assertEqual(self.persist(plan=plan, version=before)["write_mode"], "created")
        self.assertTrue(self.receipt()["source"]["ledger_before_version"].endswith(".123456701Z"))

    def test_submicrosecond_version_change_before_commit_atomically_aborts(self):
        before = NanosecondVersion(2026, 10, 3, 23, 59, 1, nanosecond=123456701, tzinfo=timezone.utc)
        changed = NanosecondVersion(2026, 10, 3, 23, 59, 1, nanosecond=123456702, tzinfo=timezone.utc)
        self.store.versions[LEDGER] = before
        self.store.before_commit = lambda store: store.versions.update({LEDGER: changed})
        plan = self.build(source_ledger_update_time=before)
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist(plan=plan, version=before)
        self.assertEqual(self.store.values[LEDGER], self.previous)
        self.assertNotIn(plan.receipt_path, self.store.values)

    def test_source_version_and_current_version_must_both_match_on_create(self):
        advanced = VERSION + timedelta(seconds=1)
        self.store.versions[LEDGER] = advanced
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist(version=advanced)  # Current matches, original source version does not.
        self.assertEqual(self.store.values[LEDGER], self.previous)
        self.assertNotIn(self.plan.receipt_path, self.store.values)

    def test_concurrent_receipt_creation_aborts_without_overwrite(self):
        self.store.before_commit = lambda store: store.external_change(
            self.plan.receipt_path, {"synthetic_conflicting_receipt": True}
        )
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist()
        self.assertEqual(self.store.values[LEDGER], self.previous)
        self.assertEqual(self.receipt(), {"synthetic_conflicting_receipt": True})
        with self.assertRaises(IntervalReceiptConflict):
            self.persist()

    def test_ledger_changes_before_commit_atomically_abort(self):
        self.store.before_commit = lambda store: store.external_change(LEDGER, {**self.previous, "changed": True})
        with self.assertRaises(IntervalReceiptUncertain):
            self.persist()
        self.assertNotIn(self.plan.receipt_path, self.store.values)
        self.assertEqual(self.store.values[LEDGER]["earn_accrual_checkpoint"], self.previous["earn_accrual_checkpoint"])

    def test_later_cycle_and_performance_delivery_failures_do_not_remove_source(self):
        self.persist()
        for error in ("synthetic later cycle failure", "synthetic PerformanceStore unavailable"):
            try:
                raise RuntimeError(error)
            except RuntimeError:
                pass
            self.assertEqual(self.receipt()["interval"], self.interval)
        self.assertEqual(self.store.values[LEDGER], self.following)

    def test_normalizer_reload_has_no_queue_or_receipt_field_to_drop(self):
        from trade_state_support import normalize_trade_state
        self.persist()
        retained = copy.deepcopy(self.receipt())
        reloaded = normalize_trade_state(
            self.store.values[LEDGER], trend_universe=(), last_good_payload_key="last_good",
            action_history_key="actions", retired_positions_key="retired",
        )
        self.assertEqual(reloaded["earn_accrual_checkpoint"], self.following["earn_accrual_checkpoint"])
        self.assertEqual(reloaded["external_cash_flow_cursor"], self.following["external_cash_flow_cursor"])
        self.assertFalse(any("receipt" in key or "outbox" in key for key in reloaded))
        self.assertEqual(self.receipt(), retained)

    def test_module_execution_has_no_socket_or_performance_store_dependency(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network is prohibited")):
            self.persist()
            self.persist()
        self.assertEqual(self.receipt()["interval"], self.interval)

    def test_next_interval_retains_previous_receipt_and_legacy_state_is_not_fabricated(self):
        self.assertFalse(any("__interval_receipt_" in path for path in self.store.values))
        self.persist()
        first = copy.deepcopy(self.receipt())
        next_state = copy.deepcopy(self.following)
        next_state["earn_accrual_checkpoint"]["observed_at"] = "2026-10-04T00:01:00+00:00"
        next_state["external_cash_flow_cursor"]["observed_at"] = "2026-10-04T00:01:00+00:00"
        next_interval = {
            **self.interval,
            "start_at": END,
            "end_at": "2026-10-04T00:01:00+00:00",
            "net_external_cash_flow": "0",
        }
        plan = self.build(
            previous_state=self.following, next_state=next_state,
            interval=next_interval, source_ledger_update_time=self.store.versions[LEDGER],
        )
        self.persist(plan=plan)
        self.assertEqual(self.receipt(), first)
        self.assertIn(plan.receipt_path, self.store.values)
        self.assertEqual(len([path for path in self.store.values if "__interval_receipt_" in path]), 2)
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist()  # Old poststate is no longer current; no rewrite or guessed ancestry.
        self.assertEqual(self.receipt(), first)

    def test_plan_is_detached_from_input_mutation_and_preserves_unrelated_fields(self):
        self.interval["end_equity_usdt"] = "9999"
        self.following["unknown_future_field"]["keep"] = False
        self.persist()
        self.assertEqual(self.receipt()["interval"]["end_equity_usdt"], "1100")
        self.assertEqual(self.store.values[LEDGER]["unknown_future_field"], {"keep": True})
        self.assertEqual(self.store.values[LEDGER]["order_submission"], self.previous["order_submission"])

    def test_logical_identity_uses_normalized_times_but_exact_payload_hash_differs(self):
        equivalent = {**self.interval, "start_at": START.replace("+00:00", "Z")}
        plan = self.build(interval=equivalent)
        self.assertEqual(plan.receipt_path, self.plan.receipt_path)
        self.assertNotEqual(plan.payload_sha256, self.plan.payload_sha256)

    def test_bad_interval_scope_times_numbers_or_metadata_fail_closed(self):
        invalid = [
            {**self.interval, "extra": True},
            {**self.interval, "account_scope_sha256": "b" * 64},
            {**self.interval, "end_at": START},
            {**self.interval, "start_at": "2026-10-03T23:59:00"},
            {**self.interval, "start_at": "2026-10-03T23:59:00.1234567Z"},
            {**self.interval, "end_equity_usdt": "NaN"},
            {**self.interval, "end_equity_usdt": "1e1000000"},
            {**self.interval, "end_equity_usdt": "0"},
            {**self.interval, "net_external_cash_flow": True},
            {**self.interval, "currency": "BTC"},
            {**self.interval, "valuation_basis": "guessed"},
        ]
        for interval in invalid:
            with self.subTest(interval=interval):
                with self.assertRaises(IntervalReceiptBlocked):
                    self.build(interval=interval)
        self.assertEqual(self.store.values[LEDGER], self.previous)

    def test_checkpoint_cursor_scope_unsettled_and_unrelated_state_changes_fail_closed(self):
        for mutate in (
            lambda state: state["external_cash_flow_cursor"].update(observed_at=START),
            lambda state: state["earn_accrual_checkpoint"].update(account_scope_sha256="b" * 64),
            lambda state: state.update(daily_trend_third_fee_usdt=2),
            lambda state: state.pop("unknown_future_field"),
            lambda state: state["earn_accounted_net_changes"].update(USDT="1"),
        ):
            next_state = copy.deepcopy(self.following)
            mutate(next_state)
            with self.assertRaises(IntervalReceiptBlocked):
                self.build(next_state=next_state)
        for status in ("SUBMISSION_UNKNOWN", "FILLED_ACCOUNTING_PENDING"):
            previous = copy.deepcopy(self.previous)
            following = copy.deepcopy(self.following)
            previous["order_submission"]["state"] = status
            following["order_submission"]["state"] = status
            with self.assertRaises(IntervalReceiptBlocked):
                self.build(previous_state=previous, next_state=following)

    def test_wrong_owner_reference_or_ledger_path_never_runs_transaction(self):
        with self.assertRaises(IntervalReceiptBlocked):
            persist_interval_source_receipt(
                bound_client=self.store,
                transaction_runner=self.store.run,
                ledger_ref=self.store.ref(LEDGER), owner_ref=self.store.ref(LEDGER + "__wrong"),
                owner_id="owner-one", expected_ledger_update_time=VERSION, plan=self.plan,
            )
        self.assertEqual(self.store.transactions, [])

    def test_broken_runner_cannot_skip_readback_and_claim_confirmation(self):
        calls = []
        def broken(callback):
            calls.append(callback)
            return self.store.run(callback) if len(calls) == 1 else None
        with self.assertRaises(IntervalReceiptUncertain):
            persist_interval_source_receipt(
                bound_client=self.store,
                transaction_runner=broken, ledger_ref=self.store.ref(LEDGER),
                owner_ref=self.store.ref(OWNER), owner_id="owner-one",
                expected_ledger_update_time=VERSION, plan=self.plan,
            )
        self.assertEqual(self.receipt()["interval"], self.interval)

    def test_replaced_plan_cannot_write_unrelated_or_inconsistent_material(self):
        injected = json.loads(self.plan.patch_json)
        injected["unapproved_authority_field"] = True
        altered_allowed = json.loads(self.plan.patch_json)
        altered_allowed["daily_external_principal_usdt"] = 999.0
        receipt = json.loads(self.plan.receipt_json)
        receipt["interval"]["end_equity_usdt"] = "9999"
        def canonical(value):
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        for forged in (
            replace(self.plan, patch_json=canonical(injected)),
            replace(self.plan, patch_json=canonical(altered_allowed)),
            replace(self.plan, receipt_json=canonical(receipt)),
            replace(self.plan, ledger_before_sha256="b" * 64),
            replace(self.plan, ledger_after_sha256="b" * 64),
            replace(self.plan, payload_sha256="b" * 64),
            replace(self.plan, receipt_path=LEDGER + "__wrong_receipt"),
        ):
            with self.subTest(forged=forged):
                self.store = AtomicStore(self.previous)
                failure = None
                try:
                    self.persist(plan=forged)
                except (IntervalReceiptBlocked, IntervalReceiptUncertain) as exc:
                    failure = exc
                self.assertEqual(self.store.values[LEDGER], self.previous)
                self.assertEqual(set(self.store.values), {LEDGER, OWNER})
                self.assertIsInstance(failure, IntervalReceiptBlocked)
                self.assertEqual(self.store.transactions, [])

    def test_same_relative_paths_on_mixed_clients_fail_before_any_read(self):
        other = AtomicStore(self.previous)
        other._database_string = "projects/other-project/databases/other-database"
        for target in ("ledger", "owner", "receipt", "transaction"):
            with self.subTest(target=target):
                self.store.read_calls = other.read_calls = 0
                ledger_ref, owner_ref = self.store.ref(LEDGER), self.store.ref(OWNER)
                runner = self.store.run
                if target == "ledger":
                    ledger_ref = other.ref(LEDGER)
                elif target == "owner":
                    owner_ref = other.ref(OWNER)
                elif target == "receipt":
                    ledger_ref.parent = Collection(other, "strategy")
                else:
                    runner = other.run
                with self.assertRaises(IntervalReceiptBlocked):
                    persist_interval_source_receipt(
                        bound_client=self.store, transaction_runner=runner,
                        ledger_ref=ledger_ref, owner_ref=owner_ref, owner_id="owner-one",
                        expected_ledger_update_time=VERSION, plan=self.plan,
                    )
                self.assertEqual(self.store.read_calls + other.read_calls, 0)
                self.assertEqual(self.store.values[LEDGER], self.previous)
                self.assertNotIn(self.plan.receipt_path, self.store.values)
                self.assertNotIn(self.plan.receipt_path, other.values)

    def test_equivalent_database_separate_client_and_wrong_full_path_are_rejected(self):
        other = AtomicStore(self.previous)  # Same database string, still not bound object.
        with self.assertRaises(IntervalReceiptBlocked):
            persist_interval_source_receipt(
                bound_client=self.store, transaction_runner=self.store.run,
                ledger_ref=self.store.ref(LEDGER), owner_ref=other.ref(OWNER), owner_id="owner-one",
                expected_ledger_update_time=VERSION, plan=self.plan,
            )
        class WrongFullPath(Ref):
            @property
            def _document_path(self):
                return "projects/other-project/databases/other-database/documents/" + self.path
        with self.assertRaises(IntervalReceiptBlocked):
            persist_interval_source_receipt(
                bound_client=self.store, transaction_runner=self.store.run,
                ledger_ref=WrongFullPath(self.store, LEDGER), owner_ref=self.store.ref(OWNER),
                owner_id="owner-one", expected_ledger_update_time=VERSION, plan=self.plan,
            )
        self.assertEqual(self.store.read_calls + other.read_calls, 0)

    def test_database_changes_before_callback_are_rejected_before_reads(self):
        def wrong_database(callback):
            self.store._database_string = "projects/synthetic-project/databases/switched-database"
            return self.store.run(callback)
        with self.assertRaises(IntervalReceiptBlocked):
            persist_interval_source_receipt(
                bound_client=self.store, transaction_runner=wrong_database,
                ledger_ref=self.store.ref(LEDGER), owner_ref=self.store.ref(OWNER), owner_id="owner-one",
                expected_ledger_update_time=VERSION, plan=self.plan,
            )
        self.assertEqual(self.store.read_calls, 0)
        self.assertNotIn(self.plan.receipt_path, self.store.values)

    def test_readback_mixed_backend_is_uncertain_before_read_and_source_is_retained(self):
        other = AtomicStore(self.previous)
        calls = []
        def mixed(callback):
            calls.append(callback)
            return self.store.run(callback) if len(calls) == 1 else other.run(callback)
        with self.assertRaises(IntervalReceiptUncertain):
            persist_interval_source_receipt(
                bound_client=self.store, transaction_runner=mixed,
                ledger_ref=self.store.ref(LEDGER), owner_ref=self.store.ref(OWNER), owner_id="owner-one",
                expected_ledger_update_time=VERSION, plan=self.plan,
            )
        self.assertEqual(self.store.read_calls, 3)
        self.assertEqual(other.read_calls, 0)
        self.assertEqual(self.receipt()["interval"], self.interval)

    def test_fully_rebound_forgery_is_regenerated_against_transactional_source(self):
        # Hashes can be recomputed by a caller. A fabricated before-state must
        # still fail against the actual ledger before create/update are staged.
        forged_previous = copy.deepcopy(self.previous)
        forged_previous["unknown_future_field"]["keep"] = False
        forged_next = copy.deepcopy(self.following)
        forged_next["unknown_future_field"]["keep"] = False
        forged = self.build(previous_state=forged_previous, next_state=forged_next)
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist(plan=forged)
        self.assertEqual(self.store.values[LEDGER], self.previous)
        self.assertNotIn(self.plan.receipt_path, self.store.values)
        self.assertEqual(self.store.read_calls, 3)

    def test_identical_retry_rejects_altered_patch_before_reads_or_reaccounting(self):
        self.persist()
        saved = copy.deepcopy(self.store.values)
        count = self.store.read_calls
        patch = json.loads(self.plan.patch_json)
        patch["daily_external_principal_usdt"] = 999.0
        forged = replace(self.plan, patch_json=json.dumps(patch, sort_keys=True, separators=(",", ":")))
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist(plan=forged)
        self.assertEqual(self.store.values, saved)
        self.assertEqual(self.store.read_calls, count)

    def test_unbounded_or_missing_transaction_attempt_contract_fails_before_reads(self):
        for attempts in (None, 5, True):
            with self.subTest(attempts=attempts):
                def invalid_runner(callback):
                    transaction = Transaction(self.store)
                    transaction._max_attempts = attempts
                    return callback(transaction)
                with self.assertRaises(IntervalReceiptBlocked):
                    persist_interval_source_receipt(
                        bound_client=self.store, transaction_runner=invalid_runner,
                        ledger_ref=self.store.ref(LEDGER), owner_ref=self.store.ref(OWNER),
                        owner_id="owner-one", expected_ledger_update_time=VERSION, plan=self.plan,
                    )
                self.assertEqual(self.store.read_calls, 0)
                self.assertNotIn(self.plan.receipt_path, self.store.values)

    def test_coherently_rebound_after_digest_is_compared_with_actual_next_state(self):
        receipt = json.loads(self.plan.receipt_json)
        receipt["source"]["ledger_after_sha256"] = "b" * 64
        forged = replace(
            self.plan, ledger_after_sha256="b" * 64,
            receipt_json=json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        )
        with self.assertRaises(IntervalReceiptBlocked):
            self.persist(plan=forged)
        self.assertEqual(self.store.values[LEDGER], self.previous)
        self.assertNotIn(self.plan.receipt_path, self.store.values)
        self.assertEqual(self.store.read_calls, 3)

    def test_unknown_schema_malformed_json_and_patch_binding_fail_before_reads(self):
        for mutate in (
            lambda receipt: receipt.update(schema_version="candidate.v999"),
            lambda receipt: receipt["source"].update(patch_sha256="b" * 64),
            lambda receipt: receipt.update(interval_id_sha256="b" * 64),
            lambda receipt: receipt.update(extra_metadata=True),
        ):
            receipt = json.loads(self.plan.receipt_json)
            mutate(receipt)
            forged = replace(self.plan, receipt_json=json.dumps(receipt, sort_keys=True, separators=(",", ":")))
            with self.assertRaises(IntervalReceiptBlocked):
                self.persist(plan=forged)
            self.assertEqual(self.store.read_calls, 0)
        for invalid in ("[]", "{", "null", "NaN"):
            with self.assertRaises(IntervalReceiptBlocked):
                self.persist(plan=replace(self.plan, patch_json=invalid))
            self.assertEqual(self.store.read_calls, 0)


if __name__ == "__main__":
    unittest.main()
