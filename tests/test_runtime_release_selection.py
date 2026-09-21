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


def test_workflow_pin_mismatch_is_rejected_for_execution_paths() -> None:
    with pytest.raises(
        selection.ReleaseSelectionError, match="BINANCE_RUNTIME_WORKFLOW_SHA"
    ):
        selection.verify_workflow_revision(
            configured_workflow_sha=PIN_A,
            configured_release_sha=PIN_B,
            actual_sha=PIN_B,
            runtime_enabled=True,
        )
    assert (
        selection.verify_workflow_revision(
            configured_workflow_sha=PIN_A,
            configured_release_sha=PIN_B,
            actual_sha=PIN_A,
            reconcile_only=True,
        )
        == PIN_A
    )


def test_distinct_workflow_and_release_pins_keep_app_identity() -> None:
    """Trigger/workflow SHA may differ from the approved application release SHA."""

    assert (
        selection.verify_workflow_revision(
            configured_workflow_sha=PIN_A,
            configured_release_sha=PIN_B,
            actual_sha=PIN_A,
            runtime_enabled=True,
        )
        == PIN_A
    )
    selected = selection.select_release_sha(
        configured_sha=PIN_B,
        runtime_enabled=True,
    )
    assert selected == PIN_B
    assert selected != PIN_A
    assert selected != TIP


def test_main_tip_past_workflow_pin_fails_closed_without_rewriting_release() -> None:
    """Unrelated main tip movement must not rewrite the app pin or soften the gate."""

    with pytest.raises(
        selection.ReleaseSelectionError, match="BINANCE_RUNTIME_WORKFLOW_SHA"
    ):
        selection.verify_workflow_revision(
            configured_workflow_sha=PIN_A,
            configured_release_sha=PIN_B,
            actual_sha=TIP,
            runtime_enabled=True,
        )
    assert (
        selection.select_release_sha(
            configured_sha=PIN_B,
            runtime_enabled=True,
        )
        == PIN_B
    )


def test_missing_workflow_pin_falls_back_to_release_pin_identity() -> None:
    assert (
        selection.verify_workflow_revision(
            configured_workflow_sha=None,
            configured_release_sha=PIN_A,
            actual_sha=PIN_A,
            runtime_enabled=True,
        )
        == PIN_A
    )
    with pytest.raises(
        selection.ReleaseSelectionError, match="BINANCE_RUNTIME_RELEASE_SHA"
    ):
        selection.verify_workflow_revision(
            configured_workflow_sha="",
            configured_release_sha=PIN_A,
            actual_sha=TIP,
            validate_only=True,
        )
    with pytest.raises(
        selection.ReleaseSelectionError,
        match="BINANCE_RUNTIME_WORKFLOW_SHA is unset",
    ):
        selection.verify_workflow_revision(
            configured_workflow_sha=None,
            configured_release_sha=None,
            actual_sha=PIN_A,
            runtime_enabled=True,
        )


def test_candidate_full_cycle_may_omit_workflow_pin() -> None:
    assert (
        selection.verify_workflow_revision(
            configured_workflow_sha=None,
            configured_release_sha=PIN_A,
            actual_sha=TIP,
            validate_only=True,
            full_cycle=True,
            runtime_enabled=False,
            reconcile_only=False,
            candidate_sha=PIN_B,
        )
        == TIP
    )


def test_disabled_noop_does_not_require_workflow_pin() -> None:
    assert (
        selection.verify_workflow_revision(
            configured_workflow_sha=None,
            configured_release_sha=None,
            actual_sha=None,
            runtime_enabled=False,
            reconcile_only=False,
            validate_only=False,
        )
        == ""
    )


def test_runtime_identity_triplet_is_emitted() -> None:
    line = selection.format_runtime_identity(
        workflow_sha=PIN_A,
        release_sha=PIN_B,
        uv_lock_sha256="d" * 64,
        uv_version="uv 0.11.19",
        python_version="3.11.9",
    )
    assert "workflow_sha=" + PIN_A in line
    assert "release_sha=" + PIN_B in line
    assert "uv_lock_sha256=" + ("d" * 64) in line
    assert "uv_version=uv 0.11.19" in line
    assert "python_version=3.11.9" in line
    with pytest.raises(selection.ReleaseSelectionError, match="uv_lock_sha256"):
        selection.format_runtime_identity(
            workflow_sha=PIN_A,
            release_sha=PIN_B,
            uv_lock_sha256="not-a-hash",
            uv_version="uv 0.11.19",
            python_version="3.11.9",
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
