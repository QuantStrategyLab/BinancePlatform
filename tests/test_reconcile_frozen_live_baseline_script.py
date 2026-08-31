from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_frozen_live_baseline.py"


def test_reconciliation_script_imports_from_an_arbitrary_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Create a redacted no-order Binance reconciliation candidate" in result.stdout
