import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from scripts import migrate_daily_accounting_state as m
from tests.test_daily_accounting_migration import Ref, Snapshot, _ledger
from tests.test_approved_accounting_rebase import ArchiveTransaction
from tests.test_forward_earn_accounting import materials, NOW


def setup(monkeypatch):
    initial, fresh, _cash = materials()
    cp = initial['earn_accrual_checkpoint']
    ledger = _ledger(last_balance_snapshot={'USDT': 100, 'BNB': 3}, accounting_rebase={'archive_document': 'preserve_old_archive'})
    control = {'state': 'RECONCILE_ONLY', 'source': {'original_evidence': {'account_scope_sha256': 'a' * 64}}}
    refs = {'ledger_ref': Ref(Snapshot(ledger)), 'control_ref': Ref(Snapshot(control)), 'owner_ref': Ref(Snapshot(None))}
    archive = Ref(Snapshot(None))
    fields = {k: copy.deepcopy(initial.get(k, 0)) for k in m._ACCOUNTING_FIELDS}
    fields.update(earn_accrual_checkpoint=cp, earn_accounted_net_changes={'USDT': '0', 'BNB': '0'},
                  external_cash_flow_cursor=initial['external_cash_flow_cursor'])
    proposal = {'source_sha': 'c625413dcc601412358efbb5373e38788da95d0e',
                'ledger_sha256': m.digest(ledger), 'control_sha256': m.digest(control),
                'ledger_update_time': m._timestamp(refs['ledger_ref'].snapshot.update_time),
                'proposed_fields': fields, 'historical_difference_unresolved': True}
    monkeypatch.setattr(m, 'APPROVED_PROSPECTIVE_SHA256', m.digest(proposal))
    return refs, archive, proposal, fresh


def test_exact_approval_loaded_without_retaining_secret(monkeypatch):
    _, _, proposal, _ = setup(monkeypatch)
    monkeypatch.setenv('BINANCE_APPROVED_PROSPECTIVE_OPENING', json.dumps(proposal))
    assert m._load_prospective_approval() == proposal
    import os
    assert 'BINANCE_APPROVED_PROSPECTIVE_OPENING' not in os.environ


@pytest.mark.parametrize('change', ['ledger', 'control', 'owner', 'archive', 'version', 'payload'])
def test_prospective_cas_rejects_changes_before_any_write(monkeypatch, change):
    refs, archive, proposal, _ = setup(monkeypatch)
    if change == 'ledger': refs['ledger_ref'].snapshot.value['daily_equity_base'] += 1
    if change == 'control': refs['control_ref'].snapshot.value['state'] = 'ACTIVE_LKG'
    if change == 'owner': refs['owner_ref'] = Ref(Snapshot({'owner': 'busy'}))
    if change == 'archive': archive.snapshot = Snapshot({'old': True})
    if change == 'version': refs['ledger_ref'].snapshot.update_time = NOW.isoformat()
    if change == 'payload': proposal['proposed_fields']['daily_equity_base'] += 1
    tx = ArchiveTransaction()
    with pytest.raises(m.MigrationBlocked):
        m._prospective_transaction(tx, refs=refs, archive_ref=archive, approved=proposal, now=NOW)
    assert not tx.writes and not tx.creates


def test_prospective_transaction_archives_entire_prior_ledger_and_preserves_control(monkeypatch):
    refs, archive, proposal, _ = setup(monkeypatch)
    tx = ArchiveTransaction()
    written = m._prospective_transaction(tx, refs=refs, archive_ref=archive, approved=proposal, now=NOW)
    assert len(tx.creates) == len(tx.writes) == 1
    backup = tx.creates[0][1]
    assert backup['ledger'] == refs['ledger_ref'].snapshot.value
    assert backup['recovery_control'] == refs['control_ref'].snapshot.value
    assert backup['ledger']['accounting_rebase']['archive_document'] == 'preserve_old_archive'
    patch = tx.writes[0][1]
    assert patch['earn_accounted_net_changes'] == {'USDT': '0', 'BNB': '0'}
    assert 'is_circuit_broken' not in patch and 'order_submission' not in patch
    assert patch['accounting_rebase']['approved_proposal_run_id'] == '34690028846'
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
    refs['control_ref'].snapshot.value['source']['original_evidence']['account_scope_sha256'] = cp_scope
    approved['control_sha256'] = m.digest(refs['control_ref'].snapshot.value)
    monkeypatch.setattr(m, 'APPROVED_PROSPECTIVE_SHA256', m.digest(approved))
    kwargs = dict(refs=refs, client=object(), expected={'account_scope_sha256': cp_scope},
                  approved=approved, now=NOW-timedelta(seconds=10), clock=lambda: NOW)
    if change:
        with pytest.raises(m.MigrationBlocked): m._prospective_preflight(**kwargs)
    else:
        assert m._prospective_preflight(**kwargs) == NOW


@pytest.mark.parametrize('outcome', ['success', 'write_timeout', 'readback_timeout', 'readback_mismatch'])
def test_apply_is_single_attempt_and_archives_without_activating(monkeypatch, outcome):
    from google.cloud import firestore
    refs, archive, approved, _ = setup(monkeypatch)
    refs['ledger_ref'].parent = SimpleNamespace(document=lambda name: archive)
    committed = []
    class Owner(Ref):
        def get(self, **kw):
            if committed and outcome == 'readback_timeout': raise TimeoutError('private data')
            return super().get(**kw)
    refs['owner_ref'] = Owner(Snapshot(None))
    class Tx(ArchiveTransaction):
        def create(self, ref, value):
            super().create(ref, value); ref.snapshot = Snapshot(copy.deepcopy(value))
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
    kwargs = dict(client=object(), expected={}, now=NOW, fixed_now=True)
    if outcome == 'success':
        result = m._apply_prospective_rebase(refs, **kwargs)
        assert result['status'] == 'rebased' and result['control_unchanged'] is True
        assert result['execution_authority_granted'] is False
        monkeypatch.setenv('BINANCE_APPROVED_PROSPECTIVE_OPENING', json.dumps(approved))
        with pytest.raises(m.MigrationBlocked): m._apply_prospective_rebase(refs, **kwargs)
    else:
        with pytest.raises(m.MigrationApplyUncertain) as error: m._apply_prospective_rebase(refs, **kwargs)
        assert 'private' not in str(error.value)
    assert attempts == [1] and len(tx.creates) == len(tx.writes) == 1
    assert refs['control_ref'].snapshot.value['state'] == 'RECONCILE_ONLY'
    assert refs['ledger_ref'].snapshot.value['is_circuit_broken'] is True
