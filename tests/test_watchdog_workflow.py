from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "watchdog.yml"
RUNTIME_WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
QSL = ROOT / "qsl.toml"


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

    def test_watchdog_uses_binance_runtime_identity(self) -> None:
        text = self.workflow_text

        self.assertIn("id-token: write", text)
        self.assertIn(
            "GCP_WORKLOAD_IDENTITY_PROVIDER: "
            "projects/677468735457/locations/global/workloadIdentityPools/github-actions/providers/github-main",
            text,
        )
        self.assertIn(
            "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT: "
            "binance-platform-runtime@binancequant.iam.gserviceaccount.com",
            text,
        )
        self.assertIn("uses: google-github-actions/auth@v3", text)
        self.assertIn("workload_identity_provider: ${{ env.GCP_WORKLOAD_IDENTITY_PROVIDER }}", text)
        self.assertIn("service_account: ${{ env.GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT }}", text)
        self.assertIn("WATCHDOG_MAX_AGE_SECONDS: ${{ vars.WATCHDOG_MAX_AGE_SECONDS || '4500' }}", text)

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
