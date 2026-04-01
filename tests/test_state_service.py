import unittest
from types import SimpleNamespace

from application.state_service import append_trend_pool_source_logs, load_cycle_state
from infra.state_store import load_runtime_trade_state, save_runtime_trade_state


class StateServiceTests(unittest.TestCase):
    def test_load_cycle_state_marks_report_aborted_when_state_load_fails(self):
        report = {"status": "ok"}
        observed_errors = []

        result = load_cycle_state(
            SimpleNamespace(),
            report,
            allow_new_trend_entries_on_degraded=False,
            state_loader=lambda *, normalize: None,
            resolve_runtime_trend_pool=lambda *_args, **_kwargs: None,
            normalize_trade_state=lambda state: state,
            update_trend_pool_state=lambda *_args, **_kwargs: None,
            runtime_set_trade_state=lambda *_args, **_kwargs: None,
            get_runtime_trend_universe=lambda state: state,
            append_report_error=lambda report, message, stage: observed_errors.append((stage, message)),
            trend_universe_setter=lambda _value: None,
        )

        self.assertIsNone(result)
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(len(observed_errors), 1)
        self.assertEqual(observed_errors[0][0], "state_load")

    def test_load_cycle_state_refreshes_runtime_state_metadata(self):
        runtime = SimpleNamespace(name="runtime")
        report = {"status": "ok", "gating_summary": {}, "gating_events": []}
        observed = {"trend_universe": None, "persist_reasons": []}
        raw_state = {"foo": "bar"}
        normalized_state = {"normalized": True}
        trend_pool_resolution = {"degraded": True, "source_kind": "last_known_good"}
        runtime_trend_universe = {"ETHUSDT": {"base_asset": "ETH"}}

        result = load_cycle_state(
            runtime,
            report,
            allow_new_trend_entries_on_degraded=True,
            state_loader=lambda *, normalize: raw_state,
            resolve_runtime_trend_pool=lambda _runtime, _raw_state: (runtime_trend_universe, trend_pool_resolution),
            normalize_trade_state=lambda state: normalized_state if state is raw_state else None,
            update_trend_pool_state=lambda state, resolution: state.update(resolution_seen=resolution["source_kind"]),
            runtime_set_trade_state=lambda _runtime, _report, state, reason: observed["persist_reasons"].append(
                (reason, dict(state))
            ),
            get_runtime_trend_universe=lambda state: runtime_trend_universe if state is normalized_state else None,
            append_report_error=lambda *_args, **_kwargs: None,
            trend_universe_setter=lambda value: observed.__setitem__("trend_universe", value),
        )

        self.assertEqual(observed["trend_universe"], runtime_trend_universe)
        self.assertEqual(observed["persist_reasons"][0][0], "trend_pool_metadata_refresh")
        self.assertEqual(observed["persist_reasons"][0][1]["resolution_seen"], "last_known_good")
        self.assertEqual(
            result,
            (normalized_state, trend_pool_resolution, runtime_trend_universe, True),
        )

    def test_load_cycle_state_records_degraded_buy_pause_gate(self):
        report = {"status": "ok", "gating_summary": {}, "gating_events": []}

        result = load_cycle_state(
            SimpleNamespace(),
            report,
            allow_new_trend_entries_on_degraded=False,
            state_loader=lambda *, normalize: {"ok": True},
            resolve_runtime_trend_pool=lambda *_args, **_kwargs: (
                {"ETHUSDT": {"base_asset": "ETH"}},
                {"degraded": True, "source_kind": "last_known_good", "source": "last_known_good"},
            ),
            normalize_trade_state=lambda state: state,
            update_trend_pool_state=lambda *_args, **_kwargs: None,
            runtime_set_trade_state=lambda *_args, **_kwargs: None,
            get_runtime_trend_universe=lambda state: {"ETHUSDT": {"base_asset": "ETH"}},
            append_report_error=lambda *_args, **_kwargs: None,
            trend_universe_setter=lambda _value: None,
        )

        self.assertFalse(result[3])
        self.assertEqual(report["gating_summary"]["trend_buy_paused_degraded_mode"], 1)
        self.assertEqual(report["gating_events"][0]["category"], "trend")

    def test_append_trend_pool_source_logs_appends_all_lines(self):
        log_buffer = []

        append_trend_pool_source_logs(
            log_buffer,
            {"source_kind": "fresh_upstream"},
            allow_new_trend_entries=False,
            formatter=lambda resolution, *, allow_new_trend_entries: [
                resolution["source_kind"],
                f"allow_new={allow_new_trend_entries}",
            ],
            append_log_fn=lambda target, message: target.append(message),
        )

        self.assertEqual(log_buffer, ["fresh_upstream", "allow_new=False"])


class StateStoreTests(unittest.TestCase):
    def test_load_runtime_trade_state_uses_default_collection_document(self):
        observed = {}

        result = load_runtime_trade_state(
            normalize_fn=lambda value: value,
            default_state_factory=dict,
            loader_fn=lambda **kwargs: observed.update(kwargs) or {"ok": True},
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(observed["collection"], "strategy")
        self.assertEqual(observed["document"], "MULTI_ASSET_STATE")
        self.assertTrue(observed["normalize"])

    def test_save_runtime_trade_state_uses_default_collection_document(self):
        observed = {}

        save_runtime_trade_state(
            {"ok": True},
            normalize_fn=lambda value: value,
            saver_fn=lambda data, **kwargs: observed.update({"data": data, **kwargs}),
        )

        self.assertEqual(observed["data"], {"ok": True})
        self.assertEqual(observed["collection"], "strategy")
        self.assertEqual(observed["document"], "MULTI_ASSET_STATE")


if __name__ == "__main__":
    unittest.main()
