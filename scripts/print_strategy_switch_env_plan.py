from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
CRYPTO_STRATEGIES_SRC = ROOT.parent / "CryptoStrategies" / "src"

for candidate in (ROOT, QPK_SRC, CRYPTO_STRATEGIES_SRC):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from strategy_registry import (  # noqa: E402
    BINANCE_PLATFORM,
    get_platform_profile_status_matrix,
    resolve_strategy_definition,
    resolve_strategy_metadata,
)


def build_switch_plan(profile: str) -> dict[str, object]:
    definition = resolve_strategy_definition(profile, platform_id=BINANCE_PLATFORM)
    metadata = resolve_strategy_metadata(definition.profile, platform_id=BINANCE_PLATFORM)
    status_row = next(
        row for row in get_platform_profile_status_matrix() if row["canonical_profile"] == definition.profile
    )

    set_env = {
        "STRATEGY_PROFILE": definition.profile,
    }
    keep_env = [
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "TG_TOKEN",
        "GLOBAL_TELEGRAM_CHAT_ID",
    ]
    optional_env = [
        "NOTIFY_LANG",
        "BTC_STATUS_REPORT_INTERVAL_HOURS",
        "STRATEGY_ARTIFACT_FILE",
        "STRATEGY_ARTIFACT_MANIFEST_FILE",
        "STRATEGY_ARTIFACT_FIRESTORE_COLLECTION",
        "STRATEGY_ARTIFACT_FIRESTORE_DOCUMENT",
        "STRATEGY_ARTIFACT_MAX_AGE_DAYS",
        "STRATEGY_ARTIFACT_ACCEPTABLE_MODES",
        "STRATEGY_ARTIFACT_EXPECTED_SIZE",
        "STRATEGY_ARTIFACT_ALLOW_NEW_ENTRIES_ON_DEGRADED",
        "BTC_WEIGHT",
        "TREND_WEIGHT",
        "DYNAMIC_MODE",
        "DYNAMIC_REGIME_MODE",
        "DYNAMIC_REGIME_OFF_CUT",
        "DYNAMIC_HARD_SMA200_RATIO",
        "DYNAMIC_HARD_MA200_SLOPE",
        "DYNAMIC_SOFT_SMA200_RATIO",
        "DYNAMIC_HARD_BTC_WEIGHT",
        "DYNAMIC_HARD_TREND_WEIGHT",
        "DYNAMIC_SOFT_BTC_WEIGHT",
        "DYNAMIC_SOFT_TREND_WEIGHT",
    ]
    notes = [
        "Binance runtime resolves strategy artifacts through STRATEGY_ARTIFACT_* settings.",
        "Switching is mainly STRATEGY_PROFILE plus the shared strategy artifact settings.",
        "Keep exchange credentials and Telegram settings stable across strategy switches.",
    ]

    return {
        "platform": BINANCE_PLATFORM,
        "canonical_profile": definition.profile,
        "display_name": metadata.display_name,
        "eligible": status_row["eligible"],
        "enabled": status_row["enabled"],
        "required_inputs": sorted(definition.required_inputs),
        "target_mode": definition.target_mode,
        "set_env": set_env,
        "keep_env": keep_env,
        "optional_env": optional_env,
        "remove_if_present": [],
        "hints": {
            "strategy_artifact_default_firestore_collection": "strategy",
            "strategy_artifact_default_firestore_document": "CRYPTO_LIVE_POOL_ROTATION_LIVE_POOL",
            "default_local_artifact": str(ROOT / "artifacts" / "live_pool_legacy.json"),
            "default_local_artifact_manifest": str(ROOT / "artifacts" / "artifact_manifest.json"),
        },
        "notes": notes,
    }


def _print_plan(plan: dict[str, object]) -> None:
    print(f"platform: {plan['platform']}")
    print(f"profile: {plan['canonical_profile']} ({plan['display_name']})")
    print(f"eligible: {plan['eligible']}  enabled: {plan['enabled']}")
    print(f"required_inputs: {', '.join(plan['required_inputs'])}")
    print(f"target_mode: {plan['target_mode']}")
    print("\nset_env:")
    for key, value in plan["set_env"].items():
        print(f"  {key}={value}")
    print("\nkeep_env:")
    for key in plan["keep_env"]:
        print(f"  {key}")
    print("\noptional_env:")
    for key in plan["optional_env"]:
        print(f"  {key}")
    print("\nremove_if_present:")
    for key in plan["remove_if_present"]:
        print(f"  {key}")
    if plan["hints"]:
        print("\nhints:")
        for key, value in plan["hints"].items():
            print(f"  {key}: {value}")
    if plan["notes"]:
        print("\nnotes:")
        for note in plan["notes"]:
            print(f"  - {note}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    plan = build_switch_plan(args.profile)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    _print_plan(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
