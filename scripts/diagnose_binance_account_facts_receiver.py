#!/usr/bin/env python3
"""Make one authenticated, read-only readiness request to the QRS receiver."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

QRS_ENDPOINT = "https://qsl-strategy-switch-console.pigbibi.workers.dev/api/internal/binance-account-facts"
_USER_AGENT = "QSL-AccountFacts-Readiness/1.0"

_READINESS_FIELDS = {"ok", "ready", "binding_valid", "account_options_readable", "unique_match"}
_READINESS_ERROR_FIELDS = _READINESS_FIELDS | {"error"}
_TOKEN_ERROR_CODES = {"binance_account_facts_token_invalid"}
_BINDING_ERROR_CODES = {"binance_account_facts_binding_unmatched"}
_CONFIG_ERROR_CODES = {
    "binance_account_facts_token_unavailable",
    "binance_account_facts_binding_missing",
    "binance_account_facts_binding_invalid",
}
_READINESS_ERROR_CODES = _TOKEN_ERROR_CODES | _BINDING_ERROR_CODES | _CONFIG_ERROR_CODES | {
    "binance_account_facts_account_options_unavailable",
    "binance_account_facts_unavailable",
}
_MAX_BODY_BYTES = 4096


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class ReceiverDiagnosis:
    category: str
    http_status: int | None = None
    body_shape: str = "none"
    receiver_error: str | None = None
    cloudflare_code: int | None = None


def _json_body_shape(raw: bytes, headers) -> tuple[str, str | None, int | None]:
    if len(raw) > _MAX_BODY_BYTES:
        return "oversized", None, None
    if not raw:
        return "empty", None, None
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        body = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON constant")),
        )
    except (UnicodeError, ValueError, RecursionError):
        return "invalid_json", None, None
    if not isinstance(body, dict):
        return "json_non_object", None, None

    if (set(body) == _READINESS_FIELDS
            and all(type(body.get(key)) is bool for key in _READINESS_FIELDS)):
        if all(body[key] is True for key in _READINESS_FIELDS):
            return "readiness", None, None
        return "readiness_not_ready", None, None

    if (set(body) == _READINESS_ERROR_FIELDS
            and all(type(body.get(key)) is bool for key in _READINESS_FIELDS)
            and body.get("ok") is False and body.get("ready") is False
            and isinstance(body.get("error"), str)
            and body["error"] in _READINESS_ERROR_CODES):
        error = body["error"]
        if error in _TOKEN_ERROR_CODES:
            return "receiver_error", "token_invalid", None
        if error in _BINDING_ERROR_CODES:
            return "receiver_error", "binding_unmatched", None
        if error in _CONFIG_ERROR_CODES:
            return "receiver_error", "config_or_binding", None
        if error == "binance_account_facts_account_options_unavailable":
            return "receiver_error", "account_options_unavailable", None
        return "receiver_error", "receiver_unavailable", None

    if (set(body) == {"ok", "error"} and body.get("ok") is False
            and isinstance(body.get("error"), str)
            and body["error"] in _TOKEN_ERROR_CODES):
        return "token_error", "token_invalid", None

    cf_code = _cloudflare_error_code(body, headers)
    if cf_code is not None:
        return "cloudflare_error", None, cf_code
    return "json_object_other", None, None


def _cloudflare_error_code(body: dict, headers) -> int | None:
    """Expose only the numeric code from the standard Cloudflare error envelope."""
    if str(headers.get("Server", "")).lower() != "cloudflare":
        return None
    if set(body) != {"success", "errors", "messages", "result"}:
        return None
    if body.get("success") is not False or body.get("messages") != [] or body.get("result") is not None:
        return None
    errors = body.get("errors")
    if not isinstance(errors, list) or len(errors) != 1 or not isinstance(errors[0], dict):
        return None
    error = errors[0]
    if (set(error) != {"code", "message"} or type(error.get("code")) is not int
            or not 0 <= error["code"] <= 999_999 or not isinstance(error.get("message"), str)):
        return None
    return error["code"]


def _read_once(request: Request, *, opener_factory=build_opener) -> tuple[int, bytes, object]:
    try:
        with opener_factory(_NoRedirect).open(request, timeout=15) as response:
            return response.status, response.read(_MAX_BODY_BYTES + 1), response.headers
    except HTTPError as error:
        try:
            return error.code, error.read(_MAX_BODY_BYTES + 1), error.headers or {}
        finally:
            error.close()


def diagnose_receiver(*, token: str, opener_factory=build_opener) -> ReceiverDiagnosis:
    if not isinstance(token, str) or not token or token != token.strip():
        return ReceiverDiagnosis("token_missing")
    request = Request(
        QRS_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="GET",
    )
    try:
        status, raw, headers = _read_once(request, opener_factory=opener_factory)
    except Exception:
        return ReceiverDiagnosis("transport")
    body_shape, safe_code, cf_code = _json_body_shape(raw, headers)
    if status == 200 and body_shape == "readiness":
        return ReceiverDiagnosis("ready", status, body_shape)
    if safe_code:
        return ReceiverDiagnosis(safe_code, status, body_shape, safe_code)
    if cf_code is not None:
        return ReceiverDiagnosis("cloudflare_rejected", status, body_shape, cloudflare_code=cf_code)
    if 300 <= status <= 399:
        return ReceiverDiagnosis("redirect", status, body_shape)
    if 100 <= status <= 599:
        return ReceiverDiagnosis("http_rejected", status, body_shape)
    return ReceiverDiagnosis("response_invalid", body_shape=body_shape)


def main() -> int:
    result = diagnose_receiver(token=os.environ.get("BINANCE_ACCOUNT_FACTS_SYNC_TOKEN", ""))
    fields = [f"category={result.category}"]
    if result.http_status is not None:
        fields.append(f"http_status={result.http_status}")
    fields.append(f"body_shape={result.body_shape}")
    if result.receiver_error is not None:
        fields.append(f"receiver_code={result.receiver_error}")
    if result.cloudflare_code is not None:
        fields.append(f"cloudflare_code={result.cloudflare_code}")
    print("binance_account_facts_receiver_diagnosis " + " ".join(fields))
    return 0 if result.category == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
