"""Offline economic-accounting and recovery-boundary regressions."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from quant_platform_kit.common.broker_reconciliation import calculate_broker_observation_sha256
from application.broker_reconciliation import build_reconciliation_candidate, collect_read_only_reconciliation_observations
from tests.test_broker_reconciliation import _target
from application.reconciliation_recovery import collect_recovery_source, validate_source

NOW = datetime(2026, 9, 8, 17, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=5)


class Client:
    def __init__(self):
        self.bonus = True
        self.account_reads = 0
        self.open_orders = []
        self.history_incomplete = False

    def get_account(self):
        self.account_reads += 1
        return {"uid": "123", "balances": [{"asset": "USDT", "free": "101" if self.bonus else "100", "locked": "0"}]}

    def get_open_orders(self):
        return self.open_orders

    def get_my_trades(self, **kwargs):
        return []

    def _request_margin_api(self, method, path, **kwargs):
        assert method == "get"
        if path.endswith("rewardsRecord"):
            return {"total": 1, "rows": [{"asset": "USDT", "type": "BONUS", "rewards": "1", "time": int((NOW - timedelta(hours=1)).timestamp() * 1000)}]}
        if path.endswith("hisrec") or path.endswith("withdraw/history"):
            return []
        return {"total": 1 if self.history_incomplete else 0}


def inputs():
    client = Client()
    client.bonus = False
    observations = collect_read_only_reconciliation_observations(client, strategy_symbols=["BTCUSDT"], local_execution_ledger={"cycle": 1}, now=NOW)
    draft = build_reconciliation_candidate(observations=observations, runtime_target=_target(), observed_at=NOW)
    expected = {key: value for key, value in draft.evidence.to_dict().items() if key.endswith("_sha256") and key not in {"baseline_target_sha256", "runtime_target_sha256", "evidence_sha256"}}
    client.bonus = True
    client.account_reads = 0
    args = dict(client=client, runtime_target=_target(), expected=expected, ledger={"cycle": 1}, symbols=["BTCUSDT"], history_start=START, now=NOW, source_run={"id": 123, "head_sha": "a" * 40, "head_branch": "main", "event": "workflow_dispatch", "path": ".github/workflows/main.yml"})
    return args


def test_bonus_proof_enrolls_fresh_snapshot_without_modifying_frozen_expected():
    args = inputs()
    frozen = copy.deepcopy(args["expected"])
    source = collect_recovery_source(**args)
    assert args["expected"] == frozen
    assert args["client"].account_reads == 2  # before/after complete history, exact same balance required
    assert source["candidate"]["positions_sha256"] != frozen["positions_sha256"]
    assert source["candidate"]["open_orders_sha256"] == frozen["open_orders_sha256"]
    assert '"free"' not in json.dumps(source)
    assert source["source"]["proof"]["spot_bonus_reconciliation"]["historical_balance_hashes_match"] is True
    validate_source(source, runtime_target=args["runtime_target"], expected=frozen, now=NOW)


@pytest.mark.parametrize("change", ["identity", "ledger", "unexplained", "incomplete", "concurrent_balance"])
def test_incomplete_or_unexplained_observations_never_enroll(change):
    args = inputs()
    if change == "identity":
        args["expected"]["account_scope_sha256"] = "f" * 64
    elif change == "ledger":
        args["ledger"] = {"cycle": 2}
    elif change == "unexplained":
        args["expected"]["positions_sha256"] = "f" * 64
    elif change == "incomplete":
        args["client"].history_incomplete = True
    elif change == "concurrent_balance":
        client = args["client"]
        original = client.get_account
        def changing():
            value = original()
            if client.account_reads > 1:
                value["balances"][0]["free"] = "102"
            return value
        client.get_account = changing
    with pytest.raises(ValueError):
        collect_recovery_source(**args)


def test_source_digest_is_not_a_substitute_for_verifying_frozen_config():
    args = inputs()
    source = collect_recovery_source(**args)
    changed = {**args["expected"], "cash_sha256": "f" * 64}
    with pytest.raises(ValueError):
        validate_source(source, runtime_target=args["runtime_target"], expected=changed, now=NOW)
    with pytest.raises(ValueError):
        validate_source(source, runtime_target=args["runtime_target"], expected=args["expected"], now=NOW + timedelta(minutes=31))


def test_candidate_payload_tampering_rejected():
    args = inputs()
    source = collect_recovery_source(**args)
    source["candidate"]["cash_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        validate_source(source, runtime_target=args["runtime_target"], expected=args["expected"], now=NOW)


def test_confirmation_requires_real_bounded_console_receipt():
    from application.reconciliation_recovery import verify_confirmation
    args = inputs()
    candidate = validate_source(collect_recovery_source(**args), runtime_target=args["runtime_target"], expected=args["expected"], now=NOW)
    for value in ({}, {"ok": True, "confirmation": {"confirmed_by": "operator"}}):
        with pytest.raises(ValueError):
            verify_confirmation(value, recovery_id="test-recovery", candidate=candidate)


def test_atomic_storage_does_not_rebaseline_with_an_active_owner():
    from scripts.binance_recovery_controller import compare_and_set_control
    class Ref:
        def __init__(self, data): self.data = data
        def get(self, **kwargs): return SimpleNamespace(exists=self.data is not None, to_dict=lambda: self.data)
    class Tx:
        def __init__(self): self.writes = []
        def set(self, *args): self.writes.append(args)
    tx = Tx()
    with pytest.raises(ValueError):
        compare_and_set_control(tx, control_ref=Ref(None), owner_ref=Ref({"owner_id": "running"}), ledger_ref=Ref({"cycle": 1}), previous=None, next_value={"state": "ACTIVE_LKG"}, ledger_sha256=calculate_broker_observation_sha256({"cycle": 1}))
    assert tx.writes == []


def test_atomic_storage_rejects_concurrent_ledger_or_control_changes():
    from scripts.binance_recovery_controller import compare_and_set_control
    class Ref:
        def __init__(self, data): self.data = data
        def get(self, **kwargs): return SimpleNamespace(exists=self.data is not None, to_dict=lambda: self.data)
    for ledger, control in (({"cycle": 2}, None), ({"cycle": 1}, {"state": "ACTIVE_LKG"})):
        tx = SimpleNamespace(set=lambda *_: pytest.fail("unexpected write"))
        with pytest.raises(ValueError):
            compare_and_set_control(tx, control_ref=Ref(control), owner_ref=Ref(None), ledger_ref=Ref(ledger), previous=None, next_value={}, ledger_sha256=calculate_broker_observation_sha256({"cycle": 1}))


def active_control():
    from quant_platform_kit.common.reconciliation_recovery import calculate_reconciliation_recovery_confirmation_sha256, ReconciliationRecoveryTransitionPlan
    args = inputs()
    package = collect_recovery_source(**args)
    candidate = package["candidate"]
    confirmation = {"schema_version": "qsl_reconciliation_recovery_confirmation.v1", "recovery_id": "binance-123-1", "candidate_sha256": candidate["candidate_sha256"], "dual_review_binding_sha256": candidate["candidate_sha256"], "confirmed_at": (NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "confirmed_by": "test-operator", "no_order": True, "execution_authority_granted": False}
    confirmation["confirmation_sha256"] = calculate_reconciliation_recovery_confirmation_sha256(confirmation)
    plan = ReconciliationRecoveryTransitionPlan(recovery_id="binance-123-1", candidate_sha256=candidate["candidate_sha256"], confirmation_sha256=confirmation["confirmation_sha256"], baseline_id=candidate["baseline_id"], baseline_target_sha256=candidate["baseline_target_sha256"], expected_digests={key: candidate[key] for key in args["expected"] if key != "account_scope_sha256"}, verified_at=NOW + timedelta(seconds=2))
    return args, {"state": "ACTIVE_LKG", "recovery_id": "binance-123-1", **package, "confirmation": confirmation, "transition_plan": plan.to_dict()}


def test_full_runtime_setup_consumes_recovery_with_empty_catalog_allowlist(monkeypatch):
    import runtime_config_support
    import strategy_registry
    import strategy_runtime
    import application.reconciliation_recovery as recovery
    args, control = active_control()
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps({key: value for key, value in args["runtime_target"].to_dict().items() if key in {"platform_id", "strategy_profile", "dry_run_only", "deployment_selector", "account_selector", "account_scope", "service_name", "live_continuity"}}))
    monkeypatch.setenv("STRATEGY_PROFILE", "crypto_live_pool_rotation")
    monkeypatch.setenv("BINANCE_DRY_RUN", "false")
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "true")
    monkeypatch.setattr(recovery, "load_activated_target", lambda target: recovery.activated_target(target, control, expected=args["expected"]))
    assert not strategy_registry.BINANCE_ENABLED_PROFILES
    runtime = runtime_config_support.build_live_runtime(now_utc=NOW)
    loaded = strategy_runtime.load_strategy_runtime(runtime.strategy_profile, runtime_target=runtime.runtime_target)
    assert loaded.profile == "crypto_live_pool_rotation"
    assert runtime.standard_execution_permitted is True
    import main
    monkeypatch.setattr(main, "bind_trade_state_access", lambda **_: (None, None, None, None))
    built = main.build_live_runtime(now_utc=NOW)
    assert built.standard_execution_permitted is True
    assert main.STRATEGY_RUNTIME.profile == "crypto_live_pool_rotation"
    # Loading an ordinary new profile still has no execution grant.
    with pytest.raises(ValueError):
        strategy_runtime.load_strategy_runtime(runtime.strategy_profile)
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    assert runtime_config_support.build_live_runtime(now_utc=NOW).standard_execution_permitted is False


@pytest.mark.parametrize("change", ["missing_control", "wrong_account", "wrong_profile", "wrong_confirmation"])
def test_runtime_setup_never_substitutes_catalog_eligibility_for_recovery(monkeypatch, change):
    import runtime_config_support
    import application.reconciliation_recovery as recovery
    args, control = active_control()
    target = {key: value for key, value in args["runtime_target"].to_dict().items() if key in {"platform_id", "strategy_profile", "dry_run_only", "deployment_selector", "account_selector", "account_scope", "service_name", "live_continuity"}}
    if change == "missing_control":
        control = None
    elif change == "wrong_account":
        args["expected"]["account_scope_sha256"] = "f" * 64
    elif change == "wrong_profile":
        target["strategy_profile"] = "crypto_equity_combo"
    else:
        control["confirmation"]["candidate_sha256"] = "f" * 64
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(target))
    monkeypatch.delenv("STRATEGY_PROFILE", raising=False)
    monkeypatch.setenv("BINANCE_DRY_RUN", "false")
    monkeypatch.setattr(recovery, "load_activated_target", lambda target: recovery.activated_target(target, control, expected=args["expected"]))
    with pytest.raises(ValueError):
        runtime_config_support.build_live_runtime(now_utc=NOW)


def test_active_override_requires_complete_matching_atomic_record():
    from application.reconciliation_recovery import activated_target
    args, control = active_control()
    result = activated_target(args["runtime_target"], control, expected=args["expected"])
    assert result.live_continuity.state == "ACTIVE_LKG"
    original = result.to_dict()
    original["live_continuity"]["state"] = "RECONCILE_ONLY"
    assert original == args["runtime_target"].to_dict()
    for key in ("candidate", "confirmation", "transition_plan", "source"):
        invalid = copy.deepcopy(control)
        invalid.pop(key)
        with pytest.raises((ValueError, KeyError)):
            activated_target(args["runtime_target"], invalid, expected=args["expected"])


def test_active_override_rejects_wrong_account_and_tampered_plan():
    from application.reconciliation_recovery import activated_target
    args, control = active_control()
    for expected, changes in (({**args["expected"], "account_scope_sha256": "f" * 64}, {}), (args["expected"], {"candidate_sha256": "f" * 64})):
        invalid = copy.deepcopy(control)
        invalid["transition_plan"].update(changes)
        with pytest.raises(ValueError):
            activated_target(args["runtime_target"], invalid, expected=expected)


def test_atomic_storage_writes_only_control_when_all_reads_match():
    from scripts.binance_recovery_controller import compare_and_set_control
    class Ref:
        def __init__(self, data): self.data = data
        def get(self, **kwargs): return SimpleNamespace(exists=self.data is not None, to_dict=lambda: self.data)
    writes = []
    control = Ref({"state": "RECONCILE_ONLY"})
    value = {"state": "ACTIVE_LKG"}
    compare_and_set_control(SimpleNamespace(set=lambda *args: writes.append(args)), control_ref=control,
        owner_ref=Ref(None), ledger_ref=Ref({"cycle": 1}), previous=control.data, next_value=value,
        ledger_sha256=calculate_broker_observation_sha256({"cycle": 1}))
    assert writes == [(control, value)]


def test_controller_failure_is_sanitized_and_not_retried(monkeypatch, capsys):
    from scripts import binance_recovery_controller as controller
    calls = []
    def fail(*args):
        calls.append(args)
        raise RuntimeError("private-provider-response-with-credentials")
    monkeypatch.setattr(controller, "run", fail)
    assert controller.main(["prepare"]) == 2
    output = capsys.readouterr()
    assert "private-provider" not in output.out + output.err
    assert len(calls) == 1


def test_confirmation_failure_never_collects_broker_or_applies_state(monkeypatch):
    from scripts import binance_recovery_controller as controller
    args, control = active_control()
    control["state"] = "RECONCILE_ONLY"
    for name, value in {"GITHUB_REPOSITORY": controller.REPOSITORY, "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_REF": controller.REPOSITORY + "/.github/workflows/main.yml@refs/heads/main", "RUNTIME_TARGET_ENABLED": "false", "RECONCILE_ONLY": "true", "GITHUB_RUN_ID": "124", "GITHUB_SHA": "a" * 40}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(controller, "FROZEN_EXPECTED_SHA256", calculate_broker_observation_sha256(args["expected"]))
    monkeypatch.setattr(controller, "BASELINE_ID", args["runtime_target"].live_continuity.baseline_id)
    monkeypatch.setattr(controller, "BASELINE_TARGET_SHA256", args["runtime_target"].live_continuity.baseline_target_sha256)
    monkeypatch.setattr(controller, "resolve_runtime_target_from_env", lambda **_: args["runtime_target"])
    monkeypatch.setattr(controller, "_expected_digests", lambda: args["expected"])
    monkeypatch.setattr(controller, "verified_run", lambda *a, **kw: control["source"]["run"])
    monkeypatch.setattr(controller, "validate_source", lambda *a, **kw: SimpleNamespace(**control["candidate"]))
    monkeypatch.setattr(controller, "_symbols_from_env", lambda: ["BTCUSDT"])
    monkeypatch.setattr(controller, "MANAGED_SYMBOLS_SHA256", calculate_broker_observation_sha256(["BTCUSDT"]))
    class Ref:
        def __init__(self, data): self.data = data
        def get(self, **kw): return SimpleNamespace(exists=self.data is not None, to_dict=lambda: self.data)
    docs = {controller.CONTROL_DOCUMENT: control, "MULTI_ASSET_STATE": args["ledger"], "MULTI_ASSET_STATE__owner": None}
    monkeypatch.setattr(controller, "get_firestore_client", lambda: SimpleNamespace(collection=lambda _: SimpleNamespace(document=lambda name: Ref(docs[name]))))
    monkeypatch.setattr(controller, "request_json", lambda *a, **kw: {"ok": False})
    monkeypatch.setattr(controller, "connect_client", lambda *a, **kw: pytest.fail("broker read before actual confirmation"))
    monkeypatch.setattr(controller, "_save_control", lambda *a, **kw: pytest.fail("write without confirmation"))
    with pytest.raises(ValueError, match="confirmation"):
        controller.run("activate", control["recovery_id"])


def test_console_request_identifies_the_platform_and_never_redirects(monkeypatch):
    from scripts import binance_recovery_controller as controller
    calls = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b'{"ok":true}'
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request)
            return Response()
    def build(handler):
        assert handler().redirect_request(None) is None
        return Opener()
    monkeypatch.setattr(controller, "build_opener", build)
    assert controller.request_json(controller.CONSOLE, "test-token", payload={}) == {"ok": True}
    assert len(calls) == 1
    assert calls[0].get_header("User-agent") == "QuantStrategyLab-BinancePlatform/1.0 (reconciliation-controller)"
    assert calls[0].get_header("Authorization") == "Bearer test-token"


def test_http_failure_reports_only_status_without_provider_body(monkeypatch, capsys):
    from urllib.error import HTTPError
    from scripts import binance_recovery_controller as controller
    def fail(*args):
        raise HTTPError(controller.CONSOLE, 403, "private provider message", {}, None)
    monkeypatch.setattr(controller, "run", fail)
    assert controller.main(["prepare"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["http_status"] == 403
    assert "private provider" not in json.dumps(result)
