"""Select the configured Binance runtime release commit.

This module only chooses which source tree to check out. It does not grant
trading authority, change risk limits, or replace LIVE risk authority checks.
Missing or illegal pins fail closed and never fall back to the moving main tip.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_TRUTHY = frozenset({"true", "1", "yes"})
_FALSY = frozenset({"false", "0", "no", ""})


class ReleaseSelectionError(ValueError):
    """Fail-closed release selection or checkout mismatch."""


def _normalize_sha(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    if _FULL_SHA.fullmatch(text) is None:
        raise ReleaseSelectionError(
            f"invalid {field}: expected a full 40-character lowercase hex commit SHA"
        )
    return text


def _as_bool(value: str | bool | None, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    raise ReleaseSelectionError(f"invalid {field}: expected true or false")


def select_release_sha(
    *,
    configured_sha: str | None,
    candidate_sha: str | None = None,
    validate_only: bool = False,
    full_cycle: bool = False,
    runtime_enabled: bool = False,
    reconcile_only: bool = False,
) -> str:
    """Return the exact commit SHA that must be checked out.

    A non-empty candidate is allowed only for the existing no-submit full-cycle
    validation combination. Otherwise the protected configured pin is required.
    """

    candidate = _normalize_sha(candidate_sha, field="candidate_release_sha")
    configured = _normalize_sha(configured_sha, field="BINANCE_RUNTIME_RELEASE_SHA")

    if candidate is not None:
        if (
            not validate_only
            or not full_cycle
            or runtime_enabled
            or reconcile_only
        ):
            raise ReleaseSelectionError(
                "candidate_release_sha is only allowed with validate_only=true, "
                "full_cycle=true, runtime disabled, and reconcile_only=false"
            )
        return candidate

    if configured is None:
        raise ReleaseSelectionError(
            "BINANCE_RUNTIME_RELEASE_SHA is required for non-disabled runtime "
            "checkout; refusing to fall back to main tip"
        )
    return configured


def resolve_checkout_head(*, repo_root: Path | None = None) -> str:
    root = Path.cwd() if repo_root is None else repo_root
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseSelectionError("checkout HEAD is unavailable") from exc
    return _normalize_sha(revision, field="checkout_head") or ""


def verify_checkout_matches_selected(
    *,
    expected_sha: str,
    repo_root: Path | None = None,
    actual_head: str | None = None,
) -> str:
    expected = _normalize_sha(expected_sha, field="expected_release_sha")
    if expected is None:
        raise ReleaseSelectionError("expected_release_sha is required")
    actual = (
        _normalize_sha(actual_head, field="checkout_head")
        if actual_head is not None
        else resolve_checkout_head(repo_root=repo_root)
    )
    if actual != expected:
        raise ReleaseSelectionError(
            "checkout HEAD does not match the selected runtime release SHA"
        )
    return actual


def _env_flag(name: str) -> bool:
    return _as_bool(os.environ.get(name), field=name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify the current checkout HEAD matches EXPECTED_RELEASE_SHA",
    )
    args = parser.parse_args(argv)

    try:
        if args.verify:
            expected = os.environ.get("EXPECTED_RELEASE_SHA")
            actual = verify_checkout_matches_selected(expected_sha=expected or "")
            print(actual)
            return 0

        selected = select_release_sha(
            configured_sha=os.environ.get("BINANCE_RUNTIME_RELEASE_SHA"),
            candidate_sha=os.environ.get("CANDIDATE_RELEASE_SHA"),
            validate_only=_env_flag("VALIDATE_ONLY"),
            full_cycle=_env_flag("FULL_CYCLE"),
            runtime_enabled=_env_flag("RUNTIME_TARGET_ENABLED"),
            reconcile_only=_env_flag("RECONCILE_ONLY"),
        )
        print(selected)
        return 0
    except ReleaseSelectionError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
