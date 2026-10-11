"""Thin adapter: map Binance SUBMISSION_UNKNOWN deny paths onto QPK execution_kernel T2/T3.

N13: consult-only / redundant fail-closed. Local raise still always wins.
Does not own claims or change allow / reconcile-success paths.
Does not touch FILLED_ACCOUNTING_PENDING (B13-b G3; BN-only).
"""

from __future__ import annotations

from quant_platform_kit.execution_kernel import (
    ExecutionGuardDecision,
    SubmissionCertainty,
    SubmissionRetrySnapshot,
    SubmissionUnknownSnapshot,
    deny_blind_retry_after_uncertain_transport,
    deny_new_cycle_when_submission_unknown,
)


def consult_t2_unknown_new_cycle() -> ExecutionGuardDecision:
    """Consult T2 when cycle entry finds SUBMISSION_UNKNOWN and will raise.

    Caller must still raise ExecutionIntegrityError("order_reconciliation_uncertain").
    """
    return deny_new_cycle_when_submission_unknown(
        SubmissionUnknownSnapshot(
            submission_certainty=SubmissionCertainty.UNKNOWN,
            requesting_new_cycle_submit=True,
        )
    )


def consult_t3_deny_blind_retry(
    *,
    transport_uncertain: bool = True,
    reconciled: bool = False,
) -> ExecutionGuardDecision:
    """Consult T3 on paths that already refuse blind retry after uncertain transport.

    Caller must still raise OrderReconciliationError("order_reconciliation_uncertain").
    Do not call on reconcile-success return paths.
    """
    return deny_blind_retry_after_uncertain_transport(
        SubmissionRetrySnapshot(
            transport_uncertain=bool(transport_uncertain),
            reconciled=bool(reconciled),
            requesting_blind_retry=True,
        )
    )
