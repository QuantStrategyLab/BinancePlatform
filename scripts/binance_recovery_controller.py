#!/usr/bin/env python3
"""Explicit main-only recovery controller. Never imports or invokes main.py."""
# ruff: noqa: E402
from __future__ import annotations

import json
import os
import re
import sys
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.cloud import firestore
from google.cloud.firestore_v1.transaction import Transaction as FirestoreTransaction
from quant_platform_kit.binance import connect_client
from quant_platform_kit.common.broker_reconciliation import BrokerReconciliationEvidence, calculate_broker_observation_sha256 as digest
from quant_platform_kit.common.reconciliation_recovery import evaluate_reconciliation_recovery_activation
from quant_platform_kit.common.runtime_target import resolve_runtime_target_from_env
from application.broker_reconciliation import _expected_digests, build_reconciliation_candidate, collect_read_only_reconciliation_observations, diagnose_balance_snapshot
from application.reconciliation_recovery import collect_recovery_source, console_snapshot, validate_source, verify_confirmation
from application.rebased_recovery import (
    ARCHIVE_DOCUMENT,
    MIGRATION_RUN_ID,
    MIGRATION_RUN_SHA,
    collect_post_rebase_source,
    validate_post_rebase_material,
    validate_post_rebase_source,
)
from live_services import get_firestore_client
from scripts.reconcile_frozen_live_baseline import _symbols_from_env

REPOSITORY = "QuantStrategyLab/BinancePlatform"
CONSOLE = "https://qsl-strategy-switch-console.pigbibi.workers.dev"
STAGE = "preflight"
MANAGED_SYMBOLS_SHA256 = "eb824ac33ac9781344cf717bd5a9ca291cfda87b2e8ca26d2ccc3b444635bbc8"
CONTROL_DOCUMENT = "MULTI_ASSET_STATE__recovery"
# Reviewed immutable baseline receipt: Runtime 33768005187, artifact 9898377291,
# evidence 90847854166609af782f1ba908825877182dbe5af50fe4fb4ed75447d880cdf2.
# This bounded adapter does not accept a user-supplied historical window/hash.
HISTORY_START = datetime(2026, 9, 3, 14, 38, 58, tzinfo=timezone.utc)
FROZEN_EXPECTED_SHA256 = "c4d1820390935d4bbad52094b21c671e68a58b04c3a0e32869fa6aadb9ea3b90"
BASELINE_ID = "crypto-binance-lkg-20260830"
BASELINE_TARGET_SHA256 = "f24b854707c62e8b9266c49a25fba2756e3dd708043595206d8b93d281c51245"

# Only exact application-owned codes may leave the read-only diagnostic.
# Never expose exception text, provider responses, quantities or account IDs.
DIAGNOSTIC_REASON_CODES = frozenset({
    "post_rebase_account_identity_mismatch", "post_rebase_archive_invalid",
    "post_rebase_archive_missing", "post_rebase_balance_amount_invalid",
    "post_rebase_candidate_source_mismatch", "post_rebase_earn_page_incomplete",
    "post_rebase_earn_read_failed", "post_rebase_enrollment_blocked",
    "post_rebase_history_incomplete", "post_rebase_history_window_invalid",
    "post_rebase_ledger_invalid", "post_rebase_locked_balance_present",
    "post_rebase_migration_run_invalid", "post_rebase_non_reward_activity_present",
    "post_rebase_open_orders_present", "post_rebase_order_state_unsafe",
    "post_rebase_quantity_changed_during_read", "post_rebase_quantity_mismatch",
    "post_rebase_recent_executions_present", "post_rebase_source_binding_mismatch",
    "post_rebase_source_run_invalid", "post_rebase_unknown_spot_balance",
})


class RecoveryWriteUncertain(RuntimeError):
    """A post-rebase write or publication may already be durable."""


class PostRebaseAtomicPrecondition(ValueError):
    """The post-rebase CAS failed before scheduling its control write."""


class _PostRebaseNoRetryCommitTransaction(FirestoreTransaction):
    """Firestore transaction whose mutating Commit RPC is never retried."""

    def _commit(self):
        if not self.in_progress:
            raise ValueError("post_rebase_transaction_not_started")
        commit_response = self._client._firestore_api.commit(
            request={
                "database": self._client._database_string,
                "writes": self._write_pbs,
                "transaction": self._id,
            },
            retry=None,
            metadata=self._client._rpc_metadata,
        )
        self._clean_up()
        self.write_results = list(commit_response.write_results)
        self.commit_time = commit_response.commit_time
        return self.write_results


def _post_rebase_transaction(db):
    return _PostRebaseNoRetryCommitTransaction(db, max_attempts=1)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        return None


def request_json(url, token, *, payload=None):
    if not token:
        raise ValueError("recovery_credential_missing")
    request = Request(url, data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json",
                               "User-Agent": "QuantStrategyLab-BinancePlatform/1.0 (reconciliation-controller)"},
                      method="GET" if payload is None else "POST")
    with build_opener(_NoRedirect).open(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError("recovery_http_not_acknowledged")
        body = response.read(262145)
        if len(body) > 262144:
            raise ValueError("recovery_response_too_large")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("recovery_response_invalid")
    return result


def verified_run(run_id, *, expected_sha=None, completed=True):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise ValueError("recovery_run_invalid")
    value = request_json(f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{run_id}", os.environ.get("GITHUB_TOKEN", ""))
    if (value.get("id") != int(run_id) or value.get("head_branch") != "main"
            or value.get("event") != "workflow_dispatch" or value.get("path") != ".github/workflows/main.yml"
            or value.get("repository", {}).get("full_name") != REPOSITORY
            or not re.fullmatch(r"[0-9a-f]{40}", value.get("head_sha", ""))
            or (expected_sha is not None and value.get("head_sha") != expected_sha)
            or (completed and (value.get("status") != "completed" or value.get("conclusion") != "success"))):
        raise ValueError("recovery_source_workflow_unverified")
    return {key: value[key] for key in ("id", "head_sha", "head_branch", "event", "path")}


def compare_and_set_control(transaction, *, control_ref, owner_ref, ledger_ref, previous, next_value, ledger_sha256):
    owner = owner_ref.get(transaction=transaction, retry=None)
    ledger = ledger_ref.get(transaction=transaction, retry=None)
    control = control_ref.get(transaction=transaction, retry=None)
    actual = control.to_dict() if control.exists else None
    if (owner.exists or not ledger.exists or digest(ledger.to_dict()) != ledger_sha256
            or actual != previous or (actual is not None and actual.get("state") != "RECONCILE_ONLY")):
        raise ValueError("recovery_atomic_precondition_changed")
    transaction.set(control_ref, next_value)


def _save_control(db, refs, *, previous, next_value, ledger_sha256):
    @firestore.transactional
    def apply(transaction):
        compare_and_set_control(transaction, **refs, previous=previous, next_value=next_value, ledger_sha256=ledger_sha256)
    # No retry on contention: a new read/confirmation must be evaluated first.
    apply(db.transaction(max_attempts=1))


def compare_and_set_post_rebase_control(
    transaction, *, refs, previous, next_value, ledger_sha256, archive_sha256
):
    owner = refs["owner_ref"].get(transaction=transaction, retry=None)
    ledger = refs["ledger_ref"].get(transaction=transaction, retry=None)
    control = refs["control_ref"].get(transaction=transaction, retry=None)
    archive = refs["archive_ref"].get(transaction=transaction, retry=None)
    actual = control.to_dict() if control.exists else None
    if (
        owner.exists
        or not ledger.exists
        or digest(ledger.to_dict()) != ledger_sha256
        or not archive.exists
        or digest(archive.to_dict()) != archive_sha256
        or actual != previous
        or (actual is not None and actual.get("state") != "RECONCILE_ONLY")
    ):
        raise PostRebaseAtomicPrecondition("post_rebase_atomic_precondition_changed")
    transaction.set(refs["control_ref"], next_value)


def save_post_rebase_control(
    db, refs, *, previous, next_value, ledger_sha256, archive_sha256
):
    @firestore.transactional
    def apply(transaction):
        compare_and_set_post_rebase_control(
            transaction,
            refs=refs,
            previous=previous,
            next_value=next_value,
            ledger_sha256=ledger_sha256,
            archive_sha256=archive_sha256,
        )

    try:
        apply(_post_rebase_transaction(db))
    except PostRebaseAtomicPrecondition:
        raise
    except Exception as exc:
        raise RecoveryWriteUncertain("post_rebase_control_commit_unknown") from exc
    try:
        control = refs["control_ref"].get(retry=None)
        ledger = refs["ledger_ref"].get(retry=None)
        archive = refs["archive_ref"].get(retry=None)
        owner = refs["owner_ref"].get(retry=None)
        if (
            not control.exists
            or control.to_dict() != next_value
            or not ledger.exists
            or digest(ledger.to_dict()) != ledger_sha256
            or not archive.exists
            or digest(archive.to_dict()) != archive_sha256
            or owner.exists
        ):
            raise RecoveryWriteUncertain("post_rebase_control_readback_mismatch")
    except RecoveryWriteUncertain:
        raise
    except Exception as exc:
        raise RecoveryWriteUncertain("post_rebase_control_readback_unknown") from exc


def run(action, recovery_id=""):
    global STAGE
    if (os.getenv("GITHUB_REPOSITORY") != REPOSITORY or os.getenv("GITHUB_REF") != "refs/heads/main"
            or os.getenv("GITHUB_WORKFLOW_REF") != f"{REPOSITORY}/.github/workflows/main.yml@refs/heads/main"
            or os.getenv("RUNTIME_TARGET_ENABLED", "").lower() != "false"
            or os.getenv("RECONCILE_ONLY") != "true"):
        raise ValueError("recovery_requires_disabled_main_runtime")
    target = resolve_runtime_target_from_env(env=os.environ, expected_platform_id="binance")
    expected = _expected_digests()
    if (digest(expected) != FROZEN_EXPECTED_SHA256 or target.live_continuity is None
            or target.live_continuity.baseline_id != BASELINE_ID
            or target.live_continuity.baseline_target_sha256 != BASELINE_TARGET_SHA256):
        raise ValueError("recovery_frozen_anchor_changed")
    STAGE = "workflow_provenance"
    current_run = verified_run(os.environ.get("GITHUB_RUN_ID", ""), expected_sha=os.environ.get("GITHUB_SHA"), completed=False)
    STAGE = "control_read"
    db = get_firestore_client()
    collection = db.collection("strategy")
    refs = {
        "control_ref": collection.document(CONTROL_DOCUMENT),
        "owner_ref": collection.document("MULTI_ASSET_STATE__owner"),
        "ledger_ref": collection.document("MULTI_ASSET_STATE"),
        "archive_ref": collection.document(ARCHIVE_DOCUMENT),
    }
    snapshot = refs["control_ref"].get(retry=None)
    previous = snapshot.to_dict() if snapshot.exists else None
    ledger_snapshot = refs["ledger_ref"].get(retry=None)
    if not ledger_snapshot.exists or refs["owner_ref"].get(retry=None).exists:
        raise ValueError("recovery_ledger_unavailable_or_owned")
    ledger = ledger_snapshot.to_dict()
    archive_snapshot = refs["archive_ref"].get(retry=None)
    archive = archive_snapshot.to_dict() if archive_snapshot.exists else None
    is_post_rebase = archive is not None
    symbols = _symbols_from_env()
    if digest(list(symbols)) != MANAGED_SYMBOLS_SHA256:
        raise ValueError("recovery_managed_symbols_changed")
    if action in {"prepare", "diagnose"}:
        if previous is not None and previous.get("state") != "RECONCILE_ONLY":
            raise ValueError("recovery_already_active")
        if action == "diagnose" and not is_post_rebase:
            raise ValueError("post_rebase_archive_missing")
        STAGE = "broker_collection"
        client = connect_client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30)
        if is_post_rebase:
            collected_at = datetime.now(timezone.utc)
            migration_run = verified_run(MIGRATION_RUN_ID, expected_sha=MIGRATION_RUN_SHA)
            package = collect_post_rebase_source(
                client=client,
                runtime_target=target,
                legacy_expected=expected,
                ledger=ledger,
                archive=archive,
                symbols=symbols,
                source_run=current_run,
                migration_run=migration_run,
                now=collected_at,
                observe_only_non_managed_spot=True,
            )
            candidate = validate_post_rebase_source(
                package,
                runtime_target=target,
                legacy_expected=expected,
                now=collected_at,
            )
        else:
            package = collect_recovery_source(client=client, runtime_target=target, expected=expected, ledger=ledger,
                                              symbols=symbols, history_start=HISTORY_START, source_run=current_run)
            candidate = validate_source(package, runtime_target=target, expected=expected)
        if action == "diagnose":
            return {"status": "diagnosed", "source_kind": "post_rebase",
                    "non_managed_spot_policy": package["source"]["proof"]["non_managed_spot_policy"],
                    "observed_non_managed_asset_count": package["source"]["proof"]["observed_non_managed_asset_count"],
                    "historical_difference_unresolved": True, "no_order": True,
                    "write_performed": False, "execution_authority_granted": False}
        recovery_id = f"binance-{current_run['id']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
        value = {"state": "RECONCILE_ONLY", "recovery_id": recovery_id, **package}
        STAGE = "candidate_storage"
        if is_post_rebase:
            save_post_rebase_control(
                db,
                refs,
                previous=previous,
                next_value=value,
                ledger_sha256=candidate.local_execution_ledger_sha256,
                archive_sha256=digest(archive),
            )
        else:
            _save_control(
                db,
                {key: refs[key] for key in ("control_ref", "owner_ref", "ledger_ref")},
                previous=previous,
                next_value=value,
                ledger_sha256=candidate.local_execution_ledger_sha256,
            )
        STAGE = "console_publication"
        payload = console_snapshot(
            candidate,
            recovery_id=recovery_id,
            now=collected_at if is_post_rebase else None,
        )
        try:
            acknowledgement = request_json(CONSOLE + "/api/internal/sync-reconciliation-recovery-source", os.getenv("RECONCILIATION_RECOVERY_SYNC_TOKEN", ""), payload=payload)
            if (acknowledgement.get("ok") is not True or acknowledgement.get("source_id") != payload["source_id"]
                    or acknowledgement.get("recovery_count") != 1 or acknowledgement.get("generated_at") != payload["generated_at"]):
                raise ValueError("recovery_publication_not_acknowledged")
        except Exception as exc:
            if is_post_rebase:
                raise RecoveryWriteUncertain("post_rebase_publication_unknown") from exc
            raise
        result = {"status": "awaiting_human_confirmation", "recovery_id": recovery_id, "candidate_sha256": candidate.candidate_sha256, "no_order": True, "execution_authority_granted": False}
        if is_post_rebase:
            result.update(source_kind="post_rebase", historical_difference_unresolved=True)
        return result
    if not previous or previous.get("recovery_id") != recovery_id or previous.get("state") != "RECONCILE_ONLY":
        raise ValueError("recovery_request_missing_or_changed")
    source_run = previous["source"]["run"]
    if verified_run(source_run["id"], expected_sha=source_run["head_sha"]) != source_run:
        raise ValueError("recovery_source_run_changed")
    is_post_rebase = previous.get("source", {}).get("kind") == "post_rebase"
    if is_post_rebase:
        validation_at = datetime.now(timezone.utc)
        migration_run = previous["source"]["migration_run"]
        if verified_run(
            migration_run["id"], expected_sha=MIGRATION_RUN_SHA
        ) != migration_run:
            raise ValueError("post_rebase_migration_run_changed")
        if archive is None:
            raise ValueError("post_rebase_archive_missing")
        if digest(archive) != previous["source"].get("archive_sha256"):
            raise ValueError("post_rebase_archive_changed")
        validate_post_rebase_material(ledger, archive)
        candidate = validate_post_rebase_source(
            previous,
            runtime_target=target,
            legacy_expected=expected,
            now=validation_at,
        )
    else:
        candidate = validate_source(previous, runtime_target=target, expected=expected)
    if previous["source"]["symbols_sha256"] != digest(list(symbols)):
        raise ValueError("recovery_managed_symbols_changed")
    STAGE = "confirmation_read"
    response = request_json(CONSOLE + "/api/internal/reconciliation-recovery-confirmation?" + urlencode({"recovery_id": recovery_id}), os.getenv("RECONCILIATION_RECOVERY_CONTROLLER_TOKEN", ""))
    confirmation = verify_confirmation(response, recovery_id=recovery_id, candidate=candidate)
    STAGE = "post_confirmation_collection"
    # Broker reads begin only after the actual server-issued confirmation.
    client = connect_client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30)
    observed_at = datetime.now(timezone.utc)
    ledger_snapshot = refs["ledger_ref"].get(retry=None)
    if not ledger_snapshot.exists or refs["owner_ref"].get(retry=None).exists:
        raise ValueError("recovery_ledger_unavailable_or_owned")
    ledger = ledger_snapshot.to_dict()
    if is_post_rebase:
        archive_snapshot = refs["archive_ref"].get(retry=None)
        if not archive_snapshot.exists:
            raise ValueError("post_rebase_archive_missing")
        archive = archive_snapshot.to_dict()
        if digest(archive) != previous["source"].get("archive_sha256"):
            raise ValueError("post_rebase_archive_changed")
        validate_post_rebase_material(ledger, archive)
        fresh = collect_post_rebase_source(
            client=client,
            runtime_target=target,
            legacy_expected=expected,
            ledger=ledger,
            archive=archive,
            symbols=symbols,
            source_run=current_run,
            migration_run=migration_run,
            now=observed_at,
            observe_only_non_managed_spot=True,
        )
        fresh_candidate = validate_post_rebase_source(
            fresh, runtime_target=target, legacy_expected=expected, now=observed_at
        )
        if (
            fresh_candidate.account_scope_sha256 != candidate.account_scope_sha256
            or fresh_candidate.expected_digests != candidate.expected_digests
        ):
            raise ValueError("post_rebase_fresh_candidate_changed")
        current_evidence = BrokerReconciliationEvidence.from_dict(
            fresh["source"]["reconciled_evidence"]
        )
        validate_post_rebase_source(
            previous,
            runtime_target=target,
            legacy_expected=expected,
            now=observed_at,
        )
    else:
        observations = collect_read_only_reconciliation_observations(client, strategy_symbols=symbols, local_execution_ledger=ledger, now=observed_at)
        if observations.open_orders:
            raise ValueError("recovery_open_orders_present")
        current_expected = {"account_scope_sha256": candidate.account_scope_sha256, **candidate.expected_digests}
        current = build_reconciliation_candidate(observations=observations, runtime_target=target, env_reader=lambda *_: json.dumps(current_expected), observed_at=observed_at)
        if diagnose_balance_snapshot(client.get_account(), expected_digests=current_expected)["reason_code"] != "balance_snapshot_matches":
            raise ValueError("recovery_balance_changed_during_read")
        validate_source(previous, runtime_target=target, expected=expected)
        current_evidence = current.evidence
    evaluation = evaluate_reconciliation_recovery_activation(recovery_id=recovery_id, candidate=candidate, confirmation=confirmation,
        current_evidence=current_evidence, current_live_continuity_state=target.live_continuity.state)
    if not evaluation.ready_for_atomic_state_transition:
        raise ValueError("recovery_post_confirmation_reconciliation_blocked")
    if action == "activate":
        STAGE = "atomic_activation"
        value = {**previous, "state": "ACTIVE_LKG", "transition_plan": evaluation.transition_plan.to_dict(), "confirmation": confirmation.to_dict()}
        if is_post_rebase:
            save_post_rebase_control(
                db,
                refs,
                previous=previous,
                next_value=value,
                ledger_sha256=candidate.local_execution_ledger_sha256,
                archive_sha256=digest(archive),
            )
        else:
            _save_control(
                db,
                {key: refs[key] for key in ("control_ref", "owner_ref", "ledger_ref")},
                previous=previous,
                next_value=value,
                ledger_sha256=candidate.local_execution_ledger_sha256,
            )
    result = {"status": "active_lkg" if action == "activate" else "verified", "recovery_id": recovery_id,
              "no_order": True, "execution_authority_granted": False, "runtime_target_enabled": False}
    if is_post_rebase:
        result.update(source_kind="post_rebase", historical_difference_unresolved=True)
    return result


def main(argv=None):
    parser = ArgumentParser(description="Prepare, verify or adopt the designated Binance recovery; never trade")
    parser.add_argument("action", choices=("diagnose", "prepare", "verify", "activate"))
    parser.add_argument("--recovery-id", default="")
    args = parser.parse_args(argv)
    if args.action not in {"prepare", "diagnose"} and not re.fullmatch(r"binance-[0-9]+-[0-9]+", args.recovery_id):
        parser.error("verify/activate require the exact recovery ID shown by prepare")
    try:
        result = run(args.action, args.recovery_id)
    except RecoveryWriteUncertain:
        print(json.dumps({"status": "uncertain", "stage": STAGE, "reason_code": "post_rebase_outcome_unknown", "no_retry": True, "no_order": True}))
        return 2
    except HTTPError as exc:
        print(json.dumps({"status": "blocked", "stage": STAGE, "reason_code": "recovery_http_request_failed", "http_status": exc.code, "no_order": True}))
        return 2
    except Exception as exc:
        # Never serialize provider messages, credentials or broker payloads.
        reason = "recovery_operation_failed"
        if args.action == "diagnose" and type(exc) is ValueError and str(exc) in DIAGNOSTIC_REASON_CODES:
            reason = str(exc)
        print(json.dumps({"status": "blocked", "stage": STAGE, "reason_code": reason, "no_order": True}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
