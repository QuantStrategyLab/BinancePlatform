#!/usr/bin/env python3
"""Select and validate the latest bounded scheduler observation artifact."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_REPOSITORY = "QuantStrategyLab/BinancePlatform"
_WORKFLOW_PATH = ".github/workflows/runtime-target-lifecycle.yml"
_JOB_NAME = "Observe Binance scheduler"
_ARTIFACT_PREFIX = "binance-scheduler-observation-"
_MAX_AGE = timedelta(minutes=75)
_MAX_API_BODY = 256 * 1024
_MAX_ARTIFACT_SIZE = 1024
_MAX_RUNS = 10
_MAX_JOBS = 10
_MAX_ARTIFACTS = 10
_VALID_STATES = frozenset({"enabled", "disabled", "unknown"})
_SHA256 = re.compile(r"^[0-9a-f]{40}$")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_OPENER = build_opener(_NoRedirectHandler())


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _api_base(value: str | None) -> str | None:
    raw = (value or "").rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path:
        return None
    return raw


def _api_json(url: str, token: str) -> dict[str, Any] | None:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Binance-Runtime-Scheduler-Observer",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with _OPENER.open(request, timeout=10) as response:
            if response.status != 200:
                return None
            body = response.read(_MAX_API_BODY + 1)
    except (HTTPError, URLError, OSError, TimeoutError):
        return None
    if len(body) > _MAX_API_BODY:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_repository_run(run: object, now: datetime) -> bool:
    if not isinstance(run, dict):
        return False
    repository = run.get("repository")
    created_at = _timestamp(run.get("created_at"))
    run_id = run.get("id")
    attempt = run.get("run_attempt")
    return bool(
        type(run_id) is int
        and run_id > 0
        and type(attempt) is int
        and attempt > 0
        and isinstance(repository, dict)
        and repository.get("full_name") == _REPOSITORY
        and str(run.get("path") or "").split("@", 1)[0] == _WORKFLOW_PATH
        and run.get("head_branch") == "main"
        and run.get("event") in {"schedule", "workflow_dispatch"}
        and isinstance(run.get("head_sha"), str)
        and _SHA256.fullmatch(run["head_sha"]) is not None
        and created_at is not None
        and created_at <= now
        and now - created_at <= _MAX_AGE
    )


def select_latest_observation(
    *,
    token: str,
    api_base: str,
    now: datetime | None = None,
    get_json: Callable[[str, str], dict[str, Any] | None] = _api_json,
) -> dict[str, str] | None:
    """Return a bounded locator for the latest successful main observer job."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not token or _api_base(api_base) is None:
        return None
    base = api_base.rstrip("/")
    runs_url = (
        f"{base}/repos/{_REPOSITORY}/actions/workflows/runtime-target-lifecycle.yml"
        "/runs?branch=main&per_page=10"
    )
    runs_payload = get_json(runs_url, token)
    rows = runs_payload.get("workflow_runs") if isinstance(runs_payload, dict) else None
    if not isinstance(rows, list) or len(rows) > _MAX_RUNS:
        return None
    runs = [run for run in rows if _is_repository_run(run, current)]
    runs.sort(key=lambda run: _timestamp(run["created_at"]) or current, reverse=True)

    inspected = 0
    for run in runs:
        if inspected >= 5:
            break
        inspected += 1
        run_id = run["id"]
        attempt = run["run_attempt"]
        jobs_url = f"{base}/repos/{_REPOSITORY}/actions/runs/{run_id}/jobs?per_page=10"
        jobs_payload = get_json(jobs_url, token)
        jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
        if not isinstance(jobs, list) or len(jobs) > _MAX_JOBS:
            continue
        matching_jobs = [job for job in jobs if isinstance(job, dict) and job.get("name") == _JOB_NAME]
        if len(matching_jobs) != 1:
            continue
        job = matching_jobs[0]
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            continue

        artifacts_url = f"{base}/repos/{_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=10"
        artifacts_payload = get_json(artifacts_url, token)
        artifacts = artifacts_payload.get("artifacts") if isinstance(artifacts_payload, dict) else None
        if not isinstance(artifacts, list) or len(artifacts) > _MAX_ARTIFACTS:
            continue
        expected_name = f"{_ARTIFACT_PREFIX}{run_id}-{attempt}"
        matches = [artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("name") == expected_name]
        if len(matches) != 1:
            continue
        artifact = matches[0]
        created_at = _timestamp(artifact.get("created_at"))
        expires_at = _timestamp(artifact.get("expires_at"))
        size = artifact.get("size_in_bytes")
        if (
            artifact.get("expired") is not False
            or type(size) is not int
            or size <= 0
            or size > _MAX_ARTIFACT_SIZE
            or created_at is None
            or created_at > current
            or current - created_at > _MAX_AGE
            or expires_at is None
            or expires_at <= current
        ):
            continue
        return {
            "run_id": str(run_id),
            "artifact_name": expected_name,
            "artifact_created_at": created_at.isoformat().replace("+00:00", "Z"),
            "artifact_size": str(size),
        }
    return None


def validate_observation(
    *,
    artifact_path: str,
    artifact_created_at: str,
    artifact_size: str,
    available: str,
    now: datetime | None = None,
) -> dict[str, str]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fallback = {
        "scheduler_state": "unknown",
        "observed_at": "",
    }
    try:
        metadata_time = _timestamp(artifact_created_at)
        metadata_size = int(artifact_size)
        path = Path(artifact_path)
        if (
            available != "true"
            or metadata_time is None
            or metadata_time > current
            or current - metadata_time > _MAX_AGE
            or metadata_size <= 0
            or metadata_size > _MAX_ARTIFACT_SIZE
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size <= 0
            or path.stat().st_size > _MAX_ARTIFACT_SIZE
        ):
            return fallback
        body = path.read_bytes()
        if len(body) > _MAX_ARTIFACT_SIZE:
            return fallback
        payload = json.loads(body, object_pairs_hook=_pairs_without_duplicates)
        if not isinstance(payload, dict) or set(payload) != {"scheduler_state", "observed_at"}:
            return fallback
        state = payload.get("scheduler_state")
        observed_at_raw = payload.get("observed_at")
        observed_at = _timestamp(observed_at_raw)
        if (
            not isinstance(state, str)
            or state not in _VALID_STATES
            or not isinstance(observed_at_raw, str)
            or not observed_at_raw.endswith("Z")
            or observed_at is None
            or observed_at > current
            or observed_at > metadata_time
            or current - observed_at > _MAX_AGE
        ):
            return fallback
        return {"scheduler_state": state, "observed_at": observed_at.isoformat(timespec="seconds").replace("+00:00", "Z")}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return fallback


def _write_outputs(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")


def _select_main() -> int:
    try:
        api_base = _api_base(os.environ.get("GITHUB_API_URL"))
        repository = os.environ.get("GITHUB_REPOSITORY")
        candidate = None
        if repository == _REPOSITORY and api_base is not None:
            candidate = select_latest_observation(
                token=os.environ.get("GITHUB_TOKEN", ""),
                api_base=api_base,
            )
    except Exception:  # noqa: BLE001 - metadata failures are a safe unknown
        candidate = None
    values = {
        "scheduler_artifact_available": "true" if candidate is not None else "false",
        "scheduler_artifact_run_id": candidate["run_id"] if candidate else "",
        "scheduler_artifact_name": candidate["artifact_name"] if candidate else "",
        "scheduler_artifact_created_at": candidate["artifact_created_at"] if candidate else "",
        "scheduler_artifact_size": candidate["artifact_size"] if candidate else "",
    }
    try:
        _write_outputs(values)
    except OSError:
        values["scheduler_artifact_available"] = "false"
    print(json.dumps({"scheduler_artifact_available": values["scheduler_artifact_available"]}, sort_keys=True))
    return 0


def _validate_main(artifact_path: str, output_path: str) -> int:
    result = validate_observation(
        artifact_path=artifact_path,
        artifact_created_at=os.environ.get("SCHEDULER_ARTIFACT_CREATED_AT", ""),
        artifact_size=os.environ.get("SCHEDULER_ARTIFACT_SIZE", ""),
        available=os.environ.get("SCHEDULER_ARTIFACT_AVAILABLE", "false"),
    )
    try:
        Path(output_path).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        result = {
            "scheduler_state": "unknown",
            "observed_at": "",
        }
        print(json.dumps(result, sort_keys=True))
        return 1
    try:
        _write_outputs(result)
    except OSError:
        result = {
            "scheduler_state": "unknown",
            "observed_at": "",
        }
        try:
            Path(output_path).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            print(json.dumps(result, sort_keys=True))
            return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("select")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--artifact-path", required=True)
    validate.add_argument("--output-path", required=True)
    args = parser.parse_args(argv)
    return _select_main() if args.command == "select" else _validate_main(args.artifact_path, args.output_path)


if __name__ == "__main__":
    raise SystemExit(main())
