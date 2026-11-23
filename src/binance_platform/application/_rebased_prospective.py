"""Prospective rebase diagnosis and source validation."""
from __future__ import annotations

from binance_platform.application._rebased_shared import (
    APPROVED_PROSPECTIVE_SHA256,
    BrokerReconciliationBaselineCandidate,
    BrokerReconciliationEvidence,
    Decimal,
    EarnCheckpointUnavailable,
    MAX_HISTORY,
    Mapping,
    PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID,
    PROSPECTIVE_ARCHIVE_DOCUMENT,
    PROSPECTIVE_ARCHIVE_SHA256,
    PROSPECTIVE_LEDGER_SHA256,
    PROSPECTIVE_MIGRATION_RUN_ID,
    PROSPECTIVE_MIGRATION_RUN_SHA,
    Sequence,
    _amount,
    _evidence,
    _facade_const,
    _facade_digest,
    _quantity,
    _utc,
    _valid_run,
    _validate_frozen_target,
    collect_bnb_dividend_quantity,
    collect_earn_checkpoint,
    collect_read_only_reconciliation_observations,
    collect_spot_usdt_external_cash_flows,
    compare_earn_checkpoints,
    datetime,
    evaluate_broker_reconciliation_baseline_enrollment,
    is_prospective_archive_document,
    prepare_forward_earn_state,
    prospective_archive_document,
    prospective_migration_binding,
    re,
    timedelta,
    timezone,
)

def validate_prospective_rebase_material(
    ledger: Mapping[str, object],
    archive: Mapping[str, object],
) -> dict[str, object]:
    """Validate a prospective opening archive without enrolling it for recovery.

    The completed 2026-09-12 migration stays bound to its exact digests. Newer
    one-shot applies bind dynamically through proposal_run_id + digest.
    """
    if not isinstance(ledger, Mapping) or not isinstance(archive, Mapping):
        raise ValueError("prospective_rebase_archive_invalid")
    archive_document = archive.get("archive_document")
    if archive_document == PROSPECTIVE_ARCHIVE_DOCUMENT:
        if _facade_digest(archive) != _facade_const('PROSPECTIVE_ARCHIVE_SHA256', PROSPECTIVE_ARCHIVE_SHA256):
            raise ValueError("prospective_rebase_archive_invalid")
        if _facade_digest(ledger) != _facade_const('PROSPECTIVE_LEDGER_SHA256', PROSPECTIVE_LEDGER_SHA256):
            raise ValueError("prospective_rebase_ledger_invalid")
        required_approval_digest = _facade_const('APPROVED_PROSPECTIVE_SHA256', APPROVED_PROSPECTIVE_SHA256)
        required_run_id = _facade_const('PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID', PROSPECTIVE_APPROVED_PROPOSAL_RUN_ID)
        required_ledger_digest = _facade_const('PROSPECTIVE_LEDGER_SHA256', PROSPECTIVE_LEDGER_SHA256)
    elif is_prospective_archive_document(archive_document):
        required_approval_digest = archive.get("approved_proposal_sha256")
        required_run_id = archive.get("approved_proposal_run_id")
        required_ledger_digest = archive.get("new_ledger_sha256")
        try:
            expected_archive = prospective_archive_document(required_run_id)
        except Exception:
            raise ValueError("prospective_rebase_archive_invalid") from None
        if (
            not isinstance(required_approval_digest, str)
            or len(required_approval_digest) != 64
            or expected_archive != archive_document
            or not isinstance(required_ledger_digest, str)
            or len(required_ledger_digest) != 64
            or _facade_digest(ledger) != required_ledger_digest
        ):
            raise ValueError("prospective_rebase_archive_invalid")
    else:
        raise ValueError("prospective_rebase_archive_invalid")

    old_ledger = archive.get("ledger")
    archived_control = archive.get("recovery_control")
    approved = archive.get("approved_proposal")
    proposed = approved.get("proposed_fields") if isinstance(approved, Mapping) else None
    marker = ledger.get("accounting_rebase")
    expected_marker = {
        key: archive.get(key)
        for key in (
            "archive_document",
            "started_at",
            "opening_balance_observed_at",
            "historical_difference_unresolved",
            "approved_proposal_run_id",
            "approved_proposal_sha256",
        )
    }
    if (
        not isinstance(old_ledger, Mapping)
        or not isinstance(archived_control, Mapping)
        or not isinstance(approved, Mapping)
        or not isinstance(proposed, Mapping)
        or not isinstance(marker, Mapping)
        or _facade_digest(approved) != required_approval_digest
        or approved.get("historical_difference_unresolved") is not True
        or approved.get("ledger_sha256") != _facade_digest(old_ledger)
        or approved.get("control_sha256") != _facade_digest(archived_control)
        or archive.get("approved_proposal_run_id") != required_run_id
        or archive.get("approved_proposal_sha256") != required_approval_digest
        or archive.get("historical_difference_unresolved") is not True
        or archive.get("new_ledger_sha256") != required_ledger_digest
        or archive.get("valuation_price_source") != "binance_get_avg_price_estimate"
    ):
        raise ValueError("prospective_rebase_archive_invalid")
    if archive_document != PROSPECTIVE_ARCHIVE_DOCUMENT and (
        approved.get("proposal_run_id") != required_run_id
        or approved.get("archive_document") != archive_document
    ):
        raise ValueError("prospective_rebase_archive_invalid")
    if (
        dict(marker) != expected_marker
        or marker.get("opening_balance_observed_at")
        != proposed.get("earn_accrual_checkpoint", {}).get("observed_at")
        or ledger != {**old_ledger, **proposed, "accounting_rebase": expected_marker}
        or ledger.get("order_submission", {}).get("state") not in {"RESERVED", "TERMINAL"}
    ):
        raise ValueError("prospective_rebase_ledger_invalid")
    raw_opening = ledger.get("last_balance_snapshot")
    if (
        not isinstance(raw_opening, Mapping)
        or not raw_opening
        or any(not isinstance(asset, str) or not asset for asset in raw_opening)
    ):
        raise ValueError("prospective_rebase_ledger_invalid")
    opening_quantities = {
        asset.upper(): _quantity(_amount(value)) for asset, value in raw_opening.items()
    }
    if len(opening_quantities) != len(raw_opening):
        raise ValueError("prospective_rebase_ledger_invalid")
    return {
        "opening_at": _utc(marker["opening_balance_observed_at"]),
        "opening_quantities": opening_quantities,
        "managed_assets": tuple(proposed["earn_accrual_checkpoint"]["assets"]),
        "archived_control": archived_control,
    }


def _same_cash_flow_slice(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    keys = (
        "new_deposit_principal_usdt",
        "new_confirmed_deposit_count",
        "new_deposit_completed_at",
        "new_unsupported_deposit_count",
        "new_or_changed_withdrawal_count",
    )
    return all(first.get(key) == second.get(key) for key in keys) and (
        first.get("cursor", {}).get("records") == second.get("cursor", {}).get("records")
    )


def _prospective_spot_snapshot(
    account: Mapping[str, object],
) -> tuple[str, dict[str, tuple[Decimal, Decimal]], str]:
    try:
        uid = account["uid"]
        rows = account["balances"]
        if (
            not isinstance(uid, (str, int))
            or isinstance(uid, bool)
            or not str(uid)
            or not isinstance(rows, list)
            or len(rows) > 5000
        ):
            raise ValueError
        spot = {}
        canonical = []
        for row in rows:
            asset = row["asset"]
            if (
                not isinstance(row, Mapping)
                or not isinstance(asset, str)
                or not 0 < len(asset) <= 128
                or not asset.isprintable()
                or any(char.isspace() for char in asset)
                or asset in spot
            ):
                raise ValueError
            free, locked = _amount(row["free"]), _amount(row["locked"])
            if locked:
                raise ValueError
            spot[asset] = (free, locked)
            canonical.append(
                {"asset": asset, "free": format(free, "f"), "locked": format(locked, "f")}
            )
    except (KeyError, TypeError, ValueError):
        raise ValueError("prospective_rebase_spot_unverified") from None
    return (
        _facade_digest({"account_uid": str(uid)}),
        spot,
        _facade_digest(sorted(canonical, key=lambda row: row["asset"])),
    )


def _collect_prospective_rebase_private(
    *,
    client: object,
    runtime_target: object,
    legacy_expected: Mapping[str, str],
    ledger: Mapping[str, object],
    archive: Mapping[str, object],
    symbols: Sequence[str],
    source_run: Mapping[str, object],
    migration_run: Mapping[str, object],
    now: datetime | None = None,
    clock=None,
) -> dict[str, object]:
    """Collect private forward proof and stable Spot evidence in memory."""
    _validate_frozen_target(runtime_target)
    material = validate_prospective_rebase_material(ledger, archive)
    binding = prospective_migration_binding(archive)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    opening_at = material["opening_at"]
    if not opening_at < observed_at or observed_at - opening_at > _facade_const('MAX_HISTORY', MAX_HISTORY):
        raise ValueError("prospective_rebase_history_window_invalid")
    if not _valid_run(source_run):
        raise ValueError("prospective_rebase_source_run_invalid")
    if not _valid_run(migration_run) or (
        migration_run["id"] != binding["run_id"]
        or migration_run["head_sha"] != binding["head_sha"]
    ):
        raise ValueError("prospective_rebase_migration_run_invalid")
    if _facade_digest(ledger) != binding["ledger_sha256"] or _facade_digest(archive) != binding["archive_sha256"]:
        raise ValueError("prospective_rebase_migration_run_invalid")

    assets = material["managed_assets"]
    expected_scope = legacy_expected.get("account_scope_sha256")
    try:
        first = collect_earn_checkpoint(
            client,
            assets=assets,
            observed_at=observed_at,
            expected_account_scope_sha256=expected_scope,
        )
    except EarnCheckpointUnavailable:
        raise ValueError("prospective_rebase_checkpoint_unavailable") from None
    try:
        first_flows = collect_spot_usdt_external_cash_flows(
            client,
            now=observed_at,
            cursor=ledger.get("external_cash_flow_cursor"),
        )
    except ValueError:
        raise ValueError("prospective_rebase_cash_flow_unverified") from None
    try:
        first_dividends = collect_bnb_dividend_quantity(
            client, start=opening_at, end=observed_at
        ) if "BNB" in assets else {"quantity": Decimal(0), "identities": ()}
    except ValueError:
        raise ValueError("prospective_rebase_dividend_unverified") from None

    final_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if not observed_at < final_at <= observed_at + timedelta(minutes=2):
        raise ValueError("prospective_rebase_observation_window_invalid")
    try:
        second = collect_earn_checkpoint(
            client,
            assets=assets,
            observed_at=final_at,
            expected_account_scope_sha256=expected_scope,
        )
        second_flows = collect_spot_usdt_external_cash_flows(
            client,
            now=final_at,
            cursor=ledger.get("external_cash_flow_cursor"),
        )
    except EarnCheckpointUnavailable:
        raise ValueError("prospective_rebase_checkpoint_unavailable") from None
    except ValueError:
        raise ValueError("prospective_rebase_cash_flow_unverified") from None
    try:
        second_dividends = collect_bnb_dividend_quantity(
            client, start=opening_at, end=final_at
        ) if "BNB" in assets else {"quantity": Decimal(0), "identities": ()}
    except ValueError:
        raise ValueError("prospective_rebase_dividend_unverified") from None
    if not _same_cash_flow_slice(first_flows, second_flows):
        raise ValueError("prospective_rebase_cash_flow_changed_during_read")
    if (
        first_dividends.get("identities") != second_dividends.get("identities")
        or first_dividends.get("quantity") != second_dividends.get("quantity")
    ):
        raise ValueError("prospective_rebase_dividend_changed_during_read")
    first_flows = {**first_flows, "bnb_dividend_quantity": format(first_dividends["quantity"], "f")}
    second_flows = {**second_flows, "bnb_dividend_quantity": format(second_dividends["quantity"], "f")}

    try:
        first_state = prepare_forward_earn_state(ledger, first, first_flows)
        second_state = prepare_forward_earn_state(ledger, second, second_flows)
        compare_earn_checkpoints(
            first,
            second,
            verified_net_changes={asset: "0" for asset in assets},
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("prospective_rebase_conservation_unverified") from None
    if (
        first_state.get("earn_accrual_checkpoint") != first
        or second_state.get("earn_accrual_checkpoint") != second
        or _facade_digest(ledger) != binding["ledger_sha256"]
    ):
        raise ValueError("prospective_rebase_conservation_unverified")

    try:
        final_account = client.get_account()
        final_scope, final_spot, final_spot_sha256 = _prospective_spot_snapshot(final_account)
        for asset, row in second["assets"].items():
            if asset not in final_spot or final_spot[asset] != (
                _amount(row["spot_free"]),
                _amount(row["spot_locked"]),
            ):
                raise ValueError("spot changed")
        observations = collect_read_only_reconciliation_observations(
            client,
            strategy_symbols=symbols,
            local_execution_ledger=ledger,
            now=final_at,
            lookback=final_at - opening_at,
            account_snapshot=final_account,
        )
        after_scope, _after_spot, after_spot_sha256 = _prospective_spot_snapshot(
            client.get_account()
        )
    except Exception:
        raise ValueError("prospective_rebase_spot_changed_during_read") from None
    if (
        final_scope != expected_scope
        or after_scope != expected_scope
        or _facade_digest(observations.account_scope) != expected_scope
    ):
        raise ValueError("prospective_rebase_account_identity_mismatch")
    if final_spot_sha256 != after_spot_sha256:
        raise ValueError("prospective_rebase_spot_changed_during_read")
    if observations.open_orders:
        raise ValueError("prospective_rebase_open_orders_present")
    if observations.recent_executions:
        raise ValueError("prospective_rebase_recent_executions_present")
    return {
        "observed_at": final_at,
        "observations": observations,
        "proof": {
            "forward_accounting_conserved": True,
            "checkpoint_samples": 2,
            "whole_spot_double_read_match": True,
            "spot_sha256": _facade_digest(observations.positions),
            "first_checkpoint_sha256": _facade_digest(first),
            "final_checkpoint_sha256": _facade_digest(second),
            "cash_flow_slice_sha256": _facade_digest(
                {
                    key: second_flows[key]
                    for key in (
                        "new_deposit_principal_usdt",
                        "new_confirmed_deposit_count",
                        "new_deposit_completed_at",
                        "new_unsupported_deposit_count",
                        "new_or_changed_withdrawal_count",
                        "cursor",
                    )
                }
            ),
        },
    }


def collect_prospective_rebase_diagnosis(**kwargs) -> dict[str, object]:
    """Return only a sanitized summary of the private forward proof."""
    _collect_prospective_rebase_private(**kwargs)
    return {
        "status": "diagnosed",
        "source_kind": "prospective_rebase",
        "historical_difference_unresolved": True,
        "forward_accounting_conserved": True,
        "checkpoint_samples": 2,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }


def collect_prospective_rebase_source(**kwargs) -> dict[str, object]:
    """Enroll the exact prospective opening after independent forward proof."""
    private = _collect_prospective_rebase_private(**kwargs)
    runtime_target = kwargs["runtime_target"]
    legacy_expected = kwargs["legacy_expected"]
    ledger = kwargs["ledger"]
    archive = kwargs["archive"]
    symbols = kwargs["symbols"]
    source_run = kwargs["source_run"]
    migration_run = kwargs["migration_run"]
    observed_at = private["observed_at"]
    observations = private["observations"]
    binding = prospective_migration_binding(archive)
    if (
        _facade_digest(ledger) != binding["ledger_sha256"]
        or _facade_digest(archive) != binding["archive_sha256"]
        or migration_run["id"] != binding["run_id"]
        or migration_run["head_sha"] != binding["head_sha"]
    ):
        raise ValueError("prospective_rebase_migration_run_invalid")
    reconciled = _evidence(
        runtime_target=runtime_target,
        account_scope_sha256=legacy_expected["account_scope_sha256"],
        observations=observations,
        observed_at=observed_at,
    )
    source = {
        "kind": "prospective_rebase",
        "run": dict(source_run),
        "migration_run": dict(migration_run),
        "archive_document": binding["archive_document"],
        "archive_sha256": binding["archive_sha256"],
        "new_ledger_sha256": binding["ledger_sha256"],
        "frozen_expected_sha256": _facade_digest(legacy_expected),
        "runtime_target_sha256": _facade_digest(runtime_target.to_dict()),
        "symbols_sha256": _facade_digest(list(symbols)),
        "historical_difference_unresolved": True,
        "reconciled_evidence": reconciled.to_dict(),
        "proof": private["proof"],
    }
    evaluation = evaluate_broker_reconciliation_baseline_enrollment(
        (reconciled,), source_receipts_sha256=_facade_digest(source), now=observed_at
    )
    if evaluation.candidate is None:
        raise ValueError("prospective_rebase_enrollment_blocked")
    return {"candidate": evaluation.candidate.to_dict(), "source": source}


def validate_prospective_rebase_source(
    package: Mapping[str, object],
    *,
    runtime_target: object,
    legacy_expected: Mapping[str, str],
    now: datetime | None = None,
    require_fresh: bool = True,
) -> BrokerReconciliationBaselineCandidate:
    """Strictly validate a stored prospective package without private values."""
    _validate_frozen_target(runtime_target)
    try:
        source = package["source"]
        proof = source["proof"]
        candidate = BrokerReconciliationBaselineCandidate.from_dict(package["candidate"])
        evidence = BrokerReconciliationEvidence.from_dict(source["reconciled_evidence"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("prospective_rebase_source_binding_mismatch") from None
    if not isinstance(source, Mapping) or not isinstance(proof, Mapping):
        raise ValueError("prospective_rebase_source_binding_mismatch")
    source_keys = {
        "kind", "run", "migration_run", "archive_document", "archive_sha256",
        "new_ledger_sha256", "frozen_expected_sha256", "runtime_target_sha256",
        "symbols_sha256", "historical_difference_unresolved", "reconciled_evidence", "proof",
    }
    proof_keys = {
        "forward_accounting_conserved", "checkpoint_samples",
        "whole_spot_double_read_match", "spot_sha256", "first_checkpoint_sha256",
        "final_checkpoint_sha256", "cash_flow_slice_sha256",
    }
    sha_values = (
        source.get("archive_sha256"), source.get("new_ledger_sha256"),
        source.get("frozen_expected_sha256"), source.get("runtime_target_sha256"),
        source.get("symbols_sha256"), proof.get("spot_sha256"),
        proof.get("first_checkpoint_sha256"), proof.get("final_checkpoint_sha256"),
        proof.get("cash_flow_slice_sha256"),
    )
    expected_digests = {
        "positions_sha256": evidence.positions_sha256,
        "cash_sha256": evidence.cash_sha256,
        "open_orders_sha256": evidence.open_orders_sha256,
        "recent_executions_sha256": evidence.recent_executions_sha256,
        "local_execution_ledger_sha256": evidence.local_execution_ledger_sha256,
    }
    if (
        set(source) != source_keys
        or set(proof) != proof_keys
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in sha_values
        )
        or source.get("kind") != "prospective_rebase"
        or not _valid_run(source.get("run"))
        or not _valid_run(source.get("migration_run"))
        or source.get("frozen_expected_sha256") != _facade_digest(legacy_expected)
        or source.get("runtime_target_sha256") != _facade_digest(runtime_target.to_dict())
        or source.get("historical_difference_unresolved") is not True
        or proof.get("forward_accounting_conserved") is not True
        or proof.get("checkpoint_samples") != 2
        or proof.get("whole_spot_double_read_match") is not True
        or proof.get("spot_sha256") != evidence.positions_sha256
        or candidate.source_receipts_sha256 != _facade_digest(source)
        or candidate.source_evidence_sha256 != (evidence.evidence_sha256,)
        or candidate.expected_digests != expected_digests
        or candidate.account_scope_sha256 != legacy_expected.get("account_scope_sha256")
        or candidate.local_execution_ledger_sha256 != source.get("new_ledger_sha256")
        or candidate.platform_id != runtime_target.platform_id
        or candidate.strategy_profile != runtime_target.strategy_profile
        or candidate.baseline_id != runtime_target.live_continuity.baseline_id
        or candidate.baseline_target_sha256
        != runtime_target.live_continuity.baseline_target_sha256
        or evidence.account_scope_sha256 != legacy_expected.get("account_scope_sha256")
        or evidence.platform_id != runtime_target.platform_id
        or evidence.strategy_profile != runtime_target.strategy_profile
        or evidence.baseline_id != runtime_target.live_continuity.baseline_id
        or evidence.baseline_target_sha256
        != runtime_target.live_continuity.baseline_target_sha256
        or evidence.runtime_target_sha256
        != runtime_target.live_continuity.baseline_target_sha256
        or evidence.positions_match is not True
        or evidence.cash_match is not True
        or evidence.open_orders_match is not True
        or evidence.recent_executions_match is not True
        or evidence.local_execution_ledger_match is not True
    ):
        raise ValueError("prospective_rebase_source_binding_mismatch")
    archive_document = source.get("archive_document")
    migration_run = source.get("migration_run")
    if archive_document == PROSPECTIVE_ARCHIVE_DOCUMENT:
        if (
            migration_run.get("id") != _facade_const('PROSPECTIVE_MIGRATION_RUN_ID', PROSPECTIVE_MIGRATION_RUN_ID)
            or migration_run.get("head_sha") != _facade_const('PROSPECTIVE_MIGRATION_RUN_SHA', PROSPECTIVE_MIGRATION_RUN_SHA)
            or source.get("archive_sha256") != _facade_const('PROSPECTIVE_ARCHIVE_SHA256', PROSPECTIVE_ARCHIVE_SHA256)
            or source.get("new_ledger_sha256") != _facade_const('PROSPECTIVE_LEDGER_SHA256', PROSPECTIVE_LEDGER_SHA256)
        ):
            raise ValueError("prospective_rebase_source_binding_mismatch")
    elif is_prospective_archive_document(archive_document):
        try:
            expected_document = prospective_archive_document(str(migration_run["id"]))
        except Exception as exc:
            raise ValueError("prospective_rebase_source_binding_mismatch") from exc
        if (
            expected_document != archive_document
            or not isinstance(migration_run.get("head_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", migration_run["head_sha"])
        ):
            raise ValueError("prospective_rebase_source_binding_mismatch")
    else:
        raise ValueError("prospective_rebase_source_binding_mismatch")
    if require_fresh:
        evaluated = evaluate_broker_reconciliation_baseline_enrollment(
            (evidence,), source_receipts_sha256=_facade_digest(source), now=now
        )
        if evaluated.candidate is None or evaluated.candidate.to_dict() != candidate.to_dict():
            raise ValueError("prospective_rebase_candidate_source_mismatch")
    return candidate

