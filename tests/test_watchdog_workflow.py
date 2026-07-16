from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "watchdog.yml"
RUNTIME_WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
QSL = ROOT / "qsl.toml"
IDENTITY_NAMES = (
    "GCP_PROJECT_ID",
    "GCP_WORKLOAD_IDENTITY_PROVIDER",
    "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT",
)
EXPECTED_DIGEST_PATTERN = re.compile(r'EXPECTED_OIDC_IDENTITY_SHA256="([0-9a-f]{64})"')


def _identity_digest(values: dict[str, str]) -> str:
    canonical = b"\0".join(values[name].encode("utf-8") for name in IDENTITY_NAMES)
    return hashlib.sha256(canonical).hexdigest()


def _preflight_script(workflow_text: str) -> str:
    step = workflow_text.index("Validate deployment identity configuration")
    run_marker = "        run: |\n"
    start = workflow_text.index(run_marker, step) + len(run_marker)
    lines: list[str] = []
    for line in workflow_text[start:].splitlines():
        if line.startswith("      - "):
            break
        if line.startswith("          "):
            lines.append(line[10:])
        elif not line:
            lines.append("")
        else:
            break
    return "\n".join(lines)


def _run_preflight(script: str, values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(values)
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _project_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def _dependency(prefix: str) -> str:
    for dep in _project_dependencies():
        if dep.startswith(prefix):
            return dep
    raise AssertionError(f"{PYPROJECT} does not pin {prefix}")


class WatchdogWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_text = WORKFLOW.read_text(encoding="utf-8")

    def test_watchdog_uses_repository_variables_before_remote_actions(self) -> None:
        text = self.workflow_text

        self.assertIn("id-token: write", text)
        for name in (
            "GCP_PROJECT_ID",
            "GCP_WORKLOAD_IDENTITY_PROVIDER",
            "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT",
        ):
            self.assertIn(f"{name}: ${{{{ vars.{name} }}}}", text)
        self.assertIn(
            "for name in GCP_PROJECT_ID GCP_WORKLOAD_IDENTITY_PROVIDER "
            "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT; do",
            text,
        )
        self.assertIn(
            'echo "::error::Required repository variable ${name} is not configured."',
            text,
        )
        preflight = text.index("- name: Validate deployment identity configuration")
        checkout = text.index("- uses: actions/checkout@v6")
        auth = text.index("- name: Authenticate to Google Cloud")
        self.assertLess(preflight, checkout)
        self.assertLess(preflight, auth)
        self.assertIn("uses: google-github-actions/auth@v3", text)
        self.assertIn("workload_identity_provider: ${{ env.GCP_WORKLOAD_IDENTITY_PROVIDER }}", text)
        self.assertIn("service_account: ${{ env.GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT }}", text)
        self.assertIn("WATCHDOG_MAX_AGE_SECONDS: ${{ vars.WATCHDOG_MAX_AGE_SECONDS || '4500' }}", text)

    def test_oidc_identity_digest_is_fixed_and_shared(self) -> None:
        scripts = (
            _preflight_script(RUNTIME_WORKFLOW.read_text(encoding="utf-8")),
            _preflight_script(self.workflow_text),
        )
        digests: list[str] = []

        for script in scripts:
            matches = EXPECTED_DIGEST_PATTERN.findall(script)
            self.assertEqual(len(matches), 1)
            digests.extend(matches)
            self.assertIn("readonly EXPECTED_OIDC_IDENTITY_SHA256=", script)
            self.assertIn("set -euo pipefail", script)
            self.assertNotIn("set -x", script)
            self.assertIn(r"printf '%s\0%s\0%s'", script)
            self.assertLess(script.index('"$GCP_PROJECT_ID"'), script.index('"$GCP_WORKLOAD_IDENTITY_PROVIDER"'))
            self.assertLess(
                script.index('"$GCP_WORKLOAD_IDENTITY_PROVIDER"'),
                script.index('"$GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT"'),
            )

        self.assertEqual(len(set(digests)), 1)

    def test_oidc_identity_digest_fails_closed_without_value_output(self) -> None:
        baseline = {
            "GCP_PROJECT_ID": "synthetic-project",
            "GCP_WORKLOAD_IDENTITY_PROVIDER": "synthetic-provider",
            "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT": "synthetic-service-account",
        }
        expected = _identity_digest(baseline)

        for workflow in (RUNTIME_WORKFLOW, WORKFLOW):
            script = _preflight_script(workflow.read_text(encoding="utf-8"))
            script, replacements = EXPECTED_DIGEST_PATTERN.subn(
                f'EXPECTED_OIDC_IDENTITY_SHA256="{expected}"',
                script,
            )
            self.assertEqual(replacements, 1)
            baseline_result = _run_preflight(script, baseline)
            self.assertEqual(baseline_result.returncode, 0)
            baseline_output = baseline_result.stdout + baseline_result.stderr
            self.assertNotIn(expected, baseline_output)
            for value in baseline.values():
                self.assertNotIn(value, baseline_output)

            candidates: list[dict[str, str]] = []
            for name in IDENTITY_NAMES:
                empty = dict(baseline)
                empty[name] = ""
                candidates.append(empty)

                modified = dict(baseline)
                modified[name] = f"{modified[name]}-modified"
                candidates.append(modified)

            for left, right in ((0, 1), (0, 2), (1, 2)):
                swapped = dict(baseline)
                swapped[IDENTITY_NAMES[left]], swapped[IDENTITY_NAMES[right]] = (
                    swapped[IDENTITY_NAMES[right]],
                    swapped[IDENTITY_NAMES[left]],
                )
                candidates.append(swapped)
            candidates.append(
                {
                    IDENTITY_NAMES[0]: baseline[IDENTITY_NAMES[0]] + baseline[IDENTITY_NAMES[1]],
                    IDENTITY_NAMES[1]: baseline[IDENTITY_NAMES[2]],
                    IDENTITY_NAMES[2]: baseline[IDENTITY_NAMES[0]],
                }
            )

            for candidate in candidates:
                result = _run_preflight(script, candidate)
                self.assertNotEqual(result.returncode, 0)
                output = result.stdout + result.stderr
                self.assertNotIn(expected, output)
                for value in candidate.values():
                    if value:
                        self.assertNotIn(value, output)

    def test_watchdog_installs_locked_internal_dependency(self) -> None:
        text = self.workflow_text

        self.assertIn("python -m pip install --upgrade pip uv", text)
        self.assertIn("uv sync --frozen --no-dev", text)
        self.assertIn("uv run --no-sync python - <<'PY'", text)

    def test_runtime_workflow_uses_cached_uv_environment(self) -> None:
        text = RUNTIME_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('LOCK_FILE="uv.lock"', text)
        self.assertIn('HASH_FILE="${CACHE_ROOT}/uv.lock.sha256"', text)
        self.assertIn('"$VENV_PATH/bin/python" -m ensurepip --upgrade', text)
        self.assertIn('"$VENV_PATH/bin/python" -m pip install --upgrade pip uv', text)
        self.assertIn('export UV_PROJECT_ENVIRONMENT="$VENV_PATH"', text)
        self.assertIn('UV_BIN="$VENV_PATH/bin/uv"', text)
        self.assertIn('env UV_PROJECT_ENVIRONMENT="$VENV_PATH" "$UV_BIN" sync --frozen --no-dev', text)
        self.assertNotIn('"$PYTHON_BIN" -m pip install --upgrade pip uv', text)
        self.assertIn('"$LOCK_FILE unchanged; reusing cached venv."', text)

    def test_runtime_workflow_exposes_strategy_artifact_variables(self) -> None:
        text = RUNTIME_WORKFLOW.read_text(encoding="utf-8")

        for name in (
            "STRATEGY_ARTIFACT_FILE",
            "STRATEGY_ARTIFACT_MANIFEST_FILE",
            "STRATEGY_ARTIFACT_FIRESTORE_COLLECTION",
            "STRATEGY_ARTIFACT_FIRESTORE_DOCUMENT",
            "STRATEGY_ARTIFACT_MAX_AGE_DAYS",
            "STRATEGY_ARTIFACT_ACCEPTABLE_MODES",
            "STRATEGY_ARTIFACT_EXPECTED_SIZE",
            "STRATEGY_ARTIFACT_ALLOW_NEW_ENTRIES_ON_DEGRADED",
        ):
            self.assertIn(f"{name}: ${{{{ vars.{name} }}}}", text)

    def test_watchdog_reads_firestore_heartbeat_with_supported_qpk_api(self) -> None:
        text = self.workflow_text

        self.assertIn("from quant_platform_kit.common.health import HealthMonitor, is_heartbeat_fresh", text)
        self.assertIn("heartbeat = HealthMonitor().read()", text)
        self.assertIn("alive = is_heartbeat_fresh(heartbeat, max_age_seconds)", text)
        self.assertNotIn(".check_alive()", text)

    def test_watchdog_qpk_pin_matches_lockfile(self) -> None:
        requirement = _dependency("quant-platform-kit @ ")
        lock = LOCK.read_text(encoding="utf-8")
        revision = requirement.rsplit("@", maxsplit=1)[1]

        self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertIn(f"QuantPlatformKit.git?rev={revision}#{revision}", lock)

    def test_qsl_qpk_pin_matches_manifest(self) -> None:
        requirement = _dependency("quant-platform-kit @ ")
        qsl = tomllib.loads(QSL.read_text(encoding="utf-8"))

        self.assertEqual(
            qsl["qsl"]["requires"]["quant_platform_kit"],
            requirement.rsplit("@", maxsplit=1)[1],
        )

    def test_crypto_strategies_pin_matches_qpk_health_dependency(self) -> None:
        requirement = _dependency("crypto-strategies @ ")
        lock = LOCK.read_text(encoding="utf-8")

        self.assertIn("CryptoStrategies.git?rev=ef78312d7653095f585c4f75d45bf765bedc2751", lock)
        self.assertIn("@ef78312d7653095f585c4f75d45bf765bedc2751", requirement)
        self.assertNotIn("@eb7bf665c5199f7f075af61ef5c86171eea1f057", lock)


if __name__ == "__main__":
    unittest.main()
