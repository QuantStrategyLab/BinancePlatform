from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError

import pytest

from application.account_facts import (
    AccountFactsUnavailable,
    ReadOnlyBinanceClient,
    build_source_binding_id,
    collect_account_facts,
    validate_account_facts_payload,
)


APP_SHA = "8cb56617115fa45028e34d788e71884b6a303d77"
PROTECTED_RUNTIME_SHA = "d" * 40
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
    checkout = workflow.index("      - name: Checkout trusted default-branch reader source")
    setup_uv = workflow.index("      - name: Set up uv")
    preflight = workflow.index("      - name: Verify trusted checkout and exact Runtime source")
    install = workflow.index("uv sync --frozen --no-dev")

    assert checkout < setup_uv < preflight < install
    setup_uv_step = workflow[setup_uv:preflight]
    assert "uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9" in setup_uv_step
    assert "with:" not in setup_uv_step
    assert "secrets." not in setup_uv_step
    assert workflow.count("astral-sh/setup-uv@") == 1

    assert "ref: ${{ vars.BINANCE_ACCOUNT_FACTS_READER_REVISION }}" in workflow
    assert 'BINANCE_ACCOUNT_FACTS_READER_REVISION: ${{ vars.BINANCE_ACCOUNT_FACTS_READER_REVISION || \'\' }}' in workflow
    assert "READER_PUBLIC_REVISION: ${{ vars.BINANCE_ACCOUNT_FACTS_READER_REVISION || '' }}" in workflow
    assert "ref: ${{ github.sha }}" not in workflow
    assert "git rev-parse HEAD" in workflow
    assert "workflow_call:" in workflow
    assert "workflow_run:" not in workflow
    assert "github.ref == 'refs/heads/runtime-production'" in workflow
    assert "timeout-minutes: 5" in workflow
    assert "continue-on-error: true" in workflow


def _parent_run_api(url, _token):
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if "/actions/workflows/main.yml/runs?status=" in url:
        status = parse_qs(parsed.query)["status"][0]
        rows = [{"id": 501, "path": ".github/workflows/main.yml@refs/heads/runtime-production", "status": "in_progress"}] if status == "in_progress" else []
        return {"total_count": len(rows), "workflow_runs": rows}
    if parsed.path.endswith("/actions/workflows/main.yml/runs"):
        return {"total_count": 1, "workflow_runs": [{
            "id": 501, "name": "Runtime · strategy",
            "path": ".github/workflows/main.yml@refs/heads/runtime-production",
            "head_branch": "runtime-production", "status": "in_progress",
        }]}
    if parsed.path.endswith("/actions/runs/501"):
        return {
            "id": 501, "run_attempt": 1, "name": "Runtime · strategy",
            "path": ".github/workflows/main.yml@refs/heads/runtime-production",
            "event": "workflow_dispatch", "status": "in_progress",
            "head_branch": "runtime-production", "head_sha": "d" * 40,
            "repository": {"full_name": "QuantStrategyLab/BinancePlatform"},
            "head_repository": {"full_name": "QuantStrategyLab/BinancePlatform"},
        }
    if parsed.path.endswith("/actions/runs/501/jobs"):
        return {"total_count": 1, "jobs": [{
            "id": 701, "name": "deploy", "status": "completed", "conclusion": "success",
            "started_at": "2026-10-02T10:00:00Z",
            "steps": [
                {"name": "Resolve approved runtime release SHA", "status": "completed", "conclusion": "success"},
                {"name": "4. Run trading strategy", "status": "completed", "conclusion": "success", "started_at": "2026-10-02T10:01:00Z", "completed_at": "2026-10-02T10:05:00Z"},
                {"name": "5. Stage execution report for isolated log publisher", "status": "completed", "conclusion": "success", "started_at": "2026-10-02T10:05:01Z", "completed_at": "2026-10-02T10:06:00Z"},
            ],
        }]}
    if parsed.path.endswith("/actions/runs/501/artifacts"):
        return {"total_count": 1, "artifacts": [{
            "name": "binance-execution-report-501", "expired": False,
            "size_in_bytes": 123, "created_at": "2026-10-02T10:05:10Z",
        }]}
    raise AssertionError("unexpected synthetic GitHub API path")


def _install_parent_release_log(monkeypatch, reader, selected_sha=APP_SHA):
    log_text = (
        f"Selected runtime release SHA {selected_sha} "
        f"(workflow github.sha={'d' * 40} is not used as the application execution identity)."
    )
    monkeypatch.setattr(reader, "_read_job_log_text", lambda *_args: log_text.encode())


def _current_terminal_run_api(url, _token, mutation=None):
    from urllib.parse import urlparse

    parsed = urlparse(url)
    source_sha = (
        "9cfcf0531d1ea176e6f26590cf15edbd31bd6567"
        if mutation == "legacy_old"
        else "e" * 40 if mutation == "wrong_workflow_sha" else PROTECTED_RUNTIME_SHA
    )
    source_path = (
        ".github/workflows/other.yml@refs/heads/runtime-production"
        if mutation == "wrong_path"
        else ".github/workflows/main.yml@refs/heads/runtime-production"
    )
    source_repository = (
        "other/repository" if mutation == "wrong_repository" else "QuantStrategyLab/BinancePlatform"
    )
    if "/actions/workflows/main.yml/runs?status=" in url:
        return {"total_count": 0, "workflow_runs": []}
    if parsed.path.endswith("/actions/workflows/main.yml/runs"):
        run_id = 602 if mutation == "newer_terminal" else 601
        return {"total_count": 2, "workflow_runs": [{
            "id": run_id, "name": "Runtime · strategy",
            "path": source_path,
            "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
            "head_branch": "runtime-production", "head_sha": source_sha,
            "run_attempt": 1, "repository": {"full_name": source_repository},
        }]}
    if parsed.path.endswith("/actions/runs/601"):
        attempt = 2 if mutation == "attempt_two" else 1
        return {
            "id": 601, "run_attempt": attempt, "name": "Runtime · strategy",
            "path": source_path,
            "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
            "head_branch": "runtime-production", "head_sha": source_sha,
            "repository": {"full_name": source_repository},
            "head_repository": {"full_name": source_repository},
        }
    if parsed.path.endswith("/actions/runs/601/jobs"):
        upload_conclusion = "failure" if mutation == "failed_report" else "success"
        return {"total_count": 1, "jobs": [{
            "id": 701, "name": "deploy", "status": "completed", "conclusion": "success",
            "started_at": "2026-10-02T11:00:00Z",
            "steps": [
                {"name": "Resolve approved runtime release SHA", "status": "completed", "conclusion": "success"},
                {"name": "4. Run trading strategy", "status": "completed", "conclusion": "success", "started_at": "2026-10-02T11:01:00Z", "completed_at": "2026-10-02T11:05:00Z"},
                {"name": "5. Stage execution report for isolated log publisher", "status": "completed", "conclusion": upload_conclusion, "started_at": "2026-10-02T11:05:01Z", "completed_at": "2026-10-02T11:06:00Z"},
            ],
        }]}
    if parsed.path.endswith("/actions/runs/601/artifacts"):
        created_at = "2026-10-02T11:04:59Z" if mutation == "early_artifact" else "2026-10-02T11:05:10Z"
        return {"artifacts": [{
            "name": "binance-execution-report-601", "expired": False,
            "size_in_bytes": 123, "created_at": created_at,
        }]}
    raise AssertionError("unexpected synthetic GitHub API path")


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("wrong_application", "trigger_release_mismatch"),
        ("attempt_two", "trigger_identity_mismatch"),
        ("wrong_path", "trigger_identity_mismatch"),
        ("wrong_repository", "trigger_identity_mismatch"),
        ("wrong_workflow_sha", "trigger_identity_mismatch"),
        ("failed_report", "parent_strategy_or_report_unverified"),
        ("early_artifact", "trigger_report_artifact_missing"),
        ("newer_terminal", "runtime_run_not_latest_success"),
    ],
)
def test_manual_terminal_accepts_only_current_pinned_runtime_and_8cb_app(monkeypatch, mutation, reason):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(reader, "_api_json", lambda url, token: _current_terminal_run_api(url, token, mutation))
    _install_parent_release_log(
        monkeypatch, reader, selected_sha="e" * 40 if mutation == "wrong_application" else APP_SHA
    )
    env = {
        "SOURCE_RUN_ID": "601",
        "GITHUB_REPOSITORY": "QuantStrategyLab/BinancePlatform",
        "GITHUB_REF": "refs/heads/runtime-production",
        "BINANCE_RUNTIME_WORKFLOW_SHA": PROTECTED_RUNTIME_SHA,
        "GITHUB_TOKEN": "synthetic-token",
        "GITHUB_API_URL": "https://api.example",
    }
    if reason:
        with pytest.raises(reader.ReaderError, match=reason):
            reader.verify_terminal_source_from_env(env)


def test_manual_terminal_accepts_protected_sha_when_runtime_selected_8cb(monkeypatch):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(reader, "_api_json", _current_terminal_run_api)
    _install_parent_release_log(monkeypatch, reader)
    result = reader.verify_terminal_source_from_env({
        "SOURCE_RUN_ID": "601",
        "GITHUB_REPOSITORY": "QuantStrategyLab/BinancePlatform",
        "GITHUB_REF": "refs/heads/runtime-production",
        "BINANCE_RUNTIME_WORKFLOW_SHA": PROTECTED_RUNTIME_SHA,
        "GITHUB_TOKEN": "synthetic-token",
        "GITHUB_API_URL": "https://api.example",
    })
    assert result == {"run_id": "601", "report_artifact": "binance-execution-report-601"}


def test_manual_terminal_keeps_fixed_legacy_9cfc_path(monkeypatch):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(reader, "_api_json", lambda url, token: _current_terminal_run_api(url, token, "legacy_old"))
    _install_parent_release_log(monkeypatch, reader)
    result = reader.verify_terminal_source_from_env({
        "SOURCE_RUN_ID": "601",
        "GITHUB_REPOSITORY": "QuantStrategyLab/BinancePlatform",
        "GITHUB_REF": "refs/heads/runtime-production",
        "BINANCE_RUNTIME_WORKFLOW_SHA": PROTECTED_RUNTIME_SHA,
        "GITHUB_TOKEN": "synthetic-token",
        "GITHUB_API_URL": "https://api.example",
    })
    assert result == {"run_id": "601", "report_artifact": "binance-execution-report-601"}


def test_same_parent_runtime_read_is_allowed_only_for_exact_in_progress_run(monkeypatch):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(reader, "_api_json", _parent_run_api)
    _install_parent_release_log(monkeypatch, reader)
    result = reader.verify_parent_run(
        run_id="501", run_attempt=1, repository="QuantStrategyLab/BinancePlatform",
        ref="refs/heads/runtime-production", github_sha="d" * 40,
        runtime_workflow_sha="d" * 40, token="synthetic", api_url="https://api.example",
    )
    assert result == {"run_id": "501", "report_artifact": "binance-execution-report-501"}


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"run_attempt": 2}, "parent_run_identity_mismatch"),
        ({"repository": "other/repository"}, "parent_run_identity_mismatch"),
        ({"ref": "refs/heads/main"}, "parent_run_identity_mismatch"),
        ({"github_sha": "e" * 40}, "parent_run_identity_mismatch"),
        ({"runtime_workflow_sha": "bad"}, "parent_run_identity_mismatch"),
    ],
)
def test_same_parent_runtime_rejects_identity_or_retry_mismatch(monkeypatch, changes, reason):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(reader, "_api_json", lambda *_args: pytest.fail("invalid caller must stop before API"))
    args = {
        "run_id": "501", "run_attempt": 1,
        "repository": "QuantStrategyLab/BinancePlatform",
        "ref": "refs/heads/runtime-production", "github_sha": "d" * 40,
        "runtime_workflow_sha": "d" * 40, "token": "synthetic",
        "api_url": "https://api.example",
    }
    args.update(changes)
    with pytest.raises(reader.ReaderError, match=reason):
        reader.verify_parent_run(**args)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("other_active", "runtime_activity_present"),
        ("newer_run", "runtime_parent_not_latest"),
        ("failed_strategy", "parent_strategy_or_report_unverified"),
        ("failed_report_upload", "parent_strategy_or_report_unverified"),
        ("early_artifact", "trigger_report_artifact_missing"),
        ("duplicate_artifact", "trigger_report_artifact_missing"),
    ],
)
def test_same_parent_runtime_requires_unique_completed_strategy_inputs(monkeypatch, mutation, reason):
    from scripts import read_binance_account_facts as reader

    _install_parent_release_log(monkeypatch, reader)

    def api(url, token):
        result = _parent_run_api(url, token)
        def fail_step(step_name):
            steps = result["jobs"][0]["steps"]
            matches = [step for step in steps if step.get("name") == step_name]
            assert len(matches) == 1
            matches[0]["conclusion"] = "skipped" if mutation == "failed_strategy" else "failure"

        if mutation == "other_active" and "status=in_progress" in url:
            result["workflow_runs"].append({
                "id": 502, "path": ".github/workflows/main.yml", "status": "in_progress",
            })
            result["total_count"] = 2
        elif mutation == "newer_run" and "/actions/workflows/main.yml/runs?per_page=1" in url:
            result["workflow_runs"][0]["id"] = 502
        elif mutation == "failed_strategy" and url.endswith("/jobs?per_page=100"):
            fail_step("4. Run trading strategy")
        elif mutation == "failed_report_upload" and url.endswith("/jobs?per_page=100"):
            fail_step("5. Stage execution report for isolated log publisher")
        elif mutation in {"early_artifact", "duplicate_artifact"} and url.endswith("/artifacts?per_page=100"):
            result["artifacts"][0]["created_at"] = "2026-10-02T10:04:59Z"
            if mutation == "duplicate_artifact":
                result["artifacts"].append(dict(result["artifacts"][0]))
        return result

    monkeypatch.setattr(reader, "_api_json", api)
    with pytest.raises(reader.ReaderError, match=reason):
        reader.verify_parent_run(
            run_id="501", run_attempt=1, repository="QuantStrategyLab/BinancePlatform",
            ref="refs/heads/runtime-production", github_sha="d" * 40,
            runtime_workflow_sha="d" * 40, token="synthetic", api_url="https://api.example",
        )


def test_same_parent_rejects_actual_app_release_mismatch_even_when_protected_sha_matches(monkeypatch):
    from scripts import read_binance_account_facts as reader

    monkeypatch.setattr(reader, "_api_json", _parent_run_api)
    _install_parent_release_log(monkeypatch, reader, selected_sha="e" * 40)
    with pytest.raises(reader.ReaderError, match="trigger_release_mismatch"):
        reader.verify_parent_run(
            run_id="501", run_attempt=1, repository="QuantStrategyLab/BinancePlatform",
            ref="refs/heads/runtime-production", github_sha="d" * 40,
            runtime_workflow_sha="d" * 40, token="synthetic", api_url="https://api.example",
        )


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


@pytest.mark.parametrize(("account", "reason"), [
    (None, "spot_response_not_object"),
    ({"uid": "123456", "balances": None}, "spot_balances_invalid"),
    ({"uid": "123456", "balances": [{}] * 5001}, "spot_balances_limit_exceeded"),
    ({"uid": "123456", "balances": ["synthetic-private-row"]}, "spot_balance_row_invalid"),
    ({"uid": "123456", "balances": [{"asset": 17}]}, "spot_asset_type_invalid"),
    ({"uid": "123456", "balances": [{"asset": "synthetic-bad-symbol"}]}, "spot_asset_format_invalid"),
    ({"uid": "123456", "balances": [{"asset": "A\u200b"}]}, "spot_asset_format_invalid"),
    ({"uid": "123456", "balances": [
        {"asset": "SYNTHETIC", "free": "0", "locked": "0"},
        {"asset": "SYNTHETIC", "free": "0", "locked": "0"},
    ]}, "spot_asset_duplicate"),
])
def test_spot_validation_uses_fixed_reason_without_echoing_response_values(account, reason):
    class InvalidSpot(FakeBinance):
        def get_account(self):
            self.calls.append(("spot",))
            return account

    client = InvalidSpot()
    with pytest.raises(AccountFactsUnavailable) as raised:
        _collect(client)
    assert str(raised.value) == f"binance_account_facts_{reason}"
    assert client.calls == [("spot",)]


@pytest.mark.parametrize("asset", ["币", "１２", "𐐀", "A" * 128, "汉" * 128])
def test_unicode_asset_rules_preserve_spot_earn_and_payload_values(asset):
    class UnicodeAsset(FakeBinance):
        def get_account(self):
            self.calls.append(("spot",))
            return {
                "uid": "123456",
                "balances": [{"asset": asset, "free": "0.5", "locked": "0"}],
            }

        def get_simple_earn_flexible_product_position(self, *, current, size):
            self.calls.append(("earn", current, size))
            return {
                "rows": [{"asset": asset, "productId": "synthetic-product", "totalAmount": "0.25"}],
                "total": 1,
            }

    client = UnicodeAsset()
    payload = _collect(client)
    assert payload["assets"] == [{
        "asset": asset,
        "quantity": "0.75",
        "spot_free": "0.5",
        "spot_locked": "0",
        "flexible_earn": "0.25",
    }]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    round_tripped = json.loads(encoded)
    assert round_tripped["assets"][0]["asset"] == asset
    validate_account_facts_payload(round_tripped)


@pytest.mark.parametrize("asset", [
    "", " ", "btc", "A-B", "A\u0301", "A\u200b", "A\u202e", "A\ud800", "😀", "A" * 129,
    "A\u115f", "A\u1160", "A\u3164", "A\uffa0",
])
def test_unicode_asset_rules_reject_unsafe_or_out_of_contract_values(asset):
    class InvalidAsset(FakeBinance):
        def get_account(self):
            self.calls.append(("spot",))
            return {
                "uid": "123456",
                "balances": [{"asset": asset, "free": "0.5", "locked": "0"}],
            }

    with pytest.raises(AccountFactsUnavailable, match="spot_asset_format_invalid"):
        _collect(InvalidAsset())


@pytest.mark.parametrize("asset", ["A\u115f", "A\u1160", "A\u3164", "A\uffa0"])
def test_unicode_earn_asset_rejects_invisible_hangul_fillers(asset):
    client = FakeBinance(earn_pages=[{
        "rows": [{"asset": asset, "productId": "synthetic-product", "totalAmount": "0.25"}],
        "total": 1,
    }])
    with pytest.raises(AccountFactsUnavailable, match="flexible_earn_page_invalid"):
        _collect(client)
    assert client.calls == [("spot",), ("earn", 1, 100)]


@pytest.mark.parametrize("asset", ["A\u115f", "A\u1160", "A\u3164", "A\uffa0"])
def test_payload_validator_rejects_invisible_hangul_fillers(asset):
    payload = _collect()
    payload["assets"][0]["asset"] = asset
    with pytest.raises(AccountFactsUnavailable, match="payload_invalid"):
        validate_account_facts_payload(payload)


def test_exact_duplicate_unicode_asset_is_rejected_without_normalization():
    class DuplicateUnicodeAsset(FakeBinance):
        def get_account(self):
            self.calls.append(("spot",))
            return {
                "uid": "123456",
                "balances": [
                    {"asset": "币", "free": "0.5", "locked": "0"},
                    {"asset": "币", "free": "0.5", "locked": "0"},
                ],
            }

    with pytest.raises(AccountFactsUnavailable, match="spot_asset_duplicate"):
        _collect(DuplicateUnicodeAsset())


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
                "name": "Runtime · strategy",
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


def test_runtime_target_accepts_reconcile_only_with_valid_continuity_fingerprint():
    from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
    from scripts import read_binance_account_facts as reader

    target = {
        "platform_id": "binance",
        "strategy_profile": "synthetic-profile",
        "dry_run_only": False,
        "deployment_selector": "synthetic-deployment",
        "account_selector": ["synthetic-account"],
        "account_scope": "synthetic-scope",
        "service_name": "synthetic-service",
    }
    continuity = {
        "state": "RECONCILE_ONLY",
        "baseline_kind": "legacy_authorized",
        "baseline_id": "synthetic-baseline",
        "baseline_target_sha256": runtime_target_fingerprint(target),
        "captured_at": "2026-10-02",
    }
    target["live_continuity"] = continuity
    resolved = reader._target_identity(json.dumps(target))
    assert resolved["live_continuity"]["state"] == "RECONCILE_ONLY"

    target["live_continuity"] = {**continuity, "baseline_target_sha256": "0" * 64}
    with pytest.raises(reader.ReaderError, match="runtime_target_invalid"):
        reader._target_identity(json.dumps(target))


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


@pytest.mark.parametrize("error_summary", [{}, {"errors": []}])
def test_strategy_report_accepts_only_exact_empty_error_summary_shapes(error_summary):
    from scripts import read_binance_account_facts as reader

    report, target = _eligible_strategy_report()
    report["error_summary"] = error_summary
    reader.validate_strategy_report(report, target)


@pytest.mark.parametrize("error_summary", [
    None,
    [],
    {"errors": None},
    {"errors": ["synthetic-error"]},
    {"errors": [], "extra": "synthetic"},
])
def test_strategy_report_rejects_invalid_or_open_error_summary_shapes(error_summary):
    from scripts import read_binance_account_facts as reader

    report, target = _eligible_strategy_report()
    report["error_summary"] = error_summary
    with pytest.raises(reader.ReaderError, match="strategy_report_not_eligible"):
        reader.validate_strategy_report(report, target)


def test_strategy_report_rejects_missing_error_summary():
    from scripts import read_binance_account_facts as reader

    report, target = _eligible_strategy_report()
    del report["error_summary"]
    with pytest.raises(reader.ReaderError, match="strategy_report_not_eligible"):
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


@pytest.mark.parametrize(("status", "receiver_code", "safe_label"), [
    (400, "invalid_binance_account_facts_assets", "http_400_report_assets_invalid"),
    (401, "binance_account_facts_token_invalid", "http_401_token_invalid"),
    (409, "binance_account_facts_observation_conflict", "http_409_observation_conflict"),
    (413, "binance_account_facts_payload_too_large", "http_413_payload_too_large"),
    (503, "binance_account_facts_binding_missing", "http_503_receiver_unavailable"),
])
def test_publisher_classifies_only_known_http_error_pairs_without_leaking_body(
    tmp_path, monkeypatch, status, receiver_code, safe_label
):
    from io import BytesIO
    from urllib.error import HTTPError
    from scripts import publish_binance_account_facts as publisher

    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(_collect()), encoding="utf-8")
    secret = "synthetic-sensitive-placeholder"
    private_url = f"https://private.example/path?token={secret}"
    reads = []

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 20
            error = HTTPError(
                private_url,
                status,
                "private exception detail",
                hdrs=None,
                fp=BytesIO(json.dumps({"ok": False, "error": receiver_code}).encode()),
            )
            original_read = error.read

            def tracked_read(amount=None):
                reads.append(amount)
                return original_read(amount)

            error.read = tracked_read
            raise error

    monkeypatch.setattr(publisher, "build_opener", lambda _handler: Opener())
    with pytest.raises(publisher.PublishError) as raised:
        publisher.publish_account_facts(
            facts_path=facts_path,
            env={
                "BINANCE_ACCOUNT_FACTS_ENABLED": "true",
                "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN": "synthetic-token",
            },
        )
    assert str(raised.value) == f"account_facts_publish_{safe_label}"
    assert receiver_code not in str(raised.value)
    assert secret not in str(raised.value)
    assert "private.example" not in str(raised.value)
    assert reads == [4097]


@pytest.mark.parametrize(("status", "body", "reason"), [
    (400, b"{\"ok\":false,\"error\":\"unknown-private-code\",\"private\":\"SENSITIVE\"}",
     "account_facts_publish_http_400_body_unrecognized_json"),
    (400, b"{\"ok\":false,\"error\":\"unknown-private-code\"}",
     "account_facts_publish_http_400_body_unknown_error_code"),
    (418, b"SENSITIVE private body", "account_facts_publish_http_418_body_invalid_json"),
    (409, b"x" * 4097, "account_facts_publish_http_409_body_oversized"),
])
def test_publisher_rejects_unknown_or_oversized_http_error_body_closed(
    tmp_path, monkeypatch, status, body, reason
):
    from io import BytesIO
    from urllib.error import HTTPError
    from scripts import publish_binance_account_facts as publisher

    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(_collect()), encoding="utf-8")
    reads = []

    class Opener:
        def open(self, _request, timeout):
            error = HTTPError(
                "https://private.example/sensitive-path",
                status,
                "private exception detail",
                hdrs=None,
                fp=BytesIO(body),
            )
            original_read = error.read

            def tracked_read(amount=None):
                reads.append(amount)
                return original_read(amount)

            error.read = tracked_read
            raise error

    monkeypatch.setattr(publisher, "build_opener", lambda _handler: Opener())
    with pytest.raises(publisher.PublishError) as raised:
        publisher.publish_account_facts(
            facts_path=facts_path,
            env={
                "BINANCE_ACCOUNT_FACTS_ENABLED": "true",
                "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN": "synthetic-token",
            },
        )
    assert str(raised.value) == reason
    assert str(status) in str(raised.value)
    assert "SENSITIVE" not in str(raised.value)
    assert "private.example" not in str(raised.value)
    assert reads == [4097]


@pytest.mark.parametrize(("failure", "reason"), [
    (lambda: URLError("private network detail"), "account_facts_publish_network_failed"),
    (lambda: OSError("private socket detail"), "account_facts_publish_network_failed"),
    (lambda: TimeoutError("private timeout detail"), "account_facts_publish_timeout"),
])
def test_publisher_classifies_transport_failures_without_retry_or_leak(
    tmp_path, monkeypatch, failure, reason
):
    from scripts import publish_binance_account_facts as publisher

    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(_collect()), encoding="utf-8")
    calls = []

    class Opener:
        def open(self, _request, timeout):
            calls.append(timeout)
            raise failure()

    monkeypatch.setattr(publisher, "build_opener", lambda _handler: Opener())
    with pytest.raises(publisher.PublishError) as raised:
        publisher.publish_account_facts(
            facts_path=facts_path,
            env={
                "BINANCE_ACCOUNT_FACTS_ENABLED": "true",
                "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN": "synthetic-secret",
            },
        )
    assert str(raised.value) == reason
    assert "private" not in str(raised.value)
    assert "synthetic-secret" not in str(raised.value)
    assert calls == [20]


@pytest.mark.parametrize("ack", [b"not-json", b"\xff\xfe"])
def test_publisher_classifies_invalid_ack_without_leaking_or_retrying(tmp_path, monkeypatch, ack):
    from scripts import publish_binance_account_facts as publisher

    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(_collect()), encoding="utf-8")
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit == 4097
            return ack

    class Opener:
        def open(self, _request, timeout):
            calls.append(timeout)
            return Response()

    monkeypatch.setattr(publisher, "build_opener", lambda _handler: Opener())
    with pytest.raises(publisher.PublishError) as raised:
        publisher.publish_account_facts(
            facts_path=facts_path,
            env={
                "BINANCE_ACCOUNT_FACTS_ENABLED": "true",
                "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN": "synthetic-secret",
            },
        )
    assert str(raised.value) == "account_facts_publish_ack_invalid"
    assert "synthetic-secret" not in str(raised.value)
    assert calls == [20]


def test_receiver_diagnosis_is_one_authenticated_get_with_closed_success():
    from scripts import diagnose_binance_account_facts_receiver as diagnosis
    from scripts import publish_binance_account_facts as publisher

    assert diagnosis.QRS_ENDPOINT == publisher.QRS_ENDPOINT
    calls = []

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit == 4097
            return json.dumps({
                "ok": True, "ready": True, "binding_valid": True,
                "account_options_readable": True, "unique_match": True,
            }).encode()

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 15
            return Response()

    result = diagnosis.diagnose_receiver(token="synthetic-secret", opener_factory=lambda _handler: Opener())
    assert result.category == "ready"
    assert result.http_status == 200
    assert result.body_shape == "readiness"
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].data is None
    assert calls[0].get_header("Authorization") == "Bearer synthetic-secret"
    assert "synthetic-secret" not in repr(result)


@pytest.mark.parametrize(
    "status,body,headers,category,shape,receiver_code,cf_code",
    [
        (403, {"success": False, "errors": [{"code": 1020, "message": "private edge detail"}],
               "messages": [], "result": None}, {"Server": "cloudflare"},
         "cloudflare_rejected", "cloudflare_error", None, 1020),
        (403, {"private": "sensitive response"}, {"Server": "cloudflare"},
         "http_rejected", "json_object_other", None, None),
        (401, {"ok": False, "error": []}, {},
         "http_rejected", "json_object_other", None, None),
        (401, {"ok": False, "error": {}}, {},
         "http_rejected", "json_object_other", None, None),
        (401, {"ok": False, "error": "binance_account_facts_token_invalid"}, {},
         "token_invalid", "token_error", "token_invalid", None),
        (503, {"ok": False, "ready": False, "binding_valid": False,
               "account_options_readable": False, "unique_match": False,
               "error": "binance_account_facts_account_options_unavailable"}, {},
         "account_options_unavailable", "receiver_error", "account_options_unavailable", None),
    ],
)
def test_receiver_diagnosis_emits_only_closed_status_and_body_metadata(
    status, body, headers, category, shape, receiver_code, cf_code
):
    from io import BytesIO
    from urllib.error import HTTPError
    from scripts import diagnose_binance_account_facts_receiver as diagnosis

    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 15
            error = HTTPError(
                "https://private.example/private-path", status, "sensitive exception",
                headers, BytesIO(json.dumps(body).encode()),
            )
            raise error

    result = diagnosis.diagnose_receiver(token="synthetic-token", opener_factory=lambda _handler: Opener())
    assert result.category == category
    assert result.http_status == status
    assert result.body_shape == shape
    assert result.receiver_error == receiver_code
    assert result.cloudflare_code == cf_code
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].data is None
    assert "private edge detail" not in repr(result)
    assert "private.example" not in repr(result)
    assert "sensitive exception" not in repr(result)


def test_receiver_diagnosis_missing_token_and_transport_failure_never_retry():
    from scripts import diagnose_binance_account_facts_receiver as diagnosis

    calls = []
    missing = diagnosis.diagnose_receiver(token="", opener_factory=lambda *_: calls.append("unexpected"))
    assert missing.category == "token_missing"
    assert calls == []

    class Opener:
        def open(self, request, timeout):
            calls.append((request.method, timeout))
            raise OSError("private transport detail")

    failed = diagnosis.diagnose_receiver(token="synthetic-token", opener_factory=lambda _handler: Opener())
    assert failed.category == "transport"
    assert len(calls) == 1 and calls[0] == ("GET", 15)


def test_receiver_diagnosis_rejects_ambiguous_json_and_unverified_cloudflare_code():
    from scripts import diagnose_binance_account_facts_receiver as diagnosis

    shape, code, cf_code = diagnosis._json_body_shape(
        b'{"ok":true,"ok":false}', {"Server": "cloudflare"}
    )
    assert (shape, code, cf_code) == ("invalid_json", None, None)
    envelope = json.dumps({
        "success": False,
        "errors": [{"code": 1020, "message": "private"}],
        "messages": [], "result": None,
    }).encode()
    shape, code, cf_code = diagnosis._json_body_shape(envelope, {"Server": "other"})
    assert (shape, code, cf_code) == ("json_object_other", None, None)


def test_receiver_diagnosis_workflow_is_manual_main_only_and_get_only():
    workflow = (Path(__file__).parents[1] / ".github" / "workflows"
                / "diagnose-account-facts-receiver.yml").read_text(encoding="utf-8")
    assert '"on":\n  workflow_dispatch:' in workflow
    assert "refs/heads/main" in workflow
    assert "git ls-remote https://github.com/QuantStrategyLab/BinancePlatform.git refs/heads/main" in workflow
    assert 'ref: ${{ github.sha }}' in workflow
    assert "git rev-parse HEAD" in workflow
    assert "group: binance-account-facts-readonly" in workflow
    assert "environment: binance-runtime" in workflow
    assert "runs-on: self-hosted" in workflow
    assert "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN: ${{ secrets.BINANCE_ACCOUNT_FACTS_SYNC_TOKEN }}" in workflow
    assert "python3 scripts/diagnose_binance_account_facts_receiver.py" in workflow
    assert "account_facts_publish" not in workflow
    assert "read_binance_account_facts.py" not in workflow
    assert "workflow_run:" not in workflow and "schedule:" not in workflow and "push:" not in workflow
    assert "RUNTIME_TARGET_ENABLED" not in workflow and "BINANCE_ACCOUNT_FACTS_ENABLED" not in workflow
