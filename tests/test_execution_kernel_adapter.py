"""N13: Binance adapter around QPK execution_kernel T2/T3 (UNKNOWN deny paths)."""

from __future__ import annotations

import unittest

from application.execution_kernel_adapter import (
    consult_t2_unknown_new_cycle,
    consult_t3_deny_blind_retry,
)


class ExecutionKernelAdapterTests(unittest.TestCase):
    def test_t2_unknown_new_cycle_denies(self):
        decision = consult_t2_unknown_new_cycle()
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.reason_code, "submission_uncertain")
        self.assertFalse(decision.allowed)

    def test_t3_uncertain_unreconciled_blind_retry_denies(self):
        decision = consult_t3_deny_blind_retry(
            transport_uncertain=True,
            reconciled=False,
        )
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.reason_code, "reconcile_required")
        self.assertFalse(decision.allowed)

    def test_t3_reconciled_allows_kernel(self):
        """If already reconciled, T3 allows; callers must not request blind retry here."""
        decision = consult_t3_deny_blind_retry(
            transport_uncertain=True,
            reconciled=True,
        )
        self.assertEqual(decision.decision, "allow")
        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
