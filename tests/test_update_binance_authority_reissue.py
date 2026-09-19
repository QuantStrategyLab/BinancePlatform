"""Synthetic tests for one-shot authority strategy+runner reissue."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts import update_binance_authority_reissue as mod

OLD_STRATEGY = "a" * 40
NEW_STRATEGY = "b" * 40
OLD_RUNNER = "c" * 40
NEW_RUNNER = "d" * 40
OLD_SOURCE = "e" * 40
NEW_SOURCE = "f" * 40
MAINT = "1" * 40


def _authority_bytes(strategy: str, runner: str, *, extra: str = "") -> bytes:
    payload = (
        "{"
        f'"decision":"APPROVE",'
        f'"authority_scope":"LIVE",'
        f'"runtime_target":{{"platform_id":"binance"}},'
        f'"strategy_revision":"{strategy}",'
        f'"runner_revision":"{runner}",'
        f'"config_sha256":"{"1" * 64}",'
        f'"continuous_inputs_allowed":true,'
        f'"mandate":{{"mandate_id":"m1"}}'
        f"{extra}"
        "}"
    )
    return payload.encode("utf-8")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _snapshot(
    *,
    enabled: str = "false",
    sha: str,
    source: str = OLD_SOURCE,
    runs: tuple[str, ...] = (),
) -> mod.PreconditionSnapshot:
    return mod.PreconditionSnapshot(
        runtime_target_enabled=enabled,
        authority_sha256=sha,
        source_revision=source,
        in_progress_run_ids=runs,
    )


def _run(
    *,
    mode: str,
    raw: bytes,
    gateway: mod.GithubGateway,
    write_token_present: bool,
    target_strategy: str = NEW_STRATEGY,
    target_runner: str = NEW_RUNNER,
    target_source: str = NEW_SOURCE,
    expected_sha: str | None = None,
    expected_source: str = OLD_SOURCE,
) -> mod.UpdateResult:
    return mod.run_update(
        mode=mode,
        raw_authority=raw,
        target_strategy_revision=target_strategy,
        target_runner_revision=target_runner,
        target_source_revision=target_source,
        expected_authority_sha256=expected_sha or _digest(raw),
        expected_source_revision=expected_source,
        gateway=gateway,
        maintenance_workflow_sha=MAINT,
        write_token_present=write_token_present,
    )


def test_invalid_and_missing_inputs_reject_without_writes() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))

    with pytest.raises(mod.AuthorityUpdateError, match="target_strategy_revision"):
        _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False, target_strategy="deadbeef")
    with pytest.raises(mod.AuthorityUpdateError, match="target_runner_revision"):
        _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False, target_runner="not-a-sha")
    with pytest.raises(mod.AuthorityUpdateError, match="target_source_revision"):
        _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False, target_source="")
    with pytest.raises(mod.AuthorityUpdateError, match="expected_authority_sha256"):
        _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False, expected_sha="abc")
    with pytest.raises(mod.AuthorityUpdateError, match="expected_source_revision"):
        _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False, expected_source="xyz")

    missing_strategy = (
        b'{"decision":"APPROVE","authority_scope":"LIVE","runtime_target":{},'
        b'"runner_revision":"' + OLD_RUNNER.encode() + b'","config_sha256":"' + (b"1" * 64)
        + b'","continuous_inputs_allowed":true,"mandate":{}}'
    )
    with pytest.raises(mod.AuthorityUpdateError, match="missing strategy_revision"):
        mod.patch_version_fields_bytes(
            missing_strategy,
            target_strategy_revision=NEW_STRATEGY,
            target_runner_revision=NEW_RUNNER,
        )
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_wrong_old_digest_rejects_without_writes() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    claimed = "0" * 64
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=claimed))
    with pytest.raises(mod.AuthorityUpdateError, match="do not match expected_authority_sha256"):
        _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False, expected_sha=claimed)
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_json_ambiguity_duplicate_key_and_nonfinite_rejected() -> None:
    dup = (
        b'{"decision":"APPROVE","authority_scope":"LIVE","runtime_target":{},'
        b'"strategy_revision":"' + OLD_STRATEGY.encode() + b'","strategy_revision":"'
        + NEW_STRATEGY.encode() + b'","runner_revision":"' + OLD_RUNNER.encode()
        + b'","config_sha256":"' + (b"1" * 64) + b'","continuous_inputs_allowed":true,"mandate":{}}'
    )
    with pytest.raises(mod.AuthorityUpdateError, match="duplicate"):
        mod.patch_version_fields_bytes(
            dup, target_strategy_revision=NEW_STRATEGY, target_runner_revision=NEW_RUNNER
        )

    bad = (
        b'{"decision":"APPROVE","authority_scope":"LIVE","runtime_target":{},'
        b'"strategy_revision":"' + OLD_STRATEGY.encode() + b'","runner_revision":"'
        + OLD_RUNNER.encode() + b'","config_sha256":"' + (b"1" * 64)
        + b'","continuous_inputs_allowed":true,"mandate":{"x":NaN}}'
    )
    with pytest.raises(mod.AuthorityUpdateError, match="non-finite|invalid constants|malformed"):
        mod.patch_version_fields_bytes(
            bad, target_strategy_revision=NEW_STRATEGY, target_runner_revision=NEW_RUNNER
        )


def test_non_version_fields_remain_semantically_equal() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    new_raw, old_s, new_s, old_r, new_r = mod.patch_version_fields_bytes(
        raw, target_strategy_revision=NEW_STRATEGY, target_runner_revision=NEW_RUNNER
    )
    assert (old_s, new_s, old_r, new_r) == (OLD_STRATEGY, NEW_STRATEGY, OLD_RUNNER, NEW_RUNNER)
    before = mod._strict_json_object(raw)
    after = mod._strict_json_object(new_raw)
    assert mod._semantic_without_versions(before) == mod._semantic_without_versions(after)
    assert set(before) == set(after)
    for key in before:
        if key in {"strategy_revision", "runner_revision"}:
            continue
        assert before[key] == after[key]
    assert after["strategy_revision"] == NEW_STRATEGY
    assert after["runner_revision"] == NEW_RUNNER


def test_plan_is_zero_write_and_apply_requires_token() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))
    planned = _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False)
    assert planned.status == "plan_ok"
    assert planned.secret_write == "not_attempted"
    assert planned.sha256_write == "not_attempted"
    assert planned.source_write == "not_attempted"
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []

    with pytest.raises(mod.AuthorityUpdateError, match="BINANCE_AUTHORITY_UPDATE_TOKEN"):
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=False)
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


@pytest.mark.parametrize(
    "enabled,runs,source,match",
    [
        ("true", (), OLD_SOURCE, "RUNTIME_TARGET_ENABLED"),
        ("false", ("1",), OLD_SOURCE, "in-progress"),
        ("false", (), "9" * 40, "SOURCE_REVISION"),
    ],
)
def test_disabled_in_progress_digest_source_preconditions(
    enabled: str, runs: tuple[str, ...], source: str, match: str
) -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(enabled=enabled, sha=digest, source=source, runs=runs))
    with pytest.raises(mod.AuthorityUpdateError, match=match):
        _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False)
    gateway.snapshot = _snapshot(sha="2" * 64)
    with pytest.raises(mod.AuthorityUpdateError, match="SHA256 does not match"):
        _run(mode="plan", raw=raw, gateway=gateway, write_token_present=False)


def test_secret_first_failure_does_not_write_variables() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest), secret_error=RuntimeError("boom"))
    with pytest.raises(mod.AuthorityUpdateError, match="secret write failed"):
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_secret_uncertain_stops_without_variable_writes() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(
        snapshot=_snapshot(sha=digest),
        secret_error=mod.AuthorityWriteUncertain("secret outcome unknown"),
    )
    with pytest.raises(mod.AuthorityWriteUncertain):
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    assert gateway.recorder.variable_puts == []


def test_sha256_second_failure_does_not_retry_secret_or_write_source() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest), sha256_error=RuntimeError("var boom"))
    with pytest.raises(mod.AuthorityUpdateError, match="SHA256 write failed"):
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    assert len(gateway.recorder.secret_puts) == 1
    assert gateway.recorder.variable_puts == []


def test_sha256_uncertain_stops_without_source_write() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(
        snapshot=_snapshot(sha=digest),
        sha256_error=mod.AuthorityWriteUncertain("sha256 outcome unknown"),
    )
    with pytest.raises(mod.AuthorityWriteUncertain):
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    assert len(gateway.recorder.secret_puts) == 1
    assert gateway.recorder.variable_puts == []


def test_source_third_failure_does_not_retry_earlier_writes() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest), source_error=RuntimeError("source boom"))
    with pytest.raises(mod.AuthorityUpdateError, match="source revision write failed"):
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    assert len(gateway.recorder.secret_puts) == 1
    assert gateway.recorder.variable_puts == [
        (mod.FIXED_SHA256_VAR, _digest(gateway.recorder.secret_puts[0][1]))
    ]


def test_source_uncertain_stops_without_retry() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(
        snapshot=_snapshot(sha=digest),
        source_error=mod.AuthorityWriteUncertain("source outcome unknown"),
    )
    with pytest.raises(mod.AuthorityWriteUncertain):
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    assert len(gateway.recorder.secret_puts) == 1
    assert len(gateway.recorder.variable_puts) == 1
    assert gateway.recorder.variable_puts[0][0] == mod.FIXED_SHA256_VAR


def test_apply_success_records_three_writes_and_public_result_has_no_json_body() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))
    result = _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    assert result.status == "apply_submitted"
    assert len(gateway.recorder.secret_puts) == 1
    assert gateway.recorder.secret_puts[0][0] == mod.FIXED_SECRET_NAME
    assert gateway.recorder.variable_puts == [
        (mod.FIXED_SHA256_VAR, result.new_authority_sha256),
        (mod.FIXED_SOURCE_VAR, NEW_SOURCE),
    ]
    public = json.dumps(mod._result_to_public_dict(result))
    assert NEW_STRATEGY in public and NEW_RUNNER in public and NEW_SOURCE in public
    assert '"decision"' not in public
    assert "APPROVE" not in public
    assert "mandate_id" not in public
    assert raw.decode() not in public


def test_idempotent_already_at_target_skips_writes() -> None:
    raw = _authority_bytes(NEW_STRATEGY, NEW_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest, source=NEW_SOURCE))
    result = _run(
        mode="apply",
        raw=raw,
        gateway=gateway,
        write_token_present=True,
        expected_source=NEW_SOURCE,
        target_source=NEW_SOURCE,
    )
    assert result.status == "already_at_target"
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_source_only_write_when_versions_already_match() -> None:
    raw = _authority_bytes(NEW_STRATEGY, NEW_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest, source=OLD_SOURCE))
    result = _run(
        mode="apply",
        raw=raw,
        gateway=gateway,
        write_token_present=True,
        expected_source=OLD_SOURCE,
        target_source=NEW_SOURCE,
    )
    assert result.status == "apply_submitted"
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == [(mod.FIXED_SOURCE_VAR, NEW_SOURCE)]
    assert result.secret_write == "not_attempted"
    assert result.sha256_write == "not_attempted"
    assert result.source_write == "submitted"


def test_cli_main_plan_with_temp_file_sanitized_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    path = tmp_path / "authority.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    monkeypatch.setenv("MAINTENANCE_RUN_ID", "17")
    monkeypatch.delenv(mod.WRITE_TOKEN_ENV, raising=False)

    def _fake_gh(args, *, token, input_bytes=None):
        del token, input_bytes
        if tuple(args[:2]) == ("variable", "get"):
            return {
                mod.RUNTIME_ENABLED_VAR: "false",
                mod.FIXED_SHA256_VAR: digest,
                mod.FIXED_SOURCE_VAR: OLD_SOURCE,
            }[args[2]].encode()
        return b"[]"

    monkeypatch.setattr(mod, "_run_gh", _fake_gh)

    code = mod.main(
        [
            "--mode",
            "plan",
            "--target-strategy-revision",
            NEW_STRATEGY,
            "--target-runner-revision",
            NEW_RUNNER,
            "--target-source-revision",
            NEW_SOURCE,
            "--expected-authority-sha256",
            digest,
            "--expected-source-revision",
            OLD_SOURCE,
            "--authority-file",
            str(path),
            "--maintenance-workflow-sha",
            MAINT,
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "plan_ok" in out
    assert "APPROVE" not in out
    assert "mandate_id" not in out
    assert raw.decode() not in out


def test_errors_do_not_embed_authority_json_or_token() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(
        snapshot=_snapshot(sha=digest),
        secret_error=RuntimeError("token=super-secret body APPROVE mandate"),
    )
    with pytest.raises(mod.AuthorityUpdateError) as excinfo:
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    message = str(excinfo.value)
    assert message == "environment secret write failed"
    assert "super-secret" not in message
    assert "APPROVE" not in message
    assert raw.decode() not in message


def test_apply_precondition_drift_before_write_is_zero_write() -> None:
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)

    @dataclass
    class DriftGateway(mod.GithubGateway):
        first: mod.PreconditionSnapshot
        second: mod.PreconditionSnapshot
        recorder: mod.WriteRecorder = field(default_factory=mod.WriteRecorder)
        reads: int = 0

        def read_preconditions(self) -> mod.PreconditionSnapshot:
            self.reads += 1
            return self.first if self.reads == 1 else self.second

        def put_environment_secret(self, *, name: str, value: bytes) -> None:
            self.recorder.secret_puts.append((name, value))

        def put_environment_variable(self, *, name: str, value: str) -> None:
            self.recorder.variable_puts.append((name, value))

    gateway = DriftGateway(first=_snapshot(sha=digest), second=_snapshot(sha="3" * 64))
    with pytest.raises(mod.AuthorityUpdateError, match="SHA256 does not match"):
        _run(mode="apply", raw=raw, gateway=gateway, write_token_present=True)
    assert gateway.reads == 2
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_gh_cli_gateway_fresh_reads_and_write_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def _fake_gh(args, *, token, input_bytes=None):
        del token, input_bytes
        calls.append(tuple(args))
        if tuple(args[:2]) == ("variable", "get"):
            name = args[2]
            if name == mod.RUNTIME_ENABLED_VAR:
                assert "--env" not in args
                return b"false"
            assert "--env" in args
            return {mod.FIXED_SHA256_VAR: "1" * 64, mod.FIXED_SOURCE_VAR: OLD_SOURCE}[name].encode()
        if tuple(args[:2]) == ("run", "list"):
            return b"[]"
        return b""

    monkeypatch.setattr(mod, "_run_gh", _fake_gh)
    gateway = mod.GhCliGateway(write_token="tok", maintenance_run_id="17")
    snapshot = gateway.read_preconditions()
    assert snapshot.runtime_target_enabled == "false"
    gateway.put_environment_variable(name=mod.FIXED_SHA256_VAR, value="2" * 64)
    gateway.put_environment_variable(name=mod.FIXED_SOURCE_VAR, value=NEW_SOURCE)
    with pytest.raises(mod.AuthorityUpdateError, match="unexpected variable"):
        gateway.put_environment_variable(name="RUNTIME_TARGET_ENABLED", value="true")
    assert any(call[:3] == ("variable", "set", mod.FIXED_SHA256_VAR) for call in calls)
    assert any(call[:3] == ("variable", "set", mod.FIXED_SOURCE_VAR) for call in calls)


def test_explicit_approved_operator_hashes_are_accepted_as_inputs_not_defaults() -> None:
    """Documented operator targets must validate, but are never hard-coded defaults."""

    approved_strategy = "7fcca8ee0280b3e66245eb673caadda8b8b3b9a3"
    approved_runner = "5d922afa5093c16fe2470ea7761277fe35f50d6a"
    approved_source = "5d922afa5093c16fe2470ea7761277fe35f50d6a"
    raw = _authority_bytes(OLD_STRATEGY, OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))
    planned = _run(
        mode="plan",
        raw=raw,
        gateway=gateway,
        write_token_present=False,
        target_strategy=approved_strategy,
        target_runner=approved_runner,
        target_source=approved_source,
    )
    assert planned.status == "plan_ok"
    assert planned.target_strategy_revision == approved_strategy
    assert planned.target_runner_revision == approved_runner
    assert planned.target_source_revision == approved_source
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert approved_strategy not in source
    assert "7fcca8ee" not in source
