from __future__ import annotations

from pathlib import Path
import tomllib


def test_qsl_metadata_has_runtime_platform_fields() -> None:
    qsl_path = Path(__file__).resolve().parents[1] / "qsl.toml"
    with qsl_path.open("rb") as f:
        qsl = tomllib.load(f)["qsl"]

    assert qsl["repo"] == "BinancePlatform"
    assert qsl["tier"] == "runtime"
    assert qsl["upgrade_ring"] == "ring_d"
    assert qsl["compat"]["bundle"] == "2026.09.0"
    assert qsl["requires"]["quant_platform_kit"]
    assert qsl["requires"]["crypto_strategies"]
