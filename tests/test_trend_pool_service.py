import unittest
from types import SimpleNamespace

from binance_platform.application.trend_pool_service import resolve_runtime_trend_pool


class TrendPoolServiceTests(unittest.TestCase):
    def test_resolve_runtime_trend_pool_uses_live_pool_when_runtime_payload_absent(self):
        runtime = SimpleNamespace(trend_pool_payload=None, now_utc="2026-03-29T00:00:00Z")
        raw_state = {"foo": "bar"}
        observed = {}

        result = resolve_runtime_trend_pool(
            runtime,
            raw_state,
            load_trend_universe_from_live_pool_fn=lambda **kwargs: observed.update(kwargs) or ({"ETHUSDT": {}}, {"ok": True}),
            get_trend_pool_contract_settings_fn=lambda: self.fail("should not read contract settings"),
            validate_trend_pool_payload_fn=lambda *_args, **_kwargs: self.fail("should not validate"),
            build_trend_pool_resolution_fn=lambda *_args, **_kwargs: self.fail("should not build resolution"),
            translate_fn=lambda key, **kwargs: key,
        )

        self.assertEqual(result, ({"ETHUSDT": {}}, {"ok": True}))
        self.assertEqual(observed, {"state": raw_state, "now_utc": "2026-03-29T00:00:00Z"})

    def test_resolve_runtime_trend_pool_builds_resolution_for_valid_runtime_payload(self):
        runtime = SimpleNamespace(trend_pool_payload={"version": "v1"}, now_utc="2026-03-29T00:00:00Z")
        observed = {}

        result = resolve_runtime_trend_pool(
            runtime,
            raw_state={},
            load_trend_universe_from_live_pool_fn=lambda **kwargs: self.fail("should not load live pool"),
            get_trend_pool_contract_settings_fn=lambda: {
                "max_age_days": 45,
                "acceptable_modes": ("core_major",),
                "expected_pool_size": 5,
            },
            validate_trend_pool_payload_fn=lambda payload, **kwargs: observed.update(
                {"payload": payload, "validate_kwargs": kwargs}
            )
            or {"ok": True},
            build_trend_pool_resolution_fn=lambda validated, **kwargs: observed.update(
                {"validated": validated, "build_kwargs": kwargs}
            )
            or {"symbol_map": {"ETHUSDT": {"base_asset": "ETH"}}, "source_kind": "fresh_upstream"},
            translate_fn=lambda key, **kwargs: key,
        )

        self.assertEqual(
            result,
            (
                {"ETHUSDT": {"base_asset": "ETH"}},
                {"symbol_map": {"ETHUSDT": {"base_asset": "ETH"}}, "source_kind": "fresh_upstream"},
            ),
        )
        self.assertEqual(observed["payload"], {"version": "v1"})
        self.assertEqual(observed["validate_kwargs"]["source_label"], "runtime:trend_pool_payload")
        self.assertEqual(observed["build_kwargs"]["messages"], ["trend_pool_loaded_runtime_payload"])

    def test_resolve_runtime_trend_pool_raises_for_invalid_runtime_payload(self):
        runtime = SimpleNamespace(trend_pool_payload={"version": "broken"}, now_utc="2026-03-29T00:00:00Z")

        with self.assertRaisesRegex(ValueError, "bad payload"):
            resolve_runtime_trend_pool(
                runtime,
                raw_state={},
                load_trend_universe_from_live_pool_fn=lambda **kwargs: self.fail("should not load live pool"),
                get_trend_pool_contract_settings_fn=lambda: {
                    "max_age_days": 45,
                    "acceptable_modes": ("core_major",),
                    "expected_pool_size": 5,
                },
                validate_trend_pool_payload_fn=lambda *_args, **_kwargs: {"ok": False, "errors": ["bad payload"]},
                build_trend_pool_resolution_fn=lambda *_args, **_kwargs: self.fail("should not build resolution"),
                translate_fn=lambda key, **kwargs: key,
            )


if __name__ == "__main__":
    unittest.main()
