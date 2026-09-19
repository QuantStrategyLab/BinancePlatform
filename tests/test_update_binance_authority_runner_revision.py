"""Synthetic tests for one-shot authority runner_revision updates."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts import update_binance_authority_runner_revision as mod

OLD_RUNNER = "a" * 40
NEW_RUNNER = "b" * 40
OTHER_RUNNER = "c" * 40
SOURCE = "d" * 40
MAINT = "e" * 40


def _authority_bytes(runner: str, *, extra: str = "") -> bytes:
    # Compact JSON without trailing newline (matches historical write hygiene).
    payload = (
        "{"
        f'"decision":"APPROVE",'
        f'"authority_scope":"LIVE",'
        f'"runtime_target":{{"platform_id":"binance"}},'
        f'"strategy_revision":"{"f" * 40}",'
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


def _snapshot(*, enabled: str = "false", sha: str, source: str = SOURCE, runs: tuple[str, ...] = ()) -> mod.PreconditionSnapshot:
    return mod.PreconditionSnapshot(
        runtime_target_enabled=enabled,
        authority_sha256=sha,
        source_revision=source,
        in_progress_run_ids=runs,
    )


def test_wrong_old_digest_rejects_without_writes() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    claimed = "0" * 64
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=claimed))
    with pytest.raises(mod.AuthorityUpdateError, match="do not match expected_authority_sha256"):
        mod.run_update(
            mode="plan",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=claimed,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=False,
        )
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_illegal_target_and_live_sha_drift_reject() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))
    with pytest.raises(mod.AuthorityUpdateError, match="target_runner_revision"):
        mod.run_update(
            mode="plan",
            raw_authority=raw,
            target_runner_revision="deadbeef",
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=False,
        )
    gateway.snapshot = _snapshot(sha="2" * 64)
    with pytest.raises(mod.AuthorityUpdateError, match="SHA256 does not match"):
        mod.run_update(
            mode="plan",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=False,
        )


def test_json_ambiguity_duplicate_key_and_nonfinite_rejected() -> None:
    dup = (
        b'{"decision":"APPROVE","authority_scope":"LIVE","runtime_target":{},'
        b'"strategy_revision":"' + (b"f" * 40) + b'","runner_revision":"' + (OLD_RUNNER.encode())
        + b'","runner_revision":"' + (NEW_RUNNER.encode())
        + b'","config_sha256":"' + (b"1" * 64) + b'","continuous_inputs_allowed":true,"mandate":{}}'
    )
    with pytest.raises(mod.AuthorityUpdateError, match="duplicate"):
        mod.patch_runner_revision_bytes(dup, target_runner_revision=OTHER_RUNNER)

    bad = (
        b'{"decision":"APPROVE","authority_scope":"LIVE","runtime_target":{},'
        b'"strategy_revision":"' + (b"f" * 40) + b'","runner_revision":"' + (OLD_RUNNER.encode())
        + b'","config_sha256":"' + (b"1" * 64)
        + b'","continuous_inputs_allowed":true,"mandate":{"x":NaN}}'
    )
    with pytest.raises(mod.AuthorityUpdateError, match="non-finite|invalid constants|malformed"):
        mod.patch_runner_revision_bytes(bad, target_runner_revision=NEW_RUNNER)


def test_non_runner_fields_remain_semantically_equal() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    new_raw, old, new = mod.patch_runner_revision_bytes(raw, target_runner_revision=NEW_RUNNER)
    assert old == OLD_RUNNER and new == NEW_RUNNER
    before = mod._strict_json_object(raw)
    after = mod._strict_json_object(new_raw)
    assert mod._semantic_without_runner(before) == mod._semantic_without_runner(after)
    assert after["runner_revision"] == NEW_RUNNER


def test_plan_is_zero_write_and_apply_requires_token() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))
    planned = mod.run_update(
        mode="plan",
        raw_authority=raw,
        target_runner_revision=NEW_RUNNER,
        expected_authority_sha256=digest,
        expected_source_revision=SOURCE,
        gateway=gateway,
        maintenance_workflow_sha=MAINT,
        write_token_present=False,
    )
    assert planned.status == "plan_ok"
    assert planned.secret_write == "not_attempted"
    assert gateway.recorder.secret_puts == []

    with pytest.raises(mod.AuthorityUpdateError, match="BINANCE_AUTHORITY_UPDATE_TOKEN"):
        mod.run_update(
            mode="apply",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=False,
        )
    assert gateway.recorder.secret_puts == []


@pytest.mark.parametrize(
    "enabled,runs,match",
    [
        ("true", (), "RUNTIME_TARGET_ENABLED"),
        ("false", ("1",), "in-progress"),
    ],
)
def test_runtime_enabled_or_in_progress_rejected(enabled: str, runs: tuple[str, ...], match: str) -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(enabled=enabled, sha=digest, runs=runs))
    with pytest.raises(mod.AuthorityUpdateError, match=match):
        mod.run_update(
            mode="plan",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=False,
        )


def test_secret_first_failure_does_not_write_variable() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(
        snapshot=_snapshot(sha=digest),
        secret_error=RuntimeError("boom"),
    )
    with pytest.raises(mod.AuthorityUpdateError, match="secret write failed"):
        mod.run_update(
            mode="apply",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=True,
        )
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_secret_uncertain_stops_without_variable_write() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(
        snapshot=_snapshot(sha=digest),
        secret_error=mod.AuthorityWriteUncertain("secret outcome unknown"),
    )
    with pytest.raises(mod.AuthorityWriteUncertain):
        mod.run_update(
            mode="apply",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=True,
        )
    assert gateway.recorder.variable_puts == []


def test_variable_second_failure_does_not_retry_secret() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(
        snapshot=_snapshot(sha=digest),
        variable_error=RuntimeError("var boom"),
    )
    with pytest.raises(mod.AuthorityUpdateError, match="variable write failed"):
        mod.run_update(
            mode="apply",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=True,
        )
    assert len(gateway.recorder.secret_puts) == 1
    assert gateway.recorder.variable_puts == []


def test_apply_success_records_both_writes_and_public_result_has_no_json_body() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))
    result = mod.run_update(
        mode="apply",
        raw_authority=raw,
        target_runner_revision=NEW_RUNNER,
        expected_authority_sha256=digest,
        expected_source_revision=SOURCE,
        gateway=gateway,
        maintenance_workflow_sha=MAINT,
        write_token_present=True,
    )
    assert result.status == "apply_submitted"
    assert len(gateway.recorder.secret_puts) == 1
    assert gateway.recorder.secret_puts[0][0] == mod.FIXED_SECRET_NAME
    assert gateway.recorder.variable_puts == [(mod.FIXED_SHA256_VAR, result.new_authority_sha256)]
    public = json.dumps(mod._result_to_public_dict(result))
    assert NEW_RUNNER in public
    assert '"decision"' not in public
    assert "APPROVE" not in public
    assert raw.decode() not in public


def test_idempotent_already_at_target_skips_writes() -> None:
    raw = _authority_bytes(NEW_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))
    result = mod.run_update(
        mode="apply",
        raw_authority=raw,
        target_runner_revision=NEW_RUNNER,
        expected_authority_sha256=digest,
        expected_source_revision=SOURCE,
        gateway=gateway,
        maintenance_workflow_sha=MAINT,
        write_token_present=True,
    )
    assert result.status == "already_at_target"
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_gh_cli_gateway_is_patched_and_never_invokes_real_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    def _fake_run(args, **kwargs):
        calls.append((tuple(args), kwargs.get("input")))
        raise AssertionError("subprocess.run should be patched in unit tests that use FakeGateway")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest))
    mod.run_update(
        mode="plan",
        raw_authority=raw,
        target_runner_revision=NEW_RUNNER,
        expected_authority_sha256=digest,
        expected_source_revision=SOURCE,
        gateway=gateway,
        maintenance_workflow_sha=MAINT,
        write_token_present=False,
    )
    assert calls == []


def test_cli_main_plan_with_temp_file_uses_readonly_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    path = tmp_path / "authority.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    monkeypatch.setenv("LIVE_RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("LIVE_BINANCE_RISK_AUTHORITY_SHA256", digest)
    monkeypatch.setenv("LIVE_BINANCE_RISK_AUTHORITY_SOURCE_REVISION", SOURCE)
    monkeypatch.setenv("LIVE_IN_PROGRESS_RUNS_JSON", "[]")
    monkeypatch.setenv("MAINTENANCE_RUN_ID", "17")
    monkeypatch.delenv(mod.WRITE_TOKEN_ENV, raising=False)

    def _fake_gh(args, *, token, input_bytes=None):
        del token, input_bytes
        if tuple(args[:2]) == ("variable", "get"):
            return {
                mod.RUNTIME_ENABLED_VAR: "false",
                mod.FIXED_SHA256_VAR: digest,
                mod.FIXED_SOURCE_VAR: SOURCE,
            }[args[2]].encode()
        return b"[]"

    monkeypatch.setattr(mod, "_run_gh", _fake_gh)

    code = mod.main(
        [
            "--mode",
            "plan",
            "--target-runner-revision",
            NEW_RUNNER,
            "--expected-authority-sha256",
            digest,
            "--expected-source-revision",
            SOURCE,
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
    assert raw.decode() not in out


def test_runtime_enabled_is_read_from_repository_scope_not_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def _fake_gh(args, *, token, input_bytes=None):
        del token, input_bytes
        calls.append(tuple(args))
        if tuple(args[:2]) != ("variable", "get"):
            return b"[]"
        name = args[2]
        argv = tuple(args)
        if name == mod.RUNTIME_ENABLED_VAR:
            assert "--env" not in argv
            assert argv == (
                "variable",
                "get",
                mod.RUNTIME_ENABLED_VAR,
                "--repo",
                mod.FIXED_REPOSITORY,
            )
            return b"false"
        assert "--env" in argv
        assert argv == (
            "variable",
            "get",
            name,
            "--repo",
            mod.FIXED_REPOSITORY,
            "--env",
            mod.FIXED_ENVIRONMENT,
        )
        return {mod.FIXED_SHA256_VAR: "1" * 64, mod.FIXED_SOURCE_VAR: SOURCE}[name].encode()

    monkeypatch.setattr(mod, "_run_gh", _fake_gh)
    snapshot = mod.GhCliGateway(maintenance_run_id="17").read_preconditions()
    assert snapshot.runtime_target_enabled == "false"
    assert snapshot.authority_sha256 == "1" * 64
    assert snapshot.source_revision == SOURCE
    enabled_call = next(call for call in calls if call[2] == mod.RUNTIME_ENABLED_VAR)
    assert "--env" not in enabled_call
    for name in (mod.FIXED_SHA256_VAR, mod.FIXED_SOURCE_VAR):
        call = next(call for call in calls if call[2] == name)
        assert call[call.index("--env") + 1] == mod.FIXED_ENVIRONMENT


@pytest.mark.parametrize(
    "failing_name,expect_env_scope",
    [
        (mod.RUNTIME_ENABLED_VAR, False),
        (mod.FIXED_SHA256_VAR, True),
        (mod.FIXED_SOURCE_VAR, True),
    ],
)
def test_missing_or_permission_failure_on_scoped_reads_is_zero_write(
    monkeypatch: pytest.MonkeyPatch,
    failing_name: str,
    expect_env_scope: bool,
) -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    write_attempts: list[tuple[str, ...]] = []

    def _fake_gh(args, *, token, input_bytes=None):
        del token
        argv = tuple(args)
        if argv[:2] in {("secret", "set"), ("variable", "set")}:
            write_attempts.append(argv)
            return b""
        if tuple(args[:2]) == ("variable", "get") and args[2] == failing_name:
            if expect_env_scope:
                assert "--env" in args
            else:
                assert "--env" not in args
            raise mod.AuthorityUpdateError("gh command failed (1)")
        if tuple(args[:2]) == ("variable", "get"):
            return {
                mod.RUNTIME_ENABLED_VAR: "false",
                mod.FIXED_SHA256_VAR: digest,
                mod.FIXED_SOURCE_VAR: SOURCE,
            }[args[2]].encode()
        return b"[]"

    monkeypatch.setattr(mod, "_run_gh", _fake_gh)
    monkeypatch.setenv("MAINTENANCE_RUN_ID", "17")
    monkeypatch.setenv(mod.WRITE_TOKEN_ENV, "test-token")
    gateway = mod.GhCliGateway(write_token="test-token", maintenance_run_id="17")
    with pytest.raises(mod.AuthorityUpdateError, match="gh command failed"):
        mod.run_update(
            mode="apply",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=True,
        )
    assert write_attempts == []


def test_apply_precondition_drift_before_write_is_zero_write() -> None:
    raw = _authority_bytes(OLD_RUNNER)
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

    gateway = DriftGateway(
        first=_snapshot(sha=digest),
        second=_snapshot(sha="3" * 64),
    )
    with pytest.raises(mod.AuthorityUpdateError, match="SHA256 does not match"):
        mod.run_update(
            mode="apply",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=True,
        )
    assert gateway.reads == 2
    assert gateway.recorder.secret_puts == []
    assert gateway.recorder.variable_puts == []


def test_gh_cli_gateway_fresh_reads_exclude_only_current_maintenance_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    snapshots = iter([
        ("false", "1" * 64, SOURCE, [{"databaseId": 17, "name": "authority update", "status": "in_progress"}]),
        ("false", "2" * 64, SOURCE, [{"databaseId": 17, "name": "authority update", "status": "in_progress"}]),
    ])
    state: dict[str, tuple] = {}

    def _fresh_gh(args, *, token, input_bytes=None):
        del token, input_bytes
        calls.append(tuple(args))
        if tuple(args[:2]) == ("variable", "get"):
            snapshot = state.get("current")
            if snapshot is None:
                snapshot = next(snapshots)
                state["current"] = snapshot
            name = args[2]
            return {
                mod.RUNTIME_ENABLED_VAR: snapshot[0],
                mod.FIXED_SHA256_VAR: snapshot[1],
                mod.FIXED_SOURCE_VAR: snapshot[2],
            }[name].encode()
        snapshot = state.pop("current", None) or next(snapshots)
        return json.dumps(snapshot[3]).encode()

    monkeypatch.setattr(mod, "_run_gh", _fresh_gh)
    gateway = mod.GhCliGateway(maintenance_run_id="17")
    first = gateway.read_preconditions()
    second = gateway.read_preconditions()

    assert first.in_progress_run_ids == ()
    assert second.in_progress_run_ids == ()
    assert first.authority_sha256 == "1" * 64
    assert second.authority_sha256 == "2" * 64
    assert sum(tuple(args[:2]) == ("variable", "get") for args in calls) == 6
    assert sum(tuple(args[:2]) == ("run", "list") for args in calls) == 2


def test_gh_cli_gateway_keeps_other_in_progress_runs_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fresh_gh(args, *, token, input_bytes=None):
        del token, input_bytes
        if tuple(args[:2]) == ("variable", "get"):
            return {
                mod.RUNTIME_ENABLED_VAR: "false",
                mod.FIXED_SHA256_VAR: "1" * 64,
                mod.FIXED_SOURCE_VAR: SOURCE,
            }[args[2]].encode()
        return json.dumps(
            [
                {"databaseId": 17, "name": "authority update", "status": "in_progress"},
                {"databaseId": 18, "name": "Runtime", "status": "in_progress"},
            ]
        ).encode()

    monkeypatch.setattr(mod, "_run_gh", _fresh_gh)
    gateway = mod.GhCliGateway(maintenance_run_id="17")
    snapshot = gateway.read_preconditions()
    assert snapshot.in_progress_run_ids == ("18",)


def test_errors_do_not_embed_authority_json_or_token() -> None:
    raw = _authority_bytes(OLD_RUNNER)
    digest = _digest(raw)
    gateway = mod.FakeGateway(snapshot=_snapshot(sha=digest), secret_error=RuntimeError("token=super-secret body"))
    with pytest.raises(mod.AuthorityUpdateError) as excinfo:
        mod.run_update(
            mode="apply",
            raw_authority=raw,
            target_runner_revision=NEW_RUNNER,
            expected_authority_sha256=digest,
            expected_source_revision=SOURCE,
            gateway=gateway,
            maintenance_workflow_sha=MAINT,
            write_token_present=True,
        )
    message = str(excinfo.value)
    assert message == "environment secret write failed"
    assert "super-secret" not in message
    assert raw.decode() not in message
