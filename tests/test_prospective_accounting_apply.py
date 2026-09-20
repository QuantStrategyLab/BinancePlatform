import copy
import json
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts import migrate_daily_accounting_state as m
from tests.test_daily_accounting_migration import Ref, Snapshot, _ledger
from tests.test_approved_accounting_rebase import ArchiveTransaction
from tests.test_forward_earn_accounting import materials, NOW

PROPOSAL_RUN_ID = "35525044419"


def _complete_proposal(proposal, run_id=PROPOSAL_RUN_ID):
    proposal = copy.deepcopy(proposal)
    proposal["proposal_run_id"] = run_id
    proposal["archive_document"] = m.prospective_archive_document(run_id)
    proposal["opening_mode"] = "prospective"
    proposal["executable_candidate"] = False
    return proposal


def setup(monkeypatch, run_id=PROPOSAL_RUN_ID):
    initial, fresh, _cash = materials()
    cp = initial['earn_accrual_checkpoint']
    ledger = _ledger(last_balance_snapshot={'USDT': 100, 'BNB': 3}, accounting_rebase={'archive_document': 'preserve_old_archive'})
    control = {'state': 'RECONCILE_ONLY', 'source': {'original_evidence': {'account_scope_sha256': 'a' * 64}}}
    refs = {'ledger_ref': Ref(Snapshot(ledger)), 'control_ref': Ref(Snapshot(control)), 'owner_ref': Ref(Snapshot(None))}
    archive = Ref(Snapshot(None))
    fields = {k: copy.deepcopy(initial.get(k, 0)) for k in m._ACCOUNTING_FIELDS}
    fields.update(earn_accrual_checkpoint=cp, earn_accounted_net_changes={'USDT': '0', 'BNB': '0'},
                  external_cash_flow_cursor=initial['external_cash_flow_cursor'])
    proposal = _complete_proposal({
        'source_sha': 'c625413dcc601412358efbb5373e38788da95d0e',
        'ledger_sha256': m.digest(ledger),
        'control_sha256': m.digest(control),
        'ledger_update_time': m._timestamp(refs['ledger_ref'].snapshot.update_time),
        'proposed_fields': fields,
        'historical_difference_unresolved': True,
    }, run_id=run_id)
    return refs, archive, proposal, fresh


def test_exact_approval_loaded_without_retaining_secret(monkeypatch):
    _, _, proposal, _ = setup(monkeypatch)
    monkeypatch.setenv('BINANCE_APPROVED_PROSPECTIVE_OPENING', json.dumps(proposal))
    assert m._load_prospective_approval() == proposal
    assert 'BINANCE_APPROVED_PROSPECTIVE_OPENING' not in os.environ


@pytest.mark.parametrize('defect', [
    'missing_run_id',
    'invalid_run_id',
    'tampered_run_id',
    'archive_mismatch',
    'legacy_fixed_digest',
    'missing_historical_flag',
])
def test_approval_rejects_missing_illegal_or_legacy_bindings(monkeypatch, defect):
    _, _, proposal, _ = setup(monkeypatch)
    if defect == 'missing_run_id':
        proposal.pop('proposal_run_id')
    elif defect == 'invalid_run_id':
        proposal['proposal_run_id'] = '0'
        proposal['archive_document'] = m.prospective_archive_document(PROPOSAL_RUN_ID)
    elif defect == 'tampered_run_id':
        proposal['proposal_run_id'] = '99999999999'
    elif defect == 'archive_mismatch':
        proposal['archive_document'] = 'MULTI_ASSET_STATE__before_rebase_1'
    elif defect == 'legacy_fixed_digest':
        # Force the retired fixed digest path to fail closed for new applies.
        monkeypatch.setattr(m, 'digest', lambda _value: m.APPROVED_PROSPECTIVE_SHA256)
    else:
        proposal['historical_difference_unresolved'] = False
    monkeypatch.setenv('BINANCE_APPROVED_PROSPECTIVE_OPENING', json.dumps(proposal))
    with pytest.raises(m.MigrationBlocked, match='prospective_approval_'):
        m._load_prospective_approval()
    assert 'BINANCE_APPROVED_PROSPECTIVE_OPENING' not in os.environ


@pytest.mark.parametrize('change', ['ledger', 'control', 'owner', 'archive', 'version', 'binding'])
def test_prospective_cas_rejects_changes_before_any_write(monkeypatch, change):
    refs, archive, proposal, _ = setup(monkeypatch)
    if change == 'ledger': refs['ledger_ref'].snapshot.value['daily_equity_base'] += 1
    if change == 'control': refs['control_ref'].snapshot.value['state'] = 'ACTIVE_LKG'
    if change == 'owner': refs['owner_ref'] = Ref(Snapshot({'owner': 'busy'}))
    if change == 'archive': archive.snapshot = Snapshot({'old': True})
    if change == 'version': refs['ledger_ref'].snapshot.update_time = NOW.isoformat()
    if change == 'binding': proposal['ledger_sha256'] = '0' * 64
    tx = ArchiveTransaction()
    with pytest.raises(m.MigrationBlocked):
        m._prospective_transaction(tx, refs=refs, archive_ref=archive, approved=proposal, now=NOW)
    assert not tx.writes and not tx.creates


def test_prospective_transaction_binds_dynamic_archive_and_marker(monkeypatch):
    refs, archive, proposal, _ = setup(monkeypatch)
    tx = ArchiveTransaction()
    written = m._prospective_transaction(tx, refs=refs, archive_ref=archive, approved=proposal, now=NOW)
    assert len(tx.creates) == len(tx.writes) == 1
    backup = tx.creates[0][1]
    assert backup['ledger'] == refs['ledger_ref'].snapshot.value
    assert backup['recovery_control'] == refs['control_ref'].snapshot.value
    assert backup['ledger']['accounting_rebase']['archive_document'] == 'preserve_old_archive'
    expected_archive = m.prospective_archive_document(PROPOSAL_RUN_ID)
    assert written['archive_document'] == expected_archive
    assert written['approved_proposal_run_id'] == PROPOSAL_RUN_ID
    assert written['approved_proposal_sha256'] == m.digest(proposal)
    patch = tx.writes[0][1]
    assert patch['earn_accounted_net_changes'] == {'USDT': '0', 'BNB': '0'}
    assert 'is_circuit_broken' not in patch and 'order_submission' not in patch
    assert patch['accounting_rebase'] == {
        'archive_document': expected_archive,
        'started_at': NOW.isoformat(),
        'opening_balance_observed_at': proposal['proposed_fields']['earn_accrual_checkpoint']['observed_at'],
        'historical_difference_unresolved': True,
        'approved_proposal_run_id': PROPOSAL_RUN_ID,
        'approved_proposal_sha256': m.digest(proposal),
    }
    assert written['new_ledger_sha256'] == m.digest({**backup['ledger'], **patch})


@pytest.mark.parametrize('change', [None, 'counter_reset', 'unknown_delta', 'new_flow', 'trade'])
def test_preflight_allows_only_continuous_income_since_approved_snapshot(monkeypatch, change):
    refs, archive, approved, checkpoint = setup(monkeypatch)
    fresh = {'earn_accrual_checkpoint': checkpoint,
             'external_cash_flow_cursor': {'observed_at': NOW.isoformat(), 'records': {}},
             'observation_completed_at': NOW.isoformat()}
    if change == 'counter_reset': checkpoint['assets']['BNB']['products']['BNB001']['realtime_rewards'] = '0'
    if change == 'unknown_delta': checkpoint['assets']['USDT'].update(spot_free='101', quantity='101')
    if change == 'new_flow': fresh['external_cash_flow_cursor']['records']['unapproved'] = {}
    monkeypatch.setattr(m, 'collect_prospective_opening', lambda *a, **kw: fresh)
    monkeypatch.setattr(m, 'collect_read_only_reconciliation_observations', lambda *a, **kw: SimpleNamespace(
        account_scope={'account_uid': 'synthetic'}, open_orders=[], recent_executions=[{}] if change == 'trade' else []))
    cp_scope = m.digest({'account_uid': 'synthetic'})
    approved['proposed_fields']['earn_accrual_checkpoint']['account_scope_sha256'] = cp_scope
    checkpoint['account_scope_sha256'] = cp_scope
    approved['control_sha256'] = m.digest(refs['control_ref'].snapshot.value)
    monkeypatch.setattr(m, '_verified_control_account_scope', lambda control, **kw: cp_scope)
    kwargs = dict(
        refs=refs, client=object(), expected={'account_scope_sha256': cp_scope},
        approved=approved, now=NOW-timedelta(seconds=10), runtime_target=object(), clock=lambda: NOW,
    )
    if change:
        with pytest.raises(m.MigrationBlocked): m._prospective_preflight(**kwargs)
    else:
        assert m._prospective_preflight(**kwargs) == NOW


def test_preflight_accepts_reconciled_enrollment_control_and_rejects_forged_identity(monkeypatch):
    from application.reconciliation_recovery import collect_recovery_source
    from tests.test_reconciliation_recovery import inputs

    refs, _archive, approved, checkpoint = setup(monkeypatch)
    args = inputs()
    package = collect_recovery_source(**args)
    control = {"state": "RECONCILE_ONLY", **package}
    scope = args["expected"]["account_scope_sha256"]
    assert scope == m.digest({"account_uid": "123"})
    approved["proposed_fields"]["earn_accrual_checkpoint"]["account_scope_sha256"] = scope
    checkpoint["account_scope_sha256"] = scope
    refs["control_ref"] = Ref(Snapshot(control))
    approved["control_sha256"] = m.digest(control)
    approved["ledger_sha256"] = m.digest(refs["ledger_ref"].snapshot.value)
    fresh = {
        "earn_accrual_checkpoint": checkpoint,
        "external_cash_flow_cursor": {"observed_at": NOW.isoformat(), "records": {}},
        "observation_completed_at": NOW.isoformat(),
    }
    monkeypatch.setattr(m, "collect_prospective_opening", lambda *a, **kw: fresh)
    monkeypatch.setattr(
        m,
        "collect_read_only_reconciliation_observations",
        lambda *a, **kw: SimpleNamespace(
            account_scope={"account_uid": "123"}, open_orders=[], recent_executions=[]
        ),
    )
    assert m._prospective_preflight(
        refs=refs,
        client=object(),
        expected=args["expected"],
        approved=approved,
        now=NOW - timedelta(seconds=10),
        runtime_target=args["runtime_target"],
        clock=lambda: NOW,
    ) == NOW

    forged = {"state": "RECONCILE_ONLY", "source": {"original_evidence": {"account_scope_sha256": scope}}}
    refs["control_ref"] = Ref(Snapshot(forged))
    approved["control_sha256"] = m.digest(forged)
    with pytest.raises(m.MigrationBlocked, match="prospective_control_identity_unverified"):
        m._prospective_preflight(
            refs=refs,
            client=object(),
            expected=args["expected"],
            approved=approved,
            now=NOW - timedelta(seconds=10),
            runtime_target=args["runtime_target"],
            clock=lambda: NOW,
        )


def test_apply_surfaces_control_identity_reason_not_generic_blocked(monkeypatch, capsys):
    refs, archive, approved, _ = setup(monkeypatch)
    live = {
        "state": "RECONCILE_ONLY",
        "source": {
            "kind": "prospective_rebase",
            "reconciled_evidence": {"account_scope_sha256": "a" * 64},
        },
    }
    refs["control_ref"] = Ref(Snapshot(live))
    approved["control_sha256"] = m.digest(live)
    approved["ledger_sha256"] = m.digest(refs["ledger_ref"].snapshot.value)
    refs["ledger_ref"].parent = SimpleNamespace(document=lambda name: archive)
    monkeypatch.setenv("BINANCE_APPROVED_PROSPECTIVE_OPENING", json.dumps(approved))
    monkeypatch.setenv("GITHUB_SHA", "e" * 40)
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic")
    monkeypatch.setattr(m, "resolve_runtime_target_from_env", lambda **kw: SimpleNamespace(
        live_continuity=SimpleNamespace(state="RECONCILE_ONLY"),
        platform_id="binance",
        strategy_profile="crypto_live_pool_rotation",
        to_dict=lambda: {"platform_id": "binance"},
    ))
    monkeypatch.setattr(m, "require_runtime_context", lambda: None)
    monkeypatch.setattr(m, "_expected_digests", lambda: {"account_scope_sha256": "a" * 64})
    monkeypatch.setattr(m, "_refs", lambda: refs)
    monkeypatch.setattr(m, "connect_client", lambda *a, **kw: object())
    monkeypatch.setattr(m, "_read_source", lambda refs: (
        refs["ledger_ref"].snapshot,
        refs["ledger_ref"].snapshot.value,
        refs["control_ref"].snapshot.value,
    ))
    assert m.main(["prospective-rebase-apply"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason_code"] == "prospective_control_identity_unverified"
    assert payload["status"] == "blocked"


def test_dynamic_archive_handoff_to_material_and_migration_binding(monkeypatch):
    from application import rebased_recovery as recovery

    refs, archive_ref, proposal, _ = setup(monkeypatch, run_id="35527010135")
    tx = ArchiveTransaction()
    written = m._prospective_transaction(
        tx, refs=refs, archive_ref=archive_ref, approved=proposal, now=NOW
    )
    backup = tx.creates[0][1]
    patch = tx.writes[0][1]
    new_ledger = {**refs["ledger_ref"].snapshot.value, **patch}
    material = recovery.validate_prospective_rebase_material(new_ledger, backup)
    binding = recovery.prospective_migration_binding(backup)
    assert material["opening_quantities"]
    assert binding["historical"] is False
    assert binding["run_id"] == 35527010135
    assert binding["head_sha"] == proposal["source_sha"]
    assert binding["archive_document"] == written["archive_document"]
    assert binding["approval_sha256"] == written["approved_proposal_sha256"]
    assert binding["ledger_sha256"] == written["new_ledger_sha256"]
    assert binding["run_id"] != recovery.PROSPECTIVE_MIGRATION_RUN_ID
    assert binding["archive_document"] != recovery.PROSPECTIVE_ARCHIVE_DOCUMENT


@pytest.mark.parametrize('outcome', ['success', 'write_timeout', 'readback_timeout', 'readback_mismatch'])
def test_apply_is_single_attempt_and_archives_without_activating(monkeypatch, outcome):
    from google.cloud import firestore
    refs, archive, approved, _ = setup(monkeypatch)
    expected_archive = m.prospective_archive_document(PROPOSAL_RUN_ID)
    created = {}

    def document(name):
        assert name == expected_archive
        return archive

    refs['ledger_ref'].parent = SimpleNamespace(document=document)
    committed = []
    class Owner(Ref):
        def get(self, **kw):
            if committed and outcome == 'readback_timeout': raise TimeoutError('private data')
            return super().get(**kw)
    refs['owner_ref'] = Owner(Snapshot(None))
    class Tx(ArchiveTransaction):
        def create(self, ref, value):
            super().create(ref, value); ref.snapshot = Snapshot(copy.deepcopy(value)); created[ref] = True
        def update(self, ref, value):
            super().update(ref, value); ref.snapshot.value.update(copy.deepcopy(value))
    tx, attempts = Tx(), []
    def transaction(*, max_attempts): attempts.append(max_attempts); return tx
    def transactional(fn):
        def execute(tx):
            result = fn(tx); committed.append(True)
            if outcome == 'write_timeout': raise TimeoutError('private data')
            if outcome == 'readback_mismatch': archive.snapshot.value['ledger']['daily_equity_base'] += 1
            return result
        return execute
    monkeypatch.setattr(firestore, 'transactional', transactional, raising=False)
    monkeypatch.setattr(m, 'get_firestore_client', lambda: SimpleNamespace(transaction=transaction))
    monkeypatch.setattr(m, '_prospective_preflight', lambda **kw: NOW)
    monkeypatch.setenv('BINANCE_APPROVED_PROSPECTIVE_OPENING', json.dumps(approved))
    target = SimpleNamespace(live_continuity=SimpleNamespace(state='RECONCILE_ONLY'))
    kwargs = dict(client=object(), expected={}, now=NOW, fixed_now=True, runtime_target=target)
    if outcome == 'success':
        result = m._apply_prospective_rebase(refs, **kwargs)
        assert result['status'] == 'rebased' and result['control_unchanged'] is True
        assert result['execution_authority_granted'] is False
        assert result['archive_document'] == expected_archive
        assert result['approved_proposal_run_id'] == PROPOSAL_RUN_ID
        assert result['approved_proposal_sha256'] == m.digest(approved)
        monkeypatch.setenv('BINANCE_APPROVED_PROSPECTIVE_OPENING', json.dumps(approved))
        with pytest.raises(m.MigrationBlocked, match='prospective_archive_already_exists'):
            m._apply_prospective_rebase(refs, **kwargs)
        assert len(tx.creates) == len(tx.writes) == 1
    else:
        with pytest.raises(m.MigrationApplyUncertain) as error: m._apply_prospective_rebase(refs, **kwargs)
        assert 'private' not in str(error.value)
        assert attempts == [1] and len(tx.creates) == len(tx.writes) == 1
    assert refs['control_ref'].snapshot.value['state'] == 'RECONCILE_ONLY'
    assert refs['ledger_ref'].snapshot.value['is_circuit_broken'] is True


def test_apply_rejects_existing_archive_with_zero_writes(monkeypatch):
    from google.cloud import firestore
    refs, archive, approved, _ = setup(monkeypatch)
    archive.snapshot = Snapshot({'already': True})
    refs['ledger_ref'].parent = SimpleNamespace(
        document=lambda name: archive if name == m.prospective_archive_document(PROPOSAL_RUN_ID) else None
    )
    attempts = []
    monkeypatch.setattr(firestore, 'transactional', lambda fn: fn, raising=False)
    monkeypatch.setattr(m, 'get_firestore_client', lambda: SimpleNamespace(
        transaction=lambda **kw: attempts.append(kw) or (_ for _ in ()).throw(AssertionError('no tx'))
    ))
    monkeypatch.setattr(m, '_prospective_preflight', lambda **kw: (_ for _ in ()).throw(AssertionError('no preflight')))
    monkeypatch.setenv('BINANCE_APPROVED_PROSPECTIVE_OPENING', json.dumps(approved))
    with pytest.raises(m.MigrationBlocked, match='prospective_archive_already_exists'):
        m._apply_prospective_rebase(
            refs, client=object(), expected={}, now=NOW, fixed_now=True, runtime_target=object(),
        )
    assert attempts == []
    assert 'BINANCE_APPROVED_PROSPECTIVE_OPENING' not in os.environ
