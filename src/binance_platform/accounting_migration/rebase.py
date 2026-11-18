"""Rebase proposal generation, encryption, and atomic rebase transactions."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from binance_platform.accounting_migration.candidate_migration import (
    collect_evidence,
    read_source,
)
from binance_platform.accounting_migration.common import (
    _ACCOUNTING_FIELDS,
    _PROSPECTIVE_PROPOSED_FIELD_KEYS,
    NEW_BASIS,
    REBASE_ARCHIVE_DOCUMENT,
    TTL,
    MigrationApplyUncertain,
    MigrationAtomicPrecondition,
    MigrationBlocked,
    MigrationHooks,
    _finite,
    _now_utc,
    _proposal_run_id,
    _timestamp,
    _validate_control,
    _validate_safe_order_state,
    prospective_archive_document,
    resolve_hooks,
)
from binance_platform.application import earn_accrual as _earn_accrual
from binance_platform.application import rebased_recovery as _rebased_recovery
from binance_platform.application import reconciliation_recovery as _reconciliation_recovery
from binance_platform.application.broker_reconciliation import (
    BinanceReconciliationReadError,
    collect_spot_usdt_external_cash_flows,
)


def collect_prospective_opening(
    client,
    *,
    ledger,
    expected,
    now,
    clock=None,
    collect_cash_flows=collect_spot_usdt_external_cash_flows,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    """Capture a current opening proposal; historical mismatches remain archived.

    No historical reward sum becomes a starting principal or new-period profit.
    The external cursor fingerprints prior events solely to prevent replay.
    """
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    clock = clock or (lambda: _now_utc(active_hooks))
    assets = tuple(sorted(ledger.get("last_balance_snapshot", {})))
    if not assets or now.tzinfo is None:
        raise MigrationBlocked("prospective_opening_scope_missing")

    def read_orders():
        orders = client.get_open_orders()
        if not isinstance(orders, list) or orders:
            raise MigrationBlocked("prospective_opening_orders_unsettled")

    stage = "initial_orders"
    try:
        read_orders()
        first = _earn_accrual.collect_earn_checkpoint(
            client,
            assets=assets,
            observed_at=now,
            expected_account_scope_sha256=expected["account_scope_sha256"],
        )
        stage = "opening_cash_cursor"
        cash = collect_cash_flows(client, now=now, cursor=None)
        if not isinstance(cash.get("cursor"), dict):
            raise MigrationBlocked("prospective_opening_cursor_unavailable")
        stage = "prices"
        prices = {}
        for asset, row in first["assets"].items():
            if asset != "USDT" and Decimal(row["quantity"]) > 0:
                price = _finite(client.get_avg_price(symbol=f"{asset}USDT")["price"])
                if price <= 0:
                    raise MigrationBlocked("price_snapshot_incomplete")
                prices[f"{asset}USDT"] = price
        read_orders()
        second = _earn_accrual.collect_earn_checkpoint(
            client,
            assets=assets,
            observed_at=now,
            expected_account_scope_sha256=expected["account_scope_sha256"],
        )
        ended_at = clock()
        if not now < ended_at <= now + timedelta(minutes=2):
            raise MigrationBlocked("prospective_opening_observation_expired")
        second["observed_at"] = ended_at.isoformat()
        try:
            _earn_accrual.compare_earn_checkpoints(
                first, second, verified_net_changes={asset: "0" for asset in assets}
            )
        except ValueError:
            raise MigrationBlocked("prospective_opening_changed_during_read") from None
        stage = "closing_cash_cursor"
        closing_cash = collect_cash_flows(client, now=ended_at, cursor=None)
        if (
            not isinstance(closing_cash.get("cursor"), dict)
            or cash["cursor"].get("records") != closing_cash["cursor"].get("records")
        ):
            raise MigrationBlocked("prospective_opening_flows_changed_during_read")
        # Only the sampling interval is checked for trades; pre-start history is archived.
        stage = "sampling_executions"
        observations = active_hooks.collect_observations_fn(
            client,
            strategy_symbols=tuple(f"{a}USDT" for a in assets if a != "USDT"),
            local_execution_ledger=ledger,
            now=ended_at,
            lookback=ended_at - now,
        )
        if (
            digest_fn(observations.account_scope) != expected["account_scope_sha256"]
            or observations.open_orders
            or observations.recent_executions
        ):
            raise MigrationBlocked("prospective_opening_activity_during_read")
        second["observed_at"] = ended_at.isoformat()
        return {
            "opening_mode": "prospective",
            "current_observation_complete": True,
            "account_scope_sha256": second["account_scope_sha256"],
            "balance_snapshot": {
                asset: float(Decimal(row["quantity"]))
                for asset, row in second["assets"].items()
            },
            "prices": prices,
            "open_order_count": 0,
            "history_complete": False,
            "history_counts": None,
            "recent_execution_count": None,
            "observation_started_at": now.isoformat(),
            "observation_completed_at": ended_at.isoformat(),
            "earn_accrual_checkpoint": second,
            "external_cash_flow_cursor": closing_cash["cursor"],
            "utc_date": ended_at.astimezone(timezone.utc).date().isoformat(),
        }
    except MigrationBlocked:
        raise
    except _earn_accrual.EarnCheckpointUnavailable as error:
        raise MigrationBlocked(str(error)) from None
    except (BinanceReconciliationReadError, ValueError, TypeError, KeyError):
        raise MigrationBlocked(f"prospective_opening_{stage}_unavailable") from None


def build_rebase_proposal(*, ledger, evidence, observed_at) -> dict[str, object]:
    """Informational new-start preview, deliberately not an apply candidate."""
    _validate_safe_order_state(ledger)
    prospective = evidence.get("opening_mode") == "prospective"
    if prospective:
        checkpoint = evidence.get("earn_accrual_checkpoint", {})
        if (
            evidence.get("current_observation_complete") is not True
            or checkpoint.get("account_scope_sha256")
            != evidence.get("account_scope_sha256")
            or not isinstance(evidence.get("external_cash_flow_cursor"), Mapping)
        ):
            raise MigrationBlocked("rebase_proposal_current_state_unsettled")
    if (
        type(evidence.get("open_order_count")) is not int
        or evidence["open_order_count"] != 0
        or (
            not prospective
            and (
                evidence.get("history_complete") is not True
                or type(evidence.get("recent_execution_count")) is not int
                or evidence["recent_execution_count"] != 0
            )
        )
    ):
        raise MigrationBlocked("rebase_proposal_current_state_unsettled")
    balances = evidence.get("balance_snapshot")
    prices = evidence.get("prices")
    if (
        not isinstance(balances, Mapping)
        or not {"USDT", "BTC", "BNB"}.issubset(balances)
        or not isinstance(prices, Mapping)
    ):
        raise MigrationBlocked("rebase_proposal_balances_incomplete")
    assets = []
    for asset, raw_quantity in sorted(balances.items()):
        if not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", asset):
            raise MigrationBlocked("rebase_proposal_asset_invalid")
        quantity = _finite(raw_quantity)
        price = 1.0 if asset == "USDT" else _finite(prices.get(f"{asset}USDT", 0.0))
        if quantity > 0 and price <= 0:
            raise MigrationBlocked("price_snapshot_incomplete")
        assets.append(
            {
                "asset": asset,
                "quantity": quantity,
                "price_usdt": price,
                "value_usdt": _finite(quantity * price),
            }
        )
    total = _finite(sum(row["value_usdt"] for row in assets))
    trend = _finite(
        sum(row["value_usdt"] for row in assets if row["asset"] not in {"USDT", "BTC", "BNB"})
    )
    if total <= 0 or observed_at.tzinfo is None:
        raise MigrationBlocked("rebase_proposal_valuation_invalid")
    proposed = {
        "daily_trend_pnl_basis": NEW_BASIS,
        "daily_trend_cash_flow_usdt": 0.0,
        "daily_trend_net_invested_usdt": 0.0,
        "daily_trend_third_fee_usdt": 0.0,
        "daily_trend_risk_base_usdt": trend,
        "daily_equity_base": total,
        "daily_trend_equity_base": trend,
        "last_balance_snapshot": {row["asset"]: row["quantity"] for row in assets},
        "last_reset_date": observed_at.astimezone(timezone.utc).date().isoformat(),
    }
    if prospective:
        proposed["earn_accrual_checkpoint"] = copy.deepcopy(
            evidence["earn_accrual_checkpoint"]
        )
        proposed["earn_accounted_net_changes"] = {
            a: "0" for a in evidence["earn_accrual_checkpoint"]["assets"]
        }
        proposed["external_cash_flow_cursor"] = copy.deepcopy(
            evidence["external_cash_flow_cursor"]
        )
    return {
        "opening_mode": "prospective" if prospective else "legacy_preview",
        "automatic_accounting_ready": False,
        "recovery_ready": False,
        "observation_started_at": evidence.get("observation_started_at"),
        "observation_completed_at": evidence.get("observation_completed_at"),
        "status": "awaiting_operator_decision",
        "executable_candidate": False,
        "historical_difference_unresolved": True,
        "complete_balance_reconciliation": False,
        "observed_at": observed_at.isoformat(),
        "assets": assets,
        "valuation_scope": "managed_assets_spot_plus_flexible_earn",
        "valuation_price_source": "binance_get_avg_price_estimate",
        "old_fields": {key: copy.deepcopy(ledger[key]) for key in proposed if key in ledger},
        "old_missing_fields": sorted(set(proposed) - set(ledger)),
        "proposed_fields": proposed,
        "preserved_circuit_breaker_latch": ledger.get("is_circuit_broken"),
        "preserve_all_other_ledger_fields": True,
        "requires_full_old_ledger_archive_before_apply": True,
        "requires_fresh_preflight_and_separate_apply_approval": True,
        "pre_start_income_classification": "unreconstructed_history",
        "current_day_activity_counts": copy.deepcopy(evidence.get("history_counts")),
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }


def encrypt_rebase_proposal(proposal, *, certificate) -> bytes:
    """Use standard CMS encryption; plaintext stays in memory on the runner."""
    if not isinstance(certificate, str) or not 100 <= len(certificate) <= 12000:
        raise MigrationBlocked("proposal_recipient_certificate_invalid")
    try:
        with tempfile.TemporaryDirectory() as directory:
            cert_path = Path(directory) / "recipient.pem"
            cert_path.write_text(certificate, encoding="utf-8")
            return subprocess.run(
                [
                    "openssl",
                    "cms",
                    "-encrypt",
                    "-aes-256-cbc",
                    "-binary",
                    "-outform",
                    "DER",
                    str(cert_path),
                ],
                input=json.dumps(proposal, sort_keys=True, allow_nan=False).encode(),
                capture_output=True,
                check=True,
                timeout=20,
            ).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        raise MigrationBlocked("proposal_encryption_failed") from None


def validate_prospective_approval(
    approved, *, hooks: MigrationHooks | None = None
) -> None:
    """Accept only a complete proposal binding; never a fixed historical digest."""
    active_hooks = resolve_hooks(hooks)
    if not isinstance(approved, Mapping):
        raise MigrationBlocked("prospective_approval_mismatch")
    # Reject the retired fixed digest so an old secret cannot apply as today's baseline.
    if active_hooks.digest_fn(approved) == active_hooks.approved_prospective_sha256:
        raise MigrationBlocked("prospective_approval_mismatch")
    try:
        run_id = _proposal_run_id(approved.get("proposal_run_id"))
    except MigrationBlocked as exc:
        raise MigrationBlocked("prospective_approval_mismatch") from exc
    source_sha = approved.get("source_sha")
    ledger_sha = approved.get("ledger_sha256")
    control_sha = approved.get("control_sha256")
    ledger_update_time = approved.get("ledger_update_time")
    proposed = approved.get("proposed_fields")
    if (
        approved.get("historical_difference_unresolved") is not True
        or approved.get("opening_mode") != "prospective"
        or approved.get("executable_candidate") is not False
        or not isinstance(source_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
        or not isinstance(ledger_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", ledger_sha)
        or not isinstance(control_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", control_sha)
        or not isinstance(ledger_update_time, str)
        or not ledger_update_time
        or not isinstance(proposed, Mapping)
        or set(proposed) != _PROSPECTIVE_PROPOSED_FIELD_KEYS
        or prospective_archive_document(run_id) != approved.get("archive_document")
    ):
        raise MigrationBlocked("prospective_approval_mismatch")
    checkpoint = proposed.get("earn_accrual_checkpoint")
    nets = proposed.get("earn_accounted_net_changes")
    cursor = proposed.get("external_cash_flow_cursor")
    if (
        not isinstance(checkpoint, Mapping)
        or not isinstance(checkpoint.get("assets"), Mapping)
        or not checkpoint["assets"]
        or not isinstance(nets, Mapping)
        or set(nets) != set(checkpoint["assets"])
        or any(value != "0" for value in nets.values())
        or not isinstance(cursor, Mapping)
    ):
        raise MigrationBlocked("prospective_approval_mismatch")


def parse_prospective_approval(
    raw: str, *, hooks: MigrationHooks | None = None
) -> dict[str, object]:
    try:
        if not 0 < len(raw) <= 48000:
            raise ValueError
        approved = json.loads(raw)
        validate_prospective_approval(approved, hooks=hooks)
        return approved
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationBlocked("prospective_approval_unavailable") from None


def verified_control_account_scope(control, *, expected, runtime_target) -> str:
    """Return account_scope proven by the live control package; never guess fields."""
    if not isinstance(control, Mapping) or control.get("state") != "RECONCILE_ONLY":
        raise MigrationBlocked("prospective_control_identity_unverified")
    source = control.get("source")
    if not isinstance(source, Mapping):
        raise MigrationBlocked("prospective_control_identity_unverified")
    expected_scope = expected.get("account_scope_sha256")
    if not isinstance(expected_scope, str) or len(expected_scope) != 64:
        raise MigrationBlocked("prospective_control_identity_unverified")

    has_confirmation = "confirmation" in control
    has_transition = "transition_plan" in control
    try:
        if has_confirmation or has_transition:
            if not (has_confirmation and has_transition):
                raise ValueError("incomplete quiesced prior")
            candidate = _reconciliation_recovery.verify_quiesced_prior_activation(
                control, runtime_target=runtime_target, expected=expected
            )
        else:
            kind = source.get("kind")
            if kind == "prospective_rebase":
                candidate = _rebased_recovery.validate_prospective_rebase_source(
                    control,
                    runtime_target=runtime_target,
                    legacy_expected=expected,
                    require_fresh=False,
                )
            elif kind == "post_rebase":
                candidate = _rebased_recovery.validate_post_rebase_source(
                    control,
                    runtime_target=runtime_target,
                    legacy_expected=expected,
                    require_fresh=False,
                )
            elif kind == "historical_continuity":
                candidate = _rebased_recovery.validate_historical_continuity_source(
                    control,
                    runtime_target=runtime_target,
                    legacy_expected=expected,
                    require_fresh=False,
                )
            elif (
                "original_evidence" in source
                and "reconciled_evidence" in source
                and "candidate" in control
            ):
                # Re-validate package consistency at its own observation time; apply already
                # binds control_sha256 and a separate 24h opening window.
                observed = source["reconciled_evidence"].get("observed_at")
                if isinstance(observed, datetime):
                    reference = observed.astimezone(timezone.utc)
                elif isinstance(observed, str):
                    reference = datetime.fromisoformat(observed.replace("Z", "+00:00"))
                else:
                    raise ValueError("enrollment observation missing")
                candidate = _reconciliation_recovery.validate_source(
                    control,
                    runtime_target=runtime_target,
                    expected=expected,
                    now=reference,
                )
            else:
                raise ValueError("unsupported control identity surface")
        scope = candidate.account_scope_sha256
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MigrationBlocked("prospective_control_identity_unverified") from None
    if not isinstance(scope, str) or scope != expected_scope:
        raise MigrationBlocked("prospective_control_identity_unverified")
    return scope


def prospective_preflight(
    *,
    refs,
    client,
    expected,
    approved,
    now,
    runtime_target,
    clock=None,
    hooks: MigrationHooks | None = None,
) -> datetime:
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    clock = clock or (lambda: _now_utc(active_hooks))
    validate_prospective_approval(approved, hooks=active_hooks)
    read_fn = active_hooks.read_source_fn or read_source
    snapshot, ledger, control = read_fn(refs)
    if (
        digest_fn(ledger) != approved["ledger_sha256"]
        or digest_fn(control) != approved["control_sha256"]
        or _timestamp(snapshot.update_time) != approved["ledger_update_time"]
        or _finite(ledger.get("daily_external_principal_usdt", 0)) != 0
    ):
        raise MigrationBlocked("prospective_source_changed")
    _validate_control(control)
    _validate_safe_order_state(ledger)
    verify_scope_fn = (
        active_hooks.verified_control_account_scope_fn or verified_control_account_scope
    )
    control_scope = verify_scope_fn(
        control, expected=expected, runtime_target=runtime_target
    )
    fields = approved["proposed_fields"]
    previous = fields["earn_accrual_checkpoint"]
    opened = _earn_accrual._time(previous["observed_at"])
    if (
        not opened < now <= opened + timedelta(hours=24)
        or opened.date() != now.date()
        or previous["account_scope_sha256"] != expected["account_scope_sha256"]
        or previous["account_scope_sha256"] != control_scope
    ):
        raise MigrationBlocked("prospective_opening_identity_or_time_changed")
    opening_fn = active_hooks.collect_prospective_opening_fn or collect_prospective_opening
    fresh = opening_fn(
        client, ledger=ledger, expected=expected, now=now, clock=clock
    )
    try:
        end = _earn_accrual._time(fresh["observation_completed_at"])
        _earn_accrual.compare_earn_checkpoints(
            previous,
            fresh["earn_accrual_checkpoint"],
            verified_net_changes=fields["earn_accounted_net_changes"],
        )
        if (
            set(fields["earn_accounted_net_changes"]) != set(previous["assets"])
            or any(v != "0" for v in fields["earn_accounted_net_changes"].values())
            or fresh["external_cash_flow_cursor"]["records"]
            != fields["external_cash_flow_cursor"]["records"]
        ):
            raise ValueError
        observations = active_hooks.collect_observations_fn(
            client,
            strategy_symbols=tuple(f"{a}USDT" for a in previous["assets"] if a != "USDT"),
            local_execution_ledger=ledger,
            now=end,
            lookback=end - opened,
        )
        if (
            digest_fn(observations.account_scope) != previous["account_scope_sha256"]
            or observations.open_orders
            or observations.recent_executions
        ):
            raise ValueError
        decision = clock()
        if not end <= decision <= now + TTL or decision.date() != opened.date():
            raise ValueError
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationBlocked("prospective_continuity_unverified") from None
    return decision


def prospective_transaction(
    transaction,
    *,
    refs,
    archive_ref,
    approved,
    now,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    validate_prospective_approval(approved, hooks=active_hooks)
    run_id = _proposal_run_id(approved["proposal_run_id"])
    archive_document = prospective_archive_document(run_id)
    approval_digest = digest_fn(approved)
    owner = refs["owner_ref"].get(transaction=transaction, retry=None)
    current = refs["ledger_ref"].get(transaction=transaction, retry=None)
    control = refs["control_ref"].get(transaction=transaction, retry=None)
    archive = archive_ref.get(transaction=transaction, retry=None)
    if (
        owner.exists
        or archive.exists
        or not current.exists
        or not control.exists
        or digest_fn(current.to_dict()) != approved["ledger_sha256"]
        or digest_fn(control.to_dict()) != approved["control_sha256"]
        or _timestamp(current.update_time) != approved["ledger_update_time"]
    ):
        raise MigrationAtomicPrecondition("prospective_atomic_precondition_changed")
    ledger, recovery = current.to_dict(), control.to_dict()
    _validate_safe_order_state(ledger)
    _validate_control(recovery)
    proposed = copy.deepcopy(approved["proposed_fields"])
    marker = {
        "archive_document": archive_document,
        "started_at": now.isoformat(),
        "opening_balance_observed_at": proposed["earn_accrual_checkpoint"]["observed_at"],
        "historical_difference_unresolved": True,
        "approved_proposal_run_id": run_id,
        "approved_proposal_sha256": approval_digest,
    }
    patch = {**proposed, "accounting_rebase": marker}
    new_digest = digest_fn({**ledger, **patch})
    backup = {
        "ledger": copy.deepcopy(ledger),
        "recovery_control": copy.deepcopy(recovery),
        "ledger_update_time": _timestamp(current.update_time),
        **marker,
        "approved_proposal": copy.deepcopy(approved),
        "new_ledger_sha256": new_digest,
        "valuation_price_source": "binance_get_avg_price_estimate",
    }
    transaction.create(archive_ref, backup)
    transaction.update(refs["ledger_ref"], patch)
    return {
        "new_ledger_sha256": new_digest,
        "archive_sha256": digest_fn(backup),
        "archive_document": archive_document,
        "approved_proposal_run_id": run_id,
        "approved_proposal_sha256": approval_digest,
    }


def apply_prospective_rebase(
    refs,
    *,
    client,
    expected,
    approved,
    now,
    fixed_now,
    runtime_target,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    from google.cloud import firestore

    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    validate_prospective_approval(approved, hooks=active_hooks)
    archive_document = prospective_archive_document(approved["proposal_run_id"])
    if approved.get("archive_document") != archive_document:
        raise MigrationBlocked("prospective_approval_mismatch")
    archive_ref = refs["ledger_ref"].parent.document(archive_document)
    if archive_ref.get(retry=None).exists:
        raise MigrationBlocked("prospective_archive_already_exists")
    preflight_fn = active_hooks.prospective_preflight_fn or prospective_preflight
    decision = preflight_fn(
        refs=refs,
        client=client,
        expected=expected,
        approved=approved,
        now=now,
        runtime_target=runtime_target,
        clock=(lambda: now + timedelta(seconds=1)) if fixed_now else None,
    )

    @firestore.transactional
    def apply(transaction):
        return prospective_transaction(
            transaction,
            refs=refs,
            archive_ref=archive_ref,
            approved=approved,
            now=decision,
            hooks=active_hooks,
        )

    try:
        written = apply(active_hooks.firestore_client_fn().transaction(max_attempts=1))
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationApplyUncertain("prospective_write_uncertain") from None
    try:
        read_fn = active_hooks.read_source_fn or read_source
        after_snapshot, after_ledger, after_control = read_fn(refs)
        archive = archive_ref.get(retry=None)
        if (
            digest_fn(after_ledger) != written["new_ledger_sha256"]
            or digest_fn(after_control) != approved["control_sha256"]
            or not archive.exists
            or digest_fn(archive.to_dict()) != written["archive_sha256"]
        ):
            raise ValueError
        updated_at = _timestamp(after_snapshot.update_time)
    except Exception:
        raise MigrationApplyUncertain("prospective_readback_uncertain") from None
    return {
        "status": "rebased",
        "stage": "prospective_accounting_apply",
        "approved_proposal_run_id": written["approved_proposal_run_id"],
        "archive_document": written["archive_document"],
        "approved_proposal_sha256": written["approved_proposal_sha256"],
        "ledger_update_time": updated_at,
        "new_ledger_sha256": written["new_ledger_sha256"],
        "archive_sha256": written["archive_sha256"],
        "old_ledger_archived": True,
        "historical_difference_unresolved": True,
        "control_unchanged": True,
        "no_order": True,
        "write_performed": True,
        "execution_authority_granted": False,
    }


def approved_rebase_fields(
    *,
    ledger,
    control,
    evidence,
    now,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    if (
        digest_fn(ledger) != active_hooks.approved_rebase_ledger_sha256
        or digest_fn(control) != active_hooks.approved_rebase_control_sha256
        or digest_fn(evidence.get("balance_snapshot"))
        != active_hooks.approved_rebase_balances_sha256
    ):
        raise MigrationBlocked("approved_rebase_source_changed")
    _validate_control(control)
    if (
        evidence.get("account_scope_sha256")
        != control["source"]["original_evidence"]["account_scope_sha256"]
    ):
        raise MigrationBlocked("account_scope_unverified")
    counts = evidence.get("history_counts")
    if (
        not isinstance(counts, Mapping)
        or not counts
        or any(
            type(value) is not int
            or value < 0
            or (name != "earn_rewards" and value != 0)
            for name, value in counts.items()
        )
    ):
        raise MigrationBlocked("approved_rebase_activity_changed")
    proposal_fn = active_hooks.build_rebase_proposal_fn or build_rebase_proposal
    return proposal_fn(ledger=ledger, evidence=evidence, observed_at=now)[
        "proposed_fields"
    ]


def rebase_transaction(
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
    hooks: MigrationHooks | None = None,
) -> dict[str, str]:
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    owner = refs["owner_ref"].get(transaction=transaction, retry=None)
    current = refs["ledger_ref"].get(transaction=transaction, retry=None)
    recovery = refs["control_ref"].get(transaction=transaction, retry=None)
    archive = archive_ref.get(transaction=transaction, retry=None)
    if (
        owner.exists
        or archive.exists
        or not current.exists
        or not recovery.exists
        or digest_fn(current.to_dict()) != active_hooks.approved_rebase_ledger_sha256
        or digest_fn(ledger) != active_hooks.approved_rebase_ledger_sha256
        or digest_fn(recovery.to_dict()) != active_hooks.approved_rebase_control_sha256
        or digest_fn(control) != active_hooks.approved_rebase_control_sha256
        or _timestamp(current.update_time) != _timestamp(ledger_update_time)
    ):
        raise MigrationAtomicPrecondition("approved_rebase_atomic_precondition_changed")
    _validate_safe_order_state(current.to_dict())
    _validate_control(control)
    if (
        set(proposed_fields) != _ACCOUNTING_FIELDS
        or digest_fn(proposed_fields.get("last_balance_snapshot"))
        != active_hooks.approved_rebase_balances_sha256
    ):
        raise MigrationAtomicPrecondition("approved_rebase_patch_invalid")
    marker = {
        "archive_document": REBASE_ARCHIVE_DOCUMENT,
        "started_at": started_at.isoformat(),
        "opening_balance_observed_at": observed_at.isoformat(),
        "historical_difference_unresolved": True,
        "approved_proposal_run_id": "34601984051",
    }
    patch = {**proposed_fields, "accounting_rebase": marker}
    new_ledger_sha = digest_fn({**ledger, **patch})
    backup = {
        "ledger": copy.deepcopy(ledger),
        "recovery_control": copy.deepcopy(control),
        "ledger_update_time": _timestamp(ledger_update_time),
        **marker,
        "new_ledger_sha256": new_ledger_sha,
        "valuation_price_source": "binance_get_avg_price_estimate",
    }
    # Both writes commit together; create prevents any archive replacement.
    transaction.create(archive_ref, backup)
    transaction.update(refs["ledger_ref"], patch)
    return {"new_ledger_sha256": new_ledger_sha, "archive_sha256": digest_fn(backup)}


def apply_approved_rebase(
    refs,
    *,
    client,
    expected,
    configured_symbols: tuple[str, ...],
    now,
    fixed_now,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    from google.cloud import firestore

    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    read_fn = active_hooks.read_source_fn or read_source
    snapshot, ledger, control = read_fn(refs)
    archive_ref = refs["ledger_ref"].parent.document(REBASE_ARCHIVE_DOCUMENT)
    if (
        digest_fn(ledger) != active_hooks.approved_rebase_ledger_sha256
        or digest_fn(control) != active_hooks.approved_rebase_control_sha256
        or archive_ref.get(retry=None).exists
    ):
        raise MigrationBlocked("approved_rebase_source_changed")
    evidence_fn = active_hooks.collect_evidence_fn or (
        lambda c, *, ledger, now, expected, require_prices=False: collect_evidence(
            c,
            ledger=ledger,
            now=now,
            expected=expected,
            configured_symbols=configured_symbols,
            require_prices=require_prices,
            hooks=active_hooks,
        )
    )
    evidence = evidence_fn(
        client, ledger=ledger, now=now, expected=expected, require_prices=True
    )
    decision_now = now if fixed_now else _now_utc(active_hooks)
    if (
        decision_now - now > TTL
        or evidence["utc_date"] != decision_now.date().isoformat()
    ):
        raise MigrationBlocked("approved_rebase_evidence_stale")
    proposed = approved_rebase_fields(
        ledger=ledger,
        control=control,
        evidence=evidence,
        now=decision_now,
        hooks=active_hooks,
    )

    @firestore.transactional
    def apply(transaction):
        return rebase_transaction(
            transaction,
            refs=refs,
            archive_ref=archive_ref,
            ledger=ledger,
            control=control,
            ledger_update_time=snapshot.update_time,
            proposed_fields=proposed,
            observed_at=now,
            started_at=decision_now,
            hooks=active_hooks,
        )

    try:
        written = apply(active_hooks.firestore_client_fn().transaction(max_attempts=1))
    except MigrationBlocked:
        raise
    except Exception:
        raise MigrationApplyUncertain("approved_rebase_write_uncertain") from None
    try:
        after_snapshot, after_ledger, after_control = read_fn(refs)
        archive = archive_ref.get(retry=None)
        if (
            digest_fn(after_ledger) != written["new_ledger_sha256"]
            or digest_fn(after_control) != active_hooks.approved_rebase_control_sha256
            or not archive.exists
            or digest_fn(archive.to_dict()) != written["archive_sha256"]
        ):
            raise ValueError("readback mismatch")
        updated_at = _timestamp(after_snapshot.update_time)
    except Exception:
        raise MigrationApplyUncertain("approved_rebase_readback_uncertain") from None
    return {
        "status": "rebased",
        "stage": "accounting_rebase_apply",
        "archive_document": REBASE_ARCHIVE_DOCUMENT,
        "ledger_update_time": updated_at,
        "historical_difference_unresolved": True,
        "old_ledger_archived": True,
        "control_unchanged": True,
        "no_order": True,
        "write_performed": True,
        "execution_authority_granted": False,
    }
