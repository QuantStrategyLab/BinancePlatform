import unittest
from pathlib import Path


class UvDependencyWorkflowTests(unittest.TestCase):
    def test_pyproject_declares_runtime_and_test_dependencies(self) -> None:
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("dependencies = [", pyproject)
        self.assertIn("quant-platform-kit @ git+https://github.com/QuantStrategyLab/", pyproject)
        self.assertIn("crypto-strategies @ git+https://github.com/QuantStrategyLab/", pyproject)
        self.assertIn("[project.optional-dependencies]", pyproject)
        self.assertIn("test = [", pyproject)
        self.assertIn("[tool.uv]", pyproject)
        self.assertIn('override-dependencies = [', pyproject)

    def test_ci_runtime_and_watchdog_use_uv_lock(self) -> None:
        ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        runtime = Path(".github/workflows/main.yml").read_text(encoding="utf-8")
        watchdog = Path(".github/workflows/watchdog.yml").read_text(encoding="utf-8")
        lockfile = Path("uv.lock").read_text(encoding="utf-8")

        self.assertTrue(lockfile.startswith("version = "))
        self.assertIn("uv sync --frozen --extra test", ci)
        self.assertIn("uv run --no-sync ruff check --exclude external .", ci)
        self.assertIn("/tmp/qpk-pin-guard/check_qpk_pin_consistency.py", ci)
        self.assertIn("https://raw.githubusercontent.com/QuantStrategyLab/QuantPlatformKit/main/QPK_PIN", ci)
        self.assertIn("uv lock --check", ci)
        self.assertIn('LOCK_FILE="uv.lock"', runtime)
        self.assertIn('"$VENV_PATH/bin/python" -m ensurepip --upgrade', runtime)
        self.assertIn('"$VENV_PATH/bin/python" -m pip install --upgrade pip uv', runtime)
        self.assertIn('export UV_PROJECT_ENVIRONMENT="$VENV_PATH"', runtime)
        self.assertIn('UV_BIN="$VENV_PATH/bin/uv"', runtime)
        self.assertIn('"$UV_BIN" sync --frozen --no-dev', runtime)
        self.assertNotIn('"$PYTHON_BIN" -m pip install --upgrade pip uv', runtime)
        self.assertIn("python -m pip install --upgrade pip uv", watchdog)
        self.assertIn("uv sync --frozen --no-dev", watchdog)
        self.assertIn("uv run --no-sync python - <<'PY'", watchdog)
        self.assertIn(
            "QuantPlatformKit.git?rev=69a0256934d081b5ef309a885384b9eb9f62cf90#69a0256934d081b5ef309a885384b9eb9f62cf90",
            lockfile,
        )
        self.assertNotIn(
            "QuantPlatformKit.git?rev=69a0256934d081b5ef309a885384b9eb9f62cf90#53b2ca73a5a50257b5d1a3c769b75c40924e4ba6",
            lockfile,
        )
        self.assertNotIn("requirements-lock.txt", ci)
        self.assertNotIn("requirements.txt", ci)


if __name__ == "__main__":
    unittest.main()
