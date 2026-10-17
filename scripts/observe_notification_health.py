"""Correlate existing monitor observations after its existing cloud auth step."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notifications import operational, unified_notifications as unified  # noqa: E402


def observe(workflow, execution):
    if not unified.enabled():
        return
    if workflow.get("status") == "healthy":
        operational.recover_workflow(workflow)
    elif workflow.get("status") in {"alert", "unavailable"}:
        operational.notify_workflow(workflow, "")
    if execution.get("status") in {"alert", "unavailable"}:
        missing = execution.get("reports_returned") == 0
        category = "receipt_missing" if missing else "receipt_unacceptable"
        unified.external_notice(lambda: operational.send_configured(operational.message(category)),
            kind=category, reason="execution_receipt")


def main():
    if not unified.enabled():
        return 0
    try:
        workflow = json.loads(Path(os.environ["NOTIFICATION_WORKFLOW_ASSESSMENT_PATH"]).read_text())
        execution = json.loads(Path(os.environ["NOTIFICATION_EXECUTION_ASSESSMENT_PATH"]).read_text())
        observe(workflow, execution)
        return 0
    except Exception:
        print(json.dumps({"notification_status": "blocked", "reason": "notification_observations_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
