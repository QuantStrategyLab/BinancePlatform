import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import sys
import threading
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

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            self.send_response(status)
            if status == 302:
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/redirect"
                )
            self.end_headers()
            self.wfile.write(b"mock-error-response-body")

        def do_GET(self) -> None:
            requests.append(
                {"path": self.path, "authorization": self.headers.get("Authorization")}
            )
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env.update(
            {
                "GITHUB_API_URL": f"http://127.0.0.1:{server.server_port}/api/v3",
                "GITHUB_TOKEN": "synthetic-dispatch-token",
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", _dispatch_script()],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
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
