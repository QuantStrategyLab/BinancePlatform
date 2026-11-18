#!/usr/bin/env python3
"""Workflow entry for runtime release SHA selection (no authority grant)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
for _path in (_SRC_ROOT, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from binance_platform.runtime_release_selection import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
