"""Historical continuity material validation and conservation proofs."""
from __future__ import annotations

from binance_platform.application._rebased_shared import (
    Decimal,
    EarnCheckpointUnavailable,
    InvalidOperation,
    Mapping,
    Sequence,
    _add_reward_quantity,
    _amount,
    _checkpoint_assets_equal,
    _checkpoint_nontime_metadata_equal,
    _cursor_evolution_allowed,
    _daily_loss_fields_unchanged,
    _exact_decimal_sum,
    _facade_digest,
    _historical_spot_digest,
    _nets_evolution_allowed,
    _utc,
    _valid_run,
    _validate_frozen_target,
    collect_earn_checkpoint,
    collect_read_only_reconciliation_observations,
    compare_earn_checkpoints,
    datetime,
    diagnose_chunked_balance_flows,
    prospective_migration_binding,
    timedelta,
    timezone,
)

def validate_historical_continuity_material(
    ledger: Mapping[str, object],
    archive: Mapping[str, object],
) -> dict[str, object]:
    """Validate immutable opening archive; current ledger is verification target only."""
    if not isinstance(ledger, Mapping) or not isinstance(archive, Mapping):
        raise ValueError("historical_continuity_archive_invalid")
    binding = prospective_migration_binding(archive)
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
        or _facade_digest(approved) != binding["approval_sha256"]
        or approved.get("historical_difference_unresolved") is not True
        or approved.get("ledger_sha256") != _facade_digest(old_ledger)
        or approved.get("control_sha256") != _facade_digest(archived_control)
        or archive.get("archive_document") != binding["archive_document"]
        or archive.get("approved_proposal_run_id") != binding["proposal_run_id"]
        or archive.get("approved_proposal_sha256") != binding["approval_sha256"]
        or archive.get("historical_difference_unresolved") is not True
        or _facade_digest(archive) != binding["archive_sha256"]
        or dict(marker) != expected_marker
        or ledger.get("order_submission", {}).get("state") not in {"RESERVED", "TERMINAL"}
    ):
        raise ValueError("historical_continuity_archive_invalid")
    opening_checkpoint = proposed.get("earn_accrual_checkpoint")
    opening_snapshot = proposed.get("last_balance_snapshot")
    if not isinstance(opening_checkpoint, Mapping) or not isinstance(opening_snapshot, Mapping):
        raise ValueError("historical_continuity_archive_invalid")
    # Quantity-bearing ledger surfaces must still match the opening unless history explains them.
    # The earn checkpoint inside the current ledger is never a continuity bridge by itself.
    return {
        "opening_at": _utc(marker["opening_balance_observed_at"]),
        "opening_checkpoint": opening_checkpoint,
        "opening_snapshot": opening_snapshot,
        "opening_quantities": {
            asset.upper(): _amount(value) for asset, value in opening_snapshot.items()
        },
        "managed_assets": tuple(opening_checkpoint["assets"]),
        "current_ledger_sha256": _facade_digest(ledger),
        "archive_sha256": binding["archive_sha256"],
        "archived_control": archived_control,
    }


def _validate_continuity_inputs(
    *,
    runtime_target: object,
    ledger: Mapping[str, object],
    archive: Mapping[str, object],
    source_run: Mapping[str, object],
    migration_run: Mapping[str, object],
    now: datetime | None,
    clock,
) -> dict[str, object]:
    """Validate continuity inputs, timelines, and extract approved opening products."""
    _validate_frozen_target(runtime_target)
    material = validate_historical_continuity_material(ledger, archive)
    binding = prospective_migration_binding(archive)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    opening_at = material["opening_at"]
    if not opening_at < observed_at:
        raise ValueError("historical_continuity_window_invalid")
    if not _valid_run(source_run):
        raise ValueError("historical_continuity_source_run_invalid")
    if not _valid_run(migration_run) or (
        migration_run["id"] != binding["run_id"]
        or migration_run["head_sha"] != binding["head_sha"]
    ):
        raise ValueError("historical_continuity_migration_run_invalid")
    if _facade_digest(archive) != binding["archive_sha256"]:
        raise ValueError("historical_continuity_migration_run_invalid")

    # Current ledger earn checkpoint cannot substitute for archive-bound history.
    current_checkpoint = ledger.get("earn_accrual_checkpoint")
    current_snapshot = ledger.get("last_balance_snapshot")
    if (
        not isinstance(current_checkpoint, Mapping)
        or not isinstance(current_snapshot, Mapping)
    ):
        raise ValueError("historical_continuity_conservation_unverified")

    final_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if not observed_at < final_at <= observed_at + timedelta(minutes=2):
        raise ValueError("historical_continuity_observation_window_invalid")

    try:
        current_checkpoint_at = _utc(current_checkpoint.get("observed_at"))
        opening_checkpoint_at = _utc(material["opening_checkpoint"].get("observed_at"))
    except ValueError:
        raise ValueError("historical_continuity_observation_timeline_invalid") from None
    if not (
        opening_at <= opening_checkpoint_at <= current_checkpoint_at <= final_at
        and opening_at <= current_checkpoint_at
    ):
        raise ValueError("historical_continuity_observation_timeline_invalid")

    approved_reward_products = frozenset(
        (asset, product_id)
        for asset, row in material["opening_checkpoint"].get("assets", {}).items()
        if isinstance(row, Mapping)
        for product_id in (row.get("products") or {})
        if isinstance(product_id, str) and product_id
    )
    opening_reward_product_totals: dict[tuple[str, str], str] = {}
    for asset, row in material["opening_checkpoint"].get("assets", {}).items():
        if not isinstance(row, Mapping):
            continue
        products = row.get("products") or {}
        if not isinstance(products, Mapping):
            continue
        for product_id, product in products.items():
            if not isinstance(product_id, str) or not product_id or not isinstance(product, Mapping):
                continue
            total = product.get("total")
            if isinstance(total, str) and total:
                opening_reward_product_totals[(asset, product_id)] = total

    return {
        "material": material,
        "opening_at": opening_at,
        "final_at": final_at,
        "current_checkpoint": current_checkpoint,
        "current_snapshot": current_snapshot,
        "approved_reward_products": approved_reward_products,
        "opening_reward_product_totals": opening_reward_product_totals,
    }


def _collect_and_validate_continuity_history(
    client: object,
    *,
    opening_at: datetime,
    final_at: datetime,
    managed_assets: Sequence[str],
    approved_reward_products: frozenset[tuple[str, str]],
    opening_reward_product_totals: Mapping[tuple[str, str], str],
) -> tuple[dict[str, object], Mapping[str, object], int, int, int]:
    """Read and validate chunked balance history across the continuity window."""
    history = diagnose_chunked_balance_flows(
        client,
        start=opening_at,
        end=final_at,
        now=final_at,
        managed_reward_assets=managed_assets,
        approved_reward_products=approved_reward_products,
        opening_reward_product_totals=opening_reward_product_totals,
    )
    if history.get("history_complete_for_requested_surfaces") is not True:
        reason = history.get("reason_code")
        if reason == "balance_history_duplicate_event":
            raise ValueError("historical_continuity_duplicate_event")
        if reason == "balance_history_unapproved_reward_product":
            raise ValueError("historical_continuity_unapproved_reward_product")
        raise ValueError("historical_continuity_history_incomplete")
    counts = history.get("history_counts")
    if not isinstance(counts, Mapping):
        raise ValueError("historical_continuity_history_incomplete")
    lifecycle_surfaces = {"earn_rewards", "earn_subscriptions", "earn_redemptions"}
    unsupported = [
        name for name, value in counts.items()
        if name not in lifecycle_surfaces and value not in (0, None)
    ]
    if unsupported:
        raise ValueError("historical_continuity_unsupported_activity")
    for surface in ("earn_rewards", "earn_subscriptions", "earn_redemptions"):
        if counts.get(surface) not in (0, None) and type(counts.get(surface)) is not int:
            raise ValueError("historical_continuity_history_incomplete")
    return (
        history,
        counts,
        int(counts.get("earn_rewards") or 0),
        int(counts.get("earn_subscriptions") or 0),
        int(counts.get("earn_redemptions") or 0),
    )


def _read_continuity_broker_state(
    client: object,
    *,
    symbols: Sequence[str],
    ledger: Mapping[str, object],
    opening_at: datetime,
    final_at: datetime,
    managed: Sequence[str],
    expected_scope: object,
) -> tuple[object, dict[str, object], int]:
    """Double-read account quantities and collect observations and Earn checkpoint."""
    try:
        account = client.get_account()
        observations = collect_read_only_reconciliation_observations(
            client,
            strategy_symbols=symbols,
            local_execution_ledger=ledger,
            now=final_at,
            lookback=final_at - opening_at,
            account_snapshot=account,
        )
        after = client.get_account()
    except Exception:
        raise ValueError("historical_continuity_broker_read_failed") from None
    if (
        _facade_digest(observations.account_scope) != expected_scope
        or _facade_digest({"account_uid": str(after.get("uid") or "")}) != expected_scope
    ):
        raise ValueError("historical_continuity_account_identity_mismatch")
    if observations.open_orders:
        raise ValueError("historical_continuity_open_orders_present")
    if observations.recent_executions:
        raise ValueError("historical_continuity_unsupported_activity")

    first_spot = _historical_spot_digest(account, managed)
    second_spot = _historical_spot_digest(after, managed)
    if first_spot["digest"] != second_spot["digest"]:
        raise ValueError("historical_continuity_quantity_changed_during_read")
    observed_count = second_spot["observed_non_managed_asset_count"]

    try:
        broker_checkpoint = collect_earn_checkpoint(
            client,
            assets=managed,
            observed_at=final_at,
            expected_account_scope_sha256=expected_scope,
        )
    except EarnCheckpointUnavailable:
        raise ValueError("historical_continuity_checkpoint_unavailable") from None
    if broker_checkpoint["account_scope_sha256"] != expected_scope:
        raise ValueError("historical_continuity_account_identity_mismatch")
    try:
        if _utc(broker_checkpoint.get("observed_at")) != final_at:
            raise ValueError("historical_continuity_observation_timeline_invalid")
    except ValueError as exc:
        if str(exc) == "historical_continuity_observation_timeline_invalid":
            raise
        raise ValueError("historical_continuity_observation_timeline_invalid") from None
    for asset, row in broker_checkpoint["assets"].items():
        spot = second_spot["balances"].get(asset)
        if spot is None or spot != (_amount(row["spot_free"]), _amount(row["spot_locked"])):
            raise ValueError("historical_continuity_quantity_changed_during_read")
    return observations, broker_checkpoint, observed_count


def _product_fact_sum(
    facts: Sequence[object],
    *,
    asset: str,
    product_id: str | None = None,
    kind: str | None = None,
) -> Decimal:
    """Sum decimal fact amounts filtered by asset and optional product_id/kind."""
    amounts = [
        _amount(fact.get("amount"))
        for fact in facts
        if (
            isinstance(fact, Mapping)
            and fact.get("asset") == asset
            and (product_id is None or fact.get("product_id") == product_id)
            and (kind is None or fact.get("kind") == kind)
        )
    ]
    return _exact_decimal_sum(amounts)


def _verify_continuity_lifecycle_conservation(
    *,
    history: Mapping[str, object],
    opening_checkpoint: Mapping[str, object],
    current_checkpoint: Mapping[str, object],
    broker_checkpoint: Mapping[str, object],
    opening_quantities: Mapping[str, object],
    broker_quantities: Mapping[str, Decimal],
    ledger_snapshot_quantities: Mapping[str, Decimal],
    managed: Sequence[str],
    earn_rewards: int,
) -> None:
    """Verify Earn rewards and product subscription/redemption conservation."""
    totals = history.get("reward_quantity_totals")
    if earn_rewards > 0 and not isinstance(totals, Mapping):
        raise ValueError("rewards")
    bonus_by_asset: dict[str, Decimal] = {}
    realtime_by_asset: dict[str, Decimal] = {}
    expected_quantities: dict[str, Decimal] = {}
    for asset, opening_qty in opening_quantities.items():
        if earn_rewards == 0:
            bonus = Decimal(0)
            realtime = Decimal(0)
        else:
            row = totals.get(asset)
            if not isinstance(row, Mapping):
                raise ValueError("rewards")
            bonus = _amount(row.get("BONUS"))
            realtime = _amount(row.get("REALTIME"))
        bonus_by_asset[asset] = bonus
        realtime_by_asset[asset] = realtime
        expected_quantities[asset] = _add_reward_quantity(
            _add_reward_quantity(_amount(opening_qty), bonus), realtime
        )
    if broker_quantities != expected_quantities:
        raise ValueError("rewards")
    if ledger_snapshot_quantities != expected_quantities:
        raise ValueError("snapshot")

    opening_products = {
        (asset, product_id)
        for asset, row in opening_checkpoint.get("assets", {}).items()
        if isinstance(row, Mapping)
        for product_id in (row.get("products") or {})
    }
    current_products = {
        (asset, product_id)
        for asset, row in broker_checkpoint.get("assets", {}).items()
        if isinstance(row, Mapping)
        for product_id in (row.get("products") or {})
    }
    added_products = current_products - opening_products
    removed_products = opening_products - current_products
    subscription_facts = history.get("_private_lifecycle_subscription_facts") or []
    redemption_facts = history.get("_private_lifecycle_redemption_facts") or []
    reward_facts = history.get("_private_lifecycle_reward_facts") or []
    if (
        not isinstance(subscription_facts, list)
        or not isinstance(redemption_facts, list)
        or not isinstance(reward_facts, list)
    ):
        raise ValueError("lifecycle")
    fact_products: set[tuple[str, str]] = set()
    for fact in (*subscription_facts, *redemption_facts, *reward_facts):
        if not isinstance(fact, Mapping):
            raise ValueError("lifecycle")
        asset = fact.get("asset")
        product_id = fact.get("product_id")
        if (
            not isinstance(asset, str)
            or not asset
            or not isinstance(product_id, str)
            or not product_id
        ):
            raise ValueError("lifecycle")
        fact_products.add((asset, product_id))

    # Product universe is opening U ending U all lifecycle facts.
    for asset, product_id in opening_products | current_products | fact_products:
        opening_total = Decimal(0)
        if (asset, product_id) in opening_products:
            opening_total = _amount(
                opening_checkpoint["assets"][asset]["products"][product_id]["total"]
            )
        sub_total = _product_fact_sum(subscription_facts, asset=asset, product_id=product_id)
        redeem_total = _product_fact_sum(redemption_facts, asset=asset, product_id=product_id)
        realtime_total = _product_fact_sum(
            reward_facts, asset=asset, product_id=product_id, kind="REALTIME"
        )
        simulated = _add_reward_quantity(
            _add_reward_quantity(opening_total, sub_total), realtime_total
        ) - redeem_total
        if simulated < 0:
            raise ValueError("lifecycle")
        if (asset, product_id) in current_products:
            ending_total = _amount(
                broker_checkpoint["assets"][asset]["products"][product_id]["total"]
            )
            if simulated != ending_total:
                raise ValueError("lifecycle")
        elif simulated != 0:
            raise ValueError("lifecycle")

    for asset, product_id in added_products:
        sub_amount = _product_fact_sum(subscription_facts, asset=asset, product_id=product_id)
        if not any(
            isinstance(fact, Mapping)
            and fact.get("asset") == asset
            and fact.get("product_id") == product_id
            for fact in subscription_facts
        ):
            raise ValueError("lifecycle")
        product = broker_checkpoint["assets"][asset]["products"][product_id]
        if _amount(product["total"]) != _add_reward_quantity(
            sub_amount, _amount(product["realtime_rewards"])
        ):
            raise ValueError("lifecycle")
    for asset, product_id in removed_products:
        if not any(
            isinstance(fact, Mapping)
            and fact.get("asset") == asset
            and fact.get("product_id") == product_id
            for fact in redemption_facts
        ):
            raise ValueError("lifecycle")
        redeem_amount = _product_fact_sum(redemption_facts, asset=asset, product_id=product_id)
        opening_product = opening_checkpoint["assets"][asset]["products"][product_id]
        if redeem_amount != _amount(opening_product["total"]):
            raise ValueError("lifecycle")

    lifecycle_mutated = bool(added_products or removed_products) or any(
        isinstance(fact, Mapping) and fact.get("asset") in managed
        for fact in (*subscription_facts, *redemption_facts)
    )
    for asset in managed:
        opening_row = opening_checkpoint["assets"][asset]
        current_row = broker_checkpoint["assets"][asset]
        asset_subs = _product_fact_sum(subscription_facts, asset=asset)
        asset_redeems = _product_fact_sum(redemption_facts, asset=asset)
        spot_delta = _amount(current_row["spot_free"]) - _amount(opening_row["spot_free"])
        if spot_delta != bonus_by_asset[asset] + asset_redeems - asset_subs:
            raise ValueError("bonus_spot")
        shared = set(opening_row["products"]) & set(current_row["products"])
        counter_delta = Decimal(0)
        for product_id in shared:
            before = opening_row["products"][product_id]
            after = current_row["products"][product_id]
            if before.get("auto_subscribe") != after.get("auto_subscribe"):
                raise ValueError("lifecycle")
            delta = _amount(after["realtime_rewards"]) - _amount(before["realtime_rewards"])
            if delta < 0:
                raise ValueError("lifecycle")
            counter_delta = _add_reward_quantity(counter_delta, delta)
            product_subs = _product_fact_sum(subscription_facts, asset=asset, product_id=product_id)
            product_redeems = _product_fact_sum(redemption_facts, asset=asset, product_id=product_id)
            principal_before = _amount(before["total"]) - _amount(before["realtime_rewards"])
            principal_after = _amount(after["total"]) - _amount(after["realtime_rewards"])
            if principal_after != principal_before + product_subs - product_redeems:
                raise ValueError("lifecycle")
        for product_id, after in current_row["products"].items():
            if product_id not in shared:
                counter_delta = _add_reward_quantity(counter_delta, _amount(after["realtime_rewards"]))
        if counter_delta != realtime_by_asset[asset]:
            raise ValueError("realtime_earn")
    if not lifecycle_mutated:
        verified = {asset: format(bonus_by_asset[asset], "f") for asset in managed}
        compare_earn_checkpoints(opening_checkpoint, current_checkpoint, verified_net_changes=verified)
        compare_earn_checkpoints(opening_checkpoint, broker_checkpoint, verified_net_changes=verified)


def _verify_continuity_conservation(
    *,
    ledger: Mapping[str, object],
    archive: Mapping[str, object],
    material: Mapping[str, object],
    history: Mapping[str, object],
    current_checkpoint: Mapping[str, object],
    current_snapshot: Mapping[str, object],
    broker_checkpoint: Mapping[str, object],
    final_at: datetime,
    earn_rewards: int,
    earn_subscriptions: int,
    earn_redemptions: int,
) -> None:
    """Verify ledger and broker conservation from approved opening through final_at."""
    managed = material["managed_assets"]
    proposed = archive["approved_proposal"]["proposed_fields"]
    opening_checkpoint = material["opening_checkpoint"]
    opening_snapshot = material["opening_snapshot"]
    opening_principal = proposed.get("daily_external_principal_usdt", 0)
    broker_quantities = {
        asset: _amount(row["quantity"])
        for asset, row in broker_checkpoint["assets"].items()
    }
    ledger_snapshot_quantities = {
        str(asset).upper(): _amount(value)
        for asset, value in current_snapshot.items()
    }
    try:
        if not _daily_loss_fields_unchanged(proposed, ledger):
            raise ValueError("daily_loss")
        if _amount(ledger.get("daily_external_principal_usdt", opening_principal)) != _amount(opening_principal):
            raise ValueError("principal")
        if not _cursor_evolution_allowed(
            proposed.get("external_cash_flow_cursor"),
            ledger.get("external_cash_flow_cursor"),
            final_at=final_at,
        ):
            raise ValueError("cursor")
        if not _nets_evolution_allowed(
            proposed.get("earn_accounted_net_changes"),
            ledger.get("earn_accounted_net_changes"),
            managed,
        ):
            raise ValueError("nets")
        if set(current_snapshot) != set(opening_snapshot):
            raise ValueError("scope")
        if set(current_checkpoint.get("assets", ())) != set(opening_checkpoint["assets"]):
            raise ValueError("scope")
        if set(broker_checkpoint["assets"]) != set(opening_checkpoint["assets"]):
            raise ValueError("scope")
        if ledger_snapshot_quantities != broker_quantities:
            raise ValueError("snapshot")
        if current_checkpoint.get("assets") != broker_checkpoint["assets"]:
            raise ValueError("checkpoint")
        if not _checkpoint_nontime_metadata_equal(opening_checkpoint, current_checkpoint):
            raise ValueError("checkpoint")
        if earn_rewards == 0 and earn_subscriptions == 0 and earn_redemptions == 0:
            if broker_quantities != material["opening_quantities"]:
                raise ValueError("unchanged")
            if ledger_snapshot_quantities != material["opening_quantities"]:
                raise ValueError("snapshot")
            if not _checkpoint_assets_equal(opening_checkpoint, current_checkpoint):
                raise ValueError("unchanged")
            if not _checkpoint_assets_equal(opening_checkpoint, broker_checkpoint):
                raise ValueError("unchanged")
            compare_earn_checkpoints(
                opening_checkpoint,
                broker_checkpoint,
                verified_net_changes={asset: "0" for asset in managed},
            )
        else:
            _verify_continuity_lifecycle_conservation(
                history=history,
                opening_checkpoint=opening_checkpoint,
                current_checkpoint=current_checkpoint,
                broker_checkpoint=broker_checkpoint,
                opening_quantities=material["opening_quantities"],
                broker_quantities=broker_quantities,
                ledger_snapshot_quantities=ledger_snapshot_quantities,
                managed=managed,
                earn_rewards=earn_rewards,
            )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ValueError("historical_continuity_conservation_unverified") from None


def _build_continuity_proof(
    *,
    history: Mapping[str, object],
    counts: Mapping[str, object],
    managed: Sequence[str],
    observed_count: int,
    earn_rewards: int,
    earn_subscriptions: int,
) -> dict[str, object]:
    """Build the redacted historical continuity proof dictionary."""
    proof: dict[str, object] = {
        "history_complete_for_requested_surfaces": True,
        "history_counts": dict(counts),
        "chunk_count": history.get("chunk_count"),
        "quantity_double_read_match": True,
        "managed_asset_count": len(managed),
        "non_managed_spot_policy": "observe_only",
        "observed_non_managed_asset_count": observed_count,
        "earn_rewards_observed": earn_rewards,
        "earn_reward_conservation_verified": earn_rewards > 0 or earn_subscriptions > 0,
        "current_ledger_bound": True,
        "opening_checkpoint_used_as_bridge": False,
    }
    classification_counts = history.get("reward_product_classification_counts")
    if isinstance(classification_counts, Mapping):
        allowed_keys = {
            "opening_approved",
            "lifecycle_subscription_proven",
            "out_of_scope_asset",
            "lifecycle_evidence_incomplete",
            "product_mapping_ambiguous",
            "unexplained_managed_impact",
        }
        proof["reward_product_classification_counts"] = {
            str(key): int(value)
            for key, value in classification_counts.items()
            if key in allowed_keys and type(value) is int
        }
    return proof

