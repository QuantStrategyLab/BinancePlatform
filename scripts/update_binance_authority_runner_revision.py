"""One-shot LIVE authority runner_revision update (no trading enablement).

Updates only ``runner_revision`` inside the fixed binance-runtime secret
``BINANCE_RISK_AUTHORITY_JSON`` and the matching ``BINANCE_RISK_AUTHORITY_SHA256``
variable.  Does not grant orders, change RECONCILE_ONLY, or auto-dispatch
validation.  Default mode is plan (zero writes).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXED_REPOSITORY = "QuantStrategyLab/BinancePlatform"
FIXED_ENVIRONMENT = "binance-runtime"
FIXED_SECRET_NAME = "BINANCE_RISK_AUTHORITY_JSON"
FIXED_SHA256_VAR = "BINANCE_RISK_AUTHORITY_SHA256"
FIXED_SOURCE_VAR = "BINANCE_RISK_AUTHORITY_SOURCE_REVISION"
RUNTIME_ENABLED_VAR = "RUNTIME_TARGET_ENABLED"
WRITE_TOKEN_ENV = "BINANCE_AUTHORITY_UPDATE_TOKEN"

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUNNER_FIELD = re.compile(
    r'("runner_revision"\s*:\s*")([0-9a-f]{40})(")'
)


class AuthorityUpdateError(ValueError):
    """Fail-closed precondition or payload error (sanitized message only)."""


class AuthorityWriteUncertain(RuntimeError):
    """A write may have succeeded; do not retry or roll back automatically."""


@dataclass(frozen=True)
class PreconditionSnapshot:
    runtime_target_enabled: str
    authority_sha256: str
    source_revision: str
    in_progress_run_ids: tuple[str, ...]


@dataclass
class UpdateResult:
    mode: str
    status: str
    target_runner_revision: str
    old_authority_sha256: str
    new_authority_sha256: str
    source_revision: str
    maintenance_workflow_sha: str
    secret_write: str = "not_attempted"
    variable_write: str = "not_attempted"
    notes: tuple[str, ...] = ()


@dataclass
class WriteRecorder:
    secret_puts: list[tuple[str, bytes]] = field(default_factory=list)
    variable_puts: list[tuple[str, str]] = field(default_factory=list)


class GithubGateway:
    """Read/write surface; production uses gh CLI, tests inject fakes."""

    def read_preconditions(self) -> PreconditionSnapshot:
        raise NotImplementedError

    def put_environment_secret(self, *, name: str, value: bytes) -> None:
        raise NotImplementedError

    def put_environment_variable(self, *, name: str, value: str) -> None:
        raise NotImplementedError


def _require_sha(value: str, *, field_name: str, length: int) -> str:
    text = (value or "").strip().lower()
    pattern = _FULL_SHA if length == 40 else _SHA256
    if pattern.fullmatch(text) is None:
        raise AuthorityUpdateError(f"invalid {field_name}")
    return text


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def _reject_constant(token: str) -> Any:
        raise AuthorityUpdateError("authority JSON contains non-finite or invalid constants")

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise AuthorityUpdateError("authority JSON contains duplicate keys")
            out[key] = value
        return out

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_pairs,
        )
    except UnicodeDecodeError as exc:
        raise AuthorityUpdateError("authority JSON is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise AuthorityUpdateError("authority JSON is malformed") from exc
    if not isinstance(payload, dict):
        raise AuthorityUpdateError("authority JSON root must be an object")
    return payload


def _semantic_without_runner(payload: Mapping[str, Any]) -> Any:
    return {key: value for key, value in payload.items() if key != "runner_revision"}


def patch_runner_revision_bytes(raw: bytes, *, target_runner_revision: str) -> tuple[bytes, str, str]:
    """Return (new_bytes, old_runner, new_runner) with a surgical field patch."""

    target = _require_sha(target_runner_revision, field_name="target_runner_revision", length=40)
    payload = _strict_json_object(raw)
    if "runner_revision" not in payload:
        raise AuthorityUpdateError("authority JSON missing runner_revision")
    old_runner = payload.get("runner_revision")
    if type(old_runner) is not str or _FULL_SHA.fullmatch(old_runner) is None:
        raise AuthorityUpdateError("authority JSON runner_revision is invalid")

    if old_runner == target:
        again = _strict_json_object(raw)
        if _semantic_without_runner(again) != _semantic_without_runner(payload):
            raise AuthorityUpdateError("authority JSON semantic mismatch on reparse")
        return raw, old_runner, target

    text = raw.decode("utf-8")
    matches = list(_RUNNER_FIELD.finditer(text))
    if len(matches) != 1:
        raise AuthorityUpdateError("authority JSON runner_revision field is ambiguous")
    if matches[0].group(2) != old_runner:
        raise AuthorityUpdateError("authority JSON runner_revision encoding mismatch")

    patched = _RUNNER_FIELD.sub(
        lambda match: f"{match.group(1)}{target}{match.group(3)}",
        text,
        count=1,
    )
    new_raw = patched.encode("utf-8")
    if raw.endswith((b"\n", b"\r")) != new_raw.endswith((b"\n", b"\r")):
        raise AuthorityUpdateError("authority JSON newline framing changed")

    new_payload = _strict_json_object(new_raw)
    if new_payload.get("runner_revision") != target:
        raise AuthorityUpdateError("patched runner_revision mismatch")
    if _semantic_without_runner(new_payload) != _semantic_without_runner(payload):
        raise AuthorityUpdateError("non-runner authority fields would change")
    return new_raw, old_runner, target


def verify_old_digest(raw: bytes, *, expected_sha256: str) -> str:
    expected = _require_sha(expected_sha256, field_name="expected_authority_sha256", length=64)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise AuthorityUpdateError("authority bytes do not match expected_authority_sha256")
    return actual


def assert_preconditions(
    snapshot: PreconditionSnapshot,
    *,
    expected_sha256: str,
    expected_source_revision: str,
) -> None:
    expected_sha = _require_sha(expected_sha256, field_name="expected_authority_sha256", length=64)
    expected_source = _require_sha(
        expected_source_revision, field_name="expected_source_revision", length=40
    )
    enabled = (snapshot.runtime_target_enabled or "").strip().lower()
    if enabled != "false":
        raise AuthorityUpdateError("RUNTIME_TARGET_ENABLED must be false before apply/plan")
    if (snapshot.authority_sha256 or "").strip().lower() != expected_sha:
        raise AuthorityUpdateError("live BINANCE_RISK_AUTHORITY_SHA256 does not match expectation")
    if (snapshot.source_revision or "").strip().lower() != expected_source:
        raise AuthorityUpdateError("live BINANCE_RISK_AUTHORITY_SOURCE_REVISION does not match expectation")
    # Explicit empty tuple is OK; None/missing handled by gateway raising before this.
    if snapshot.in_progress_run_ids:
        raise AuthorityUpdateError("in-progress Runtime-related runs are present")


def run_update(
    *,
    mode: str,
    raw_authority: bytes,
    target_runner_revision: str,
    expected_authority_sha256: str,
    expected_source_revision: str,
    gateway: GithubGateway,
    maintenance_workflow_sha: str,
    write_token_present: bool,
) -> UpdateResult:
    mode_normalized = (mode or "").strip().lower()
    if mode_normalized not in {"plan", "apply"}:
        raise AuthorityUpdateError("mode must be plan or apply")
    maintenance = _require_sha(
        maintenance_workflow_sha, field_name="maintenance_workflow_sha", length=40
    )
    target = _require_sha(target_runner_revision, field_name="target_runner_revision", length=40)

    snapshot = gateway.read_preconditions()
    assert_preconditions(
        snapshot,
        expected_sha256=expected_authority_sha256,
        expected_source_revision=expected_source_revision,
    )
    old_digest = verify_old_digest(raw_authority, expected_sha256=expected_authority_sha256)
    new_raw, old_runner, new_runner = patch_runner_revision_bytes(
        raw_authority, target_runner_revision=target
    )
    new_digest = hashlib.sha256(new_raw).hexdigest()

    notes: list[str] = [
        "maintenance_workflow_sha is not the trading runner_revision",
        "workflow concurrency does not lock external secret/variable writers; "
        "coordinate operators before apply",
        "API success is not proof of byte-identical cloud consume; run no-submit "
        "full_cycle separately after apply",
    ]

    if old_runner == new_runner:
        return UpdateResult(
            mode=mode_normalized,
            status="already_at_target",
            target_runner_revision=target,
            old_authority_sha256=old_digest,
            new_authority_sha256=new_digest,
            source_revision=_require_sha(
                expected_source_revision, field_name="expected_source_revision", length=40
            ),
            maintenance_workflow_sha=maintenance,
            notes=tuple(notes + ["no secret or variable write required"]),
        )

    if mode_normalized == "plan":
        return UpdateResult(
            mode=mode_normalized,
            status="plan_ok",
            target_runner_revision=target,
            old_authority_sha256=old_digest,
            new_authority_sha256=new_digest,
            source_revision=_require_sha(
                expected_source_revision, field_name="expected_source_revision", length=40
            ),
            maintenance_workflow_sha=maintenance,
            notes=tuple(notes + ["plan mode performs zero writes"]),
        )

    if not write_token_present:
        raise AuthorityUpdateError("apply requires BINANCE_AUTHORITY_UPDATE_TOKEN")

    # Re-read immediately before writes (drift check).
    snapshot_again = gateway.read_preconditions()
    assert_preconditions(
        snapshot_again,
        expected_sha256=expected_authority_sha256,
        expected_source_revision=expected_source_revision,
    )

    result = UpdateResult(
        mode=mode_normalized,
        status="apply_incomplete",
        target_runner_revision=target,
        old_authority_sha256=old_digest,
        new_authority_sha256=new_digest,
        source_revision=_require_sha(
            expected_source_revision, field_name="expected_source_revision", length=40
        ),
        maintenance_workflow_sha=maintenance,
        notes=tuple(notes),
    )

    try:
        gateway.put_environment_secret(name=FIXED_SECRET_NAME, value=new_raw)
        result.secret_write = "submitted"
    except AuthorityWriteUncertain:
        result.secret_write = "uncertain"
        result.status = "stopped_after_secret_uncertain"
        raise
    except Exception as exc:
        result.secret_write = "failed"
        result.status = "stopped_before_variable_write"
        raise AuthorityUpdateError("environment secret write failed") from exc

    try:
        gateway.put_environment_variable(name=FIXED_SHA256_VAR, value=new_digest)
        result.variable_write = "submitted"
    except AuthorityWriteUncertain:
        result.variable_write = "uncertain"
        result.status = "stopped_after_variable_uncertain"
        raise
    except Exception as exc:
        result.variable_write = "failed"
        result.status = "stopped_after_secret_before_variable_ok"
        raise AuthorityUpdateError(
            "environment variable write failed after secret submit; do not retry blindly"
        ) from exc

    result.status = "apply_submitted"
    return result


def _run_gh(args: Sequence[str], *, token: str | None, input_bytes: bytes | None = None) -> bytes:
    env = os.environ.copy()
    if token is not None:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    try:
        completed = subprocess.run(
            ["gh", *args],
            input=input_bytes,
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise AuthorityUpdateError("gh CLI is unavailable") from exc
    if completed.returncode != 0:
        # Never include stdout/stderr bodies (may contain sensitive material).
        raise AuthorityUpdateError(f"gh command failed ({completed.returncode})")
    return completed.stdout


class GhCliGateway(GithubGateway):
    def __init__(self, *, write_token: str | None = None, maintenance_run_id: str | None = None):
        self._write_token = write_token
        self._maintenance_run_id = (maintenance_run_id or "").strip()

    def _read_environment_variable(self, name: str) -> str:
        value = _run_gh(
            [
                "variable",
                "get",
                name,
                "--repo",
                FIXED_REPOSITORY,
                "--env",
                FIXED_ENVIRONMENT,
            ],
            token=None,
        )
        return value.decode("utf-8").strip()

    def _read_repository_variable(self, name: str) -> str:
        value = _run_gh(
            ["variable", "get", name, "--repo", FIXED_REPOSITORY],
            token=None,
        )
        return value.decode("utf-8").strip()

    def read_preconditions(self) -> PreconditionSnapshot:
        if not self._maintenance_run_id.isdigit() or int(self._maintenance_run_id) <= 0:
            raise AuthorityUpdateError("current maintenance run id is invalid")

        # Every read is a fresh read. Cached LIVE_* values would make the
        # apply-time drift check indistinguishable from the initial check.
        # RUNTIME_TARGET_ENABLED is a repository variable.  The authority
        # digest/source metadata remain environment-scoped and must not be
        # silently read from the repository scope.
        enabled = self._read_repository_variable(RUNTIME_ENABLED_VAR)
        sha = self._read_environment_variable(FIXED_SHA256_VAR)
        source = self._read_environment_variable(FIXED_SOURCE_VAR)
        try:
            in_progress_json = _run_gh(
                [
                    "run",
                    "list",
                    "--repo",
                    FIXED_REPOSITORY,
                    "--status",
                    "in_progress",
                    "--limit",
                    "100",
                    "--json",
                    "databaseId,name,status",
                ],
                token=None,
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuthorityUpdateError("in-progress run inventory is malformed") from exc
        try:
            parsed = json.loads(in_progress_json)
        except json.JSONDecodeError as exc:
            raise AuthorityUpdateError("in-progress run inventory is malformed") from exc
        if not isinstance(parsed, list):
            raise AuthorityUpdateError("in-progress run inventory is malformed")
        run_ids: list[str] = []
        for item in parsed:
            if not isinstance(item, Mapping):
                raise AuthorityUpdateError("in-progress run inventory is malformed")
            run_id = item.get("databaseId")
            name = str(item.get("name") or "")
            status = str(item.get("status") or "")
            if status != "in_progress":
                continue
            if str(run_id) == self._maintenance_run_id:
                continue
            # Only the exact current maintenance run may be excluded. Any
            # other in-progress run remains a blocking precondition.
            run_ids.append(str(run_id))
            _ = name
        return PreconditionSnapshot(
            runtime_target_enabled=str(enabled),
            authority_sha256=str(sha),
            source_revision=str(source),
            in_progress_run_ids=tuple(run_ids),
        )

    def put_environment_secret(self, *, name: str, value: bytes) -> None:
        if not self._write_token:
            raise AuthorityUpdateError("apply requires BINANCE_AUTHORITY_UPDATE_TOKEN")
        if name != FIXED_SECRET_NAME:
            raise AuthorityUpdateError("refusing to write unexpected secret name")
        _run_gh(
            [
                "secret",
                "set",
                name,
                "--repo",
                FIXED_REPOSITORY,
                "--env",
                FIXED_ENVIRONMENT,
            ],
            token=self._write_token,
            input_bytes=value,
        )

    def put_environment_variable(self, *, name: str, value: str) -> None:
        if not self._write_token:
            raise AuthorityUpdateError("apply requires BINANCE_AUTHORITY_UPDATE_TOKEN")
        if name != FIXED_SHA256_VAR:
            raise AuthorityUpdateError("refusing to write unexpected variable name")
        _run_gh(
            [
                "variable",
                "set",
                name,
                "--repo",
                FIXED_REPOSITORY,
                "--env",
                FIXED_ENVIRONMENT,
                "--body",
                value,
            ],
            token=self._write_token,
        )


@dataclass
class FakeGateway(GithubGateway):
    snapshot: PreconditionSnapshot
    recorder: WriteRecorder = field(default_factory=WriteRecorder)
    secret_error: Exception | None = None
    variable_error: Exception | None = None
    read_count: int = 0

    def read_preconditions(self) -> PreconditionSnapshot:
        self.read_count += 1
        return self.snapshot

    def put_environment_secret(self, *, name: str, value: bytes) -> None:
        if self.secret_error is not None:
            raise self.secret_error
        self.recorder.secret_puts.append((name, value))

    def put_environment_variable(self, *, name: str, value: str) -> None:
        if self.variable_error is not None:
            raise self.variable_error
        self.recorder.variable_puts.append((name, value))


def _result_to_public_dict(result: UpdateResult) -> dict[str, Any]:
    return {
        "mode": result.mode,
        "status": result.status,
        "repository": FIXED_REPOSITORY,
        "environment": FIXED_ENVIRONMENT,
        "secret_name": FIXED_SECRET_NAME,
        "sha256_var": FIXED_SHA256_VAR,
        "target_runner_revision": result.target_runner_revision,
        "old_authority_sha256": result.old_authority_sha256,
        "new_authority_sha256": result.new_authority_sha256,
        "source_revision": result.source_revision,
        "maintenance_workflow_sha": result.maintenance_workflow_sha,
        "secret_write": result.secret_write,
        "variable_write": result.variable_write,
        "notes": list(result.notes),
    }


def _load_authority_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    mode = path.stat().st_mode
    if stat.S_IMODE(mode) & 0o077:
        # Best-effort warning path: still fail closed on world/group readable.
        raise AuthorityUpdateError("authority temp file mode is too permissive")
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "apply"), required=True)
    parser.add_argument("--target-runner-revision", required=True)
    parser.add_argument("--expected-authority-sha256", required=True)
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--authority-file", required=True)
    parser.add_argument(
        "--maintenance-workflow-sha",
        default=os.environ.get("GITHUB_SHA", ""),
        help="SHA of the maintenance workflow revision (not trading runner)",
    )
    args = parser.parse_args(argv)

    authority_path = Path(args.authority_file)
    write_token = os.environ.get(WRITE_TOKEN_ENV, "")
    write_token_present = bool(write_token.strip())
    if args.mode == "plan":
        write_token_present = False
        write_token = ""

    gateway: GithubGateway = GhCliGateway(
        write_token=write_token or None,
        maintenance_run_id=os.environ.get("MAINTENANCE_RUN_ID"),
    )
    try:
        raw = _load_authority_bytes(authority_path)
        result = run_update(
            mode=args.mode,
            raw_authority=raw,
            target_runner_revision=args.target_runner_revision,
            expected_authority_sha256=args.expected_authority_sha256,
            expected_source_revision=args.expected_source_revision,
            gateway=gateway,
            maintenance_workflow_sha=args.maintenance_workflow_sha,
            write_token_present=write_token_present and args.mode == "apply",
        )
        print(json.dumps(_result_to_public_dict(result), separators=(",", ":"), sort_keys=True))
        return 0
    except AuthorityWriteUncertain as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 3
    except AuthorityUpdateError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    finally:
        # Caller also truncates; never log file contents.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
