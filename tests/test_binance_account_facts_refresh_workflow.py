import json
import os
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from pathlib import Path
import re
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/refresh-binance-account-facts.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _dispatch_script() -> str:
    workflow = _workflow_text()
    match = re.search(
        r"(?ms)^        run: \|\n(?P<body>(?:^          .*\n)+)", workflow
    )
    assert match, "dispatcher must keep one inline, independently testable script"
    return textwrap.dedent(match.group("body"))


def _run_against_mock(status: int) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    requests: list[dict] = []

    class Opener:
        def open(self, request, timeout):
            requests.append({"path": urlsplit(request.full_url).path,
                "authorization": request.get_header("Authorization"),
                "body": json.loads(request.data)})
            if status not in (200, 204):
                raise HTTPError(request.full_url, status, "mock-error-response-body", {}, None)
            return self

        def __enter__(self):
            self.status = status
            return self

        def __exit__(self, *args):
            return False

    def opener_factory(redirect_handler):
        # Exercise the actual redirect policy without opening any socket.
        assert redirect_handler().redirect_request(None, None, 302, "mock", {}, "http://example.invalid") is None
        return Opener()

    stdout, stderr, code = StringIO(), StringIO(), 0
    with patch.dict(os.environ, {"GITHUB_API_URL": "http://example.invalid/api/v3",
            "GITHUB_TOKEN": "synthetic-dispatch-token"}), \
            patch("urllib.request.build_opener", side_effect=opener_factory), \
            redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            exec(compile(_dispatch_script(), "<workflow-dispatch-fake>", "exec"), {"__name__": "__main__"})
        except SystemExit as error:
            code = error.code
    result = subprocess.CompletedProcess([], code, stdout.getvalue(), stderr.getvalue())
    return result, requests


class BinanceAccountFactsRefreshWorkflowTests(unittest.TestCase):
    def test_dispatch_is_gated_fixed_and_minimally_privileged(self) -> None:
        workflow = _workflow_text()
        gate = workflow.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0]

        assert 'cron: "17 6 * * *"' in workflow
        assert "default: false" in workflow
        assert "type: boolean" in workflow
        assert "inputs.enabled == true" in gate
        assert "vars.BINANCE_ACCOUNT_FACTS_REFRESH_ENABLED == 'true'" in gate
        assert "github.repository == 'QuantStrategyLab/BinancePlatform'" in gate
        assert "github.ref == 'refs/heads/main'" in gate
        assert "actions: write" in workflow
        assert "contents: read" in workflow
        assert "contents: write" not in workflow
        assert "id-token:" not in workflow
        assert "BINANCE_API_KEY" not in workflow
        assert "BINANCE_API_SECRET" not in workflow
        assert "BINANCE_ACCOUNT_FACTS_SYNC_TOKEN" not in workflow
        assert "RUNTIME_TARGET_ENABLED" not in workflow
        assert "BINANCE_DRY_RUN" not in workflow
        assert "source_mode:" not in workflow
        assert "workflow:" not in workflow.split("    inputs:", 1)[1].split("permissions:", 1)[0]
        assert "/repos/QuantStrategyLab/BinancePlatform/actions/" in workflow
        assert "workflows/binance-account-facts.yml/dispatches" in workflow
        assert '"ref": "runtime-production"' in workflow
        assert '"source_mode": "direct_read"' in workflow
        assert "while " not in workflow

    def test_dispatch_posts_only_fixed_target_once_and_accepts_success_codes(self) -> None:
        for status in (200, 204):
            with self.subTest(status=status):
                result, requests = _run_against_mock(status)

                assert result.returncode == 0, result.stderr
                assert result.stdout.strip() == "account_facts_dispatch=accepted"
                assert result.stderr == ""
                assert requests == [
                    {
                        "path": (
                            "/api/v3/repos/QuantStrategyLab/BinancePlatform/actions/"
                            "workflows/binance-account-facts.yml/dispatches"
                        ),
                        "authorization": "Bearer synthetic-dispatch-token",
                        "body": {
                            "ref": "runtime-production",
                            "inputs": {
                                "enabled": True,
                                "source_mode": "direct_read",
                            },
                        },
                    }
                ]

    def test_ambiguous_dispatch_failure_is_redacted_and_never_retried(self) -> None:
        result, requests = _run_against_mock(500)

        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.strip() == "account_facts_dispatch_outcome_unknown"
        assert "synthetic-dispatch-token" not in result.stdout + result.stderr
        assert len(requests) == 1

    def test_redirect_is_not_followed_or_disclosed(self) -> None:
        result, requests = _run_against_mock(302)

        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.strip() == "account_facts_dispatch_outcome_unknown"
        assert "synthetic-dispatch-token" not in result.stdout + result.stderr
        assert "mock-error-response-body" not in result.stdout + result.stderr
        assert "127.0.0.1" not in result.stdout + result.stderr
        assert len(requests) == 1
        assert requests[0]["path"].endswith("/binance-account-facts.yml/dispatches")
