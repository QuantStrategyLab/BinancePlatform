"""Pure validation and aggregation for the Binance homepage account facts.

This module deliberately knows only the two read-only wallet response shapes.
It does not read runtime state, load execution authority, write accounting state,
or calculate a quote-currency valuation.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from quant_platform_kit.common.broker_reconciliation import (
    calculate_broker_observation_sha256,
)


SCHEMA_VERSION = "binance_account_facts.v1"
SCOPE = "spot+flexible_earn"
SOURCE_BINDING_KIND = "binance_readonly_scope_revision"
UNCOVERED_SCOPES = ["funding", "margin", "futures", "locked_earn"]
PAGE_SIZE = 100
MAX_EARN_POSITIONS = 10_000
MAX_WALLET_ROWS = 32
WALLET_VALUATION_CURRENCY = "USDT"
WALLET_VALUATION_SOURCE = "GET /sapi/v1/asset/wallet/balance"
WALLET_VALUATION_SCOPE = "provider_returned_wallet_rows"
WALLET_VALUATION_FAILURES = frozenset({
    "wallet_read_failed",
    "wallet_response_invalid",
    "wallet_row_invalid",
    "wallet_duplicate_name",
    "wallet_inactive_nonzero",
    "wallet_balance_invalid",
})
PROVIDER_PRODUCT_TYPE_SOURCE = "GET /api/v3/account.accountType"
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_WALLET_DECIMAL = re.compile(r"(?:0|[1-9]\d{0,29})(?:\.\d{1,30})?\Z")
_HANGUL_FILLERS = frozenset("\u115f\u1160\u3164\uffa0")


def _valid_asset(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    for character in value:
        if character.isascii():
            if not ("A" <= character <= "Z" or "0" <= character <= "9"):
                return False
        elif character in _HANGUL_FILLERS or unicodedata.category(character)[0] not in {"L", "N"}:
            return False
    return True


class AccountFactsUnavailable(ValueError):
    """A fixed, provider-independent failure code safe for workflow logs."""


class ReadOnlyBinanceClient:
    """Expose exactly the two Binance wallet GET methods used by the reader."""

    __slots__ = ("__client",)

    def __init__(self, client: Any) -> None:
        self.__client = client

    def get_account(self) -> Any:
        return self.__client.get_account()

    def get_simple_earn_flexible_product_position(
        self, *, current: int, size: int
    ) -> Any:
        return self.__client.get_simple_earn_flexible_product_position(
            current=current, size=size
        )

    def get_wallet_valuation_usdt(self) -> Any:
        """Read quote-valued wallet rows using the locked SDK's signed SAPI path."""
        return self.__client._request_margin_api(
            "get",
            "asset/wallet/balance",
            signed=True,
            version=1,
            data={"quoteAsset": WALLET_VALUATION_CURRENCY, "needBalanceDetail": "false"},
            allow_redirects=False,
        )


def _fail(code: str) -> AccountFactsUnavailable:
    return AccountFactsUnavailable(f"binance_account_facts_{code}")


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _fail("timestamp_invalid")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parsed_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise _fail("payload_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _fail("payload_invalid") from None
    if parsed.tzinfo is None:
        raise _fail("payload_invalid")
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not 0 < len(value) <= 80:
        raise _fail("amount_invalid")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise _fail("amount_invalid") from None
    if not result.is_finite() or result < 0:
        raise _fail("amount_invalid")
    _decimal_text(result)
    return result


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite() or value < 0:
        raise _fail("amount_invalid")
    if value.is_zero():
        return "0"
    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    del sign
    whole_digits = max(1, len(digits) + exponent)
    fraction_digits = max(0, -exponent)
    if whole_digits > 30 or fraction_digits > 30:
        raise _fail("amount_invalid")
    try:
        normalized = Decimal((0, tuple(digits), exponent))
        return format(normalized, "f")
    except Exception:
        raise _fail("amount_invalid") from None


def _wallet_valuation_unavailable(reason_code: str) -> dict[str, Any]:
    if reason_code not in WALLET_VALUATION_FAILURES:
        reason_code = "wallet_response_invalid"
    return {
        "status": "unavailable",
        "amount": None,
        "currency": None,
        "source": WALLET_VALUATION_SOURCE,
        "scope": WALLET_VALUATION_SCOPE,
        "observed_at": None,
        "wallet_count": None,
        "reason_code": reason_code,
    }


def _wallet_valuation(response: Any, observed_at: datetime) -> dict[str, Any]:
    if not isinstance(response, list) or not 1 <= len(response) <= MAX_WALLET_ROWS:
        return _wallet_valuation_unavailable("wallet_response_invalid")
    names: set[str] = set()
    total = Decimal(0)
    try:
        with localcontext() as context:
            context.prec = 100
            for row in response:
                if not isinstance(row, Mapping) or set(row) != {"activate", "balance", "walletName"}:
                    return _wallet_valuation_unavailable("wallet_row_invalid")
                name, active, balance_text = row["walletName"], row["activate"], row["balance"]
                if not isinstance(name, str) or not name.strip() or len(name) > 128 or type(active) is not bool:
                    return _wallet_valuation_unavailable("wallet_row_invalid")
                if name in names:
                    return _wallet_valuation_unavailable("wallet_duplicate_name")
                names.add(name)
                if not isinstance(balance_text, str) or not _WALLET_DECIMAL.fullmatch(balance_text):
                    return _wallet_valuation_unavailable("wallet_balance_invalid")
                amount = _decimal(balance_text)
                if amount and not active:
                    return _wallet_valuation_unavailable("wallet_inactive_nonzero")
                total += amount
    except AccountFactsUnavailable:
        return _wallet_valuation_unavailable("wallet_balance_invalid")
    except Exception:
        return _wallet_valuation_unavailable("wallet_response_invalid")
    try:
        amount_text = _decimal_text(total)
        observed_text = _utc_timestamp(observed_at)
    except AccountFactsUnavailable:
        return _wallet_valuation_unavailable("wallet_response_invalid")
    return {
        "status": "available",
        "amount": amount_text,
        "currency": WALLET_VALUATION_CURRENCY,
        "source": WALLET_VALUATION_SOURCE,
        "scope": WALLET_VALUATION_SCOPE,
        "observed_at": observed_text,
        "wallet_count": len(response),
    }


def _validate_wallet_valuation(summary: Any) -> None:
    common = {"status", "amount", "currency", "source", "scope", "observed_at", "wallet_count"}
    if not isinstance(summary, Mapping):
        raise _fail("payload_invalid")
    if summary.get("source") != WALLET_VALUATION_SOURCE or summary.get("scope") != WALLET_VALUATION_SCOPE:
        raise _fail("payload_invalid")
    if summary.get("status") == "available":
        if set(summary) != common or summary.get("currency") != WALLET_VALUATION_CURRENCY:
            raise _fail("payload_invalid")
        amount, count = summary.get("amount"), summary.get("wallet_count")
        if not isinstance(amount, str) or type(count) is not int or not 1 <= count <= MAX_WALLET_ROWS:
            raise _fail("payload_invalid")
        if not _WALLET_DECIMAL.fullmatch(amount):
            raise _fail("payload_invalid")
        parsed = _decimal(amount)
        if _decimal_text(parsed) != amount or not isinstance(summary.get("observed_at"), str):
            raise _fail("payload_invalid")
        _parsed_timestamp(summary["observed_at"])
    elif summary.get("status") == "unavailable":
        if set(summary) != common | {"reason_code"} or any(
            summary.get(key) is not None for key in ("amount", "currency", "observed_at", "wallet_count")
        ) or summary.get("reason_code") not in WALLET_VALUATION_FAILURES:
            raise _fail("payload_invalid")
    else:
        raise _fail("payload_invalid")


def build_source_binding_id(
    *, account_scope_sha256: str, reader_public_revision: str,
    approved_application_revision: str,
) -> str:
    """Hash the canonical scope/UID/revision tuple used by the QRS binding."""
    if not _HEX_SHA256.fullmatch(str(account_scope_sha256)):
        raise _fail("account_identity_unbound")
    if not _GIT_SHA.fullmatch(str(reader_public_revision)):
        raise _fail("reader_revision_invalid")
    if not _GIT_SHA.fullmatch(str(approved_application_revision)):
        raise _fail("application_revision_invalid")
    material = {
        "approved_application_revision": approved_application_revision,
        "account_scope_sha256": account_scope_sha256,
        "reader_public_revision": reader_public_revision,
        "scope": SCOPE,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def collect_account_facts(
    client: ReadOnlyBinanceClient,
    *,
    expected_account_scope_sha256: str,
    target_id: str,
    reader_public_revision: str,
    approved_application_revision: str,
    observed_started_at: datetime,
    clock,
) -> dict[str, Any]:
    """Collect Spot + Flexible Earn quantities and independent wallet valuation."""
    if not isinstance(client, ReadOnlyBinanceClient):
        raise _fail("readonly_client_required")
    if not isinstance(target_id, str) or not target_id.strip():
        raise _fail("target_binding_invalid")
    if not _HEX_SHA256.fullmatch(str(expected_account_scope_sha256)):
        raise _fail("account_identity_unbound")
    if not _GIT_SHA.fullmatch(str(reader_public_revision)):
        raise _fail("reader_revision_invalid")
    if not _GIT_SHA.fullmatch(str(approved_application_revision)):
        raise _fail("application_revision_invalid")
    started = _utc_timestamp(observed_started_at)
    started_at_utc = observed_started_at.astimezone(timezone.utc)

    try:
        account = client.get_account()
    except Exception:
        raise _fail("spot_read_failed") from None
    if not isinstance(account, Mapping):
        raise _fail("spot_response_not_object")
    uid = account.get("uid")
    if isinstance(uid, bool) or not isinstance(uid, (str, int)) or not str(uid):
        raise _fail("account_identity_missing")
    account_scope_sha256 = calculate_broker_observation_sha256(
        {"account_uid": str(uid)}
    )
    if account_scope_sha256 != expected_account_scope_sha256:
        raise _fail("account_identity_mismatch")
    raw_account_type = account.get("accountType")
    provider_product_value = (
        "SPOT" if type(raw_account_type) is str and raw_account_type == "SPOT" else "unknown"
    )

    balances = account.get("balances")
    if not isinstance(balances, list):
        raise _fail("spot_balances_invalid")
    if len(balances) > 5000:
        raise _fail("spot_balances_limit_exceeded")
    assets: dict[str, dict[str, Decimal]] = {}
    for row in balances:
        if not isinstance(row, Mapping):
            raise _fail("spot_balance_row_invalid")
        asset = row.get("asset")
        if not isinstance(asset, str):
            raise _fail("spot_asset_type_invalid")
        if not _valid_asset(asset):
            raise _fail("spot_asset_format_invalid")
        if asset in assets:
            raise _fail("spot_asset_duplicate")
        assets[asset] = {
            "spot_free": _decimal(row.get("free")),
            "spot_locked": _decimal(row.get("locked")),
            "flexible_earn": Decimal(0),
        }
    spot_finished_at = clock()
    spot_finished = _utc_timestamp(spot_finished_at)
    if spot_finished_at.astimezone(timezone.utc) < started_at_utc:
        raise _fail("observation_time_invalid")

    current = 1
    total_positions: int | None = None
    seen_products: set[str] = set()
    while True:
        try:
            response = client.get_simple_earn_flexible_product_position(
                current=current, size=PAGE_SIZE
            )
        except Exception:
            raise _fail("flexible_earn_read_failed") from None
        if not isinstance(response, Mapping):
            raise _fail("flexible_earn_page_invalid")
        rows, page_total = response.get("rows"), response.get("total")
        if (
            not isinstance(rows, list)
            or type(page_total) is not int
            or page_total < 0
            or page_total > MAX_EARN_POSITIONS
            or len(rows) > PAGE_SIZE
            or (total_positions is not None and total_positions != page_total)
        ):
            raise _fail("flexible_earn_page_invalid")
        total_positions = page_total
        expected_rows = min(PAGE_SIZE, max(0, page_total - (current - 1) * PAGE_SIZE))
        if len(rows) != expected_rows:
            raise _fail("flexible_earn_page_incomplete")
        for row in rows:
            if not isinstance(row, Mapping):
                raise _fail("flexible_earn_page_invalid")
            asset = row.get("asset")
            product_id = row.get("productId")
            if (
                not _valid_asset(asset)
                or not isinstance(product_id, str)
                or not product_id
                or product_id in seen_products
            ):
                raise _fail("flexible_earn_page_invalid")
            if asset not in assets:
                # Do not synthesize missing Spot components as zero for an
                # Earn-only asset; the source response must explicitly bind it.
                raise _fail("spot_asset_unobserved")
            seen_products.add(product_id)
            amount = _decimal(row.get("totalAmount"))
            with localcontext() as context:
                context.prec = 100
                assets[asset]["flexible_earn"] += amount
        if len(seen_products) > MAX_EARN_POSITIONS:
            raise _fail("flexible_earn_page_invalid")
        if len(seen_products) == page_total:
            break
        current += 1
        if current > (MAX_EARN_POSITIONS // PAGE_SIZE) + 1:
            raise _fail("flexible_earn_page_incomplete")
    earn_finished_at = clock()
    earn_finished = _utc_timestamp(earn_finished_at)
    try:
        wallet_response = client.get_wallet_valuation_usdt()
    except Exception:
        wallet_response = None
        wallet_valuation = _wallet_valuation_unavailable("wallet_read_failed")
        wallet_observed_at = None
    else:
        wallet_observed_at = clock()
        wallet_valuation = _wallet_valuation(wallet_response, wallet_observed_at)
    finished_at = clock()
    finished = _utc_timestamp(finished_at)
    if (
        earn_finished_at.astimezone(timezone.utc) < spot_finished_at.astimezone(timezone.utc)
        or finished_at.astimezone(timezone.utc) < earn_finished_at.astimezone(timezone.utc)
        or (
            wallet_observed_at is not None
            and (
                wallet_observed_at.astimezone(timezone.utc) < earn_finished_at.astimezone(timezone.utc)
                or finished_at.astimezone(timezone.utc) < wallet_observed_at.astimezone(timezone.utc)
            )
        )
    ):
        raise _fail("observation_time_invalid")

    try:
        with localcontext() as context:
            context.prec = 100
            asset_rows = []
            for asset in sorted(assets):
                row = assets[asset]
                quantity = row["spot_free"] + row["spot_locked"] + row["flexible_earn"]
                if quantity == 0:
                    continue
                asset_rows.append(
                    {
                        "asset": asset,
                        "quantity": _decimal_text(quantity),
                        "spot_free": _decimal_text(row["spot_free"]),
                        "spot_locked": _decimal_text(row["spot_locked"]),
                        "flexible_earn": _decimal_text(row["flexible_earn"]),
                    }
                )
    except AccountFactsUnavailable:
        raise
    except Exception:
        raise _fail("amount_invalid") from None
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": "binance",
        "target_id": target_id,
        "account_scope_sha256": account_scope_sha256,
        "source_binding": {
            "kind": SOURCE_BINDING_KIND,
            "id": build_source_binding_id(
                account_scope_sha256=account_scope_sha256,
                reader_public_revision=reader_public_revision,
                approved_application_revision=approved_application_revision,
            ),
        },
        "source_revision": reader_public_revision,
        "approved_application_revision": approved_application_revision,
        "observed_started_at": started,
        "observed_finished_at": finished,
        "spot_observed_at": spot_finished,
        "provider_product_type": {
            "value": provider_product_value,
            "source": PROVIDER_PRODUCT_TYPE_SOURCE,
            "observed_at": spot_finished,
        },
        "earn_observed_at": earn_finished,
        "snapshot_atomic": False,
        "scope": SCOPE,
        "completeness": "complete_for_scope",
        "assets": asset_rows,
        "wallet_valuation": wallet_valuation,
        "uncovered_scopes": list(UNCOVERED_SCOPES),
        "no_order": True,
        "execution_authority_granted": False,
    }


def validate_account_facts_payload(payload: Any) -> dict[str, Any]:
    """Validate a complete private projection before it can be published."""
    required = {
        "schema_version", "platform", "target_id", "account_scope_sha256",
        "source_binding", "source_revision", "approved_application_revision",
        "observed_started_at", "observed_finished_at", "spot_observed_at",
        "earn_observed_at", "snapshot_atomic", "scope", "completeness",
        "assets", "uncovered_scopes", "no_order", "execution_authority_granted",
    }
    if (
        not isinstance(payload, Mapping)
        or not required.issubset(payload)
        or set(payload) - required - {"wallet_valuation", "provider_product_type"}
    ):
        raise _fail("payload_invalid")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("platform") != "binance"
        or not isinstance(payload.get("target_id"), str)
        or not payload["target_id"].strip()
        or payload.get("scope") != SCOPE
        or payload.get("completeness") != "complete_for_scope"
        or payload.get("snapshot_atomic") is not False
        or payload.get("no_order") is not True
        or payload.get("execution_authority_granted") is not False
        or payload.get("uncovered_scopes") != UNCOVERED_SCOPES
        or not _HEX_SHA256.fullmatch(str(payload.get("account_scope_sha256", "")))
        or not _GIT_SHA.fullmatch(str(payload.get("source_revision", "")))
        or not _GIT_SHA.fullmatch(str(payload.get("approved_application_revision", "")))
    ):
        raise _fail("payload_invalid")
    binding = payload.get("source_binding")
    if (
        not isinstance(binding, Mapping)
        or set(binding) != {"kind", "id"}
        or binding.get("kind") != SOURCE_BINDING_KIND
        or not _HEX_SHA256.fullmatch(str(binding.get("id", "")))
        or binding["id"] != build_source_binding_id(
            account_scope_sha256=payload["account_scope_sha256"],
            reader_public_revision=payload["source_revision"],
            approved_application_revision=payload["approved_application_revision"],
        )
    ):
        raise _fail("payload_invalid")
    observation_times = {}
    for key in (
        "observed_started_at", "observed_finished_at", "spot_observed_at", "earn_observed_at"
    ):
        observation_times[key] = _parsed_timestamp(payload.get(key))
    if not (
        observation_times["observed_started_at"]
        <= observation_times["spot_observed_at"]
        <= observation_times["earn_observed_at"]
        <= observation_times["observed_finished_at"]
    ):
        raise _fail("payload_invalid")
    if "provider_product_type" in payload:
        provider_product_type = payload["provider_product_type"]
        if (
            not isinstance(provider_product_type, Mapping)
            or set(provider_product_type) != {"value", "source", "observed_at"}
            or not isinstance(provider_product_type.get("value"), str)
            or provider_product_type.get("value") not in ("SPOT", "unknown")
            or provider_product_type.get("source") != PROVIDER_PRODUCT_TYPE_SOURCE
            or not isinstance(provider_product_type.get("observed_at"), str)
            or provider_product_type["observed_at"] != payload.get("spot_observed_at")
        ):
            raise _fail("payload_invalid")
        product_type_time = _parsed_timestamp(provider_product_type["observed_at"])
        if _utc_timestamp(product_type_time) != provider_product_type["observed_at"]:
            raise _fail("payload_invalid")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise _fail("payload_invalid")
    seen_assets: set[str] = set()
    for row in assets:
        if not isinstance(row, Mapping) or set(row) != {
            "asset", "quantity", "spot_free", "spot_locked", "flexible_earn"
        }:
            raise _fail("payload_invalid")
        asset = row.get("asset")
        if not _valid_asset(asset) or asset in seen_assets:
            raise _fail("payload_invalid")
        seen_assets.add(asset)
        amount_texts = [row.get(name) for name in ("spot_free", "spot_locked", "flexible_earn", "quantity")]
        if any(not isinstance(text, str) for text in amount_texts):
            raise _fail("payload_invalid")
        amounts = [_decimal(text) for text in amount_texts]
        if any(_decimal_text(amount) != text for amount, text in zip(amounts, amount_texts, strict=True)):
            raise _fail("payload_invalid")
        spot_free, spot_locked, flexible_earn, quantity = amounts
        if quantity == 0:
            raise _fail("payload_invalid")
        with localcontext() as context:
            context.prec = 100
            if quantity != spot_free + spot_locked + flexible_earn:
                raise _fail("payload_invalid")
    if "wallet_valuation" in payload:
        _validate_wallet_valuation(payload["wallet_valuation"])
        wallet_valuation = payload["wallet_valuation"]
        if wallet_valuation["status"] == "available":
            wallet_observed_at = _parsed_timestamp(wallet_valuation["observed_at"])
            if not (
                observation_times["earn_observed_at"]
                <= wallet_observed_at
                <= observation_times["observed_finished_at"]
            ):
                raise _fail("payload_invalid")
    return dict(payload)
