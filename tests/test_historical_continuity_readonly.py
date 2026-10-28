"""Production provenance stays diagnostic-only, with actual accounting replay."""
import copy
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from application import rebased_recovery as recovery
from scripts import historical_continuity_readonly as diagnostic
from scripts import migrate_daily_accounting_state as migration
from tests.test_historical_continuity import (
    NOW_BEYOND, LATER_BEYOND, _empty_history_client, _bnb_earn_row,
    _earn_page_for_asset, _evolve_ledger_without_daily_loss,
)
from tests.test_prospective_recovery_diagnosis import (
    _material, Client, _controller_setup,
)
from tests.test_broker_reconciliation import _target


def production_run(head):
    return dict(id=500, head_sha=head, head_branch="runtime-production",
                event="workflow_dispatch", path=".github/workflows/main.yml")


class StableClient(Client):
    def __init__(self):
        super().__init__()
        self.history, self.calls = _empty_history_client()

    def get_simple_earn_flexible_product_position(self, *, current, size, asset=None):
        assert current == 1 and size == 100
        return _earn_page_for_asset(asset, [_bnb_earn_row()])

    def _request_margin_api(self, method, path, **kwargs):
        return self.history._request_margin_api(method, path, **kwargs)

    def __getattr__(self, name):
        pytest.fail(f"Unexpected broker capability: {name}")


def collector_args(monkeypatch):
    ledger, archive, _ = _material(monkeypatch)
    # Exercise the current dynamic archive contract, not a hard-coded old approval.
    proposal = archive['approved_proposal']
    proposal.update(proposal_run_id="900", source_sha="d" * 40,
                    archive_document="MULTI_ASSET_STATE__before_rebase_900")
    marker = dict(ledger['accounting_rebase'], archive_document=proposal['archive_document'],
                  approved_proposal_run_id="900", approved_proposal_sha256=migration.digest(proposal))
    ledger['accounting_rebase'] = marker
    archive.update(marker, new_ledger_sha256=migration.digest(ledger))
    ledger = _evolve_ledger_without_daily_loss(ledger, observed_at=NOW_BEYOND)
    client = StableClient()
    return dict(client=diagnostic.ReadonlyClient(client), runtime_target=_target(),
                legacy_expected={'account_scope_sha256':migration.digest({'account_uid':'synthetic'})},
                ledger=ledger, archive=archive, symbols=('BNBUSDT',),
                source_run=production_run('b' * 40),
                migration_run=dict(id=900, head_sha='d' * 40, head_branch='main',
                                   event='workflow_dispatch', path='.github/workflows/main.yml'),
                now=NOW_BEYOND, clock=lambda: LATER_BEYOND), client


def test_production_diagnosis_reuses_actual_chunked_reads_without_candidate_or_writes(monkeypatch):
    args, client = collector_args(monkeypatch)
    before = copy.deepcopy((args['ledger'], args['archive']))
    def forbidden(*_args, **_kwargs):
        pytest.fail('diagnosis attempted candidate enrollment')
    monkeypatch.setattr(recovery, 'evaluate_broker_reconciliation_baseline_enrollment', forbidden)
    result = recovery.collect_historical_continuity_readonly(**args, tool_revision='b' * 40)
    assert result['status'] == 'diagnosed' and result['chunk_count'] >= 2
    assert result['write_performed'] is False and result['execution_authority_granted'] is False
    assert (args['ledger'], args['archive']) == before
    assert len({(start, end) for _, start, end in client.calls}) >= 2
    assert 'source' not in result and 'candidate' not in result


@pytest.mark.parametrize('changes', [
    {'head_branch':'main'}, {'event':'push'}, {'path':'.github/workflows/other.yml'},
    {'head_sha':'c' * 40}, {'id':True}, {'private':'PRIVATE'},
])
def test_production_provenance_is_exact_and_cannot_enter_old_enrollment(monkeypatch, changes):
    args, _ = collector_args(monkeypatch)
    args['source_run'].update(changes)
    with pytest.raises(ValueError, match='historical_continuity_source_run_invalid'):
        recovery.collect_historical_continuity_readonly(**args, tool_revision='b' * 40)
    args['source_run'] = production_run('b' * 40)
    for function in (recovery.collect_historical_continuity_diagnosis,
                     recovery.collect_historical_continuity_source):
        with pytest.raises(ValueError, match='historical_continuity_source_run_invalid'):
            function(**args)


@pytest.mark.parametrize('change', ['archive', 'migration_ref', 'migration_sha', 'account', 'checkpoint'])
def test_existing_archive_and_account_contracts_are_not_bypassed(monkeypatch, change):
    args, _ = collector_args(monkeypatch)
    if change == 'archive':
        args['archive']['ledger']['unknown_future_field'] = {'forged':True}
    elif change == 'migration_ref':
        args['migration_run']['head_branch'] = 'runtime-production'
    elif change == 'migration_sha':
        args['migration_run']['head_sha'] = 'e' * 40
    elif change == 'account':
        args['legacy_expected']['account_scope_sha256'] = 'e' * 64
    else:
        args['ledger']['earn_accrual_checkpoint'] = {}
    with pytest.raises(ValueError):
        recovery.collect_historical_continuity_readonly(**args, tool_revision='b' * 40)


@pytest.mark.parametrize('operation', ['create_order','cancel_order','withdraw','transfer','set','subscribe'])
def test_readonly_client_exposes_no_trading_or_writing_methods(operation):
    client = diagnostic.ReadonlyClient(SimpleNamespace())
    with pytest.raises(AttributeError):
        getattr(client, operation)
    for method, path in [('post','asset/transfer'), ('delete','order'), ('get','PRIVATE_PATH')]:
        with pytest.raises(ValueError, match='historical_readonly_operation_forbidden'):
            client._request_margin_api(method, path)


def install_production_context(monkeypatch):
    head = subprocess.check_output(['git','rev-parse','HEAD'], cwd=migration.ROOT, text=True).strip()
    for key,value in dict(GITHUB_REPOSITORY=migration.REPOSITORY,
         GITHUB_REF='refs/heads/runtime-production',
         GITHUB_WORKFLOW_REF=migration.REPOSITORY+'/.github/workflows/main.yml@refs/heads/runtime-production',
         GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_SHA=head, BINANCE_RUNTIME_WORKFLOW_SHA=head,
         RUNTIME_SOURCE_KIND='historical_readonly', RUNTIME_TARGET_ENABLED='false',
         RECONCILE_ONLY='true', GITHUB_RUN_ID='500', GITHUB_TOKEN='synthetic').items():
        monkeypatch.setenv(key,value)
    return head


@pytest.fixture
def active_setup(monkeypatch):
    controller,target,expected,recovery_id,docs,writes,_ = _controller_setup(monkeypatch,prepared=True)
    controller.run('activate', recovery_id)  # Existing synthetic fixture, before diagnosis tripwires.
    assert docs[controller.CONTROL_DOCUMENT].snapshot.value['state'] == 'ACTIVE_LKG'
    writes.clear()
    head = install_production_context(monkeypatch)
    before = {key:copy.deepcopy(ref.snapshot.value) for key,ref in docs.items()}
    for ref in docs.values():
        ref.snapshot.update_time = '2026-09-12T10:00:00Z'
        for name in ('set','create','update','delete'):
            monkeypatch.setattr(ref,name,lambda *_a,**_k:pytest.fail('diagnosis wrote a document'),raising=False)
    ledger_ref = docs['MULTI_ASSET_STATE']
    ledger_ref.parent = SimpleNamespace(document=lambda name: docs[name])
    refs = dict(ledger_ref=ledger_ref,control_ref=docs[controller.CONTROL_DOCUMENT],
                owner_ref=docs['MULTI_ASSET_STATE__owner'])
    monkeypatch.setattr(migration,'_refs',lambda:refs)
    monkeypatch.setattr(migration,'resolve_runtime_target_from_env',lambda **_:target)
    monkeypatch.setattr(migration,'_expected_digests',lambda:expected)
    monkeypatch.setattr(migration,'_symbols_from_env',lambda:('BNBUSDT',))
    monkeypatch.setattr(migration,'connect_client',lambda *_a,**_k:StableClient())
    monkeypatch.setattr(controller,'request_json',lambda *_a,**_k:dict(
        production_run(head),status='in_progress',repository={'full_name':migration.REPOSITORY}))
    monkeypatch.setattr('live_risk_authority.load_live_risk_authority',lambda *_a,**_k:pytest.fail('LIVE authority loaded'))
    return docs,before,writes


def test_actual_active_control_binding_and_all_source_readbacks_stay_unchanged(active_setup):
    docs,before,writes = active_setup
    result = diagnostic.run(now=NOW_BEYOND,clock=lambda:LATER_BEYOND)
    assert result['status'] == 'diagnosed' and result['owner_absent'] is True
    assert not writes and {key:ref.snapshot.value for key,ref in docs.items()} == before
    output = diagnostic.project_result(result,exit_code=0)
    assert output['status'] == 'diagnosed' and output['execution_permitted'] is False
    assert 'current_ledger_sha256' not in output
    assert 'reward_product_failure_stage' not in output


@pytest.mark.parametrize('change', ['owner','control','archive','readback'])
def test_active_control_archive_and_concurrent_change_fail_closed(active_setup,monkeypatch,change):
    docs,_,writes = active_setup
    if change == 'owner':
        docs['MULTI_ASSET_STATE__owner'].snapshot.exists = True
    elif change == 'control':
        docs[migration.CONTROL_DOCUMENT].snapshot.value['confirmation']['candidate_sha256'] = '0' * 64
    elif change == 'archive':
        docs[recovery.PROSPECTIVE_ARCHIVE_DOCUMENT].snapshot.value['ledger']['private'] = 'changed'
    else:
        original = recovery.collect_historical_continuity_readonly
        def collect(**kwargs):
            result = original(**kwargs)
            docs['MULTI_ASSET_STATE'].snapshot.update_time = '2026-09-12T10:01:00Z'
            return result
        monkeypatch.setattr(recovery,'collect_historical_continuity_readonly',collect)
    with pytest.raises(migration.MigrationBlocked):
        diagnostic.run(now=NOW_BEYOND,clock=lambda:LATER_BEYOND)
    assert not writes


def test_projection_never_exposes_free_keys_values_or_unknown_counts():
    value = dict(status='diagnosed',source_kind='historical_continuity',no_order=True,
                 write_performed=False,execution_authority_granted=False,
                 historical_difference_unresolved=True,ledger_unchanged=True,
                 control_unchanged=True,archive_unchanged=True,owner_absent=True,chunk_count=3,
                 reward_product_classification_counts={'123456789':'PRIVATE','opening_approved':False},
                 reward_product_failure_stage='reward_identity_missing',
                 amount='PRIVATE',observed_at='PRIVATE',asset='123456789')
    result = diagnostic.project_result(value,exit_code=0)
    assert result['status'] == 'diagnosed'
    assert 'reward_product_failure_stage' not in result
    assert all(v is None for v in result['reward_product_classification_counts'].values())
    assert 'PRIVATE' not in json.dumps(result) and '123456789' not in json.dumps(result)
    for reason in ['PRIVATE', {'account':'PRIVATE'}, [], None, 123456789]:
        assert diagnostic.project_result({'status':'blocked','reason_code':reason},exit_code=2)['reason_code'] == 'historical_readonly_failed'
    for changes in [{'owner_absent':False},{'write_performed':True},{'chunk_count':True}, {'chunk_count':None}]:
        assert diagnostic.project_result(value|changes,exit_code=0)['status'] == 'blocked'


@pytest.mark.parametrize('case,classification,stage', [
    ('missing_identity', 'lifecycle_evidence_incomplete', 'reward_identity_missing'),
    ('out_of_scope', 'out_of_scope_asset', None),
    ('missing_subscription', 'lifecycle_evidence_incomplete', 'lifecycle_conservation_unverified'),
])
def test_actual_rejected_history_preserves_only_partial_classification(monkeypatch,case,classification,stage):
    args, client = collector_args(monkeypatch)
    before = copy.deepcopy((args['ledger'], args['archive']))
    original = client.history._request_margin_api
    def read(method,path,**kwargs):
        if path.endswith('/rewardsRecord'):
            row = dict(asset='BNB',projectId='PRIVATE_PRODUCT',type='REALTIME',rewards='0.01',
                       time=kwargs['data']['startTime'] + 1)
            if case == 'missing_identity':
                row.pop('projectId')
            elif case == 'out_of_scope':
                row['asset'] = 'PRIVATE_ASSET'
            return {'rows':[row], 'total':1}
        return original(method,path,**kwargs)
    monkeypatch.setattr(client.history,'_request_margin_api',read)
    result = recovery.collect_historical_continuity_readonly(**args,tool_revision='b'*40)
    assert result['status'] == 'blocked'
    assert result['reason_code'] == 'historical_continuity_unapproved_reward_product'
    assert result['classification_scope'] == 'partial_history'
    assert result['reward_product_classification_counts'][classification] == 1
    assert result['reward_product_failure_stage'] == stage
    assert type(result['chunk_count']) is int and result['chunk_count'] > 0
    assert result['no_order'] is True and result['write_performed'] is False
    assert result['execution_authority_granted'] is False
    assert 'PRIVATE' not in json.dumps(result) and 'candidate' not in result
    assert (args['ledger'],args['archive']) == before


@pytest.mark.parametrize('changed', [None,'ledger','control','archive','owner'])
def test_partial_failure_still_checks_final_source_and_has_no_writes(active_setup,monkeypatch,changed):
    docs,before,writes = active_setup
    def rejected(**kwargs):
        if changed:
            key = {'ledger':'MULTI_ASSET_STATE','control':migration.CONTROL_DOCUMENT,
                   'archive':recovery.PROSPECTIVE_ARCHIVE_DOCUMENT,
                   'owner':'MULTI_ASSET_STATE__owner'}[changed]
            docs[key].snapshot.update_time = '2026-09-12T10:02:00Z'
            if changed == 'owner':
                docs[key].snapshot.exists = True
        raise recovery._HistoricalRewardProductBlocked(dict(chunk_count=2,
            reward_product_classification_counts={'product_mapping_ambiguous':1}))
    monkeypatch.setattr(recovery,'_collect_historical_continuity_observations',rejected)
    if changed:
        with pytest.raises(migration.MigrationBlocked,match='historical_readonly_source_changed'):
            diagnostic.run(now=NOW_BEYOND,clock=lambda:LATER_BEYOND)
    else:
        result = diagnostic.run(now=NOW_BEYOND,clock=lambda:LATER_BEYOND)
        output = diagnostic.project_result(result,exit_code=2)
        assert output['status'] == 'blocked' and output['execution_permitted'] is False
        assert output['classification_scope'] == 'partial_history' and output['chunk_count'] == 2
        assert output['reward_product_classification_counts']['product_mapping_ambiguous'] == 1
        assert output['reward_product_classification_counts']['opening_approved'] is None
    assert not writes and {key:ref.snapshot.value for key,ref in docs.items()} == before


def test_blocked_projection_requires_partial_contract_and_sanitizes_counts():
    value = dict(status='blocked',reason_code='historical_continuity_unapproved_reward_product',
                 source_kind='historical_continuity',classification_scope='partial_history',
                 no_order=True,write_performed=False,execution_authority_granted=False,
                 historical_difference_unresolved=True,ledger_unchanged=True,
                 control_unchanged=True,archive_unchanged=True,owner_absent=True,
                 chunk_count=True,reward_product_classification_counts={
                     'lifecycle_evidence_incomplete':1,'opening_approved':False,
                     'lifecycle_subscription_proven':-1,'out_of_scope_asset':10000001,
                     'product_mapping_ambiguous':'PRIVATE','123456789':3},
                 asset='PRIVATE',amount='PRIVATE',time='PRIVATE')
    result = diagnostic.project_result(value,exit_code=2)
    assert result['chunk_count'] is None and result['classification_scope'] == 'partial_history'
    counts = result['reward_product_classification_counts']
    assert counts['lifecycle_evidence_incomplete'] == 1
    assert all(v is None for k,v in counts.items() if k != 'lifecycle_evidence_incomplete')
    assert len(counts) == 6 and 'PRIVATE' not in json.dumps(result) and '123456789' not in json.dumps(result)
    assert 'reward_product_failure_stage' not in result
    for stage in ('reward_identity_missing','lifecycle_conservation_unverified'):
        marked = value|{'reward_product_failure_stage':stage}
        assert diagnostic.project_result(marked,exit_code=2)['reward_product_failure_stage'] == stage
        for changes in ({'reason_code':'historical_readonly_failed'}, {'owner_absent':False}):
            assert 'reward_product_failure_stage' not in diagnostic.project_result(marked|changes,exit_code=2)
    for stage in ('PRIVATE','123456789',{},[],None,1,False):
        marked = value|{'reward_product_failure_stage':stage}
        assert 'reward_product_failure_stage' not in diagnostic.project_result(marked,exit_code=2)
    for key,wrong in dict(source_kind='PRIVATE',classification_scope='PRIVATE',no_order=False,
                          write_performed=True,execution_authority_granted=True,
                          historical_difference_unresolved=False,ledger_unchanged=False,
                          control_unchanged=False,archive_unchanged=False,owner_absent=False).items():
        assert 'classification_scope' not in diagnostic.project_result(value|{key:wrong},exit_code=2)
    for exit_code in (0,1):
        assert 'classification_scope' not in diagnostic.project_result(value,exit_code=exit_code)
    for key in ('chunk_count','reward_product_classification_counts'):
        missing = {k:v for k,v in value.items() if k != key}
        output = diagnostic.project_result(missing,exit_code=2)
        if key == 'chunk_count':
            assert output[key] is None
        else:
            assert all(v is None for v in output[key].values())


@pytest.mark.parametrize('stage', ['reward_identity_missing','lifecycle_conservation_unverified'])
def test_known_failure_stage_requires_final_source_checks_before_projection(active_setup,monkeypatch,stage):
    docs,before,writes = active_setup
    def rejected(**kwargs):
        raise recovery._HistoricalRewardProductBlocked(dict(chunk_count=1,
            reward_product_failure_stage=stage,
            reward_product_classification_counts={'lifecycle_evidence_incomplete':1}))
    monkeypatch.setattr(recovery,'_collect_historical_continuity_observations',rejected)
    result = diagnostic.run(now=NOW_BEYOND,clock=lambda:LATER_BEYOND)
    output = diagnostic.project_result(result,exit_code=2)
    assert output['status'] == 'blocked' and output['execution_permitted'] is False
    assert output['reward_product_failure_stage'] == stage
    assert not writes and {key:ref.snapshot.value for key,ref in docs.items()} == before
    original = migration._read_earn_diagnosis_source
    reads = []
    def changed(refs):
        value = original(refs)
        reads.append(True)
        if len(reads) == 2:
            value['owner_exists'] = True
        return value
    monkeypatch.setattr(migration,'_read_earn_diagnosis_source',changed)
    with pytest.raises(migration.MigrationBlocked,match='historical_readonly_source_changed'):
        diagnostic.run(now=NOW_BEYOND,clock=lambda:LATER_BEYOND)
    assert not writes


@pytest.mark.parametrize('changes', [
    {'head_branch':'main'}, {'head_sha':'e' * 40}, {'status':'completed'},
    {'event':'push'}, {'path':'.github/workflows/other.yml'},
    {'repository':{'full_name':'PRIVATE'}}, {'id':501},
])
def test_current_run_uses_real_exact_production_metadata(monkeypatch,changes):
    from scripts import binance_recovery_controller as controller
    head = install_production_context(monkeypatch)
    value = dict(production_run(head), status='in_progress', repository={'full_name':migration.REPOSITORY})
    monkeypatch.setattr(controller,'request_json',lambda *_a,**_k:value)
    assert diagnostic._current_run() == production_run(head)
    value.update(changes)
    with pytest.raises(ValueError,match='historical_readonly_source_invalid'):
        diagnostic._current_run()


def test_dedicated_context_rejects_main_wrong_pin_and_other_modes(monkeypatch):
    head = install_production_context(monkeypatch)
    migration.require_runtime_context(diagnostic.ACTION)
    for key,value in [('GITHUB_REF','refs/heads/main'),('RUNTIME_SOURCE_KIND','earn_readonly'),
                      ('BINANCE_RUNTIME_WORKFLOW_SHA','f' * 40),('RUNTIME_TARGET_ENABLED','true')]:
        with monkeypatch.context() as patch:
            patch.setenv(key,value)
            with pytest.raises(ValueError):
                migration.require_runtime_context(diagnostic.ACTION)
    with pytest.raises(ValueError):
        migration.require_runtime_context('apply')
    with monkeypatch.context() as patch:
        patch.setenv('GITHUB_SHA','e' * 40)
        patch.setenv('BINANCE_RUNTIME_WORKFLOW_SHA','e' * 40)
        with pytest.raises(ValueError,match='tool_revision_mismatch'):
            migration.require_runtime_context(diagnostic.ACTION)
    assert head


@pytest.mark.parametrize("exception,reason,expected", [
    ("blocked", "historical_readonly_owned", "historical_readonly_owned"),
    ("blocked", "historical_continuity_archive_invalid", "historical_continuity_archive_invalid"),
    ("blocked", "historical_continuity_history_incomplete", "historical_continuity_history_incomplete"),
    ("blocked", "PRIVATE_ACCOUNT_123456789", "historical_readonly_failed"),
    ("value", "PRIVATE_BALANCE_123456789", "historical_readonly_failed"),
    ("type", "PRIVATE_ACCOUNT_123456789", "migration_blocked"),
    ("type", "historical_readonly_owned", "migration_blocked"),
    ("partial", "", None),
    ("success", "", None),
])
def test_real_script_entry_preserves_only_fixed_diagnostic_errors(tmp_path,monkeypatch,exception,reason,expected):
    # Importing the helper creates the named MigrationBlocked class. Running the
    # actual CLI then creates __main__.MigrationBlocked: do not replace this with
    # an imported main() call, which would miss the production launcher defect.
    install_production_context(monkeypatch)
    attempted = tmp_path / "unexpected-network"
    startup = tmp_path / "sitecustomize.py"
    startup.write_text(
        "from scripts import historical_continuity_readonly as diagnostic\n"
        "import socket\n"
        "from pathlib import Path\n"
        "class NoNetwork(socket.socket):\n"
        "    def connect(self, *args, **kwargs):\n"
        f"        Path({str(attempted)!r}).touch()\n"
        "        raise AssertionError('external connection forbidden')\n"
        "    connect_ex = connect\n"
        "socket.socket = NoNetwork\n"
        "def synthetic_result(**kwargs):\n"
        f"    kind, reason = {exception!r}, {reason!r}\n"
        "    if kind == 'success':\n"
        "        return dict(status='diagnosed', no_order=True, write_performed=False, execution_authority_granted=False)\n"
        "    if kind == 'partial':\n"
        "        return dict(status='blocked',reason_code='historical_continuity_unapproved_reward_product',\n"
        "            reward_product_failure_stage='reward_identity_missing',\n"
        "            source_kind='historical_continuity',classification_scope='partial_history',\n"
        "            no_order=True,write_performed=False,execution_authority_granted=False,\n"
        "            historical_difference_unresolved=True,ledger_unchanged=True,control_unchanged=True,\n"
        "            archive_unchanged=True,owner_absent=True,chunk_count=2,\n"
        "            reward_product_classification_counts={'lifecycle_evidence_incomplete':1})\n"
        "    error = {'blocked':diagnostic.migration.MigrationBlocked, 'value':ValueError, 'type':TypeError}[kind]\n"
        "    raise error(reason)\n"
        "diagnostic.run = synthetic_result\n"
    )
    env = dict(os.environ, PYTHONPATH=str(tmp_path)+os.pathsep+str(migration.ROOT))
    result = subprocess.run([str(migration.ROOT/'.venv/bin/python'),
                             'scripts/migrate_daily_accounting_state.py',diagnostic.ACTION],
                            cwd=migration.ROOT,env=env,capture_output=True,text=True,check=False)
    assert not attempted.exists()
    assert result.stderr == '' and 'PRIVATE' not in result.stdout and '123456789' not in result.stdout
    output = json.loads(result.stdout)
    assert output['no_order'] is True
    if exception == 'partial':
        assert result.returncode == 2 and output['status'] == 'blocked'
        projection = diagnostic.project_result(output,exit_code=result.returncode)
        assert projection['status'] == 'blocked' and projection['execution_permitted'] is False
        assert projection['classification_scope'] == 'partial_history' and projection['chunk_count'] == 2
        assert projection['reward_product_failure_stage'] == 'reward_identity_missing'
        assert projection['reward_product_classification_counts']['lifecycle_evidence_incomplete'] == 1
        assert projection['reward_product_classification_counts']['opening_approved'] is None
    elif expected is None:
        assert result.returncode == 0 and output['status'] == 'diagnosed'
        assert output['write_performed'] is False and output['execution_authority_granted'] is False
    else:
        assert result.returncode == 2 and output['status'] == 'blocked'
        assert output['reason_code'] == expected
