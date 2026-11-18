#!/usr/bin/env python3
"""Inspect, preview, or atomically apply bounded Binance accounting operations.

The command has no order, transfer, redemption, subscription, cancellation, or
notification path.  Preview writes one short-lived redacted candidate file.
Apply accepts only that fixed artifact path, repeats the read-only evidence, and
updates an allowlist of fields in the existing Firestore ledger transaction.
The private Spot scope preview publishes once to the authenticated console.
"""

# ruff: noqa: E402, F401

from __future__ import annotations

import json
import os
import re
import sys
from argparse import ArgumentParser
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
for path in (SRC_ROOT, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from quant_platform_kit.binance import connect_client
from quant_platform_kit.common.broker_reconciliation import (
    calculate_broker_observation_sha256 as digest,
)
from quant_platform_kit.common.broker_reconciliation_enrollment import (
    BrokerReconciliationBaselineCandidate,
)
from quant_platform_kit.common.runtime_target import resolve_runtime_target_from_env

from binance_platform.accounting_migration.candidate_migration import (
    build_candidate as _impl_build_candidate,
    compare_and_apply as _impl_compare_and_apply,
    collect_evidence as _impl_collect_evidence,
    inspect_control as _impl_inspect_control,
    quiesce_control as _impl_quiesce_control,
    quiesce_control_transaction as _impl_quiesce_control_transaction,
    read_source as _impl_read_source,
    release_stale_owner as _impl_release_stale_owner,
    release_stale_owner_transaction as _release_stale_owner_transaction,
    strict_balance_snapshot as _strict_balance_snapshot,
    validate_candidate as _impl_validate_candidate,
    validate_stale_owner_control as _validate_stale_owner_control,
    verify_applied as _impl_verify_applied,
)
from binance_platform.accounting_migration.common import (
    _ACCOUNTING_FIELDS,
    _EARN_PAGE_SIZE,
    _MAX_PRIVATE_SCOPE_BODY_BYTES,
    _MAX_SPOT_BALANCE_ROWS,
    APPROVED_PROSPECTIVE_SHA256,
    APPROVED_REBASE_BALANCES_SHA256,
    APPROVED_REBASE_CONTROL_SHA256,
    APPROVED_REBASE_LEDGER_SHA256,
    CONTROL_DOCUMENT,
    LEDGER_DOCUMENT,
    NEW_BASIS,
    OWNER_DOCUMENT,
    PREVIEW_PATH,
    PRIVATE_SCOPE_CONSOLE_URL,
    PROSPECTIVE_ARCHIVE_DOCUMENT,
    QUIESCE_CONTROL_SHA256,
    QUIESCE_CONTROL_UPDATE_TIME,
    REBASE_ARCHIVE_DOCUMENT,
    REBASE_PROPOSAL_PATH,
    REPOSITORY,
    SCHEMA_VERSION,
    TTL,
    MigrationApplyUncertain,
    MigrationAtomicPrecondition,
    MigrationBlocked,
    MigrationHooks,
    _canonical_sha as _impl_canonical_sha,
    _candidate_digest as _impl_candidate_digest,
    _diagnosis_decimal,
    _direction,
    _finite,
    _private_spot_account as _impl_private_spot_account,
    _proposal_run_id,
    _same_balance,
    _snapshot_marker,
    _snapshot_matches,
    _timestamp,
    _validate_control,
    _validate_safe_order_state,
    _zero_activity,
    firestore_refs,
    is_prospective_archive_document,
    ledger_scope as _ledger_scope,
    parse_symbols,
    prospective_archive_document,
    validate_runtime_context,
)
from binance_platform.accounting_migration.diagnostics import (
    _default_request_json,
    audit_ledger as _impl_audit_ledger,
    build_private_spot_scope_payload,
    classify_activity_evidence,
    collect_private_spot_scope_preview as _impl_collect_private_spot_scope_preview,
    diagnose_earn_forward as _impl_diagnose_earn_forward,
    forward_accounting_eligibility as _forward_accounting_eligibility,
    preview_external_cash_flow as _impl_preview_external_cash_flow,
    publish_private_spot_scope_preview as _impl_publish_private_spot_scope_preview,
    read_earn_diagnosis_source as _read_earn_diagnosis_source,
    summarize_bnb_wallet_activity as _summarize_bnb_wallet_activity,
    trade_net_diagnosis as _trade_net_diagnosis,
    validate_bnb_dividend_rows as _validate_bnb_dividend_rows,
    validated_private_scope_source as _impl_validated_private_scope_source,
)
from binance_platform.accounting_migration.rebase import (
    apply_approved_rebase as _impl_apply_approved_rebase,
    apply_prospective_rebase as _impl_apply_prospective_rebase,
    approved_rebase_fields as _approved_rebase_fields,
    build_rebase_proposal,
    collect_prospective_opening as _impl_collect_prospective_opening,
    encrypt_rebase_proposal,
    parse_prospective_approval,
    prospective_preflight as _impl_prospective_preflight,
    prospective_transaction as _impl_prospective_transaction,
    rebase_transaction as _impl_rebase_transaction,
    validate_prospective_approval as _impl_validate_prospective_approval,
    verified_control_account_scope as _impl_verified_control_account_scope,
)
from binance_platform.application.broker_reconciliation import (
    BinanceReconciliationReadError,
    _expected_digests,
    collect_read_only_reconciliation_observations,
    collect_spot_usdt_external_cash_flows,
    diagnose_balance_flows,
    diagnose_bnb_wallet_activity,
)
from binance_platform.application.portfolio_service import (
    maybe_rebase_daily_state_for_balance_change,
)
from binance_platform.live_services import get_firestore_client
from binance_platform.runtime_support import ExecutionIntegrityError

__all__ = [
    "APPROVED_PROSPECTIVE_SHA256",
    "APPROVED_REBASE_BALANCES_SHA256",
    "APPROVED_REBASE_CONTROL_SHA256",
    "APPROVED_REBASE_LEDGER_SHA256",
    "BinanceReconciliationReadError",
    "BrokerReconciliationBaselineCandidate",
    "CONTROL_DOCUMENT",
    "ExecutionIntegrityError",
    "LEDGER_DOCUMENT",
    "MigrationApplyUncertain",
    "MigrationAtomicPrecondition",
    "MigrationBlocked",
    "NEW_BASIS",
    "OWNER_DOCUMENT",
    "PREVIEW_PATH",
    "PRIVATE_SCOPE_CONSOLE_URL",
    "PROSPECTIVE_ARCHIVE_DOCUMENT",
    "QUIESCE_CONTROL_SHA256",
    "QUIESCE_CONTROL_UPDATE_TIME",
    "REBASE_ARCHIVE_DOCUMENT",
    "REBASE_PROPOSAL_PATH",
    "REPOSITORY",
    "SCHEMA_VERSION",
    "TTL",
    "audit_ledger",
    "build_candidate",
    "build_private_spot_scope_payload",
    "build_rebase_proposal",
    "classify_activity_evidence",
    "collect_private_spot_scope_preview",
    "collect_prospective_opening",
    "compare_and_apply",
    "diagnose_earn_forward",
    "digest",
    "encrypt_rebase_proposal",
    "inspect_control",
    "is_prospective_archive_document",
    "main",
    "preview_external_cash_flow",
    "proposal_run_id_from_env",
    "prospective_archive_document",
    "publish_private_spot_scope_preview",
    "release_stale_owner",
    "require_runtime_context",
    "run",
    "validate_candidate",
]


def _hooks() -> MigrationHooks:
    """Bind current module globals so monkeypatched tests flow through cleanly."""
    return MigrationHooks(
        digest_fn=digest,
        connect_client_fn=connect_client,
        firestore_client_fn=get_firestore_client,
        collect_observations_fn=collect_read_only_reconciliation_observations,
        collect_cash_flows_fn=collect_spot_usdt_external_cash_flows,
        diagnose_flows_fn=diagnose_balance_flows,
        diagnose_bnb_wallet_fn=diagnose_bnb_wallet_activity,
        maybe_rebase_daily_state_fn=maybe_rebase_daily_state_for_balance_change,
        quiesce_control_sha256=QUIESCE_CONTROL_SHA256,
        quiesce_control_update_time=QUIESCE_CONTROL_UPDATE_TIME,
        approved_rebase_ledger_sha256=APPROVED_REBASE_LEDGER_SHA256,
        approved_rebase_control_sha256=APPROVED_REBASE_CONTROL_SHA256,
        approved_rebase_balances_sha256=APPROVED_REBASE_BALANCES_SHA256,
        approved_prospective_sha256=APPROVED_PROSPECTIVE_SHA256,
        datetime_cls=datetime,
        read_source_fn=_read_source,
        collect_evidence_fn=_collect_evidence,
        validate_stale_owner_control_fn=_validate_stale_owner_control,
        private_spot_account_fn=_private_spot_account,
        prospective_preflight_fn=_prospective_preflight,
        verified_control_account_scope_fn=_verified_control_account_scope,
        validated_private_scope_source_fn=_validated_private_scope_source,
        collect_prospective_opening_fn=collect_prospective_opening,
        build_rebase_proposal_fn=build_rebase_proposal,
        encrypt_rebase_proposal_fn=encrypt_rebase_proposal,
        build_candidate_fn=build_candidate,
        compare_and_apply_fn=compare_and_apply,
        verify_applied_fn=_verify_applied,
        publish_private_spot_scope_preview_fn=publish_private_spot_scope_preview,
        request_json_fn=_request_json,
    )


def _symbols_from_env() -> tuple[str, ...]:
    return parse_symbols(os.environ.get("BINANCE_RECONCILIATION_SYMBOLS"))


def _canonical_sha(value) -> str:
    return _impl_canonical_sha(value, digest_fn=digest)


def _candidate_digest(candidate: Mapping[str, object]) -> str:
    return _impl_candidate_digest(candidate, digest_fn=digest)


def require_runtime_context() -> None:
    validate_runtime_context(os.environ)


def _private_spot_account(account, *, expected_account_scope_sha256: str) -> dict[str, object]:
    return _impl_private_spot_account(
        account,
        expected_account_scope_sha256=expected_account_scope_sha256,
        digest_fn=digest,
    )


def _refs():
    return firestore_refs(get_firestore_client)


def _read_source(refs, *, proposal_target=None, expected=None):
    return _impl_read_source(refs, proposal_target=proposal_target, expected=expected)


def _request_json(url: str, token: str, *, payload=None):
    from scripts import binance_recovery_controller as controller

    return controller.request_json(url, token, payload=payload)


def collect_prospective_opening(
    client, *, ledger, expected, now, clock=None, collect_cash_flows=None
):
    kwargs = {
        "ledger": ledger,
        "expected": expected,
        "now": now,
        "clock": clock,
        "hooks": _hooks(),
    }
    if collect_cash_flows is not None:
        kwargs["collect_cash_flows"] = collect_cash_flows
    return _impl_collect_prospective_opening(client, **kwargs)


def proposal_run_id_from_env() -> str:
    return _proposal_run_id(os.environ.get("GITHUB_RUN_ID"))


def _load_prospective_approval():
    raw = os.environ.pop("BINANCE_APPROVED_PROSPECTIVE_OPENING", "")
    return parse_prospective_approval(raw, hooks=_hooks())


def _validate_prospective_approval(approved):
    return _impl_validate_prospective_approval(approved, hooks=_hooks())


def _verified_control_account_scope(control, *, expected, runtime_target):
    return _impl_verified_control_account_scope(
        control, expected=expected, runtime_target=runtime_target
    )


def _prospective_preflight(*, refs, client, expected, approved, now, runtime_target, clock=None):
    return _impl_prospective_preflight(
        refs=refs,
        client=client,
        expected=expected,
        approved=approved,
        now=now,
        runtime_target=runtime_target,
        clock=clock,
        hooks=_hooks(),
    )


def _prospective_transaction(transaction, *, refs, archive_ref, approved, now):
    return _impl_prospective_transaction(
        transaction, refs=refs, archive_ref=archive_ref, approved=approved, now=now, hooks=_hooks()
    )


def _apply_prospective_rebase(refs, *, client, expected, now, fixed_now, runtime_target):
    approved = _load_prospective_approval()
    return _impl_apply_prospective_rebase(
        refs,
        client=client,
        expected=expected,
        approved=approved,
        now=now,
        fixed_now=fixed_now,
        runtime_target=runtime_target,
        hooks=_hooks(),
    )


def _rebase_transaction(
    transaction,
    *,
    refs,
    archive_ref,
    ledger,
    control,
    ledger_update_time,
    proposed_fields,
    observed_at,
    started_at,
):
    return _impl_rebase_transaction(
        transaction,
        refs=refs,
        archive_ref=archive_ref,
        ledger=ledger,
        control=control,
        ledger_update_time=ledger_update_time,
        proposed_fields=proposed_fields,
        observed_at=observed_at,
        started_at=started_at,
        hooks=_hooks(),
    )


def _apply_approved_rebase(refs, *, client, expected, now, fixed_now, configured_symbols=None):
    raw_symbols = str(os.environ.get("BINANCE_RECONCILIATION_SYMBOLS") or "").strip()
    symbols = (
        configured_symbols
        if configured_symbols is not None
        else (parse_symbols(raw_symbols) if raw_symbols else ())
    )
    return _impl_apply_approved_rebase(
        refs,
        client=client,
        expected=expected,
        configured_symbols=symbols,
        now=now,
        fixed_now=fixed_now,
        hooks=_hooks(),
    )


def build_candidate(
    *,
    ledger: Mapping[str, object],
    ledger_update_time,
    control: Mapping[str, object] | None,
    evidence: Mapping[str, object],
    source_sha: str,
    observed_at: datetime,
) -> dict[str, object]:
    return _impl_build_candidate(
        ledger=ledger,
        ledger_update_time=ledger_update_time,
        control=control,
        evidence=evidence,
        source_sha=source_sha,
        observed_at=observed_at,
        hooks=_hooks(),
    )


def validate_candidate(candidate: Mapping[str, object], *, expected_digest: str, now: datetime) -> None:
    _impl_validate_candidate(candidate, expected_digest=expected_digest, now=now, hooks=_hooks())


def compare_and_apply(transaction, *, ledger_ref, owner_ref, control_ref, candidate, balance_snapshot):
    return _impl_compare_and_apply(
        transaction,
        ledger_ref=ledger_ref,
        owner_ref=owner_ref,
        control_ref=control_ref,
        candidate=candidate,
        balance_snapshot=balance_snapshot,
        hooks=_hooks(),
    )


def _collect_evidence(client, *, ledger, now, expected, require_prices=False, configured_symbols=None):
    return _impl_collect_evidence(
        client,
        ledger=ledger,
        now=now,
        expected=expected,
        require_prices=require_prices,
        configured_symbols=(
            configured_symbols if configured_symbols is not None else _symbols_from_env()
        ),
        hooks=_hooks(),
    )


def _verify_applied(refs, *, candidate, balance_snapshot):
    return _impl_verify_applied(refs, candidate=candidate, balance_snapshot=balance_snapshot, hooks=_hooks())


def release_stale_owner(refs, *, client, target, expected):
    return _impl_release_stale_owner(refs, client=client, target=target, expected=expected, hooks=_hooks())


def inspect_control(refs):
    return _impl_inspect_control(refs, hooks=_hooks())


def _quiesce_control_transaction(transaction, refs):
    return _impl_quiesce_control_transaction(transaction, refs, hooks=_hooks())


def _quiesce_control(refs):
    return _impl_quiesce_control(refs, hooks=_hooks())


def diagnose_earn_forward(refs, *, client, expected, now, configured_symbols=None):
    return _impl_diagnose_earn_forward(
        refs,
        client=client,
        expected=expected,
        now=now,
        configured_symbols=(
            configured_symbols if configured_symbols is not None else _symbols_from_env()
        ),
        hooks=_hooks(),
    )


def _validated_private_scope_source(refs, *, initial_source=None):
    return _impl_validated_private_scope_source(refs, initial_source=initial_source, hooks=_hooks())


def collect_private_spot_scope_preview(
    refs, *, client, expected, observed_at, source_sha, initial_source=None
):
    return _impl_collect_private_spot_scope_preview(
        refs,
        client=client,
        expected=expected,
        observed_at=observed_at,
        source_sha=source_sha,
        initial_source=initial_source,
        hooks=_hooks(),
    )


def _private_scope_publication_context() -> tuple[str, str]:
    token = os.environ.get("RECONCILIATION_RECOVERY_SYNC_TOKEN", "")
    source_run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not token or not re.fullmatch(r"[1-9][0-9]{0,19}", source_run_id):
        raise MigrationBlocked("private_scope_publication_config_invalid")
    return token, source_run_id


def publish_private_spot_scope_preview(preview, *, token, source_run_id, source_sha) -> None:
    _impl_publish_private_spot_scope_preview(
        preview,
        token=token,
        source_run_id=source_run_id,
        source_sha=source_sha,
        request_json_fn=_request_json,
    )


def audit_ledger(refs, *, client, expected, now, configured_symbols=None):
    return _impl_audit_ledger(
        refs,
        client=client,
        expected=expected,
        now=now,
        configured_symbols=(
            configured_symbols if configured_symbols is not None else _symbols_from_env()
        ),
        hooks=_hooks(),
    )


def preview_external_cash_flow(
    refs, *, client, expected, now, initial_source=None, configured_symbols=None
):
    return _impl_preview_external_cash_flow(
        refs,
        client=client,
        expected=expected,
        now=now,
        configured_symbols=(
            configured_symbols if configured_symbols is not None else _symbols_from_env()
        ),
        initial_source=initial_source,
        hooks=_hooks(),
    )


def run(action: str, *, expected_digest: str = "", now: datetime | None = None):
    require_runtime_context()
    if action == "inspect":
        return inspect_control(_refs())
    if action == "quiesce":
        return _quiesce_control(_refs())
    if action == "earn-forward-diagnose":
        now = now or datetime.now(timezone.utc)
        target = resolve_runtime_target_from_env(env=os.environ, expected_platform_id="binance")
        if str(getattr(getattr(target, "live_continuity", None), "state", "")).upper() != "RECONCILE_ONLY":
            raise MigrationBlocked("runtime_target_not_reconcile_only")
        expected = _expected_digests()
        if not expected:
            raise MigrationBlocked("expected_account_scope_missing")
        refs = _refs()
        client = connect_client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30)
        return diagnose_earn_forward(refs, client=client, expected=expected, now=now)
    publication = _private_scope_publication_context() if action == "scope-preview" else None
    fixed_now = now is not None
    now = now or datetime.now(timezone.utc)
    target = resolve_runtime_target_from_env(env=os.environ, expected_platform_id="binance")
    if str(getattr(getattr(target, "live_continuity", None), "state", "")).upper() != "RECONCILE_ONLY":
        raise MigrationBlocked("runtime_target_not_reconcile_only")
    expected = _expected_digests()
    if not expected:
        raise MigrationBlocked("expected_account_scope_missing")
    refs = _refs()
    if action == "release-stale-owner":
        owner_snapshot = refs["owner_ref"].get(retry=None)
        client = None
        if owner_snapshot.exists:
            client = connect_client(
                os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30
            )
        return release_stale_owner(refs, client=client, target=target, expected=expected)
    source_options = {"proposal_target": target, "expected": expected} if action == "rebase-proposal" else {}
    ledger_snapshot, ledger, control = _read_source(refs, **source_options)
    client = connect_client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"], timeout=30)
    if action == "cash-flow-preview":
        return preview_external_cash_flow(
            refs,
            client=client,
            expected=expected,
            now=now,
            initial_source=(ledger_snapshot, ledger, control),
        )
    if action == "scope-preview":
        assert publication is not None
        preview = collect_private_spot_scope_preview(
            refs,
            client=client,
            expected=expected,
            observed_at=now,
            source_sha=os.environ["GITHUB_SHA"],
            initial_source=(ledger_snapshot, ledger, control),
        )
        publish_private_spot_scope_preview(
            preview,
            token=publication[0],
            source_run_id=publication[1],
            source_sha=os.environ["GITHUB_SHA"],
        )
        return {
            "status": "private_scope_published",
            "stage": "private_scope_publication",
            "ledger_unchanged": True,
            "no_order": True,
            "execution_authority_granted": False,
            "report_write_performed": True,
        }
    if action == "audit":
        return audit_ledger(refs, client=client, expected=expected, now=now)
    if action == "prospective-rebase-apply":
        return _apply_prospective_rebase(
            refs, client=client, expected=expected, now=now, fixed_now=fixed_now, runtime_target=target
        )
    if action == "rebase-apply":
        return _apply_approved_rebase(refs, client=client, expected=expected, now=now, fixed_now=fixed_now)
    evidence = (
        collect_prospective_opening(client, ledger=ledger, now=now, expected=expected)
        if action == "rebase-proposal"
        else _collect_evidence(client, ledger=ledger, now=now, expected=expected)
    )
    decision_now = now if fixed_now else datetime.now(timezone.utc)
    if evidence["utc_date"] != decision_now.date().isoformat():
        raise MigrationBlocked("utc_day_changed_during_evidence")
    if action == "rebase-proposal":
        proposal = build_rebase_proposal(ledger=ledger, evidence=evidence, observed_at=decision_now)
        after_snapshot, after_ledger, after_control = _read_source(refs, **source_options)
        if (
            digest(ledger) != digest(after_ledger)
            or digest(control) != digest(after_control)
            or _timestamp(ledger_snapshot.update_time) != _timestamp(after_snapshot.update_time)
        ):
            raise MigrationBlocked("proposal_ledger_changed_during_read")
        run_id = proposal_run_id_from_env()
        apply_control = (
            {**control, "state": "RECONCILE_ONLY"}
            if isinstance(control, Mapping) and control.get("state") == "ACTIVE_LKG"
            else control
        )
        proposal.update(
            proposal_run_id=run_id,
            archive_document=prospective_archive_document(run_id),
            source_sha=os.environ["GITHUB_SHA"],
            ledger_update_time=_timestamp(ledger_snapshot.update_time),
            ledger_sha256=digest(ledger),
            control_sha256=digest(apply_control),
        )
        encrypted = encrypt_rebase_proposal(
            proposal, certificate=os.environ.get("PROPOSAL_RECIPIENT_CERTIFICATE", "")
        )
        REBASE_PROPOSAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        REBASE_PROPOSAL_PATH.write_bytes(encrypted)
        return {
            "status": "encrypted_proposal_ready",
            "stage": "accounting_rebase_proposal",
            "executable_candidate": False,
            "ledger_unchanged": True,
            "no_order": True,
            "write_performed": False,
            "execution_authority_granted": False,
            "proposal_run_id": run_id,
        }
    if action == "preview":
        candidate = build_candidate(
            ledger=ledger,
            ledger_update_time=ledger_snapshot.update_time,
            control=control,
            evidence=evidence,
            source_sha=os.environ["GITHUB_SHA"],
            observed_at=decision_now,
        )
        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_PATH.write_text(json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8")
        return candidate["public_preview"]

    candidate = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    validate_candidate(candidate, expected_digest=expected_digest, now=decision_now)
    if (
        candidate.get("source_sha") != os.environ["GITHUB_SHA"]
        or candidate.get("account_scope_sha256") != evidence["account_scope_sha256"]
        or candidate.get("broker_snapshot_sha256") != evidence["broker_snapshot_sha256"]
        or candidate.get("activity_sha256") != evidence["activity_sha256"]
    ):
        raise MigrationBlocked("migration_evidence_changed")
    from google.cloud import firestore

    @firestore.transactional
    def apply(transaction):
        compare_and_apply(
            transaction,
            **refs,
            candidate=candidate,
            balance_snapshot=evidence["balance_snapshot"],
        )

    try:
        apply(get_firestore_client().transaction(max_attempts=1))
    except MigrationAtomicPrecondition:
        raise
    except Exception:
        raise MigrationApplyUncertain("migration_apply_outcome_uncertain") from None
    try:
        _verify_applied(refs, candidate=candidate, balance_snapshot=evidence["balance_snapshot"])
    except MigrationApplyUncertain:
        raise
    except Exception:
        raise MigrationApplyUncertain("migration_readback_mismatch") from None
    return {
        "status": "applied",
        "candidate_sha256": candidate["candidate_sha256"],
        "no_order": True,
        "execution_authority_granted": False,
    }


def main(argv=None) -> int:
    parser = ArgumentParser(description="Preview or apply the bounded Binance daily-accounting migration")
    parser.add_argument(
        "action",
        choices=(
            "inspect",
            "quiesce",
            "earn-forward-diagnose",
            "audit",
            "preview",
            "scope-preview",
            "cash-flow-preview",
            "rebase-proposal",
            "rebase-apply",
            "prospective-rebase-apply",
            "release-stale-owner",
            "apply",
        ),
    )
    parser.add_argument("--expected-digest", default="")
    args = parser.parse_args(argv)
    if args.action != "apply" and args.expected_digest:
        parser.error("only apply accepts an expected digest")
    if args.action == "apply" and not re.fullmatch(r"[0-9a-f]{64}", args.expected_digest):
        parser.error("apply requires the exact preview candidate digest")
    try:
        result = run(args.action, expected_digest=args.expected_digest)
    except MigrationApplyUncertain as exc:
        print(
            json.dumps(
                {
                    "status": "uncertain",
                    "stage": (
                        "private_scope_publication"
                        if args.action == "scope-preview"
                        else "stale_owner_release"
                        if args.action == "release-stale-owner"
                        else "accounting_migration_apply"
                    ),
                    "reason_code": str(exc),
                    "no_retry": True,
                    "no_order": True,
                }
            )
        )
        return 2
    except MigrationBlocked as exc:
        # These are fixed internal guard codes, never raw broker exceptions.
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "stage": "accounting_migration",
                    "reason_code": str(exc),
                    "no_order": True,
                }
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "stage": "accounting_migration",
                    "reason_code": "migration_blocked",
                    "no_order": True,
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
