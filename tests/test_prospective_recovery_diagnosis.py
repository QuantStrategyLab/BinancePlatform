"""Read-only diagnosis for the approved prospective Binance opening."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quant_platform_kit.common.broker_reconciliation import (
    calculate_broker_observation_sha256 as digest,
)
from tests.test_broker_reconciliation import _target
from tests.test_forward_earn_accounting import materials
from tests.test_post_rebase_recovery import Ref


OPENING_AT = datetime(2026, 9, 12, 11, 4, 4, 281392, tzinfo=timezone.utc)
NOW = OPENING_AT + timedelta(minutes=20)
LATER = NOW + timedelta(seconds=1)


def _material(monkeypatch):
    from application import rebased_recovery as recovery
    from scripts.migrate_daily_accounting_state import _ACCOUNTING_FIELDS

    state, _current, _cash = materials()
    checkpoint = copy.deepcopy(state["earn_accrual_checkpoint"])
    checkpoint["observed_at"] = OPENING_AT.isoformat()
    checkpoint["account_scope_sha256"] = digest({"account_uid": "synthetic"})
    state["earn_accrual_checkpoint"] = checkpoint
    state["external_cash_flow_cursor"]["observed_at"] = OPENING_AT.isoformat()
    old = {
        "is_circuit_broken": True,
        "order_submission": {"state": "TERMINAL"},
        "unknown_future_field": {"keep": True},
        "accounting_rebase": {"archive_document": "preserved-old-archive"},
    }
    control = {
        "state": "RECONCILE_ONLY",
        "source": {"original_evidence": {"account_scope_sha256": checkpoint["account_scope_sha256"]}},
    }
    proposed = {key: copy.deepcopy(state.get(key, 0)) for key in _ACCOUNTING_FIELDS}
    proposed.update(
        earn_accrual_checkpoint=copy.deepcopy(state["earn_accrual_checkpoint"]),
        earn_accounted_net_changes=copy.deepcopy(state["earn_accounted_net_changes"]),
        external_cash_flow_cursor=copy.deepcopy(state["external_cash_flow_cursor"]),
    )
    proposal = {
        "source_sha": "c625413dcc601412358efbb5373e38788da95d0e",
        "ledger_sha256": digest(old),
        "control_sha256": digest(control),
        "ledger_update_time": "2026-09-12T11:03:00Z",
        "proposed_fields": proposed,
        "historical_difference_unresolved": True,
    }
    marker = {
        "archive_document": recovery.PROSPECTIVE_ARCHIVE_DOCUMENT,
        "started_at": "2026-09-12T11:19:09.803040+00:00",
        "opening_balance_observed_at": OPENING_AT.isoformat(),
        "historical_difference_unresolved": True,
        "approved_proposal_run_id": recovery.PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID,
        "approved_proposal_sha256": digest(proposal),
    }
    ledger = {**old, **proposed, "accounting_rebase": marker}
    archive = {
        "ledger": copy.deepcopy(old),
        "recovery_control": copy.deepcopy(control),
        "ledger_update_time": proposal["ledger_update_time"],
        **marker,
        "approved_proposal": copy.deepcopy(proposal),
        "new_ledger_sha256": digest(ledger),
        "valuation_price_source": "binance_get_avg_price_estimate",
    }
    monkeypatch.setattr(recovery, "APPROVED_PROSPECTIVE_SHA256", digest(proposal))
    monkeypatch.setattr(recovery, "PROSPECTIVE_LEDGER_SHA256", digest(ledger))
    monkeypatch.setattr(recovery, "PROSPECTIVE_ARCHIVE_SHA256", digest(archive))
    return ledger, archive, control


class Client:
    def __init__(self, change=None):
        self.change = change
        self.earn_reads = 0
        self.flow_reads = 0
        self.account_reads = 0

    def get_account(self):
        self.account_reads += 1
        uid = "changed" if self.change == "account" else "synthetic"
        bnb_spot = "1.1" if self.change == "final_spot" and self.account_reads >= 3 else "1"
        return {
            "uid": uid,
            "balances": [
                {"asset": "USDT", "free": "100", "locked": "0"},
                {"asset": "BNB", "free": bnb_spot, "locked": "0"},
            ],
        }

    def get_simple_earn_flexible_product_position(self, *, current, size):
        assert current == 1 and size == 100
        self.earn_reads += 1
        increment = (
            3 - self.earn_reads if self.change == "counter_between_reads" else self.earn_reads
        )
        reward = "0" if self.change == "counter" else f"0.1000000{increment}"
        total = f"2.0000000{increment}"
        return {
            "total": 1,
            "rows": [{
                "asset": "BNB",
                "productId": "BNB001",
                "totalAmount": total,
                "cumulativeRealTimeRewards": reward,
                "collateralAmount": "0",
                "autoSubscribe": True,
                "canRedeem": True,
            }],
        }

    def get_open_orders(self):
        if self.change != "order":
            return []
        return [{"orderId": 1, "symbol": "BNBUSDT", "status": "NEW", "side": "BUY",
                 "type": "LIMIT", "origQty": "1", "executedQty": "0", "updateTime": 1}]

    def get_my_trades(self, **_kwargs):
        if self.change != "trade":
            return []
        return [{"id": 1, "orderId": 1, "symbol": "BNBUSDT", "qty": "1", "price": "1",
                 "commission": "0", "commissionAsset": "BNB", "time": 1, "isBuyer": True}]

    def _request_margin_api(self, method, path, **kwargs):
        assert method == "get" and kwargs["signed"] is True
        if path == "capital/deposit/hisrec":
            return []
        if path == "capital/withdraw/history":
            self.flow_reads += 1
            if self.change == "flow":
                return [{"id": "new-withdrawal", "status": 6}]
            return []
        raise AssertionError(f"unexpected path {path}")


def _run(monkeypatch, *, change=None, ledger_change=None, archive_change=None):
    from application.rebased_recovery import collect_prospective_rebase_diagnosis

    ledger, archive, control = _material(monkeypatch)
    if ledger_change:
        ledger[ledger_change] = "tampered"
    if archive_change:
        archive[archive_change] = "tampered"
    before = copy.deepcopy(ledger)
    result = collect_prospective_rebase_diagnosis(
        client=Client(change),
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        ledger=ledger,
        archive=archive,
        symbols=("BNBUSDT",),
        source_run={"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                    "event": "workflow_dispatch", "path": ".github/workflows/main.yml"},
        migration_run={"id": 34690695663, "head_sha": "a3ef5660e6d25fcfd5a7dedd10536a32eedde203",
                       "head_branch": "main", "event": "workflow_dispatch",
                       "path": ".github/workflows/main.yml"},
        now=NOW,
        clock=lambda: LATER,
    )
    assert ledger == before
    return result


def test_prospective_diagnosis_accepts_income_growth_without_mutating_ledger(monkeypatch):
    result = _run(monkeypatch)

    assert result == {
        "status": "diagnosed",
        "source_kind": "prospective_rebase",
        "historical_difference_unresolved": True,
        "forward_accounting_conserved": True,
        "checkpoint_samples": 2,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"ledger_change": "unknown_future_field"}, "prospective_rebase_ledger_invalid"),
        ({"archive_change": "unexpected"}, "prospective_rebase_archive_invalid"),
        ({"change": "account"}, "prospective_rebase_checkpoint_unavailable"),
        ({"change": "counter"}, "prospective_rebase_conservation_unverified"),
        ({"change": "counter_between_reads"}, "prospective_rebase_conservation_unverified"),
        ({"change": "flow"}, "prospective_rebase_conservation_unverified"),
        ({"change": "order"}, "prospective_rebase_open_orders_present"),
        ({"change": "trade"}, "prospective_rebase_recent_executions_present"),
        ({"change": "final_spot"}, "prospective_rebase_spot_changed_during_read"),
    ],
)
def test_prospective_diagnosis_fails_closed_on_material_or_activity_change(monkeypatch, kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        _run(monkeypatch, **kwargs)


@pytest.mark.parametrize("changed_during_read", [False, True])
def test_controller_prospective_diagnosis_readback_is_stable_and_never_writes(
    monkeypatch, changed_during_read
):
    from scripts import binance_recovery_controller as controller

    ledger, archive, control = _material(monkeypatch)
    target = _target()
    expected = {"account_scope_sha256": digest({"account_uid": "synthetic"})}
    for name, value in {
        "GITHUB_REPOSITORY": controller.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": controller.REPOSITORY + "/.github/workflows/main.yml@refs/heads/main",
        "RUNTIME_TARGET_ENABLED": "false",
        "RECONCILE_ONLY": "true",
        "GITHUB_RUN_ID": "400",
        "GITHUB_SHA": "b" * 40,
        "BINANCE_API_KEY": "synthetic",
        "BINANCE_API_SECRET": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(controller, "FROZEN_EXPECTED_SHA256", digest(expected))
    monkeypatch.setattr(controller, "BASELINE_ID", target.live_continuity.baseline_id)
    monkeypatch.setattr(controller, "BASELINE_TARGET_SHA256", target.live_continuity.baseline_target_sha256)
    monkeypatch.setattr(controller, "resolve_runtime_target_from_env", lambda **_: target)
    monkeypatch.setattr(controller, "_expected_digests", lambda: expected)
    monkeypatch.setattr(controller, "_symbols_from_env", lambda: ["BNBUSDT"])
    monkeypatch.setattr(controller, "MANAGED_SYMBOLS_SHA256", digest(["BNBUSDT"]))
    monkeypatch.setattr(controller, "datetime", SimpleNamespace(now=lambda _tz: NOW))
    current_run = {"id": 400, "head_sha": "b" * 40, "head_branch": "main",
                   "event": "workflow_dispatch", "path": ".github/workflows/main.yml"}
    migration_run = {"id": controller.PROSPECTIVE_MIGRATION_RUN_ID,
                     "head_sha": controller.PROSPECTIVE_MIGRATION_RUN_SHA,
                     "head_branch": "main", "event": "workflow_dispatch",
                     "path": ".github/workflows/main.yml"}
    monkeypatch.setattr(controller, "verified_run",
                        lambda run_id, **_kwargs: migration_run if int(run_id) == migration_run["id"] else current_run)
    monkeypatch.setattr(controller, "connect_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(controller, "prospective_clock", lambda: LATER)
    class ArchiveRef(Ref):
        def __init__(self, value):
            super().__init__(value)
            self.reads = 0

        def get(self, **kwargs):
            self.reads += 1
            if changed_during_read and self.reads > 1:
                changed = self.snapshot.to_dict()
                changed["changed"] = True
                return SimpleNamespace(exists=True, to_dict=lambda: changed)
            return super().get(**kwargs)

    docs = {
        controller.CONTROL_DOCUMENT: Ref(control),
        "MULTI_ASSET_STATE": Ref(ledger),
        "MULTI_ASSET_STATE__owner": Ref(None),
        controller.ARCHIVE_DOCUMENT: Ref(None),
        controller.PROSPECTIVE_ARCHIVE_DOCUMENT: ArchiveRef(archive),
    }
    db = SimpleNamespace(collection=lambda _name: SimpleNamespace(document=lambda name: docs[name]))
    monkeypatch.setattr(controller, "get_firestore_client", lambda: db)
    monkeypatch.setattr(controller, "save_post_rebase_control",
                        lambda *_args, **_kwargs: pytest.fail("diagnosis wrote control"))

    if changed_during_read:
        with pytest.raises(ValueError, match="prospective_rebase_state_changed_during_read"):
            controller.run("diagnose")
        return

    result = controller.run("diagnose")

    assert result["status"] == "diagnosed"
    assert result["ledger_unchanged"] is True
    assert result["control_unchanged"] is True
    assert result["owner_absent"] is True
    assert result["archive_unchanged"] is True
    assert result["write_performed"] is False


def _source_run(run_id=400, sha="b" * 40):
    return {"id": run_id, "head_sha": sha, "head_branch": "main",
            "event": "workflow_dispatch", "path": ".github/workflows/main.yml"}


def _migration_run():
    from application import rebased_recovery as recovery

    return {"id": recovery.PROSPECTIVE_MIGRATION_RUN_ID,
            "head_sha": recovery.PROSPECTIVE_MIGRATION_RUN_SHA,
            "head_branch": "main", "event": "workflow_dispatch",
            "path": ".github/workflows/main.yml"}


@pytest.mark.parametrize(
    "status,conclusion,accepted,allowed",
    [
        ("completed", "success", ("success",), True),
        ("completed", "failure", ("failure",), True),
        ("in_progress", None, ("success", "failure"), False),
        ("completed", "cancelled", ("success", "failure"), False),
    ],
)
def test_verified_run_requires_declared_completed_outcome(
    monkeypatch, status, conclusion, accepted, allowed
):
    from scripts import binance_recovery_controller as controller

    value = {
        **_source_run(),
        "status": status,
        "conclusion": conclusion,
        "repository": {"full_name": controller.REPOSITORY},
    }
    monkeypatch.setattr(controller, "request_json", lambda *_args, **_kwargs: value)

    if allowed:
        assert controller.verified_run(
            400, expected_sha="b" * 40, accepted_conclusions=accepted
        ) == _source_run()
    else:
        with pytest.raises(ValueError, match="recovery_source_workflow_unverified"):
            controller.verified_run(
                400, expected_sha="b" * 40, accepted_conclusions=accepted
            )


def test_verified_run_rejects_wrong_repository_even_for_terminal_failure(monkeypatch):
    from scripts import binance_recovery_controller as controller

    value = {
        **_source_run(),
        "status": "completed",
        "conclusion": "failure",
        "repository": {"full_name": "other/repository"},
    }
    monkeypatch.setattr(controller, "request_json", lambda *_args, **_kwargs: value)

    with pytest.raises(ValueError, match="recovery_source_workflow_unverified"):
        controller.verified_run(
            400, expected_sha="b" * 40, accepted_conclusions=("failure",)
        )


def _package(monkeypatch, *, client=None, now=NOW, later=LATER):
    from application.rebased_recovery import collect_prospective_rebase_source

    ledger, archive, _control = _material(monkeypatch)
    expected = {"account_scope_sha256": digest({"account_uid": "synthetic"})}
    return collect_prospective_rebase_source(
        client=client or Client(), runtime_target=_target(), legacy_expected=expected,
        ledger=ledger, archive=archive, symbols=("BNBUSDT",),
        source_run=_source_run(), migration_run=_migration_run(), now=now,
        clock=lambda: later,
    )


def test_prospective_source_enrolls_real_spot_with_forward_proof(monkeypatch):
    from application.rebased_recovery import validate_prospective_rebase_source

    package = _package(monkeypatch)
    candidate = validate_prospective_rebase_source(
        package, runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        now=LATER,
    )

    assert candidate.expected_digests["positions_sha256"] == package["source"]["proof"]["spot_sha256"]
    assert candidate.local_execution_ledger_sha256 == package["source"]["new_ledger_sha256"]
    assert package["source"]["proof"]["forward_accounting_conserved"] is True
    assert package["source"]["proof"]["whole_spot_double_read_match"] is True
    serialized = str(package)
    for private_name in ("spot_free", "spot_locked", "totalAmount", "account_uid"):
        assert private_name not in serialized


def test_prospective_source_rejects_forged_proof_and_final_spot_drift(monkeypatch):
    from application.rebased_recovery import validate_prospective_rebase_source

    with pytest.raises(ValueError, match="prospective_rebase_spot_changed_during_read"):
        _package(monkeypatch, client=Client("final_spot"))
    package = _package(monkeypatch)
    package["source"]["proof"]["forward_accounting_conserved"] = False
    with pytest.raises(ValueError, match="prospective_rebase_source_binding_mismatch"):
        validate_prospective_rebase_source(
            package, runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            now=LATER,
        )
    package = _package(monkeypatch)
    package["source"]["proof"] = "forged"
    with pytest.raises(ValueError, match="prospective_rebase_source_binding_mismatch"):
        validate_prospective_rebase_source(
            package, runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            now=LATER,
        )


def test_prospective_source_retains_existing_thirty_minute_freshness(monkeypatch):
    from application.rebased_recovery import validate_prospective_rebase_source

    package = _package(monkeypatch)
    with pytest.raises(ValueError, match="prospective_rebase_candidate_source_mismatch"):
        validate_prospective_rebase_source(
            package, runtime_target=_target(),
            legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
            now=LATER + timedelta(minutes=31),
        )


def _controller_setup(monkeypatch, *, prepared=False, client_change=None, confirmation=True,
                      owner=False, action_delay=timedelta(minutes=2),
                      stored_status="completed", stored_conclusion="success"):
    from scripts import binance_recovery_controller as controller
    from tests.test_post_rebase_recovery import _confirmation_response

    ledger, archive, original_control = _material(monkeypatch)
    expected = {"account_scope_sha256": digest({"account_uid": "synthetic"})}
    target = _target()
    recovery_id = "binance-400-1"
    control = original_control
    if prepared:
        package = _package(monkeypatch)
        control = {"state": "RECONCILE_ONLY", "recovery_id": recovery_id, **package}
    action_now = NOW + action_delay
    action_later = action_now + timedelta(seconds=1)
    current_run = _source_run(401, "c" * 40)
    runs = {400: _source_run(), 401: current_run,
            controller.PROSPECTIVE_MIGRATION_RUN_ID: _migration_run()}
    outcomes = {
        400: (stored_status, stored_conclusion),
        401: ("in_progress", None),
        controller.PROSPECTIVE_MIGRATION_RUN_ID: ("completed", "success"),
    }
    for name, value in {
        "GITHUB_REPOSITORY": controller.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": controller.REPOSITORY + "/.github/workflows/main.yml@refs/heads/main",
        "RUNTIME_TARGET_ENABLED": "false", "RECONCILE_ONLY": "true",
        "GITHUB_RUN_ID": "401", "GITHUB_SHA": "c" * 40,
        "GITHUB_RUN_ATTEMPT": "1", "BINANCE_API_KEY": "synthetic",
        "BINANCE_API_SECRET": "synthetic", "RECONCILIATION_RECOVERY_SYNC_TOKEN": "synthetic",
        "RECONCILIATION_RECOVERY_CONTROLLER_TOKEN": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(controller, "FROZEN_EXPECTED_SHA256", digest(expected))
    monkeypatch.setattr(controller, "BASELINE_ID", target.live_continuity.baseline_id)
    monkeypatch.setattr(controller, "BASELINE_TARGET_SHA256",
                        target.live_continuity.baseline_target_sha256)
    monkeypatch.setattr(controller, "resolve_runtime_target_from_env", lambda **_: target)
    monkeypatch.setattr(controller, "_expected_digests", lambda: expected)
    monkeypatch.setattr(controller, "_symbols_from_env", lambda: ["BNBUSDT"])
    monkeypatch.setattr(controller, "MANAGED_SYMBOLS_SHA256", digest(["BNBUSDT"]))
    monkeypatch.setattr(controller, "datetime", SimpleNamespace(now=lambda _tz: action_now))
    monkeypatch.setattr(controller, "prospective_clock", lambda: action_later)
    real_activation_evaluation = controller.evaluate_reconciliation_recovery_activation
    monkeypatch.setattr(
        controller,
        "evaluate_reconciliation_recovery_activation",
        lambda **kwargs: real_activation_evaluation(**kwargs, now=action_later),
    )
    def verified(run_id, *, completed=True, accepted_conclusions=("success",), **_kwargs):
        run_id = int(run_id)
        status, conclusion = outcomes[run_id]
        if completed and (status != "completed" or conclusion not in accepted_conclusions):
            raise ValueError("recovery_source_workflow_unverified")
        return runs[run_id]

    monkeypatch.setattr(controller, "verified_run", verified)
    monkeypatch.setattr(controller, "connect_client", lambda *_args, **_kwargs: Client(client_change))
    docs = {
        controller.CONTROL_DOCUMENT: Ref(control),
        "MULTI_ASSET_STATE": Ref(ledger),
        "MULTI_ASSET_STATE__owner": Ref({"owner": "busy"} if owner else None),
        controller.ARCHIVE_DOCUMENT: Ref(None),
        controller.PROSPECTIVE_ARCHIVE_DOCUMENT: Ref(archive),
    }
    writes = []

    class Transaction:
        def set(self, ref, value):
            writes.append(copy.deepcopy(value))
            ref.snapshot = type(ref.snapshot)(value)

    db = SimpleNamespace(collection=lambda _name: SimpleNamespace(document=lambda name: docs[name]))
    monkeypatch.setattr(controller, "get_firestore_client", lambda: db)
    monkeypatch.setattr(controller.firestore, "transactional", lambda fn: fn, raising=False)
    monkeypatch.setattr(controller, "_post_rebase_transaction", lambda _db: Transaction())
    requests = []

    def request(_url, _token, *, payload=None):
        requests.append(payload)
        if payload is not None:
            return {"ok": True, "source_id": payload["source_id"], "recovery_count": 1,
                    "generated_at": payload["generated_at"]}
        if not confirmation:
            return {}
        return _confirmation_response(
            control["candidate"], recovery_id, NOW + timedelta(minutes=1)
        )

    monkeypatch.setattr(controller, "request_json", request)
    return controller, target, expected, recovery_id, docs, writes, requests


def test_controller_prepare_replaces_old_root_with_prospective_source(monkeypatch):
    controller, _target_value, _expected, _recovery_id, docs, writes, requests = (
        _controller_setup(monkeypatch)
    )

    result = controller.run("prepare")

    assert result["status"] == "awaiting_human_confirmation"
    assert result["source_kind"] == "prospective_rebase"
    assert docs[controller.CONTROL_DOCUMENT].snapshot.value["source"]["kind"] == "prospective_rebase"
    assert len(writes) == 1 and len(requests) == 1
    evidence_at = datetime.fromisoformat(
        docs[controller.CONTROL_DOCUMENT].snapshot.value["source"]["reconciled_evidence"]["observed_at"]
    )
    published_at = datetime.fromisoformat(requests[0]["generated_at"].replace("Z", "+00:00"))
    assert published_at >= evidence_at


def test_controller_prepare_does_not_overwrite_existing_candidate(monkeypatch):
    controller, _target_value, _expected, _recovery_id, _docs, writes, requests = (
        _controller_setup(monkeypatch, prepared=True)
    )

    with pytest.raises(ValueError, match="prospective_rebase_prepare_control_changed"):
        controller.run("prepare")
    assert not writes and not requests


def test_controller_prepare_replaces_only_expired_strict_candidate(monkeypatch):
    from tests.test_post_rebase_recovery import _confirmation_response

    controller, _target_value, _expected, old_recovery_id, docs, writes, requests = (
        _controller_setup(
            monkeypatch,
            prepared=True,
            action_delay=timedelta(minutes=32),
        )
    )
    old_candidate = copy.deepcopy(
        docs[controller.CONTROL_DOCUMENT].snapshot.value["candidate"]
    )

    result = controller.run("prepare")

    assert result["status"] == "awaiting_human_confirmation"
    assert result["recovery_id"] != old_recovery_id
    assert result["candidate_sha256"] != old_candidate["candidate_sha256"]
    assert len(writes) == 1 and len(requests) == 1
    new_candidate = controller.validate_prospective_rebase_source(
        docs[controller.CONTROL_DOCUMENT].snapshot.value,
        runtime_target=_target(),
        legacy_expected={"account_scope_sha256": digest({"account_uid": "synthetic"})},
        require_fresh=False,
    )
    with pytest.raises(ValueError, match="recovery_confirmation_binding_invalid"):
        controller.verify_confirmation(
            _confirmation_response(
                old_candidate, old_recovery_id, NOW + timedelta(minutes=1)
            ),
            recovery_id=result["recovery_id"],
            candidate=new_candidate,
        )
    assert len(writes) == 1


def test_controller_prepare_replaces_terminal_failed_strict_candidate(monkeypatch):
    controller, _target_value, _expected, old_recovery_id, docs, writes, requests = (
        _controller_setup(monkeypatch, prepared=True, stored_conclusion="failure")
    )
    old_candidate_sha256 = docs[controller.CONTROL_DOCUMENT].snapshot.value["candidate"][
        "candidate_sha256"
    ]

    result = controller.run("prepare")

    assert result["status"] == "awaiting_human_confirmation"
    assert result["recovery_id"] != old_recovery_id
    assert result["candidate_sha256"] != old_candidate_sha256
    assert len(writes) == 1 and len(requests) == 1


@pytest.mark.parametrize(
    "stored_status,stored_conclusion",
    [("in_progress", None), ("completed", "cancelled")],
)
def test_controller_prepare_rejects_non_terminal_failure_source(
    monkeypatch, stored_status, stored_conclusion
):
    controller, _target_value, _expected, _recovery_id, _docs, writes, requests = (
        _controller_setup(
            monkeypatch,
            prepared=True,
            stored_status=stored_status,
            stored_conclusion=stored_conclusion,
        )
    )

    with pytest.raises(ValueError, match="prospective_rebase_prepare_control_changed"):
        controller.run("prepare")
    assert not writes and not requests


@pytest.mark.parametrize("field", ["confirmation", "transition_plan"])
def test_controller_prepare_rejects_candidate_with_review_or_transition(
    monkeypatch, field
):
    controller, _target_value, _expected, _recovery_id, docs, writes, requests = (
        _controller_setup(monkeypatch, prepared=True, stored_conclusion="failure")
    )
    docs[controller.CONTROL_DOCUMENT].snapshot.value[field] = {"present": True}

    with pytest.raises(ValueError, match="prospective_rebase_prepare_control_changed"):
        controller.run("prepare")
    assert not writes and not requests


@pytest.mark.parametrize("action", ["verify", "activate"])
def test_controller_never_verifies_or_activates_failed_source(monkeypatch, action):
    controller, _target_value, _expected, recovery_id, _docs, writes, requests = (
        _controller_setup(monkeypatch, prepared=True, stored_conclusion="failure")
    )

    with pytest.raises(ValueError, match="recovery_source_workflow_unverified"):
        controller.run(action, recovery_id)
    assert not writes and not requests


def test_controller_diagnose_accepts_only_original_or_strict_prospective_control(monkeypatch):
    controller, _target_value, _expected, _recovery_id, docs, writes, requests = (
        _controller_setup(monkeypatch, prepared=True)
    )
    assert controller.run("diagnose")["status"] == "diagnosed"
    docs[controller.CONTROL_DOCUMENT].snapshot.value["source"]["proof"] = "forged"
    with pytest.raises(ValueError, match="prospective_rebase_source_binding_mismatch"):
        controller.run("diagnose")
    assert not writes and not requests


@pytest.mark.parametrize("action", ["verify", "activate"])
def test_controller_prospective_confirmation_rechecks_and_runtime_consumes_active(
    monkeypatch, action
):
    from application.reconciliation_recovery import activated_target

    controller, target, expected, recovery_id, docs, writes, requests = _controller_setup(
        monkeypatch, prepared=True
    )

    result = controller.run(action, recovery_id)

    assert result["status"] == ("verified" if action == "verify" else "active_lkg")
    assert result["source_kind"] == "prospective_rebase"
    assert result["runtime_target_enabled"] is False
    assert len(requests) == 1
    if action == "verify":
        assert not writes
    else:
        assert len(writes) == 1
        active = docs[controller.CONTROL_DOCUMENT].snapshot.value
        assert active["state"] == "ACTIVE_LKG"
        assert activated_target(target, active, expected=expected).live_continuity.state == "ACTIVE_LKG"
        active["source"]["proof"] = "forged"
        with pytest.raises(ValueError, match="prospective_rebase_source_binding_mismatch"):
            activated_target(target, active, expected=expected)


@pytest.mark.parametrize(
    "setup_kwargs,reason",
    [
        ({"confirmation": False}, "recovery_confirmation_policy_invalid"),
        ({"client_change": "final_spot"}, "prospective_rebase_spot_changed_during_read"),
        ({"owner": True}, "recovery_ledger_unavailable_or_owned"),
    ],
)
def test_controller_prospective_confirmation_fails_closed(monkeypatch, setup_kwargs, reason):
    controller, _target_value, _expected, recovery_id, _docs, writes, _requests = (
        _controller_setup(monkeypatch, prepared=True, **setup_kwargs)
    )

    with pytest.raises(ValueError, match=reason):
        controller.run("activate", recovery_id)
    assert not writes
