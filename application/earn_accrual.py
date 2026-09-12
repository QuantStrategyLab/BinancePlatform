"""Prospective Earn observations and pure conservation checks; no ledger writes.

Counters are broker-reported observations, not proof of absolute principal or a
permission to trade. Callers must separately verify all non-interest flows.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from quant_platform_kit.common.broker_reconciliation import calculate_broker_observation_sha256 as digest


def _amount(value, *, signed=False):
    if not isinstance(value, str) or not 0 < len(value) <= 80:
        raise ValueError('earn_checkpoint_invalid')
    try:
        result = Decimal(value)
        if (not result.is_finite() or (not signed and result < 0)
                or abs(result) > Decimal('1e30') or result.as_tuple().exponent < -30):
            raise ValueError('earn_checkpoint_invalid')
        return result
    except InvalidOperation:
        raise ValueError('earn_checkpoint_invalid') from None


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('earn_checkpoint_time_invalid') from None


def collect_earn_checkpoint(client, *, assets, observed_at, expected_account_scope_sha256=None):
    """One Spot read and one complete Flexible position page; no historical reset."""
    try:
        assets = tuple(sorted(set(assets)))
        if not assets or len(assets) > 32 or any(not re.fullmatch(r'[A-Z0-9]{1,20}', a) for a in assets):
            raise ValueError('scope')
        if observed_at.tzinfo is None:
            raise ValueError('time')
        account = client.get_account()
        uid = account.get('uid')
        if not isinstance(uid, (str, int)) or isinstance(uid, bool) or not str(uid):
            raise ValueError('identity')
        scope = digest({'account_uid': str(uid)})
        if expected_account_scope_sha256 is not None and scope != expected_account_scope_sha256:
            raise ValueError('identity')
        rows = account['balances']
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError('spot')
        spot = {}
        for row in rows:
            asset = row['asset']
            if not re.fullmatch(r'[A-Z0-9]{1,20}', asset) or asset in spot:
                raise ValueError('spot')
            free, locked = _amount(row['free']), _amount(row['locked'])
            if locked:
                raise ValueError('locked')
            spot[asset] = (free, locked)
        response = client.get_simple_earn_flexible_product_position(current=1, size=100)
        earn_rows, total = response.get('rows'), response.get('total')
        if isinstance(total, str) and total.isascii() and total.isdecimal() and len(total) < 4:
            total = int(total)
        if not isinstance(earn_rows, list) or type(total) is not int or total != len(earn_rows) or total >= 100:
            raise ValueError('page')
        products = {a: {} for a in assets}
        seen = set()
        for row in earn_rows:
            asset, product = row['asset'], row['productId']
            if not isinstance(product, str) or not product or product in seen:
                raise ValueError('product')
            seen.add(product)
            if asset not in products:
                continue  # Existing approved scope is not expanded by this observation.
            amount, counter = _amount(row['totalAmount']), _amount(row['cumulativeRealTimeRewards'])
            if _amount(row['collateralAmount']) or type(row['autoSubscribe']) is not bool or row['canRedeem'] is not True:
                raise ValueError('availability')
            products[asset][product] = {
                'total': str(amount), 'realtime_rewards': str(counter),
                'auto_subscribe': row['autoSubscribe'], 'can_redeem': True,
            }
        observations = {}
        with localcontext() as context:
            context.prec = 100
            for asset in assets:
                free, locked = spot[asset]
                quantity = free + locked + sum((_amount(p['total']) for p in products[asset].values()), Decimal(0))
                observations[asset] = {'spot_free': str(free), 'spot_locked': str(locked),
                                       'products': products[asset], 'quantity': str(quantity)}
        return {'account_scope_sha256': scope, 'observed_at': observed_at.astimezone(timezone.utc).isoformat(),
                'assets': observations, 'execution_authority_granted': False}
    except Exception:
        raise ValueError('earn_checkpoint_unavailable') from None


def _validate(checkpoint):
    if (not isinstance(checkpoint, Mapping) or checkpoint.get('execution_authority_granted') is not False
            or not re.fullmatch(r'[0-9a-f]{64}', str(checkpoint.get('account_scope_sha256', '')))):
        raise ValueError('earn_checkpoint_invalid')
    _time(checkpoint.get('observed_at'))
    assets = checkpoint.get('assets')
    if not isinstance(assets, Mapping) or not 0 < len(assets) <= 32:
        raise ValueError('earn_checkpoint_invalid')
    for asset, row in assets.items():
        if not re.fullmatch(r'[A-Z0-9]{1,20}', asset) or not isinstance(row, Mapping):
            raise ValueError('earn_checkpoint_invalid')
        products = row.get('products')
        if not isinstance(products, Mapping) or len(products) >= 100:
            raise ValueError('earn_checkpoint_invalid')
        total = _amount(row.get('spot_free')) + _amount(row.get('spot_locked'))
        for product, position in products.items():
            if (not isinstance(product, str) or not product or not isinstance(position, Mapping)
                    or type(position.get('auto_subscribe')) is not bool or position.get('can_redeem') is not True):
                raise ValueError('earn_checkpoint_invalid')
            total += _amount(position.get('total'))
            _amount(position.get('realtime_rewards'))
        if _amount(row.get('quantity')) != total or _amount(row.get('spot_locked')):
            raise ValueError('earn_checkpoint_invalid')


def compare_earn_checkpoints(previous, current, *, verified_net_changes):
    """Check a continuous product lifecycle; daily reward summaries are not added.

    verified_net_changes must name every asset, including explicit zeroes. Its
    provenance is the caller's responsibility; this result never grants recovery.
    """
    with localcontext() as context:
        context.prec = 100
        _validate(previous)
        _validate(current)
        if (previous['account_scope_sha256'] != current['account_scope_sha256']
                or set(previous['assets']) != set(current['assets'])
                or not _time(previous['observed_at']) < _time(current['observed_at'])
                or not isinstance(verified_net_changes, Mapping)
                or set(verified_net_changes) != set(current['assets'])):
            raise ValueError('earn_checkpoint_scope_changed')
        accrual = {}
        for asset, old in previous['assets'].items():
            new = current['assets'][asset]
            if set(old['products']) != set(new['products']):
                raise ValueError('earn_product_lifecycle_unverified')
            reward = Decimal(0)
            for product, before in old['products'].items():
                after = new['products'][product]
                if before['auto_subscribe'] != after['auto_subscribe']:
                    raise ValueError('earn_product_lifecycle_unverified')
                delta = _amount(after['realtime_rewards']) - _amount(before['realtime_rewards'])
                if delta < 0:
                    raise ValueError('earn_counter_reset')
                reward += delta
            observed = _amount(new['quantity']) - _amount(old['quantity'])
            if observed != reward + _amount(verified_net_changes[asset], signed=True):
                raise ValueError('earn_quantity_change_unexplained')
            accrual[asset] = format(reward, 'f')
        return {'quantities_conserve': True, 'broker_reported_accrual': accrual,
                'absolute_principal_proven': False, 'execution_authority_granted': False}
