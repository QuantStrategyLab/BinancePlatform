#!/usr/bin/env python3
"""Verify the CryptoStrategies source identity across runtime declarations."""

import re
import sys
import tomllib
from pathlib import Path

_SHA = r"[0-9a-f]{40}"
_PROJECT = re.compile(
    rf"crypto-strategies\s*@\s*git\+https://github\.com/QuantStrategyLab/CryptoStrategies\.git@({_SHA})"
)
_LOCK = re.compile(
    rf"https://github\.com/QuantStrategyLab/CryptoStrategies\.git\?rev=({_SHA})#({_SHA})"
)
_DIRECT_LOCK = re.compile(
    rf"https://github\.com/QuantStrategyLab/CryptoStrategies\.git\?rev=({_SHA})"
)


class PinError(ValueError):
    pass


def check_pins(project, lock, qsl):
    try:
        dependencies = project["project"]["dependencies"]
        requirements = [entry for entry in dependencies if isinstance(entry, str)
                        and re.match(r"^crypto-strategies\s*@", entry)]
        packages = [entry for entry in lock["package"] if entry.get("name") == "crypto-strategies"]
        platforms = [entry for entry in lock["package"] if entry.get("name") == "binance-platform"]
        if len(requirements) != 1 or len(packages) != 1 or len(platforms) != 1:
            raise PinError("crypto source declarations must each be unique")
        direct = [entry for entry in platforms[0]["metadata"]["requires-dist"]
                  if entry.get("name") == "crypto-strategies"]
        overrides = [entry for entry in lock.get("manifest", {}).get("overrides", [])
                     if entry.get("name") == "crypto-strategies"]
        if len(direct) != 1 or len(overrides) > 1:
            raise PinError("crypto lock requirements must be unique")
        project_match = _PROJECT.fullmatch(requirements[0])
        lock_match = _LOCK.fullmatch(packages[0]["source"]["git"])
        direct_match = _DIRECT_LOCK.fullmatch(direct[0]["git"])
        override_match = _DIRECT_LOCK.fullmatch(overrides[0]["git"]) if overrides else None
        qsl_pin = qsl["qsl"]["requires"]["crypto_strategies"]
        if (not project_match or not lock_match or not direct_match
                or (overrides and not override_match) or not isinstance(qsl_pin, str)
                or not re.fullmatch(_SHA, qsl_pin)):
            raise PinError("crypto source declarations must use full immutable revisions")
        revisions = [project_match.group(1), lock_match.group(1), lock_match.group(2),
                     direct_match.group(1), qsl_pin]
        if override_match:
            revisions.append(override_match.group(1))
        if len(set(revisions)) != 1:
            raise PinError("crypto source declarations do not match")
        return qsl_pin
    except (KeyError, TypeError, AttributeError) as exc:
        raise PinError("crypto source declarations are invalid") from exc


def main():
    root = Path(__file__).resolve().parents[1]
    try:
        project, lock, qsl = (tomllib.loads((root / name).read_text(encoding="utf-8"))
                              for name in ("pyproject.toml", "uv.lock", "qsl.toml"))
        check_pins(project, lock, qsl)
    except (OSError, tomllib.TOMLDecodeError, PinError) as exc:
        print(f"CryptoStrategies pin check failed: {exc}", file=sys.stderr)
        return 1
    print("CryptoStrategies source declarations match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
