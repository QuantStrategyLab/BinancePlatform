#!/usr/bin/env python3
"""Publish one complete private Binance asset-quantity projection to QRS."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application.account_facts import AccountFactsUnavailable, validate_account_facts_payload  # noqa: E402


QRS_ENDPOINT = "https://qsl-strategy-switch-console.pigbibi.workers.dev/api/internal/binance-account-facts"
_HTTP_ERROR_LABELS = {
    (400, "invalid_binance_account_facts"): "http_400_report_invalid",
    (400, "invalid_binance_account_facts_time"): "http_400_report_time_invalid",
    (400, "invalid_binance_account_facts_quantity"): "http_400_report_quantity_invalid",
    (400, "invalid_binance_account_facts_assets"): "http_400_report_assets_invalid",
    (401, "binance_account_facts_token_invalid"): "http_401_token_invalid",
    (409, "binance_account_facts_binding_unmatched"): "http_409_binding_unmatched",
    (409, "binance_account_facts_identity_mismatch"): "http_409_identity_mismatch",
    (409, "binance_account_facts_observation_invalid"): "http_409_observation_invalid",
    (409, "binance_account_facts_observation_conflict"): "http_409_observation_conflict",
    (413, "binance_account_facts_payload_too_large"): "http_413_payload_too_large",
    (503, "binance_account_facts_token_unavailable"): "http_503_receiver_unavailable",
    (503, "binance_account_facts_binding_missing"): "http_503_receiver_unavailable",
    (503, "binance_account_facts_binding_invalid"): "http_503_receiver_unavailable",
    (503, "binance_account_facts_storage_unavailable"): "http_503_receiver_unavailable",
    (503, "binance_account_facts_unavailable"): "http_503_receiver_unavailable",
}
_HTTP_REJECTED = "account_facts_publish_http_rejected"


class PublishError(ValueError):
    """A fixed non-sensitive publisher stop code."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _http_failure_code(error: HTTPError) -> str:
    status = error.code
    if type(status) is not int or not 100 <= status <= 599:
        return _HTTP_REJECTED
    try:
        raw = error.read(4097)
        if not isinstance(raw, bytes):
            shape = "invalid_body"
        elif len(raw) > 4096:
            shape = "body_oversized"
        else:
            shape = "body_invalid_json"
            body = json.loads(raw.decode("utf-8"))
            if isinstance(body, dict):
                shape = "body_unrecognized_json"
                if (set(body) == {"ok", "error"} and body.get("ok") is False
                        and isinstance(body.get("error"), str)):
                    shape = "body_unknown_error_code"
                    label = _HTTP_ERROR_LABELS.get((status, body["error"]))
                    if label is not None:
                        return f"account_facts_publish_{label}"
        return f"account_facts_publish_http_{status}_{shape}"
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return f"account_facts_publish_http_{status}_body_invalid_json"


def publish_account_facts(*, facts_path: Path, env) -> str:
    if env.get("BINANCE_ACCOUNT_FACTS_ENABLED") != "true":
        raise PublishError("account_facts_publish_disabled")
    token = str(env.get("BINANCE_ACCOUNT_FACTS_SYNC_TOKEN") or "")
    if not token or token != token.strip():
        raise PublishError("account_facts_sync_token_missing")
    try:
        details = facts_path.lstat()
        if facts_path.is_symlink() or not facts_path.is_file() or not 0 < details.st_size <= 5_000_000:
            raise PublishError("account_facts_payload_invalid")
        payload = validate_account_facts_payload(json.loads(facts_path.read_text(encoding="utf-8")))
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except PublishError:
        raise
    except (AccountFactsUnavailable, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PublishError("account_facts_payload_invalid") from None
    request = Request(
        QRS_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            if response.status not in {200, 201}:
                raise PublishError("account_facts_publish_rejected")
            raw_ack = response.read(4097)
            if len(raw_ack) > 4096:
                raise PublishError("account_facts_publish_ack_invalid")
            ack = json.loads(raw_ack.decode("utf-8"))
    except PublishError:
        raise
    except HTTPError as error:
        try:
            failure_code = _http_failure_code(error)
        except TimeoutError:
            raise PublishError("account_facts_publish_timeout") from None
        except (URLError, OSError):
            raise PublishError("account_facts_publish_network_failed") from None
        raise PublishError(failure_code) from None
    except TimeoutError:
        raise PublishError("account_facts_publish_timeout") from None
    except (URLError, OSError):
        raise PublishError("account_facts_publish_network_failed") from None
    except (UnicodeError, ValueError, TypeError, AttributeError, RecursionError):
        raise PublishError("account_facts_publish_ack_invalid") from None
    if not isinstance(ack, dict) or ack.get("status") not in {"published", "unchanged"}:
        raise PublishError("account_facts_publish_rejected")
    return ack["status"]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("facts", type=Path)
    args = parser.parse_args(argv)
    try:
        status = publish_account_facts(facts_path=args.facts, env=os.environ)
        print(f"account_facts_publish={status}")
        return 0
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("account_facts_publish_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
