"""Compare the existing target and wallet binding without publishing."""
from __future__ import annotations

import hashlib
import json
import os
import re

from scripts import read_binance_account_facts as reader


def compare_source(env):
    nonce = env.get("RUNTIME_DAILY_COMPARE_NONCE") or ""
    if not re.fullmatch(r"[a-f0-9]{64}", nonce):
        raise ValueError("nonce_invalid")
    target = json.loads(env.get("RUNTIME_TARGET_JSON") or "null")
    if not isinstance(target, dict):
        raise ValueError("target_invalid")
    raw = env.get("BINANCE_ACCOUNT_FACTS_BINDING_JSON") or ""
    expected_scope = reader._bound_scope(raw)
    binding = reader._binding(raw, target, expected_scope,
                              env.get("BINANCE_ACCOUNT_FACTS_READER_REVISION") or "")
    selector = target.get("account_selector")
    if not isinstance(selector, list) or len(selector) != 1 or binding.get("account_selector") != selector[0]:
        raise ValueError("selector_invalid")
    value = {"platform": "binance", "service_name": target.get("service_name"),
             "strategy_profile": target.get("strategy_profile"), "account_scope": target.get("account_scope"),
             "account_selector": selector[0], "deployment_selector": target.get("deployment_selector"),
             "source_binding_id": binding["source_binding"]["id"], "target_id": binding.get("target_id")}
    if not all(isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", item, re.ASCII) for item in value.values()):
        raise ValueError("tuple_invalid")
    wrapped = {"binding": value, "domain": "qsl.binance-daily-binding.v1", "nonce": nonce}
    return hashlib.sha256(json.dumps(wrapped, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def main():
    try:
        result = {"status": "verified", "binding_digest": compare_source(os.environ)}
    except Exception:
        result = {"status": "unavailable"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
