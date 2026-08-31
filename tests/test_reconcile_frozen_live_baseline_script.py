from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_frozen_live_baseline.py"


def _script_module():
    spec = importlib.util.spec_from_file_location("binance_reconciliation_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReconciliationScriptTests(unittest.TestCase):
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
