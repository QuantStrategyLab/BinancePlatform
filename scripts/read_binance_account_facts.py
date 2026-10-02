#!/usr/bin/env python3
"""Read a strictly scoped Binance homepage snapshot from a successful Runtime.

The entrypoint is independent of main.py and exposes only two broker GETs.
All failures use fixed codes; private responses and IDs never reach logs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application.account_facts import (  # noqa: E402
    AccountFactsUnavailable,
    ReadOnlyBinanceClient,
    build_source_binding_id,
    collect_account_facts,
    validate_account_facts_payload,
)


REPOSITORY = "QuantStrategyLab/BinancePlatform"
LEGACY_RUNTIME_WORKFLOW_SHA = "9cfcf0531d1ea176e6f26590cf15edbd31bd6567"
APPROVED_APPLICATION_SHA = "8cb56617115fa45028e34d788e71884b6a303d77"
RUNTIME_WORKFLOW_NAME = "Runtime"
RUNTIME_BRANCH = "runtime-production"
EXPECTED_REPORT_ARTIFACT_PREFIX = "binance-execution-report-"
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ReaderError(ValueError):
    """A stable non-sensitive reader stop code."""


def _stop(code: str) -> ReaderError:
    return ReaderError(f"account_facts_{code}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _json_no_duplicates(raw: bytes) -> Any:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise _stop("input_invalid")
            value[key] = item
        return value

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(_stop("input_invalid")),
        )
    except ReaderError:
        raise
    except Exception:
        raise _stop("input_invalid") from None


def _api_json(url: str, token: str, *, timeout: int = 20) -> Any:
    request = Request(
        url,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                 "X-GitHub-Api-Version": "2022-11-28"},
        method="GET",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            if response.status != 200:
                raise _stop("trigger_read_failed")
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise _stop("trigger_read_failed")
            return _json_no_duplicates(data)
    except ReaderError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError):
        raise _stop("trigger_read_failed") from None


def _read_job_log_text(url: str, token: str) -> bytes:
    import requests

    def bounded_body(response) -> bytes:
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > 25_000_000):
            raise _stop("trigger_logs_unavailable")
        result = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                result.extend(chunk)
                if len(result) > 25_000_000:
                    raise _stop("trigger_logs_unavailable")
        return bytes(result)

    try:
        response = requests.get(
            url,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=30,
            allow_redirects=False,
            stream=True,
        )
        with response:
            status = response.status_code
            location = response.headers.get("Location")
            if status == 302 and location:
                from urllib.parse import urlparse

                parsed = urlparse(location)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise _stop("trigger_logs_unavailable")
                # GitHub's signed log URL is fetched without the GitHub token.
                response = requests.get(
                    location, timeout=30, allow_redirects=False, stream=True
                )
                with response:
                    if response.status_code != 200:
                        raise _stop("trigger_logs_unavailable")
                    return bounded_body(response)
            if status != 200:
                raise _stop("trigger_logs_unavailable")
            return bounded_body(response)
    except ReaderError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError, requests.RequestException):
        raise _stop("trigger_logs_unavailable") from None


def _validate_release_log(run_id: str, token: str, api_url: str) -> None:
    jobs_url = f"{api_url}/repos/{REPOSITORY}/actions/runs/{quote(run_id)}/jobs?per_page=100"
    jobs_payload = _api_json(jobs_url, token)
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    if not isinstance(jobs, list):
        raise _stop("trigger_jobs_invalid")
    matches = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("name") != "deploy":
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        resolve_steps = [
            step for step in steps
            if isinstance(step, dict) and step.get("name") == "Resolve approved runtime release SHA"
        ]
        if len(resolve_steps) == 1 and resolve_steps[0].get("conclusion") == "success" and job.get("conclusion") == "success":
            matches.append(job)
    if len(matches) != 1 or not isinstance(matches[0].get("id"), int):
        raise _stop("trigger_release_unverified")
    archive_url = f"{api_url}/repos/{REPOSITORY}/actions/jobs/{matches[0]['id']}/logs"
    raw_log = _read_job_log_text(archive_url, token)
    try:
        log_text = raw_log.decode("utf-8")
    except Exception:
        raise _stop("trigger_release_unverified") from None
    release_lines = re.findall(
        r"(?m)^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z )?"
        r"Selected runtime release SHA ([0-9a-f]{40}) "
        r"\(workflow github\.sha=[0-9a-f]{40} is not used as the application execution identity\)\.?\r?$",
        log_text,
    )
    if release_lines != [APPROVED_APPLICATION_SHA]:
        raise _stop("trigger_release_mismatch")


def _ensure_no_active_runtime_run(*, token: str, api_url: str) -> None:
    active_statuses = ("queued", "in_progress", "requested", "waiting", "pending")
    for status in active_statuses:
        runs = _api_json(
            f"{api_url}/repos/{REPOSITORY}/actions/workflows/main.yml/runs"
            f"?status={status}&per_page=100",
            token,
        )
        rows = runs.get("workflow_runs") if isinstance(runs, Mapping) else None
        if (
            not isinstance(rows, list)
            or type(runs.get("total_count")) is not int
            or runs["total_count"] > 100
        ):
            raise _stop("runtime_activity_unverified")
        if any(
            isinstance(row, Mapping)
            and isinstance(row.get("path"), str)
            and row["path"].split("@", 1)[0] == ".github/workflows/main.yml"
            and row.get("status") == status
            for row in rows
        ):
            raise _stop("runtime_activity_present")


def _ensure_latest_runtime_run(*, run_id: str, token: str, api_url: str) -> None:
    latest = _api_json(
        f"{api_url}/repos/{REPOSITORY}/actions/workflows/main.yml/runs"
        f"?per_page=1",
        token,
    )
    rows = latest.get("workflow_runs") if isinstance(latest, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise _stop("runtime_latest_unverified")
    row = rows[0]
    if (
        str(row.get("id")) != str(run_id)
        or row.get("name") != RUNTIME_WORKFLOW_NAME
        or str(row.get("path") or "").split("@", 1)[0] != ".github/workflows/main.yml"
        or row.get("event") != "workflow_dispatch"
        or row.get("status") != "completed"
        or row.get("conclusion") != "success"
        or row.get("head_branch") != RUNTIME_BRANCH
        or row.get("head_sha") != LEGACY_RUNTIME_WORKFLOW_SHA
        or not isinstance(row.get("repository"), Mapping)
        or row["repository"].get("full_name") != REPOSITORY
    ):
        raise _stop("runtime_run_not_latest_success")


def verify_source_is_current(*, run_id: str, token: str, api_url: str) -> None:
    """Fail closed if any newer or active Runtime run surrounds this read."""
    _ensure_no_active_runtime_run(token=token, api_url=api_url)
    _ensure_latest_runtime_run(run_id=run_id, token=token, api_url=api_url)


def _parse_github_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise _stop("parent_run_unverified")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise _stop("parent_run_unverified") from None
    if parsed.tzinfo is None:
        raise _stop("parent_run_unverified")
    return parsed.astimezone(timezone.utc)


def _ensure_no_other_active_parent_run(
    *, run_id: str, token: str, api_url: str
) -> None:
    active_statuses = ("queued", "in_progress", "requested", "waiting", "pending")
    own_in_progress_seen = False
    for status in active_statuses:
        result = _api_json(
            f"{api_url}/repos/{REPOSITORY}/actions/workflows/main.yml/runs"
            f"?status={status}&per_page=100",
            token,
        )
        rows = result.get("workflow_runs") if isinstance(result, Mapping) else None
        if (
            not isinstance(rows, list)
            or type(result.get("total_count")) is not int
            or result["total_count"] > 100
        ):
            raise _stop("runtime_activity_unverified")
        for row in rows:
            if not isinstance(row, Mapping):
                raise _stop("runtime_activity_unverified")
            if str(row.get("path") or "").split("@", 1)[0] != ".github/workflows/main.yml":
                raise _stop("runtime_activity_unverified")
            if str(row.get("id")) == str(run_id):
                if status != "in_progress" or row.get("status") != "in_progress":
                    raise _stop("runtime_parent_attempt_mismatch")
                own_in_progress_seen = True
                continue
            raise _stop("runtime_activity_present")
    if not own_in_progress_seen:
        raise _stop("runtime_parent_attempt_mismatch")


def _ensure_latest_parent_run(*, run_id: str, token: str, api_url: str) -> None:
    result = _api_json(
        f"{api_url}/repos/{REPOSITORY}/actions/workflows/main.yml/runs?per_page=1",
        token,
    )
    rows = result.get("workflow_runs") if isinstance(result, Mapping) else None
    if (
        not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping)
        or type(result.get("total_count")) is not int or result["total_count"] < 1
    ):
        raise _stop("runtime_latest_unverified")
    row = rows[0]
    if (
        str(row.get("id")) != str(run_id)
        or row.get("name") != RUNTIME_WORKFLOW_NAME
        or str(row.get("path") or "").split("@", 1)[0] != ".github/workflows/main.yml"
        or row.get("head_branch") != RUNTIME_BRANCH
        or row.get("status") != "in_progress"
    ):
        raise _stop("runtime_parent_not_latest")


def verify_parent_run(
    *, run_id: str, run_attempt: int, repository: str, ref: str, github_sha: str,
    runtime_workflow_sha: str, token: str, api_url: str,
) -> dict[str, str]:
    """Verify the exact in-progress Runtime parent and its completed read inputs."""
    if (
        repository != REPOSITORY
        or not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id))
        or type(run_attempt) is not int or run_attempt != 1
        or ref != f"refs/heads/{RUNTIME_BRANCH}"
        or not _GIT_SHA.fullmatch(runtime_workflow_sha)
        or github_sha != runtime_workflow_sha
    ):
        raise _stop("parent_run_identity_mismatch")

    _ensure_no_other_active_parent_run(run_id=run_id, token=token, api_url=api_url)
    _ensure_latest_parent_run(run_id=run_id, token=token, api_url=api_url)
    run = _api_json(
        f"{api_url}/repos/{REPOSITORY}/actions/runs/{quote(str(run_id))}", token
    )
    if (
        not isinstance(run, Mapping)
        or type(run.get("id")) is not int or run.get("id") != int(run_id)
        or type(run.get("run_attempt")) is not int or run.get("run_attempt") != 1
        or run.get("name") != RUNTIME_WORKFLOW_NAME
        or str(run.get("path") or "").split("@", 1)[0] != ".github/workflows/main.yml"
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "in_progress"
        or run.get("head_branch") != RUNTIME_BRANCH
        or run.get("head_sha") != runtime_workflow_sha
        or not isinstance(run.get("repository"), Mapping)
        or run["repository"].get("full_name") != REPOSITORY
        or not isinstance(run.get("head_repository"), Mapping)
        or run["head_repository"].get("full_name") != REPOSITORY
    ):
        raise _stop("parent_run_identity_mismatch")

    jobs_result = _api_json(
        f"{api_url}/repos/{REPOSITORY}/actions/runs/{quote(str(run_id))}/jobs?per_page=100",
        token,
    )
    jobs = jobs_result.get("jobs") if isinstance(jobs_result, Mapping) else None
    if not isinstance(jobs, list) or type(jobs_result.get("total_count")) is not int or jobs_result["total_count"] > 100:
        raise _stop("parent_run_jobs_unverified")
    deploy_jobs = [job for job in jobs if isinstance(job, Mapping) and job.get("name") == "deploy"]
    if len(deploy_jobs) != 1:
        raise _stop("parent_deploy_unverified")
    deploy = deploy_jobs[0]
    steps = deploy.get("steps")
    if (
        deploy.get("status") != "completed" or deploy.get("conclusion") != "success"
        or not isinstance(steps, list)
    ):
        raise _stop("parent_deploy_unverified")
    strategy_steps = [step for step in steps if isinstance(step, Mapping) and step.get("name") == "4. Run trading strategy"]
    report_steps = [step for step in steps if isinstance(step, Mapping) and step.get("name") == "5. Stage execution report for isolated log publisher"]
    if (
        len(strategy_steps) != 1 or strategy_steps[0].get("status") != "completed"
        or strategy_steps[0].get("conclusion") != "success"
        or len(report_steps) != 1 or report_steps[0].get("status") != "completed"
        or report_steps[0].get("conclusion") != "success"
    ):
        raise _stop("parent_strategy_or_report_unverified")
    deploy_started = _parse_github_time(deploy.get("started_at"))
    strategy_started = _parse_github_time(strategy_steps[0].get("started_at"))
    strategy_finished = _parse_github_time(strategy_steps[0].get("completed_at"))
    report_started = _parse_github_time(report_steps[0].get("started_at"))
    report_finished = _parse_github_time(report_steps[0].get("completed_at"))
    if (
        strategy_started < deploy_started or strategy_finished < strategy_started
        or report_started < strategy_finished or report_finished < report_started
    ):
        raise _stop("parent_run_jobs_unverified")

    # The protected setting and report binding identify the intended app, but
    # only the Runtime release-selection log proves what this parent ran.
    _validate_release_log(str(run_id), token, api_url)

    artifacts_result = _api_json(
        f"{api_url}/repos/{REPOSITORY}/actions/runs/{quote(str(run_id))}/artifacts?per_page=100",
        token,
    )
    artifacts = artifacts_result.get("artifacts") if isinstance(artifacts_result, Mapping) else None
    if (
        not isinstance(artifacts, list)
        or type(artifacts_result.get("total_count")) is not int
        or artifacts_result["total_count"] > 100
    ):
        raise _stop("trigger_report_artifact_missing")
    artifact_name = f"{EXPECTED_REPORT_ARTIFACT_PREFIX}{run_id}"
    matches = [item for item in artifacts if isinstance(item, Mapping) and item.get("name") == artifact_name]
    if len(matches) != 1:
        raise _stop("trigger_report_artifact_missing")
    artifact = matches[0]
    if (
        artifact.get("expired") is not False
        or type(artifact.get("size_in_bytes")) is not int or artifact["size_in_bytes"] <= 0
        or _parse_github_time(artifact.get("created_at")) < strategy_finished
        or _parse_github_time(artifact.get("created_at")) < deploy_started
    ):
        raise _stop("trigger_report_artifact_missing")
    return {"run_id": str(run_id), "report_artifact": artifact_name}


def verify_current_source_from_env(env: Mapping[str, str]) -> None:
    mode = env.get("SOURCE_MODE", "legacy_terminal")
    if mode == "same_parent":
        try:
            attempt = int(env.get("GITHUB_RUN_ATTEMPT", ""))
        except ValueError:
            raise _stop("parent_run_attempt_mismatch") from None
        verify_parent_run(
            run_id=str(env.get("GITHUB_RUN_ID") or ""),
            run_attempt=attempt,
            repository=str(env.get("GITHUB_REPOSITORY") or ""),
            ref=str(env.get("GITHUB_REF") or ""),
            github_sha=str(env.get("GITHUB_SHA") or ""),
            runtime_workflow_sha=str(env.get("BINANCE_RUNTIME_WORKFLOW_SHA") or ""),
            token=str(env.get("GITHUB_TOKEN") or ""),
            api_url=str(env.get("GITHUB_API_URL") or "https://api.github.com"),
        )
    elif mode == "legacy_terminal":
        verify_source_is_current(
            run_id=str(env.get("SOURCE_RUN_ID") or ""),
            token=str(env.get("GITHUB_TOKEN") or ""),
            api_url=str(env.get("GITHUB_API_URL") or "https://api.github.com"),
        )
    else:
        raise _stop("source_mode_invalid")


def verify_trigger_run(*, run_id: str, repository: str, token: str, api_url: str) -> dict[str, str]:
    """Validate source run metadata, selected application SHA, and report artifact."""
    if repository != REPOSITORY or not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise _stop("trigger_identity_invalid")
    url = f"{api_url}/repos/{REPOSITORY}/actions/runs/{quote(str(run_id))}"
    run = _api_json(url, token)
    head_repo = run.get("head_repository") if isinstance(run, dict) else None
    base_repo = run.get("repository") if isinstance(run, dict) else None
    if (
        run.get("id") != int(run_id)
        or run.get("name") != RUNTIME_WORKFLOW_NAME
        or str(run.get("path") or "").split("@", 1)[0] != ".github/workflows/main.yml"
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_branch") != RUNTIME_BRANCH
        or run.get("head_sha") != LEGACY_RUNTIME_WORKFLOW_SHA
        or not isinstance(head_repo, Mapping)
        or head_repo.get("full_name") != REPOSITORY
        or not isinstance(base_repo, Mapping)
        or base_repo.get("full_name") != REPOSITORY
    ):
        raise _stop("trigger_identity_mismatch")
    _validate_release_log(str(run_id), token, api_url)
    _ensure_no_active_runtime_run(token=token, api_url=api_url)
    artifacts = _api_json(
        f"{api_url}/repos/{REPOSITORY}/actions/runs/{quote(str(run_id))}/artifacts?per_page=100",
        token,
    )
    rows = artifacts.get("artifacts") if isinstance(artifacts, dict) else None
    expected_name = f"{EXPECTED_REPORT_ARTIFACT_PREFIX}{run_id}"
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("name") == expected_name] if isinstance(rows, list) else []
    if len(matches) != 1 or matches[0].get("expired") is not False or matches[0].get("size_in_bytes", 0) <= 0:
        raise _stop("trigger_report_artifact_missing")
    verify_source_is_current(run_id=str(run_id), token=token, api_url=api_url)
    return {"run_id": str(run_id), "report_artifact": expected_name}


def _valid_order_result_tree(value: Any, parent_key: str = "") -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in ("order", "fill", "fund", "accounting", "reconcil")):
                if isinstance(child, str) and child.strip().lower() in {
                    "unknown", "uncertain", "pending", "submission_unknown",
                    "filled_accounting_pending", "unverified", "error", "failed",
                    "incomplete", "conflict", "mismatch",
                }:
                    return False
                if "unknown" in key_text and child is True:
                    return False
                if isinstance(child, (int, float)) and not isinstance(child, bool) and child > 0 and any(
                    marker in key_text for marker in ("unknown_count", "pending_count")
                ):
                    return False
            if not _valid_order_result_tree(child, key_text):
                return False
        return True
    if isinstance(value, list):
        return all(_valid_order_result_tree(item, parent_key) for item in value)
    return True


def _report_json(path: Path) -> Mapping[str, Any]:
    try:
        details = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= 10_000_000:
            raise _stop("report_invalid")
        report = _json_no_duplicates(path.read_bytes())
    except ReaderError:
        raise
    except Exception:
        raise _stop("report_invalid") from None
    if not isinstance(report, Mapping):
        raise _stop("report_invalid")
    return report


def validate_strategy_report(report: Mapping[str, Any], runtime_target: Mapping[str, Any]) -> None:
    target = report.get("runtime_target")
    errors = report.get("error_summary")
    side_effects = report.get("side_effect_summary")
    expected_target_keys = (
        "platform_id", "strategy_profile", "account_scope", "account_selector",
        "deployment_selector", "service_name", "dry_run_only",
    )
    _validate_execution_receipt_status(report)
    if (
        report.get("platform") != "binance"
        or report.get("status") != "ok"
        or report.get("dry_run") is not False
        or report.get("standard_execution_permitted") is not True
        or not isinstance(report.get("run_id"), str)
        or not report["run_id"].strip()
        or report.get("execution_blocked_reason")
        or not isinstance(errors, Mapping)
        or errors.get("errors") != []
        or not isinstance(side_effects, Mapping)
        or type(side_effects.get("executed_call_count")) is not int
        or side_effects.get("executed_call_count") < 0
        or not isinstance(target, Mapping)
        or any(target.get(key) != runtime_target.get(key) for key in expected_target_keys)
        or not _valid_order_result_tree(report)
    ):
        raise _stop("strategy_report_not_eligible")


def _validate_execution_receipt_status(report: Mapping[str, Any]) -> None:
    observation = report.get("execution_receipt_observation")
    expected_counters = {
        "submission_attempted_count", "broker_acknowledged_count",
        "partially_filled_count", "filled_count", "transport_uncertain_count",
        "failed_count",
    }
    if not isinstance(observation, Mapping) or set(observation) != expected_counters:
        raise _stop("strategy_execution_status_unverified")
    counts = {}
    for name, value in observation.items():
        if type(value) is not int or value < 0:
            raise _stop("strategy_execution_status_unverified")
        counts[name] = value
    if counts["transport_uncertain_count"] > 0:
        raise _stop("strategy_execution_uncertain")
    attempts = counts["submission_attempted_count"]
    no_execution_observed = not any(counts.values())
    has_receipt = "execution_receipt" in report
    raw_receipt = report.get("execution_receipt")
    if not has_receipt:
        # Older approved runtimes can omit a receipt when the report has no
        # attested strategy release. Zero observed submissions is still a
        # bounded read-only eligibility fact; any broker-facing counter needs
        # an existing, valid terminal receipt.
        if no_execution_observed:
            return
        raise _stop("strategy_execution_receipt_required")
    try:
        from quant_platform_kit.common.execution_receipts import validate_execution_receipt

        receipt = validate_execution_receipt(raw_receipt)
    except Exception:
        raise _stop("strategy_execution_receipt_invalid") from None
    target = report.get("runtime_target")
    runtime_loaded = report.get("runtime_release_receipt")
    strategy_release = runtime_loaded.get("strategy_release") if isinstance(runtime_loaded, Mapping) else None
    if (
        not isinstance(target, Mapping)
        or receipt["platform"] != "binance"
        or receipt["strategy_profile"] != report.get("strategy_profile")
        or receipt["execution_mode"] != target.get("execution_mode")
        or not isinstance(strategy_release, Mapping)
        or receipt["strategy_revision"] != strategy_release.get("strategy_revision")
    ):
        raise _stop("strategy_execution_receipt_invalid")
    outcome = receipt["outcome"]
    confirmation = receipt["broker_confirmation"]
    if no_execution_observed:
        if outcome not in {"no_action", "not_due"}:
            raise _stop("strategy_submission_unclosed")
        return
    if (
        outcome != "filled"
        or confirmation != "filled"
        or attempts <= 0
        or counts["filled_count"] != attempts
        or any(
            counts[name] != 0
            for name in (
                "broker_acknowledged_count", "partially_filled_count",
                "transport_uncertain_count", "failed_count",
            )
        )
    ):
        raise _stop("strategy_submission_unclosed")


def _target_identity(raw: str) -> dict[str, Any]:
    decoded = _json_no_duplicates(raw.encode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise _stop("runtime_target_invalid")
    try:
        from quant_platform_kit.common.runtime_target import resolve_runtime_target_from_env
        from quant_platform_kit.common.live_continuity import runtime_target_permits_standard_execution

        target = resolve_runtime_target_from_env(
            env={"RUNTIME_TARGET_JSON": raw}, expected_platform_id="binance"
        )
    except Exception:
        raise _stop("runtime_target_invalid")
    value = target.to_dict()
    if (
        target.dry_run_only
        or not runtime_target_permits_standard_execution(target)
        or not isinstance(value.get("account_scope"), str)
        or not value["account_scope"]
        or value["account_scope"] == "default"
        or not target.account_selector
        or any(not isinstance(item, str) or not item.strip() or item == "default" for item in target.account_selector)
        or not isinstance(value.get("deployment_selector"), str)
        or not value["deployment_selector"]
        or value["deployment_selector"] == "default"
        or not isinstance(value.get("service_name"), str)
        or not value["service_name"]
    ):
        raise _stop("runtime_target_invalid")
    value["account_selector"] = list(target.account_selector)
    return value


def _binding(raw: str, target: Mapping[str, Any], expected_scope: str, reader_revision: str) -> Mapping[str, Any]:
    value = _json_no_duplicates(raw.encode("utf-8"))
    fields = {
        "platform", "account_key", "account_scope", "target_name", "service_name",
        "deployment_selector", "account_selector", "target_id", "account_scope_sha256",
        "reader_revision", "approved_application_revision", "source_binding",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _stop("binding_config_invalid")
    target_selectors = target.get("account_selector")
    if (
        value.get("platform") != "binance"
        or not isinstance(value.get("account_key"), str) or not value["account_key"].strip()
        or not isinstance(value.get("target_name"), str) or not value["target_name"].strip()
        or value.get("account_scope") != target.get("account_scope")
        or not isinstance(value.get("service_name"), str)
        or (value.get("service_name") and value.get("service_name") != target.get("service_name"))
        or value.get("deployment_selector") != target.get("deployment_selector")
        or not isinstance(target_selectors, list)
        or len(target_selectors) != 1
        or not isinstance(value.get("account_selector"), str)
        or value.get("account_selector") != target_selectors[0]
        or value.get("account_scope_sha256") != expected_scope
        or value.get("reader_revision") != reader_revision
        or value.get("approved_application_revision") != APPROVED_APPLICATION_SHA
        or not isinstance(value.get("target_id"), str) or not value["target_id"].strip()
    ):
        raise _stop("binding_config_mismatch")
    expected_source_binding = {
        "kind": "binance_readonly_scope_revision",
        "id": build_source_binding_id(
            account_scope_sha256=expected_scope,
            reader_public_revision=reader_revision,
            approved_application_revision=APPROVED_APPLICATION_SHA,
        ),
    }
    if value.get("source_binding") != expected_source_binding:
        raise _stop("binding_source_mismatch")
    return value


def _expected_scope(raw: str) -> str:
    digests = _json_no_duplicates(raw.encode("utf-8"))
    expected_keys = {
        "account_scope_sha256", "positions_sha256", "cash_sha256",
        "open_orders_sha256", "recent_executions_sha256", "local_execution_ledger_sha256",
    }
    if not isinstance(digests, Mapping) or set(digests) != expected_keys:
        raise _stop("account_identity_unbound")
    scope = str(digests.get("account_scope_sha256") or "").lower().removeprefix("sha256:")
    if not _SHA256.fullmatch(scope):
        raise _stop("account_identity_unbound")
    return scope


def _authority_payload(env: Mapping[str, str]) -> Mapping[str, Any]:
    raw_text = str(env.get("BINANCE_RISK_AUTHORITY_JSON") or "")
    path_text = str(env.get("BINANCE_RISK_AUTHORITY_FILE") or "")
    if raw_text and path_text:
        raise _stop("authority_identity_mismatch")
    if raw_text:
        raw = raw_text.encode("utf-8")
    else:
        if not path_text:
            raise _stop("authority_unavailable")
        path = Path(path_text)
        try:
            details = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= 65_536:
                raise _stop("authority_unavailable")
            raw = path.read_bytes()
        except ReaderError:
            raise
        except Exception:
            raise _stop("authority_unavailable") from None
    if (
        not raw or len(raw) > 65_536
        or not _SHA256.fullmatch(str(env.get("BINANCE_RISK_AUTHORITY_SHA256") or "").lower())
        or hashlib.sha256(raw).hexdigest() != env.get("BINANCE_RISK_AUTHORITY_SHA256", "").lower()
    ):
        raise _stop("authority_identity_mismatch")
    payload = _json_no_duplicates(raw)
    expected_fields = {
        "decision", "authority_scope", "runtime_target", "strategy_revision",
        "runner_revision", "config_sha256", "continuous_inputs_allowed", "mandate",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise _stop("authority_identity_mismatch")
    if (
        payload.get("decision") != "APPROVE"
        or payload.get("authority_scope") != "LIVE"
        or payload.get("continuous_inputs_allowed") is not True
        or payload.get("runner_revision") != APPROVED_APPLICATION_SHA
        or not _SHA256.fullmatch(str(payload.get("config_sha256") or ""))
        or not _GIT_SHA.fullmatch(str(payload.get("strategy_revision") or ""))
        or not isinstance(payload.get("mandate"), Mapping)
        or not _GIT_SHA.fullmatch(str(env.get("BINANCE_RISK_AUTHORITY_SOURCE_REVISION") or ""))
    ):
        raise _stop("authority_identity_mismatch")
    return payload


def _validate_authority_target(authority: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    actual = authority.get("runtime_target")
    fields = ("platform_id", "strategy_profile", "account_scope", "account_selector", "deployment_selector")
    if not isinstance(actual, Mapping) or any(actual.get(key) != target.get(key) for key in fields) or set(actual) != set(fields):
        raise _stop("authority_target_mismatch")


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    import os
    import tempfile

    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise _stop("output_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise _stop("output_write_failed") from None


def read_account_facts(*, report_path: Path, output_path: Path, env: Mapping[str, str]) -> None:
    if env.get("BINANCE_ACCOUNT_FACTS_ENABLED") != "true":
        raise _stop("disabled")
    if env.get("RUNTIME_TARGET_ENABLED") != "true" or env.get("BINANCE_DRY_RUN") != "false":
        raise _stop("runtime_not_live")
    if not env.get("BINANCE_API_KEY") or not env.get("BINANCE_API_SECRET"):
        raise _stop("credentials_missing")
    target = _target_identity(env.get("RUNTIME_TARGET_JSON", ""))
    expected_scope = _expected_scope(env.get("BINANCE_RECONCILIATION_EXPECTED_DIGESTS_JSON", ""))
    reader_revision = env.get("READER_PUBLIC_REVISION", "")
    if not _GIT_SHA.fullmatch(reader_revision):
        raise _stop("reader_revision_invalid")
    if env.get("BINANCE_RUNTIME_RELEASE_SHA") != APPROVED_APPLICATION_SHA:
        raise _stop("application_revision_mismatch")
    binding = _binding(
        env.get("BINANCE_ACCOUNT_FACTS_BINDING_JSON", ""), target, expected_scope, reader_revision
    )
    authority = _authority_payload(env)
    _validate_authority_target(authority, target)
    report = _report_json(report_path)
    validate_strategy_report(report, target)
    verify_current_source_from_env(env)

    # Client construction is network-silent: python-binance ping is disabled.
    # The capability wrapper exposes only the two signed wallet GET endpoints.
    try:
        from binance.client import Client
        raw_client = Client(
            env["BINANCE_API_KEY"], env["BINANCE_API_SECRET"],
            requests_params={"timeout": 15}, ping=False,
        )
    except Exception:
        raise _stop("client_initialization_failed") from None
    client = ReadOnlyBinanceClient(raw_client)
    started_at = datetime.now(timezone.utc)
    payload = collect_account_facts(
        client,
        expected_account_scope_sha256=expected_scope,
        target_id=str(binding["target_id"]),
        reader_public_revision=reader_revision,
        approved_application_revision=APPROVED_APPLICATION_SHA,
        observed_started_at=started_at,
        clock=lambda: datetime.now(timezone.utc),
    )
    verify_current_source_from_env(env)
    _write_private_json(output_path, validate_account_facts_payload(payload))


def preflight(*, env: Mapping[str, str], output: Path) -> None:
    mode = env.get("SOURCE_MODE", "legacy_terminal")
    if mode == "same_parent":
        try:
            attempt = int(env.get("GITHUB_RUN_ATTEMPT", ""))
        except ValueError:
            raise _stop("parent_run_attempt_mismatch") from None
        result = verify_parent_run(
            run_id=str(env.get("GITHUB_RUN_ID") or ""),
            run_attempt=attempt,
            repository=str(env.get("GITHUB_REPOSITORY") or ""),
            ref=str(env.get("GITHUB_REF") or ""),
            github_sha=str(env.get("GITHUB_SHA") or ""),
            runtime_workflow_sha=str(env.get("BINANCE_RUNTIME_WORKFLOW_SHA") or ""),
            token=str(env.get("GITHUB_TOKEN") or ""),
            api_url=str(env.get("GITHUB_API_URL") or "https://api.github.com"),
        )
    elif mode == "legacy_terminal":
        result = verify_trigger_run(
            run_id=str(env.get("SOURCE_RUN_ID") or ""),
            repository=str(env.get("GITHUB_REPOSITORY") or ""),
            token=str(env.get("GITHUB_TOKEN") or ""),
            api_url=str(env.get("GITHUB_API_URL") or "https://api.github.com"),
        )
    else:
        raise _stop("source_mode_invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"source_run_id={result['run_id']}\nreport_artifact={result['report_artifact']}\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--verify-current-source", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.preflight:
            preflight(env=os.environ, output=Path(os.getenv("GITHUB_OUTPUT") or "account-facts-preflight.json"))
            print("account_facts_preflight=ready")
            return 0
        if args.verify_current_source:
            verify_current_source_from_env(os.environ)
            print("account_facts_source_current=true")
            return 0
        if args.report is None or args.output is None:
            raise _stop("arguments_invalid")
        read_account_facts(report_path=args.report, output_path=args.output, env=os.environ)
        print("account_facts_read=complete_for_scope")
        return 0
    except (ReaderError, AccountFactsUnavailable) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("account_facts_unexpected_failure", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
