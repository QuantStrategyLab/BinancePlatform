from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from application.account_facts import (
    AccountFactsUnavailable,
    ReadOnlyBinanceClient,
    build_source_binding_id,
    collect_account_facts,
    validate_account_facts_payload,
)


APP_SHA = "8cb56617115fa45028e34d788e71884b6a303d77"
READER_SHA = "a" * 40
UID_DIGEST = "b" * 64
START = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


class FakeBinance:
    def __init__(self, *, earn_pages=None):
        self.earn_pages = earn_pages or [
            {"rows": [{"asset": "BTC", "productId": "flex-1", "totalAmount": "0.25"}], "total": 1}
        ]
        self.calls = []

    def get_account(self):
        self.calls.append(("spot",))
        return {
            "uid": "123456",
            "balances": [
                {"asset": "BTC", "free": "0.5", "locked": "0.1"},
                {"asset": "USDT", "free": "10", "locked": "0"},
            ],
        }

    def get_simple_earn_flexible_product_position(self, *, current, size):
        self.calls.append(("earn", current, size))
        return self.earn_pages[current - 1]


def _uid_hash() -> str:
    from quant_platform_kit.common.broker_reconciliation import calculate_broker_observation_sha256

    return calculate_broker_observation_sha256({"account_uid": "123456"})


def _collect(client=None, times=None):
    raw = client or FakeBinance()
    clock_values = iter(times or [START + timedelta(seconds=i) for i in (1, 2, 3)])
    return collect_account_facts(
        ReadOnlyBinanceClient(raw),
        expected_account_scope_sha256=_uid_hash(),
        target_id="synthetic-target",
        reader_public_revision=READER_SHA,
        approved_application_revision=APP_SHA,
        observed_started_at=START,
        clock=lambda: next(clock_values),
    )


def test_collects_spot_and_all_flexible_earn_as_native_quantities():
    payload = _collect()
    assert payload["scope"] == "spot+flexible_earn"
    assert payload["snapshot_atomic"] is False
    assert payload["uncovered_scopes"] == ["funding", "margin", "futures", "locked_earn"]
    assert payload["assets"] == [
        {"asset": "BTC", "quantity": "0.85", "spot_free": "0.5", "spot_locked": "0.1", "flexible_earn": "0.25"},
        {"asset": "USDT", "quantity": "10", "spot_free": "10", "spot_locked": "0", "flexible_earn": "0"},
    ]
    assert payload["source_binding"]["id"] == build_source_binding_id(
        account_scope_sha256=_uid_hash(),
        reader_public_revision=READER_SHA,
        approved_application_revision=APP_SHA,
    )
    validate_account_facts_payload(payload)


def test_synthetic_contract_fixture_matches_exact_producer_json():
    fixture_path = Path(__file__).parent / "fixtures" / "binance_account_facts.v1.synthetic.json"
    payload = _collect(times=[START + timedelta(seconds=i) for i in (1, 2, 3)])
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    assert encoded == fixture_path.read_text(encoding="utf-8")


def test_source_binding_hash_uses_canonical_json():
    material = {
        "approved_application_revision": APP_SHA,
        "account_scope_sha256": UID_DIGEST,
        "reader_public_revision": READER_SHA,
        "scope": "spot+flexible_earn",
    }
    expected = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    assert build_source_binding_id(
        account_scope_sha256=UID_DIGEST,
        reader_public_revision=READER_SHA,
        approved_application_revision=APP_SHA,
    ) == expected


def test_workflow_uses_fixed_protected_reader_revision_not_trigger_sha():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "binance-account-facts.yml").read_text()
    assert "ref: ${{ vars.BINANCE_ACCOUNT_FACTS_READER_REVISION }}" in workflow
    assert 'BINANCE_ACCOUNT_FACTS_READER_REVISION: ${{ vars.BINANCE_ACCOUNT_FACTS_READER_REVISION || \'\' }}' in workflow
    assert "READER_PUBLIC_REVISION: ${{ vars.BINANCE_ACCOUNT_FACTS_READER_REVISION || '' }}" in workflow
    assert "ref: ${{ github.sha }}" not in workflow
    assert "git rev-parse HEAD" in workflow


def test_rejects_incomplete_flexible_earn_page():
    client = FakeBinance(earn_pages=[{"rows": [], "total": 1}])
    with pytest.raises(AccountFactsUnavailable, match="flexible_earn_page_incomplete"):
        _collect(client)


def test_rejects_duplicate_flexible_earn_product_across_pages():
    client = FakeBinance(earn_pages=[
        {
            "rows": [
                {"asset": "BTC", "productId": f"product-{index}", "totalAmount": "0.1"}
                for index in range(99)
            ] + [{"asset": "BTC", "productId": "same", "totalAmount": "0.1"}],
            "total": 101,
        },
        {"rows": [{"asset": "BTC", "productId": "same", "totalAmount": "0.2"}], "total": 101},
    ])
    with pytest.raises(AccountFactsUnavailable, match="flexible_earn_page_invalid"):
        _collect(client)


def test_rejects_wrong_account_uid_before_earn_read():
    class WrongAccount(FakeBinance):
        def get_account(self):
            self.calls.append(("spot",))
            return {"uid": "other", "balances": []}

    client = WrongAccount()
    with pytest.raises(AccountFactsUnavailable, match="account_identity_mismatch"):
        _collect(client)
    assert client.calls == [("spot",)]


def test_rejects_earn_asset_without_an_explicit_spot_balance_row():
    class SpotWithoutEarnAsset(FakeBinance):
        def get_account(self):
            self.calls.append(("spot",))
            return {"uid": "123456", "balances": []}

    client = SpotWithoutEarnAsset(
        earn_pages=[{"rows": [{"asset": "BTC", "productId": "flex-1", "totalAmount": "0.25"}], "total": 1}]
    )
    with pytest.raises(AccountFactsUnavailable, match="spot_asset_unobserved"):
        _collect(client)


def test_flexible_earn_aggregation_preserves_full_decimal_precision():
    client = FakeBinance(earn_pages=[{
        "rows": [
            {"asset": "BTC", "productId": "flex-1", "totalAmount": "0.12345678901234567890123456789"},
            {"asset": "BTC", "productId": "flex-2", "totalAmount": "0.12345678901234567890123456789"},
        ],
        "total": 2,
    }])
    payload = _collect(client)
    btc = payload["assets"][0]
    assert btc["flexible_earn"] == "0.24691357802469135780246913578"


@pytest.mark.parametrize("amount", ["1e999999999", "1e-999999999", "1234567890123456789012345678901"])
def test_decimal_canonicalizer_rejects_unbounded_or_over_precision_values(amount):
    from application.account_facts import _decimal

    with pytest.raises(AccountFactsUnavailable, match="amount_invalid"):
        _decimal(amount)


def test_decimal_canonicalizer_removes_non_significant_zeros():
    from application.account_facts import _decimal, _decimal_text

    assert _decimal_text(_decimal("1.000000")) == "1"


def test_rejects_non_monotonic_observation_times():
    with pytest.raises(AccountFactsUnavailable, match="observation_time_invalid"):
        _collect(times=[START - timedelta(seconds=1), START, START])


def test_payload_validator_rejects_reordered_observations():
    payload = _collect()
    payload["spot_observed_at"], payload["earn_observed_at"] = (
        payload["earn_observed_at"], payload["spot_observed_at"]
    )
    with pytest.raises(AccountFactsUnavailable, match="payload_invalid"):
        validate_account_facts_payload(payload)


def test_zero_earn_positions_is_a_complete_empty_page():
    client = FakeBinance(earn_pages=[{"rows": [], "total": 0}])
    payload = _collect(client)
    assert client.calls == [("spot",), ("earn", 1, 100)]
    assert all(row["flexible_earn"] == "0" for row in payload["assets"])


def test_complete_all_zero_spot_and_earn_is_a_valid_empty_snapshot():
    class ZeroSpot(FakeBinance):
        def get_account(self):
            self.calls.append(("spot",))
            return {
                "uid": "123456",
                "balances": [
                    {"asset": "BTC", "free": "0", "locked": "0"},
                    {"asset": "USDT", "free": "0", "locked": "0"},
                ],
            }

    client = ZeroSpot(earn_pages=[{
        "rows": [{"asset": "BTC", "productId": "flex-zero", "totalAmount": "0.0"}],
        "total": 1,
    }])
    payload = _collect(client)
    assert payload["assets"] == []
    validate_account_facts_payload(payload)


def test_invalid_asset_is_rejected_even_when_its_quantity_is_zero():
    client = FakeBinance(earn_pages=[{
        "rows": [{"asset": "btc", "productId": "flex-zero", "totalAmount": "0"}],
        "total": 1,
    }])
    with pytest.raises(AccountFactsUnavailable, match="flexible_earn_page_invalid"):
        _collect(client)


def test_active_runtime_blocks_read_before_account_client_creation(monkeypatch):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(
        reader,
        "_api_json",
        lambda _url, _token: {
            "total_count": 1,
            "workflow_runs": [{
                "path": ".github/workflows/main.yml@refs/heads/other-branch",
                "head_branch": "other-branch",
                "status": "in_progress",
            }],
        },
    )
    with pytest.raises(reader.ReaderError, match="runtime_activity_present"):
        reader._ensure_no_active_runtime_run(token="synthetic", api_url="https://api.example")


def test_release_log_plain_text_requires_one_exact_approved_line(monkeypatch):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(reader, "_api_json", lambda _url, _token: {"jobs": [{
        "id": 123,
        "name": "deploy",
        "conclusion": "success",
        "steps": [{
            "name": "Resolve approved runtime release SHA",
            "conclusion": "success",
        }],
    }]})
    release_line = (
        f"2026-10-02T10:10:10.1234567Z Selected runtime release SHA {APP_SHA} "
        f"(workflow github.sha={'f' * 40} is not used as the application execution identity)."
    )
    monkeypatch.setattr(reader, "_read_job_log_text", lambda _url, _token: release_line.encode())
    reader._validate_release_log("1234", "synthetic-token", "https://api.example")


@pytest.mark.parametrize(
    "log_text,reason",
    [
        ("Selected runtime release SHA " + "a" * 40, "trigger_release_mismatch"),
        (
            "Selected runtime release SHA " + APP_SHA + " (workflow github.sha=" + "f" * 40
            + " is not used as the application execution identity).\n"
            + "Selected runtime release SHA " + APP_SHA + " (workflow github.sha=" + "f" * 40
            + " is not used as the application execution identity).",
            "trigger_release_mismatch",
        ),
    ],
)
def test_release_log_rejects_missing_or_duplicate_approved_line(monkeypatch, log_text, reason):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(reader, "_api_json", lambda _url, _token: {"jobs": [{
        "id": 123,
        "name": "deploy",
        "conclusion": "success",
        "steps": [{
            "name": "Resolve approved runtime release SHA",
            "conclusion": "success",
        }],
    }]})
    monkeypatch.setattr(reader, "_read_job_log_text", lambda _url, _token: log_text.encode())
    with pytest.raises(reader.ReaderError, match=reason):
        reader._validate_release_log("1234", "synthetic-token", "https://api.example")


def test_manual_run_must_still_be_the_latest_qualifying_runtime(monkeypatch):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(
        reader,
        "_api_json",
        lambda _url, _token: {
            "workflow_runs": [{
                "id": 200,
                "name": "Runtime",
                "path": ".github/workflows/main.yml@refs/heads/runtime-production",
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "failure",
                "head_branch": "runtime-production",
                "head_sha": "9cfcf0531d1ea176e6f26590cf15edbd31bd6567",
                "repository": {"full_name": "QuantStrategyLab/BinancePlatform"},
            }],
        },
    )
    with pytest.raises(reader.ReaderError, match="runtime_run_not_latest_success"):
        reader._ensure_latest_runtime_run(
            run_id="199", token="synthetic-token", api_url="https://api.example"
        )


def test_runtime_target_and_protected_binding_match_canonical_selector_shape():
    from scripts import read_binance_account_facts as reader

    target = reader._target_identity(json.dumps({
        "platform_id": "binance",
        "strategy_profile": "synthetic-profile",
        "dry_run_only": False,
        "deployment_selector": "synthetic-deployment",
        "account_selector": ["synthetic-account"],
        "account_scope": "synthetic-scope",
        "service_name": "synthetic-service",
    }))
    assert target["account_selector"] == ["synthetic-account"]
    binding = {
        "platform": "binance",
        "account_key": "synthetic-key",
        "account_scope": "synthetic-scope",
        "target_name": "synthetic-target",
        "service_name": "synthetic-service",
        "deployment_selector": "synthetic-deployment",
        "account_selector": "synthetic-account",
        "target_id": "synthetic-target-id",
        "account_scope_sha256": UID_DIGEST,
        "reader_revision": READER_SHA,
        "approved_application_revision": APP_SHA,
        "source_binding": {
            "kind": "binance_readonly_scope_revision",
            "id": build_source_binding_id(
                account_scope_sha256=UID_DIGEST,
                reader_public_revision=READER_SHA,
                approved_application_revision=APP_SHA,
            ),
        },
    }
    assert reader._binding(json.dumps(binding), target, UID_DIGEST, READER_SHA) == binding


def _eligible_strategy_report(*, receipt_outcome="no_action", confirmation=None, observation=None):
    from quant_platform_kit.common.execution_receipts import build_execution_receipt

    runtime_target = {
        "platform_id": "binance",
        "strategy_profile": "synthetic-profile",
        "dry_run_only": False,
        "execution_mode": "live",
        "deployment_selector": "synthetic-deployment",
        "account_selector": ["synthetic-account"],
        "account_scope": "synthetic-scope",
        "service_name": "synthetic-service",
    }
    release = "c" * 40
    report = {
        "platform": "binance",
        "status": "ok",
        "dry_run": False,
        "standard_execution_permitted": True,
        "run_id": "synthetic-run",
        "runtime_target": runtime_target,
        "strategy_profile": "synthetic-profile",
        "runtime_release_receipt": {
            "attestation_state": "self_attested",
            "strategy_release": {"strategy_revision": release},
        },
        "error_summary": {"errors": []},
        "side_effect_summary": {"executed_call_count": 0},
        "execution_receipt_observation": observation or {
            "submission_attempted_count": 0,
            "broker_acknowledged_count": 0,
            "partially_filled_count": 0,
            "filled_count": 0,
            "transport_uncertain_count": 0,
            "failed_count": 0,
        },
    }
    report["execution_receipt"] = build_execution_receipt(
        platform="binance",
        strategy_profile="synthetic-profile",
        strategy_revision=release,
        execution_mode="live",
        outcome=receipt_outcome,
        broker_confirmation=confirmation,
    )
    return report, runtime_target


def test_strategy_report_accepts_closed_execution_receipt():
    from scripts import read_binance_account_facts as reader

    report, target = _eligible_strategy_report()
    reader.validate_strategy_report(report, target)


def test_strategy_report_accepts_missing_receipt_only_for_explicit_zero_counters():
    from scripts import read_binance_account_facts as reader

    report, target = _eligible_strategy_report()
    del report["execution_receipt"]
    reader.validate_strategy_report(report, target)


def test_strategy_report_rejects_present_null_receipt_even_with_zero_counters():
    from scripts import read_binance_account_facts as reader

    report, target = _eligible_strategy_report()
    report["execution_receipt"] = None
    with pytest.raises(reader.ReaderError, match="strategy_execution_receipt_invalid"):
        reader.validate_strategy_report(report, target)


def test_strategy_report_rejects_missing_receipt_for_nonzero_submission_count():
    from scripts import read_binance_account_facts as reader

    observation = {
        "submission_attempted_count": 1,
        "broker_acknowledged_count": 0,
        "partially_filled_count": 0,
        "filled_count": 0,
        "transport_uncertain_count": 0,
        "failed_count": 0,
    }
    report, target = _eligible_strategy_report(observation=observation)
    del report["execution_receipt"]
    with pytest.raises(reader.ReaderError, match="strategy_execution_receipt_required"):
        reader.validate_strategy_report(report, target)


def test_strategy_report_accepts_only_exact_terminal_fill_for_nonzero_counts():
    from scripts import read_binance_account_facts as reader

    observation = {
        "submission_attempted_count": 1,
        "broker_acknowledged_count": 0,
        "partially_filled_count": 0,
        "filled_count": 1,
        "transport_uncertain_count": 0,
        "failed_count": 0,
    }
    report, target = _eligible_strategy_report(
        receipt_outcome="filled",
        confirmation="filled",
        observation=observation,
    )
    reader.validate_strategy_report(report, target)


@pytest.mark.parametrize(
    "counter_name,outcome,confirmation",
    [
        ("broker_acknowledged_count", "broker_acknowledged", "acknowledged"),
        ("partially_filled_count", "partially_filled", "partially_filled"),
    ],
)
def test_strategy_report_rejects_ack_or_partial_without_terminal_fill(
    counter_name, outcome, confirmation
):
    from scripts import read_binance_account_facts as reader

    observation = {
        "submission_attempted_count": 1,
        "broker_acknowledged_count": 0,
        "partially_filled_count": 0,
        "filled_count": 0,
        "transport_uncertain_count": 0,
        "failed_count": 0,
    }
    observation[counter_name] = 1
    report, target = _eligible_strategy_report(
        receipt_outcome=outcome,
        confirmation=confirmation,
        observation=observation,
    )
    with pytest.raises(reader.ReaderError, match="strategy_submission_unclosed"):
        reader.validate_strategy_report(report, target)


@pytest.mark.parametrize(
    "outcome,confirmation,observation,reason",
    [
        (
            "reconciliation_required", "reconciliation_required",
            {
                "submission_attempted_count": 1,
                "broker_acknowledged_count": 0,
                "partially_filled_count": 0,
                "filled_count": 0,
                "transport_uncertain_count": 1,
                "failed_count": 0,
            },
            "strategy_execution_uncertain",
        ),
        (
            "reconciliation_required", "reconciliation_required",
            {
                "submission_attempted_count": 1,
                "broker_acknowledged_count": 0,
                "partially_filled_count": 0,
                "filled_count": 0,
                "transport_uncertain_count": 0,
                "failed_count": 1,
            },
            "strategy_submission_unclosed",
        ),
        (
            "submitted", "not_observed",
            {
                "submission_attempted_count": 1,
                "broker_acknowledged_count": 0,
                "partially_filled_count": 0,
                "filled_count": 0,
                "transport_uncertain_count": 0,
                "failed_count": 0,
            },
            "strategy_submission_unclosed",
        ),
    ],
)
def test_strategy_report_rejects_uncertain_reconciliation_and_unclosed_submission(
    outcome, confirmation, observation, reason
):
    from scripts import read_binance_account_facts as reader

    report, target = _eligible_strategy_report(
        receipt_outcome=outcome,
        confirmation=confirmation,
        observation=observation,
    )
    with pytest.raises(reader.ReaderError, match=reason):
        reader.validate_strategy_report(report, target)


@pytest.mark.parametrize("ack_status", ["published", "unchanged"])
def test_publisher_accepts_only_qrs_idempotent_acknowledgements(tmp_path, monkeypatch, ack_status):
    from scripts import publish_binance_account_facts as publisher

    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(_collect()), encoding="utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"status": ack_status}).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.method == "POST"
            assert request.full_url == publisher.QRS_ENDPOINT
            assert timeout == 20
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda _handler: Opener())
    assert publisher.publish_account_facts(
        facts_path=facts_path,
        env={
            "BINANCE_ACCOUNT_FACTS_ENABLED": "true",
            "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN": "synthetic-token",
        },
    ) == ack_status


def test_publisher_rejects_legacy_recorded_ack(tmp_path, monkeypatch):
    from scripts import publish_binance_account_facts as publisher

    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(_collect()), encoding="utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"status":"recorded"}'

    class Opener:
        def open(self, _request, timeout):
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda _handler: Opener())
    with pytest.raises(publisher.PublishError, match="publish_rejected"):
        publisher.publish_account_facts(
            facts_path=facts_path,
            env={
                "BINANCE_ACCOUNT_FACTS_ENABLED": "true",
                "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN": "synthetic-token",
            },
        )
