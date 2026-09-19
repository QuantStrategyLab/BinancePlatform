#!/usr/bin/env python3
"""Workflow entry for runtime release SHA selection (no authority grant)."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from runtime_release_selection import main

if __name__ == "__main__":
    raise SystemExit(main())
