"""Pinned production-only diagnosis; no candidate, accounting or control write path."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone

from scripts import migrate_daily_accounting_state as migration

ACTION = "historical-continuity-diagnose"
SCOPE = "historical_continuity_readonly"
REASONS = frozenset({
    "historical_continuity_account_identity_mismatch", "historical_continuity_archive_invalid",
    "historical_continuity_broker_read_failed", "historical_continuity_checkpoint_unavailable",
    "historical_continuity_conservation_unverified", "historical_continuity_duplicate_event",
    "historical_continuity_history_incomplete", "historical_continuity_migration_run_invalid",
    "historical_continuity_observation_timeline_invalid", "historical_continuity_observation_window_invalid",
    "historical_continuity_open_orders_present", "historical_continuity_quantity_changed_during_read",
    "historical_continuity_source_run_invalid", "historical_continuity_unapproved_reward_product",
    "historical_continuity_unsupported_activity", "historical_continuity_window_invalid",
    "prospective_rebase_archive_invalid", "historical_readonly_source_invalid",
    "historical_readonly_control_invalid", "historical_readonly_archive_missing",
    "historical_readonly_source_changed", "historical_readonly_owned",
    "historical_readonly_failed", "migration_blocked", "historical_readonly_run_read_failed",
    "historical_readonly_migration_read_failed", "recovery_source_workflow_unverified", "recovery_run_invalid",
})


class ReadonlyClient:
    """Expose only the GET operations used by the existing collector."""
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        if name in {"get_account", "get_open_orders", "get_my_trades",
                    "get_simple_earn_flexible_product_position"}:
            return getattr(self._client, name)
        raise AttributeError("historical_readonly_operation_forbidden")

    def _request_margin_api(self, method, path, **kwargs):
        if method != "get" or path not in {
            "capital/deposit/hisrec", "capital/withdraw/history", "asset/transfer",
            "simple-earn/flexible/history/subscriptionRecord",
            "simple-earn/flexible/history/redemptionRecord",
            "simple-earn/flexible/history/rewardsRecord", "asset/assetDividend",
        }:
            raise ValueError("historical_readonly_operation_forbidden")
        return self._client._request_margin_api(method, path, **kwargs)


def _current_run():
    from scripts.binance_recovery_controller import request_json
    run_id, revision = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
        raise ValueError("historical_readonly_source_invalid")
    try:
        value = request_json(
            f"https://api.github.com/repos/{migration.REPOSITORY}/actions/runs/{run_id}",
            os.environ.get("GITHUB_TOKEN", ""),
        )
    except Exception:
        raise ValueError("historical_readonly_run_read_failed") from None
    if (value.get("id") != int(run_id) or value.get("head_sha") != revision
            or value.get("head_branch") != "runtime-production"
            or value.get("event") != "workflow_dispatch"
            or value.get("path") != ".github/workflows/main.yml"
            or value.get("repository", {}).get("full_name") != migration.REPOSITORY
            or value.get("status") != "in_progress"):
        raise ValueError("historical_readonly_source_invalid")
    return {key: value[key] for key in ("id", "head_sha", "head_branch", "event", "path")}


def run(*, now=None, clock=None):
    """Read existing source and immutable archive; verify both again after sampling."""
    from application.rebased_recovery import (
        collect_historical_continuity_readonly, prospective_migration_binding,
        validate_historical_continuity_material,
    )
    from application.reconciliation_recovery import activated_target
    from scripts.binance_recovery_controller import (
        FROZEN_EXPECTED_SHA256, MANAGED_SYMBOLS_SHA256, verified_run,
    )
    migration.require_runtime_context(ACTION)
    try:
        target = migration.resolve_runtime_target_from_env(env=os.environ, expected_platform_id="binance")
        expected, symbols = migration._expected_digests(), migration._symbols_from_env()
        if (migration.digest(expected) != FROZEN_EXPECTED_SHA256
                or migration.digest(list(symbols)) != MANAGED_SYMBOLS_SHA256
                or getattr(getattr(target, "live_continuity", None), "state", None) != "RECONCILE_ONLY"):
            raise ValueError("historical_readonly_source_invalid")
        source_run = _current_run()
        refs = migration._refs()
        before = migration._read_earn_diagnosis_source(refs)
        if before["owner_exists"]:
            raise ValueError("historical_readonly_owned")
        control = before["control"]
        if not isinstance(control, Mapping) or control.get("state") not in {"ACTIVE_LKG", "RECONCILE_ONLY"}:
            raise ValueError("historical_readonly_control_invalid")
        try:
            if control["state"] == "ACTIVE_LKG":
                # Verify the existing committed binding; discard the derived target.
                activated_target(target, control, expected=expected)
            else:
                migration._verified_control_account_scope(control, expected=expected, runtime_target=target)
        except (KeyError, TypeError, ValueError):
            raise ValueError("historical_readonly_control_invalid") from None
        ledger = before["ledger"]
        marker = ledger.get("accounting_rebase")
        name = marker.get("archive_document") if isinstance(marker, Mapping) else None
        if not migration.is_prospective_archive_document(name):
            raise ValueError("historical_continuity_archive_invalid")
        archive_ref = refs["ledger_ref"].parent.document(name)
        snapshot = archive_ref.get(retry=None)
        if not snapshot.exists:
            raise ValueError("historical_readonly_archive_missing")
        archive, archive_marker = snapshot.to_dict(), migration._snapshot_marker(snapshot)
        validate_historical_continuity_material(ledger, archive)
        binding = prospective_migration_binding(archive)
        try:
            migration_run = verified_run(binding["run_id"], expected_sha=binding["head_sha"])
        except ValueError:
            raise
        except Exception:
            raise ValueError("historical_readonly_migration_read_failed") from None
        client = ReadonlyClient(migration.connect_client(
            os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30,
        ))
        result = collect_historical_continuity_readonly(
            client=client, runtime_target=target, legacy_expected=expected,
            ledger=ledger, archive=archive, symbols=symbols, source_run=source_run,
            migration_run=migration_run, tool_revision=os.environ["GITHUB_SHA"],
            now=now or datetime.now(timezone.utc), clock=clock,
        )
        after = migration._read_earn_diagnosis_source(refs)
        if (after != before or not migration._snapshot_matches(archive_ref.get(retry=None), archive_marker)):
            raise ValueError("historical_readonly_source_changed")
        return dict(result, ledger_unchanged=True, control_unchanged=True,
                    archive_unchanged=True, owner_absent=True)
    except (ValueError, migration.MigrationBlocked) as error:
        reason = str(error)
        raise migration.MigrationBlocked(reason if reason in REASONS else "historical_readonly_failed") from None


def project_result(value, *, exit_code):
    """Only fixed codes and validated aggregate counters may reach workflow logs."""
    result = dict(status="blocked", reason_code="historical_readonly_failed",
                  diagnostic_scope=SCOPE, execution_permitted=False)
    if not isinstance(value, Mapping):
        return result
    if value.get("status") == "blocked":
        reason = value.get("reason_code")
        if type(reason) is str and reason in REASONS:
            result["reason_code"] = reason
        return result
    if (exit_code != 0 or value.get("status") != "diagnosed"
            or value.get("source_kind") != "historical_continuity"):
        return result
    for key, expected in dict(no_order=True, write_performed=False, execution_authority_granted=False,
                              historical_difference_unresolved=True, ledger_unchanged=True,
                              control_unchanged=True, archive_unchanged=True, owner_absent=True).items():
        if value.get(key) is not expected:
            return result
    count = value.get("chunk_count")
    if type(count) is not int or not 0 < count <= 1000000:
        return result
    result.update(status="diagnosed", reason_code="historical_readonly_complete", chunk_count=count,
                  historical_difference_unresolved=True)
    # This projection never supplies zero for a missing upstream count.
    from application.broker_reconciliation import _REWARD_PRODUCT_CLASSIFICATION_CODES
    raw = value.get("reward_product_classification_counts")
    result["reward_product_classification_counts"] = {
        key: raw[key] if isinstance(raw, Mapping) and type(raw.get(key)) is int and 0 <= raw[key] <= 10000000 else None
        for key in _REWARD_PRODUCT_CLASSIFICATION_CODES
    }
    return result
