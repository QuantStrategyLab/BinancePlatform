from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_frozen_live_baseline.py"


def _script_module():
    spec = importlib.util.spec_from_file_location("binance_reconciliation_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReconciliationScriptTests(unittest.TestCase):
    def _run_main_with_output(self, module, output_path: Path) -> tuple[int, dict[str, object], dict[str, object]]:
        stdout = io.StringIO()
        previous_argv = sys.argv
        try:
            sys.argv = [str(SCRIPT), "--output", str(output_path)]
            with redirect_stdout(stdout):
                exit_code = module.main()
        finally:
            sys.argv = previous_argv

        emitted = json.loads(stdout.getvalue())
        persisted = json.loads(output_path.read_text(encoding="utf-8"))
        return exit_code, emitted, persisted

    def _reconcile_only_settings(self):
        return SimpleNamespace(
            runtime_target=SimpleNamespace(
                live_continuity=SimpleNamespace(state="RECONCILE_ONLY"),
            ),
        )

    def test_reconciliation_script_imports_from_an_arbitrary_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--help"],
                cwd=directory,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Create a redacted no-order Binance reconciliation candidate", result.stdout)

    def test_reconciliation_script_uses_redacted_stable_reason_codes(self) -> None:
        module = _script_module()

        self.assertEqual(
            module._safe_reason_code(module.BinanceReconciliationReadError("Binance reconciliation account identity is unavailable.")),
            "account_identity_unavailable",
        )
        self.assertEqual(
            module._safe_reason_code(module.BinanceReconciliationReadError("sensitive exchange response: xyz")),
            "reconciliation_read_unavailable",
        )
        self.assertEqual(
            module._safe_reason_code(RuntimeError("sensitive transport response: xyz")),
            "unexpected_reconciliation_failure",
        )

    def test_unexpected_configuration_failure_persists_allowlisted_receipt(self) -> None:
        module = _script_module()
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "candidate.json"
            with patch.object(
                module,
                "resolve_runtime_target_from_env",
                side_effect=RuntimeError("sensitive configuration detail"),
            ):
                exit_code, emitted, persisted = self._run_main_with_output(module, output_path)

        self.assertEqual(exit_code, 2)
        self.assertEqual(emitted, persisted)
        self.assertEqual(
            emitted,
            {
                "status": "blocked",
                "stage": "runtime_target_load",
                "failure_class": "configuration",
                "reason_code": "unexpected_reconciliation_failure",
            },
        )
        self.assertNotIn("sensitive configuration detail", json.dumps(emitted, sort_keys=True))

    def test_reconciliation_uses_canonical_target_without_execution_settings(self) -> None:
        module = _script_module()
        target = SimpleNamespace(live_continuity=SimpleNamespace(state="RECONCILE_ONLY"))
        candidate = SimpleNamespace(to_safe_dict=lambda: {"status": "ready"})
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "candidate.json"
            with (
                patch.object(module, "resolve_runtime_target_from_env", return_value=target),
                patch.object(module, "load_runtime_trade_state", return_value={}),
                patch.object(module, "connect_client", return_value=object()),
                patch.object(module, "collect_read_only_reconciliation_observations", return_value=object()),
                patch.object(module, "build_reconciliation_candidate", return_value=candidate),
                patch.dict(
                    module.os.environ,
                    {
                        "BINANCE_API_KEY": "x",
                        "BINANCE_API_SECRET": "x",
                        "BINANCE_RECONCILIATION_SYMBOLS": "BTCUSDT",
                    },
                    clear=True,
                ),
            ):
                exit_code, emitted, persisted = self._run_main_with_output(module, output_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(emitted, {"status": "ready"})
        self.assertEqual(persisted, emitted)

    def test_client_connect_failure_uses_fixed_stage_and_class(self) -> None:
        module = _script_module()
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "candidate.json"
            with (
                patch.object(
                    module,
                    "resolve_runtime_target_from_env",
                    return_value=self._reconcile_only_settings().runtime_target,
                ),
                patch.object(module, "load_runtime_trade_state", return_value={}),
                patch.object(module, "connect_client", side_effect=RuntimeError("sensitive provider detail")),
                patch.dict(
                    module.os.environ,
                    {"BINANCE_API_KEY": "x", "BINANCE_API_SECRET": "x"},
                    clear=False,
                ),
            ):
                exit_code, emitted, persisted = self._run_main_with_output(module, output_path)

        self.assertEqual(exit_code, 2)
        self.assertEqual(emitted, persisted)
        self.assertEqual(
            emitted,
            {
                "status": "blocked",
                "stage": "client_connect",
                "failure_class": "connectivity",
                "reason_code": "unexpected_reconciliation_failure",
            },
        )
        self.assertNotIn("sensitive provider detail", json.dumps(emitted, sort_keys=True))
