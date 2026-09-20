import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class UvDependencyWorkflowTests(unittest.TestCase):
    def test_heartbeat_callers_install_and_use_locked_dependencies(self) -> None:
        for name in ("runtime-heartbeat.yml", "runtime-target-lifecycle.yml"):
            with self.subTest(workflow=name):
                workflow = Path(".github/workflows", name).read_text(encoding="utf-8")
                invocation = "uv run --no-sync python scripts/runtime_workflow_heartbeat.py"
                self.assertIn("astral-sh/setup-uv@", workflow)
                self.assertIn("uv sync --frozen --no-dev", workflow)
                self.assertIn(invocation, workflow)
                self.assertLess(workflow.index("uv sync --frozen --no-dev"), workflow.index(invocation))

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
        self.assertIn('tomllib.load(Path("qsl.toml").open("rb"))', ci)
        self.assertIn('requires.get("quant_platform_kit")', ci)
        self.assertIn('re.fullmatch(r"[0-9a-f]{40}", qpk_pin)', ci)
        self.assertIn("QuantPlatformKit/${qpk_pin}/scripts/check_qpk_pin_consistency.py", ci)
        self.assertNotIn("QuantPlatformKit/main/QPK_PIN", ci)
        self.assertIn("uv lock --check", ci)
        self.assertIn('LOCK_FILE="uv.lock"', runtime)
        self.assertIn('UV_TOOL_VENV="${CACHE_ROOT}/uv-tool"', runtime)
        self.assertIn('"$UV_TOOL_VENV/bin/python" -m ensurepip', runtime)
        self.assertNotIn('"$UV_TOOL_VENV/bin/python" -m ensurepip --upgrade', runtime)
        self.assertIn('"$UV_TOOL_VENV/bin/python" -m pip install pip uv', runtime)
        self.assertNotIn('"$UV_TOOL_VENV/bin/python" -m pip install --upgrade pip uv', runtime)
        self.assertNotIn('"$VENV_PATH/bin/python" -m pip install pip uv', runtime)
        self.assertNotIn('"$VENV_PATH/bin/python" -m pip install --upgrade pip uv', runtime)
        self.assertIn('export UV_PROJECT_ENVIRONMENT="$VENV_PATH"', runtime)
        self.assertIn('UV_BIN="$UV_TOOL_VENV/bin/uv"', runtime)
        self.assertNotIn('UV_BIN="$VENV_PATH/bin/uv"', runtime)
        self.assertIn('"$UV_BIN" sync --frozen --no-dev', runtime)
        self.assertIn('UV_VERSION_TEXT="$("$UV_BIN" --version)"', runtime)
        self.assertNotIn('"$PYTHON_BIN" -m pip install --upgrade pip uv', runtime)
        self.assertIn("python -m pip install --upgrade pip uv", watchdog)
        self.assertIn("uv sync --frozen --no-dev", watchdog)
        self.assertIn("uv run --no-sync python - <<'PY'", watchdog)
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"QuantPlatformKit\.git@([0-9a-f]{40})", pyproject)
        self.assertIsNotNone(match)
        qpk_pin = match.group(1)
        self.assertIn(f"QuantPlatformKit.git?rev={qpk_pin}#{qpk_pin}", lockfile)
        self.assertNotIn("requirements-lock.txt", ci)
        self.assertNotIn("requirements.txt", ci)

    def test_uv_outside_project_env_remains_callable_after_sync(self) -> None:
        """Regression for run 35540307399: uv used for sync must stay callable after it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "project"
            project_venv = root / "project-venv"
            tool_venv = root / "uv-tool"
            project_dir.mkdir()
            (project_dir / "pyproject.toml").write_text(
                textwrap.dedent(
                    """\
                    [project]
                    name = "uv-lifecycle-fixture"
                    version = "0.0.0"
                    requires-python = ">=3.11"
                    dependencies = []

                    [build-system]
                    requires = ["hatchling"]
                    build-backend = "hatchling.build"

                    [tool.hatch.build.targets.wheel]
                    packages = ["uv_lifecycle_fixture"]
                    """
                ),
                encoding="utf-8",
            )
            pkg = project_dir / "uv_lifecycle_fixture"
            pkg.mkdir()
            (pkg / "__init__.py").write_text('"""fixture package"""\n', encoding="utf-8")

            subprocess.run([sys.executable, "-m", "venv", str(tool_venv)], check=True)
            subprocess.run([sys.executable, "-m", "venv", str(project_venv)], check=True)
            uv_bin = tool_venv / "bin" / "uv"
            system_uv = shutil.which("uv")
            if system_uv:
                shutil.copy2(system_uv, uv_bin)
                uv_bin.chmod(0o755)
            else:
                tool_python = tool_venv / "bin" / "python"
                if not (tool_venv / "bin" / "pip").exists():
                    subprocess.run([str(tool_python), "-m", "ensurepip"], check=True)
                subprocess.run(
                    [str(tool_python), "-m", "pip", "install", "pip", "uv"],
                    check=True,
                )
            self.assertTrue(uv_bin.is_file(), "tool uv binary missing after install")
            self.assertNotEqual(
                uv_bin.resolve().parent,
                (project_venv / "bin").resolve(),
                "tool uv must live outside the project sync target",
            )

            sync_env = {
                **os.environ,
                "UV_PROJECT_ENVIRONMENT": str(project_venv),
            }
            subprocess.run(
                [str(uv_bin), "lock"],
                cwd=project_dir,
                env=sync_env,
                check=True,
            )
            subprocess.run(
                [str(uv_bin), "sync", "--frozen", "--no-dev"],
                cwd=project_dir,
                env=sync_env,
                check=True,
            )

            self.assertTrue(uv_bin.is_file(), "tool uv binary missing after project sync")
            version = subprocess.run(
                [str(uv_bin), "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("uv", version.stdout.lower())
            self.assertTrue(
                (project_venv / "bin" / "python").is_file(),
                "project environment should remain usable after sync",
            )

if __name__ == "__main__":
    unittest.main()
