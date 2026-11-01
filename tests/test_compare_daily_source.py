import hashlib
import json
from types import SimpleNamespace

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
    assert result == "518747e23ea953d8be51c1133c48bb37ed31348099faed4ffd5b38a18df9b700"
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


def test_status_only_checks_binding_without_disclosing_digest(monkeypatch, capsys):
    env, binding, source = fixture()
    env["RUNTIME_DAILY_COMPARE_STATUS_ONLY"] = "true"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert module.main() == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "verified"}
    assert source not in output and binding["account_selector"] not in output


@pytest.mark.parametrize("nonce", [None, ""])
def test_status_only_validates_without_nonce_or_digest(monkeypatch, capsys, nonce):
    env, binding, source = fixture()
    env["RUNTIME_DAILY_COMPARE_STATUS_ONLY"] = "true"
    if nonce is None:
        env.pop("RUNTIME_DAILY_COMPARE_NONCE")
        monkeypatch.delenv("RUNTIME_DAILY_COMPARE_NONCE", raising=False)
    else:
        env["RUNTIME_DAILY_COMPARE_NONCE"] = nonce
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, "hashlib", SimpleNamespace(sha256=lambda *_: pytest.fail("digest was computed")))
    assert module.main() == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "verified"}
    assert source not in output and binding["account_selector"] not in output and "digest" not in output


@pytest.mark.parametrize("nonce", [None, "", "A" * 64, "c" * 63])
def test_digest_mode_requires_lowercase_64_hex_nonce(nonce):
    env, _, _ = fixture()
    if nonce is None:
        env.pop("RUNTIME_DAILY_COMPARE_NONCE")
    else:
        env["RUNTIME_DAILY_COMPARE_NONCE"] = nonce
    with pytest.raises(ValueError, match="nonce_invalid"):
        module.compare_source(env)


@pytest.mark.parametrize("case", ["wrong_binding", "wrong_selector", "missing_target_service", "wrong_reader_revision"])
def test_status_only_failure_is_generic(monkeypatch, capsys, case):
    env, binding, source = fixture()
    if case == "wrong_binding": binding["source_binding"]["id"] = "0" * 64
    if case == "wrong_selector": binding["account_selector"] = "other"
    if case == "wrong_reader_revision": env["BINANCE_ACCOUNT_FACTS_READER_REVISION"] = "d" * 40
    if case == "missing_target_service":
        target = json.loads(env["RUNTIME_TARGET_JSON"])
        target["service_name"] = ""
        env["RUNTIME_TARGET_JSON"] = json.dumps(target)
    env["BINANCE_ACCOUNT_FACTS_BINDING_JSON"] = json.dumps(binding)
    env["RUNTIME_DAILY_COMPARE_STATUS_ONLY"] = "true"
    env.pop("RUNTIME_DAILY_COMPARE_NONCE")
    monkeypatch.delenv("RUNTIME_DAILY_COMPARE_NONCE", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert module.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "unavailable"}
    assert source not in output and binding["account_selector"] not in output and "digest" not in output


def test_default_comparison_still_emits_digest(monkeypatch, capsys):
    env, _, _ = fixture()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RUNTIME_DAILY_COMPARE_STATUS_ONLY", raising=False)
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "verified", "binding_digest": module.compare_source(env)}
