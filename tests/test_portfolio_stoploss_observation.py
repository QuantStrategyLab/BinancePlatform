"""Synthetic-only reporting tests. No exchange or notification calls."""
import ast
import json
from pathlib import Path
from collections.abc import Mapping
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from quant_platform_kit import PortfolioSnapshot, Position

from portfolio_stoploss_observation import observe_portfolio_stoploss, public_stoploss_observation


class PortfolioStoplossObservationTests(unittest.TestCase):
    def snapshot(self, positions=(), **kwargs):
        return PortfolioSnapshot(as_of=datetime.now(timezone.utc), total_equity=1000.0,
                                 positions=positions, **kwargs)

    def observe(self, snapshot, known=True, assessment=None):
        if assessment is None:
            assessment = {"scope": "MEMBER", "outcome": "PASS"}
        with patch("portfolio_stoploss_observation._reviewed_member_gate", return_value=known):
            return observe_portfolio_stoploss(snapshot, entrypoint=None, assessment=assessment)

    def test_cost_coverage_is_position_count_and_never_a_pass(self):
        for costs, status, count in [((None, None), "missing", 0),
                                     ((10.0, None), "partial", 1),
                                     ((10.0, 20.0), "complete", 2)]:
            with self.subTest(costs=costs):
                positions = tuple(Position("PRIVATE", 1.0, 25.0, cost) for cost in costs)
                result = self.observe(self.snapshot(positions))
                self.assertEqual(result["pnl_input_status"], status)
                self.assertEqual(result["cost_covered_position_count"], count)
                self.assertEqual(result["position_count"], 2)
                self.assertEqual(result["evaluation_status"], "not_evaluated")
                self.assertEqual(result["coverage_basis"], "strategy_member_position_count")
                self.assertNotIn("passed", json.dumps(result))

    def test_empty_is_not_full_cost_coverage(self):
        result = self.observe(self.snapshot())
        self.assertEqual(result["pnl_input_status"], "empty")
        self.assertEqual(result["position_count"], 0)

    def test_invalid_snapshot_and_nonfinite_values_are_unknown(self):
        snapshots = [None, SimpleNamespace(positions="bad"),
                     self.snapshot(metadata={"snapshot_validation_errors": ("PRIVATE",)})]
        for field in ("quantity", "market_value", "average_cost"):
            for bad in (True, float("nan"), float("inf"), "PRIVATE"):
                values = dict(symbol="PRIVATE", quantity=1.0, market_value=25.0, average_cost=10.0)
                values[field] = bad
                snapshots.append(self.snapshot((Position(**values),)))
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                result = self.observe(snapshot)
                self.assertEqual(result["pnl_input_status"], "invalid")
                self.assertIsNone(result["cost_covered_position_count"])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_unreviewed_gate_is_unknown_even_with_complete_costs(self):
        result = self.observe(self.snapshot((Position("PRIVATE", 1, 1, 1),)), known=False)
        self.assertEqual(result["evaluation_status"], "unknown")
        self.assertEqual(result["reason_code"], "gate_evaluation_evidence_unavailable")

    def test_public_projection_contains_unexpected_mapping_errors(self):
        class RaisingMapping(Mapping):
            def __iter__(self):
                return iter(())
            def __len__(self):
                return 0
            def __getitem__(self, key):
                raise RuntimeError("PRIVATE token")
        result = public_stoploss_observation(RaisingMapping())
        self.assertEqual(result["evaluation_status"], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_public_projection_is_strict_and_fail_closed(self):
        good = self.observe(self.snapshot())
        dirty = {**good, "account_id": "PRIVATE", "pnl": -0.42, "token": "SECRET"}
        self.assertEqual(public_stoploss_observation(dirty), good)
        for dirty in [None, {**good, "reason_code": "SECRET"},
                      {**good, "evaluation_status": "passed"},
                      {**good, "position_count": float("nan")},
                      {**good, "reason_code": []}]:
            result = public_stoploss_observation(dirty)
            self.assertEqual(result["evaluation_status"], "unknown")
            self.assertNotIn("SECRET", json.dumps(result))


class ReviewedGateTests(unittest.TestCase):
    def setUp(self):
        from strategy_runtime import load_research_only_strategy_runtime
        self.runtime = load_research_only_strategy_runtime("crypto_live_pool_rotation")
        self.assessment = {"contract_version": "qsl.risk_gate_assessment.v2",
                           "scope": "MEMBER", "outcome": "APPROVE"}

    def test_review_requires_exact_installed_revisions_and_gate_contract(self):
        from portfolio_stoploss_observation import _reviewed_member_gate, _REVIEWED_REVISIONS

        def installed(package):
            return SimpleNamespace(read_text=lambda _: json.dumps({
                "vcs_info": {"commit_id": _REVIEWED_REVISIONS[package]}}))

        with patch("portfolio_stoploss_observation.distribution", side_effect=installed):
            self.assertTrue(_reviewed_member_gate(self.runtime.entrypoint, self.assessment))
            for assessment in (None, {}, {**self.assessment, "scope": "ACCOUNT"},
                               {**self.assessment, "contract_version": "future"},
                               {**self.assessment, "outcome": []}):
                self.assertFalse(_reviewed_member_gate(self.runtime.entrypoint, assessment))
            custom = SimpleNamespace(manifest=self.runtime.entrypoint.manifest)
            self.assertFalse(_reviewed_member_gate(custom, self.assessment))
        for provenance in ({}, {"dir_info": {"editable": True}},
                           {"vcs_info": {"commit_id": "future"}}, []):
            with patch("portfolio_stoploss_observation.distribution", return_value=SimpleNamespace(
                read_text=lambda _: json.dumps(provenance)
            )):
                self.assertFalse(_reviewed_member_gate(self.runtime.entrypoint, self.assessment))

    def test_phase_projection_preserves_existing_risk_and_redacts_metadata(self):
        from application.cycle_service import _record_risk_diagnostics
        report = {"status": "ok", "diagnostics": {"existing": True}}
        observation = PortfolioStoplossObservationTests().observe(
            PortfolioStoplossObservationTests().snapshot())
        allocation = {"portfolio_stoploss_observation": {**observation, "token": "SECRET"},
                      "risk_assessment": self.assessment, "risk_flags": ("original",)}
        _record_risk_diagnostics(report, allocation, phase="pre_trade")
        _record_risk_diagnostics(report, {}, phase="post_trade")
        self.assertEqual(report["diagnostics"]["portfolio_stoploss"]["pre_trade"], observation)
        self.assertEqual(report["diagnostics"]["portfolio_stoploss"]["post_trade"]["evaluation_status"], "unknown")
        self.assertEqual(report["risk_assessment"], self.assessment)
        self.assertEqual(report["risk_outcome"], "APPROVE")
        self.assertEqual(report["risk_flags"], ["original"])
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["diagnostics"]["existing"])
        self.assertNotIn("SECRET", json.dumps(report))


class AllocationProjectionTests(unittest.TestCase):
    def test_main_allocation_whitelists_observation_without_changing_decision(self):
        # Compile the actual adapter function without starting main's runtime imports.
        source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_compute_portfolio_allocation")
        observation = PortfolioStoplossObservationTests().observe(
            PortfolioStoplossObservationTests().snapshot())
        decision = SimpleNamespace(diagnostics={"member_risk_assessment": {"outcome": "APPROVE"}},
                                   risk_flags=("original",))
        metrics = {"original_metric": 1}
        evaluation = SimpleNamespace(decision=decision, account_metrics=metrics, metadata={
            "portfolio_stoploss_observation": {**observation, "account_id": "PRIVATE", "pnl": 12},
            "secret": "PRIVATE",
        })
        mapped = []
        def mapper(value, *, account_metrics):
            mapped.append((value, account_metrics))
            return {"execution_permitted": True, "original": True}
        namespace = {"Mapping": Mapping, "public_stoploss_observation": public_stoploss_observation,
                     "_resolve_strategy_evaluation": lambda *args, **kwargs: evaluation,
                     "map_decision_to_allocation": mapper}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)
        result = namespace["_compute_portfolio_allocation"](*([None] * 9))
        self.assertEqual(mapped, [(decision, metrics)])
        self.assertEqual(result, {"execution_permitted": True, "original": True,
            "risk_assessment": {"outcome": "APPROVE"}, "risk_flags": ("original",),
            "portfolio_stoploss_observation": observation})
        self.assertNotIn("PRIVATE", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
