"""Synthetic regressions for runtime release SHA selection."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import runtime_release_selection as selection

PIN_A = "a" * 40
PIN_B = "b" * 40
TIP = "c" * 40


def test_configured_pin_is_selected_when_main_tip_differs() -> None:
    selected = selection.select_release_sha(
        configured_sha=PIN_A,
        candidate_sha=None,
        validate_only=False,
        full_cycle=False,
        runtime_enabled=True,
        reconcile_only=False,
    )
    assert selected == PIN_A
    assert selected != TIP


def test_missing_pin_refuses_main_fallback() -> None:
    with pytest.raises(selection.ReleaseSelectionError, match="refusing to fall back"):
        selection.select_release_sha(
            configured_sha="",
            runtime_enabled=True,
        )


def test_illegal_pin_format_is_rejected() -> None:
    with pytest.raises(selection.ReleaseSelectionError, match="BINANCE_RUNTIME_RELEASE_SHA"):
        selection.select_release_sha(configured_sha="not-a-sha", runtime_enabled=True)
    with pytest.raises(selection.ReleaseSelectionError, match="BINANCE_RUNTIME_RELEASE_SHA"):
        selection.select_release_sha(configured_sha="A" * 40, runtime_enabled=True)


def test_candidate_requires_no_submit_full_cycle_mode() -> None:
    ok = selection.select_release_sha(
        configured_sha=PIN_A,
        candidate_sha=PIN_B,
        validate_only=True,
        full_cycle=True,
        runtime_enabled=False,
        reconcile_only=False,
    )
    assert ok == PIN_B

    for kwargs in (
        {
            "validate_only": False,
            "full_cycle": True,
            "runtime_enabled": False,
            "reconcile_only": False,
        },
        {
            "validate_only": True,
            "full_cycle": False,
            "runtime_enabled": False,
            "reconcile_only": False,
        },
        {
            "validate_only": True,
            "full_cycle": True,
            "runtime_enabled": True,
            "reconcile_only": False,
        },
        {
            "validate_only": True,
            "full_cycle": True,
            "runtime_enabled": False,
            "reconcile_only": True,
        },
    ):
        with pytest.raises(selection.ReleaseSelectionError, match="candidate_release_sha"):
            selection.select_release_sha(
                configured_sha=PIN_A,
                candidate_sha=PIN_B,
                **kwargs,
            )


def test_candidate_illegal_format_rejected_even_in_allowed_mode() -> None:
    with pytest.raises(selection.ReleaseSelectionError, match="candidate_release_sha"):
        selection.select_release_sha(
            configured_sha=PIN_A,
            candidate_sha="deadbeef",
            validate_only=True,
            full_cycle=True,
            runtime_enabled=False,
            reconcile_only=False,
        )


def test_checkout_head_mismatch_is_rejected(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "seed"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert selection.verify_checkout_matches_selected(
        expected_sha=head, repo_root=tmp_path
    ) == head
    with pytest.raises(selection.ReleaseSelectionError, match="does not match"):
        selection.verify_checkout_matches_selected(expected_sha=PIN_A, repo_root=tmp_path)


def test_cli_select_and_verify_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "select_runtime_release_sha.py"
    env = {
        **os.environ,
        "BINANCE_RUNTIME_RELEASE_SHA": PIN_A,
        "VALIDATE_ONLY": "false",
        "FULL_CYCLE": "false",
        "RUNTIME_TARGET_ENABLED": "true",
        "RECONCILE_ONLY": "false",
    }
    selected = subprocess.run(
        [sys.executable, str(script)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    assert selected == PIN_A

    monkeypatch.setenv("EXPECTED_RELEASE_SHA", PIN_A)
    assert (
        selection.verify_checkout_matches_selected(
            expected_sha=PIN_A, actual_head=PIN_A
        )
        == PIN_A
    )
