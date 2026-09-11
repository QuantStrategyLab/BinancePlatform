import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import migrate_daily_accounting_state as migration
from tests.test_daily_accounting_migration import NOW, Ref, Snapshot, Transaction, _evidence, _ledger


def setup_rebase(monkeypatch):
    ledger = _ledger(last_reset_date="2026-08-11")
    control = {"state": "RECONCILE_ONLY", "source": {"original_evidence": {"account_scope_sha256": "a" * 64}}}
    refs = {"ledger_ref": Ref(Snapshot(ledger)), "control_ref": Ref(Snapshot(control)),
            "owner_ref": Ref(Snapshot(None))}
    archive = Ref(Snapshot(None))
    monkeypatch.setattr(migration, "APPROVED_REBASE_LEDGER_SHA256", migration.digest(ledger))
    monkeypatch.setattr(migration, "APPROVED_REBASE_CONTROL_SHA256", migration.digest(control))
    monkeypatch.setattr(migration, "APPROVED_REBASE_BALANCES_SHA256", migration.digest(_evidence()["balance_snapshot"]))
    return refs, archive, ledger, control


class ArchiveTransaction(Transaction):
    def __init__(self):
        super().__init__()
        self.creates = []

    def create(self, ref, value):
        self.creates.append((ref, copy.deepcopy(value)))


def test_approved_rebase_archives_old_data_and_only_changes_accounting_fields(monkeypatch):
    refs, archive, ledger, control = setup_rebase(monkeypatch)
    proposal = migration.build_rebase_proposal(ledger=ledger, evidence=_evidence(history_counts={"earn_rewards": 1}), observed_at=NOW)
    tx = ArchiveTransaction()
    expected = migration._rebase_transaction(tx, refs=refs, archive_ref=archive, ledger=ledger,
        control=control, ledger_update_time=refs["ledger_ref"].snapshot.update_time,
        proposed_fields=proposal["proposed_fields"], observed_at=NOW, started_at=NOW)
    assert len(tx.creates) == len(tx.writes) == 1
    saved = tx.creates[0][1]
    assert saved["ledger"] == ledger and saved["recovery_control"] == control
    assert saved["historical_difference_unresolved"] is True
    patch = tx.writes[0][1]
    assert set(patch) == migration._ACCOUNTING_FIELDS | {"accounting_rebase"}
    assert patch["last_balance_snapshot"]["BNB"] == 0.25
    assert patch["accounting_rebase"]["historical_difference_unresolved"] is True
    assert "is_circuit_broken" not in patch and "order_submission" not in patch
    assert migration.digest({**ledger, **patch}) == expected["new_ledger_sha256"]


@pytest.mark.parametrize("change", ["ledger", "control", "owner", "archive", "version"])
def test_approved_rebase_rejects_changed_source_without_queued_writes(monkeypatch, change):
    refs, archive, ledger, control = setup_rebase(monkeypatch)
    version = refs["ledger_ref"].snapshot.update_time
    if change == "ledger": refs["ledger_ref"].snapshot.value["daily_equity_base"] += 1
    if change == "control": refs["control_ref"].snapshot.value["state"] = "ACTIVE_LKG"
    if change == "owner": refs["owner_ref"] = Ref(Snapshot({"active": True}))
    if change == "archive": archive = Ref(Snapshot({"previous": True}))
    if change == "version": refs["ledger_ref"].snapshot.update_time = "2026-09-11T02:00:00Z"
    proposed = migration.build_rebase_proposal(ledger=_ledger(), evidence=_evidence(), observed_at=NOW)["proposed_fields"]
    tx = ArchiveTransaction()
    with pytest.raises(migration.MigrationAtomicPrecondition):
        migration._rebase_transaction(tx, refs=refs, archive_ref=archive, ledger=ledger,
            control=control, ledger_update_time=version, proposed_fields=proposed,
            observed_at=NOW, started_at=NOW)
    assert tx.creates == [] and tx.writes == []


@pytest.mark.parametrize("change", ["balances", "identity", "deposit", "orders", "fills", "incomplete"])
def test_rebase_preflight_rejects_material_evidence_changes(monkeypatch, change):
    _, _, ledger, control = setup_rebase(monkeypatch)
    evidence = _evidence(history_counts={"earn_rewards": 1, "deposits": 0})
    if change == "balances": evidence["balance_snapshot"]["BTC"] += 0.01
    if change == "identity": evidence["account_scope_sha256"] = "b" * 64
    if change == "deposit": evidence["history_counts"]["deposits"] = 1
    if change == "orders": evidence["open_order_count"] = 1
    if change == "fills": evidence["recent_execution_count"] = 1
    if change == "incomplete": evidence["history_complete"] = False
    with pytest.raises(migration.MigrationBlocked):
        migration._approved_rebase_fields(ledger=ledger, control=control, evidence=evidence, now=NOW)


@pytest.mark.parametrize("outcome", ["success", "write_timeout", "readback_timeout", "readback_mismatch"])
def test_rebase_runtime_is_single_attempt_and_unknown_outcome_never_retries(monkeypatch, capsys, outcome):
    from google.cloud import firestore
    refs, archive, ledger, control = setup_rebase(monkeypatch)
    committed = []
    class OwnerRef(Ref):
        def get(self, **kwargs):
            if committed and outcome == "readback_timeout":
                raise TimeoutError("synthetic private provider detail")
            return super().get(**kwargs)
    refs["owner_ref"] = OwnerRef(Snapshot(None))
    refs["ledger_ref"].parent = SimpleNamespace(document=lambda name: archive)
    class CommittingTransaction(ArchiveTransaction):
        def create(self, ref, value):
            super().create(ref, value)
            ref.snapshot = Snapshot(copy.deepcopy(value))
        def update(self, ref, value):
            super().update(ref, value)
            ref.snapshot.value.update(copy.deepcopy(value))
    tx = CommittingTransaction()
    calls = []
    def transaction(*, max_attempts):
        calls.append(max_attempts)
        return tx
    def transactional(fn):
        def execute(tx):
            result = fn(tx)
            committed.append(True)
            if outcome == "write_timeout": raise TimeoutError("synthetic private provider detail")
            if outcome == "readback_mismatch": archive.snapshot.value["ledger"]["daily_equity_base"] += 1
            return result
        return execute
    monkeypatch.setattr(firestore, "transactional", transactional, raising=False)
    monkeypatch.setattr(migration, "require_runtime_context", lambda: None)
    monkeypatch.setattr(migration, "resolve_runtime_target_from_env", lambda **kw: SimpleNamespace(
        live_continuity=SimpleNamespace(state="RECONCILE_ONLY")))
    monkeypatch.setattr(migration, "_expected_digests", lambda: {"account_scope_sha256": "a" * 64})
    monkeypatch.setattr(migration, "_refs", lambda: refs)
    monkeypatch.setattr(migration, "connect_client", lambda *a, **kw: object())
    monkeypatch.setattr(migration, "_collect_evidence", lambda *a, **kw: _evidence(
        utc_date=datetime.now(timezone.utc).date().isoformat(), history_counts={"earn_rewards": 1}))
    monkeypatch.setattr(migration, "get_firestore_client", lambda: SimpleNamespace(transaction=transaction))
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic")
    rc = migration.main(["rebase-apply"])
    output = capsys.readouterr().out
    result = json.loads(output)
    assert "private provider" not in output and "14750" not in output
    assert calls == [1] and len(tx.creates) == len(tx.writes) == 1
    assert refs["control_ref"].snapshot.value == control
    assert refs["ledger_ref"].snapshot.value["is_circuit_broken"] is True
    if outcome == "success":
        assert rc == 0 and result["status"] == "rebased" and result["old_ledger_archived"] is True
        assert migration.main(["rebase-apply"]) == 2
        assert calls == [1] and len(tx.creates) == 1
    else:
        assert rc == 2 and result["status"] == "uncertain" and result["no_retry"] is True
