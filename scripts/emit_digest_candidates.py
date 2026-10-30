#!/usr/bin/env python3
"""Project existing Binance execution reports into private daily candidates.

No broker, model, notification or account-facts calls. Counts and financial
coverage remain unknown; a source report is not a serving identity attestation.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import execution_report_heartbeat as heartbeat  # noqa: E402
from scripts.digest_candidates_store import object_uri  # noqa: E402

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._=-]{0,127}\Z")
_TARGET_FIELDS = (
    "platform_id", "strategy_profile", "service_name", "account_scope",
    "account_selector", "deployment_selector", "dry_run_only",
)


class CandidateBlocked(ValueError):
    """Fixed, non-sensitive stop code."""


def _target(value):
    if not isinstance(value, Mapping):
        raise CandidateBlocked("candidate_target_invalid")
    target = {key: value.get(key) for key in _TARGET_FIELDS}
    if (target["platform_id"] != "binance"
            or not isinstance(target["strategy_profile"], str)
            or not _IDENTIFIER.fullmatch(target["strategy_profile"])
            or type(target["dry_run_only"]) is not bool
            or not isinstance(target["account_selector"], list)
            or not target["account_selector"]
            or any(not isinstance(item, str) or not item.strip() for item in target["account_selector"])
            or any(not isinstance(target[key], str) or not target[key].strip()
                   for key in ("strategy_profile", "service_name", "account_scope", "deployment_selector"))):
        raise CandidateBlocked("candidate_target_invalid")
    return target


def _upload(path, destination):
    from google.cloud import storage

    # Reuse the authenticated monitor's ADC. Unknown write results must not
    # start a second upload; client transport retries are explicitly disabled.
    blob = storage.Blob.from_string(destination, client=storage.Client())
    blob.upload_from_filename(str(path), content_type="application/json", retry=None, timeout=30)


def project_digest_candidates(report, *, expected_target, opaque_account_uid, target_id, now):
    """Pure projection; private identity must come from approved caller inputs."""
    expected = _target(expected_target)
    if any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
           for value in (opaque_account_uid, target_id)):
        raise CandidateBlocked("candidate_identity_missing_or_invalid")
    if (not isinstance(report, Mapping) or report.get("schema_version") != "runtime_report.v1"
            or report.get("platform") != "binance"):
        raise CandidateBlocked("candidate_source_invalid")
    if (_target(report.get("runtime_target")) != expected
            or report.get("strategy_profile") != expected["strategy_profile"]
            or report.get("service_name") != expected["service_name"]
            or type(report.get("dry_run")) is not bool
            or report["dry_run"] != expected["dry_run_only"]):
        raise CandidateBlocked("candidate_source_identity_mismatch")
    try:
        started = dt.datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
        if started.tzinfo is None or now.tzinfo is None or not dt.timedelta(0) <= now - started <= dt.timedelta(hours=36):
            raise ValueError
    except (KeyError, AttributeError, TypeError, ValueError):
        raise CandidateBlocked("candidate_source_time_invalid") from None
    # Reading a completed archived cycle proves presence, not fills or success.
    status = report.get("status")
    if status not in {"ok", "aborted", "error"}:
        raise CandidateBlocked("candidate_source_status_invalid")
    error_summary = report.get("error_summary")
    source_alert = (status != "ok" or bool(report.get("errors"))
                    or bool(report.get("execution_blocked_reason"))
                    or (isinstance(error_summary, Mapping) and bool(error_summary.get("errors"))))
    row = {
        "platform_id": "binance",
        "strategy_profile": expected["strategy_profile"],
        "opaque_account_uid": opaque_account_uid,
        "target_id": target_id,
        "actually_ran": True,
        "business_day": started.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
        "as_of": started.astimezone(dt.timezone.utc).isoformat(),
        "status": "alert",  # Missing complete execution evidence stays visible.
        "fill_count": None,
        "order_count": None,
        "cycle_count": None,
        "field_status": {key: "counts_unknown" for key in ("fill_count", "order_count", "cycle_count")},
        "evidence_provenance": "candidates",
        "reason_code": "binance_source_cycle_alert" if source_alert else "binance_source_report_counts_unknown",
        "execution_status": "execution_alert" if source_alert else "execution_unknown",
        "persistence_status": "unknown",
        "delivery_status": "pending_central",
    }
    return {"schema_version": "qsl.digest_candidates.v1", "producer_status": "projected", "runs": [row]}


def _legacy_emit_digest_candidates(env, *, now=None):
    from notifications.unified_notifications import enabled
    if enabled(env):
        from notifications.unified_producer import emit_archive
        return emit_archive(env, now=now)
    if env.get("BINANCE_DIGEST_CANDIDATES_ENABLED", "false") == "false":
        return {"status": "disabled", "uploaded": False}
    if env.get("BINANCE_DIGEST_CANDIDATES_ENABLED") != "true":
        raise CandidateBlocked("candidate_enable_invalid")
    try:
        expected = _target(json.loads(env.get("RUNTIME_TARGET_JSON", "")))
    except (ValueError, TypeError):
        raise CandidateBlocked("candidate_target_invalid") from None
    uid = env.get("BINANCE_DIGEST_OPAQUE_ACCOUNT_UID", "")
    tid = env.get("BINANCE_DIGEST_TARGET_ID", "")
    if any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) for value in (uid, tid)):
        raise CandidateBlocked("candidate_identity_missing_or_invalid")
    uri = env.get("DIGEST_CANDIDATES_GCS_URI", "")
    store_target = env.get("BINANCE_DIGEST_STORE_TARGET", "")
    if not re.fullmatch(r"gs://[a-z0-9][a-z0-9._-]+/[^\s?#]+", uri) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", store_target):
        raise CandidateBlocked("candidate_store_configuration_missing_or_invalid")
    current = now or dt.datetime.now(dt.timezone.utc)
    # Existing heartbeat source locator and cloud identity, with fixed source
    # profile/service from the declared target rather than legacy defaults.
    os.environ.update({
        "RUNTIME_HEARTBEAT_REPORT_PLATFORM": "binance",
        "RUNTIME_HEARTBEAT_STRATEGY_PROFILE": expected["strategy_profile"],
        "RUNTIME_HEARTBEAT_SERVICE_NAME": expected["service_name"],
    })
    try:
        entries, source_prefix = heartbeat._list_reports(since=current - dt.timedelta(hours=36), now=current)
        recent = [entry for entry in entries if heartbeat._entry_updated_at(entry) is not None]
        latest = max(recent, key=heartbeat._entry_updated_at) if recent else None
        source_uri = heartbeat._entry_uri(latest) if latest else ""
        if not source_uri:
            raise CandidateBlocked("candidate_source_absent")
        if not source_uri.startswith(source_prefix.rstrip("/") + "/binance/" + expected["strategy_profile"] + "/"):
            raise CandidateBlocked("candidate_source_locator_mismatch")
        report = heartbeat._read_report(source_uri)
    except CandidateBlocked:
        raise
    except Exception:
        raise CandidateBlocked("candidate_source_read_failed") from None
    payload = project_digest_candidates(report, expected_target=expected, opaque_account_uid=uid, target_id=tid, now=current)
    # The only financial-capable payload remains ephemeral inside the cloud
    # runner; no Actions artifact or public dump is produced.
    with tempfile.TemporaryDirectory(prefix="binance-digest-") as directory:
        path = Path(directory) / "candidates.json"
        path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
        path.chmod(0o600)
        destination = object_uri(path, platform="binance", target=store_target, uri=uri)
        try:
            _upload(path, destination)
        except Exception:
            raise CandidateBlocked("candidate_store_write_failed_or_unknown") from None
    return {"status": "projected", "uploaded": True, "runs": 1, "counts_unknown": True, "financial_values_included": False}


def emit_digest_candidates(env, *, now=None):
    from notifications.unified_producer import emit_archive
    return emit_archive(env, now=now)


def main():
    try:
        result = emit_digest_candidates(os.environ)
        print(json.dumps(result, sort_keys=True))
        return 0
    except CandidateBlocked as error:
        uploaded = None if str(error) == "candidate_store_write_failed_or_unknown" else False
        print(json.dumps({"status": "blocked", "reason": str(error), "uploaded": uploaded}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "candidate_operation_failed_or_unknown", "uploaded": None}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

