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
from quant_platform_kit.binance import connect_client
from quant_platform_kit.common.broker_reconciliation import calculate_broker_observation_sha256 as digest
from quant_platform_kit.common.reconciliation_recovery import evaluate_reconciliation_recovery_activation
from quant_platform_kit.common.runtime_target import resolve_runtime_target_from_env
from application.broker_reconciliation import _expected_digests, build_reconciliation_candidate, collect_read_only_reconciliation_observations, diagnose_balance_snapshot
from application.reconciliation_recovery import collect_recovery_source, console_snapshot, validate_source, verify_confirmation
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
    refs = {"control_ref": collection.document(CONTROL_DOCUMENT), "owner_ref": collection.document("MULTI_ASSET_STATE__owner"), "ledger_ref": collection.document("MULTI_ASSET_STATE")}
    snapshot = refs["control_ref"].get(retry=None)
    previous = snapshot.to_dict() if snapshot.exists else None
    ledger_snapshot = refs["ledger_ref"].get(retry=None)
    if not ledger_snapshot.exists or refs["owner_ref"].get(retry=None).exists:
        raise ValueError("recovery_ledger_unavailable_or_owned")
    ledger = ledger_snapshot.to_dict()
    symbols = _symbols_from_env()
    if digest(list(symbols)) != MANAGED_SYMBOLS_SHA256:
        raise ValueError("recovery_managed_symbols_changed")
    if action == "prepare":
        if previous is not None and previous.get("state") != "RECONCILE_ONLY":
            raise ValueError("recovery_already_active")
        STAGE = "broker_collection"
        client = connect_client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30)
        package = collect_recovery_source(client=client, runtime_target=target, expected=expected, ledger=ledger,
                                          symbols=symbols, history_start=HISTORY_START, source_run=current_run)
        candidate = validate_source(package, runtime_target=target, expected=expected)
        recovery_id = f"binance-{current_run['id']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
        value = {"state": "RECONCILE_ONLY", "recovery_id": recovery_id, **package}
        STAGE = "candidate_storage"
        _save_control(db, refs, previous=previous, next_value=value, ledger_sha256=candidate.local_execution_ledger_sha256)
        STAGE = "console_publication"
        payload = console_snapshot(candidate, recovery_id=recovery_id)
        acknowledgement = request_json(CONSOLE + "/api/internal/sync-reconciliation-recovery-source", os.getenv("RECONCILIATION_RECOVERY_SYNC_TOKEN", ""), payload=payload)
        if (acknowledgement.get("ok") is not True or acknowledgement.get("source_id") != payload["source_id"]
                or acknowledgement.get("recovery_count") != 1 or acknowledgement.get("generated_at") != payload["generated_at"]):
            raise ValueError("recovery_publication_not_acknowledged")
        return {"status": "awaiting_human_confirmation", "recovery_id": recovery_id, "candidate_sha256": candidate.candidate_sha256, "no_order": True, "execution_authority_granted": False}
    if not previous or previous.get("recovery_id") != recovery_id or previous.get("state") != "RECONCILE_ONLY":
        raise ValueError("recovery_request_missing_or_changed")
    source_run = previous["source"]["run"]
    if verified_run(source_run["id"], expected_sha=source_run["head_sha"]) != source_run:
        raise ValueError("recovery_source_run_changed")
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
    observations = collect_read_only_reconciliation_observations(client, strategy_symbols=symbols, local_execution_ledger=ledger, now=observed_at)
    if observations.open_orders:
        raise ValueError("recovery_open_orders_present")
    current_expected = {"account_scope_sha256": candidate.account_scope_sha256, **candidate.expected_digests}
    current = build_reconciliation_candidate(observations=observations, runtime_target=target, env_reader=lambda *_: json.dumps(current_expected), observed_at=observed_at)
    if diagnose_balance_snapshot(client.get_account(), expected_digests=current_expected)["reason_code"] != "balance_snapshot_matches":
        raise ValueError("recovery_balance_changed_during_read")
    validate_source(previous, runtime_target=target, expected=expected)
    evaluation = evaluate_reconciliation_recovery_activation(recovery_id=recovery_id, candidate=candidate, confirmation=confirmation,
        current_evidence=current.evidence, current_live_continuity_state=target.live_continuity.state)
    if not evaluation.ready_for_atomic_state_transition:
        raise ValueError("recovery_post_confirmation_reconciliation_blocked")
    if action == "activate":
        STAGE = "atomic_activation"
        value = {**previous, "state": "ACTIVE_LKG", "transition_plan": evaluation.transition_plan.to_dict(), "confirmation": confirmation.to_dict()}
        _save_control(db, refs, previous=previous, next_value=value, ledger_sha256=candidate.local_execution_ledger_sha256)
    return {"status": "active_lkg" if action == "activate" else "verified", "recovery_id": recovery_id,
            "no_order": True, "execution_authority_granted": False, "runtime_target_enabled": False}


def main(argv=None):
    parser = ArgumentParser(description="Prepare, verify or adopt the designated Binance legacy recovery; never trade")
    parser.add_argument("action", choices=("prepare", "verify", "activate"))
    parser.add_argument("--recovery-id", default="")
    args = parser.parse_args(argv)
    if args.action != "prepare" and not re.fullmatch(r"binance-[0-9]+-[0-9]+", args.recovery_id):
        parser.error("verify/activate require the exact recovery ID shown by prepare")
    try:
        result = run(args.action, args.recovery_id)
    except HTTPError as exc:
        print(json.dumps({"status": "blocked", "stage": STAGE, "reason_code": "recovery_http_request_failed", "http_status": exc.code, "no_order": True}))
        return 2
    except Exception:
        # Never serialize provider messages, credentials or broker payloads.
        print(json.dumps({"status": "blocked", "stage": STAGE, "reason_code": "recovery_operation_failed", "no_order": True}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
