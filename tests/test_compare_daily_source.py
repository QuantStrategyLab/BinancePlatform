import hashlib
import json

import pytest

from application.account_facts import build_source_binding_id
from scripts import compare_daily_source as module
from scripts.read_binance_account_facts import APPROVED_APPLICATION_SHA


def fixture():
    target = {"service_name": "synthetic-service", "strategy_profile": "synthetic-profile", "account_scope": "live",
              "account_selector": ["synthetic-account"], "deployment_selector": "synthetic-deployment"}
    scope, revision = "b" * 64, "a" * 40
    source = build_source_binding_id(account_scope_sha256=scope, reader_public_revision=revision,
                                     approved_application_revision=APPROVED_APPLICATION_SHA)
    binding = {"platform": "binance", "account_key": "synthetic-key", "account_scope": "live",
               "target_name": "synthetic-target-name", "service_name": "", "deployment_selector": "synthetic-deployment",
               "account_selector": "synthetic-account", "target_id": "synthetic-target", "account_scope_sha256": scope,
               "reader_revision": revision, "approved_application_revision": APPROVED_APPLICATION_SHA,
               "source_binding": {"kind": "binance_readonly_scope_revision", "id": source}}
    env = {"RUNTIME_TARGET_JSON": json.dumps(target), "BINANCE_ACCOUNT_FACTS_BINDING_JSON": json.dumps(binding),
           "BINANCE_ACCOUNT_FACTS_READER_REVISION": revision, "RUNTIME_DAILY_COMPARE_NONCE": "c" * 64}
    return env, binding, source


def test_same_wallet_target_produces_only_nonce_digest_without_old_reconciliation_config():
    env, _binding, source = fixture()
    result = module.compare_source(env)
    expected = {"binding": {"platform": "binance", "service_name": "synthetic-service", "strategy_profile": "synthetic-profile",
                "account_scope": "live", "account_selector": "synthetic-account", "deployment_selector": "synthetic-deployment",
                "source_binding_id": source, "target_id": "synthetic-target"},
                "domain": "qsl.binance-daily-binding.v1", "nonce": "c" * 64}
    assert result == hashlib.sha256(json.dumps(expected, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert "synthetic-account" not in result and source not in result


@pytest.mark.parametrize("case", ["bad_nonce", "wrong_selector", "wrong_binding", "missing_target_service"])
def test_unbound_or_changed_source_is_rejected(case):
    env, binding, _source = fixture()
    if case == "bad_nonce": env["RUNTIME_DAILY_COMPARE_NONCE"] = "invalid"
    if case == "wrong_selector": binding["account_selector"] = "other"
    if case == "wrong_binding": binding["source_binding"]["id"] = "0" * 64
    if case == "missing_target_service":
        target = json.loads(env["RUNTIME_TARGET_JSON"])
        target["service_name"] = ""
        env["RUNTIME_TARGET_JSON"] = json.dumps(target)
    env["BINANCE_ACCOUNT_FACTS_BINDING_JSON"] = json.dumps(binding)
    with pytest.raises(ValueError):
        module.compare_source(env)
