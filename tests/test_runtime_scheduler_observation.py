from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from runtime_scheduler_observation import observe_runtime_scheduler_state


class RuntimeSchedulerObservationTests(unittest.TestCase):
    expected_environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "QuantStrategyLab/BinancePlatform",
        "GITHUB_REF": "refs/heads/main",
        "RUNNER_NAME": "binance-quant-runner",
    }

    @staticmethod
    def command_runner(*, crontab, daemon):
        def run(command, **kwargs):
            if command == ["crontab", "-l"]:
                result = crontab
            elif command == ["systemctl", "is-active", "cron"]:
                result = daemon
            else:
                raise AssertionError(f"unexpected command: {command}")
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(command, *result)

        return run

    def observe(self, *, crontab, daemon="active\n"):
        run = self.command_runner(crontab=crontab, daemon=(0, daemon, ""))
        with (
            patch("runtime_scheduler_observation.pwd.getpwuid", return_value=SimpleNamespace(pw_name="ubuntu")),
            patch("runtime_scheduler_observation.os.stat", return_value=SimpleNamespace(st_mode=0o100755)),
            patch("runtime_scheduler_observation.os.access", return_value=True),
        ):
            return observe_runtime_scheduler_state(environ=self.expected_environment, run_command=run)

    def test_exact_hourly_cron_and_active_daemon_are_enabled(self):
        state = self.observe(
            crontab=(
                0,
                "# comment\n0 * * * * /home/ubuntu/binance-quant/ops/dispatch-runtime.sh >> /tmp/runtime.log 2>&1\n",
                "",
            )
        )

        self.assertEqual(state, "enabled")

    def test_exact_cron_and_inactive_daemon_are_disabled(self):
        exact = "0 * * * * /home/ubuntu/binance-quant/ops/dispatch-runtime.sh\n"
        for daemon in ("inactive\n", "failed\n"):
            with self.subTest(daemon=daemon):
                run = self.command_runner(crontab=(0, exact, ""), daemon=(3, daemon, ""))
                with (
                    patch(
                        "runtime_scheduler_observation.pwd.getpwuid",
                        return_value=SimpleNamespace(pw_name="ubuntu"),
                    ),
                    patch("runtime_scheduler_observation.os.stat", return_value=SimpleNamespace(st_mode=0o100755)),
                    patch("runtime_scheduler_observation.os.access", return_value=True),
                ):
                    state = observe_runtime_scheduler_state(
                        environ=self.expected_environment,
                        run_command=run,
                    )
                self.assertEqual(state, "disabled")

    def test_missing_ambiguous_or_unreadable_scheduler_is_unknown(self):
        exact = "0 * * * * /home/ubuntu/binance-quant/ops/dispatch-runtime.sh"
        cases = (
            (0, "# no active entries\n", ""),
            (0, f"{exact}\n{exact}\n", ""),
            (0, "5 * * * * /home/ubuntu/binance-quant/ops/dispatch-runtime.sh\n", ""),
            (0, f"{exact} >> $(hostname).log 2>&1\n", ""),
            (0, f"{exact} >> /tmp/runtime%Y.log 2>&1\n", ""),
            (1, "", "redacted"),
        )
        for crontab in cases:
            with self.subTest(crontab=crontab):
                self.assertEqual(self.observe(crontab=crontab), "unknown")

        run = self.command_runner(
            crontab=subprocess.TimeoutExpired(["crontab", "-l"], 2),
            daemon=(0, "active\n", ""),
        )
        with (
            patch("runtime_scheduler_observation.pwd.getpwuid", return_value=SimpleNamespace(pw_name="ubuntu")),
            patch("runtime_scheduler_observation.os.stat", return_value=SimpleNamespace(st_mode=0o100755)),
            patch("runtime_scheduler_observation.os.access", return_value=True),
        ):
            self.assertEqual(
                observe_runtime_scheduler_state(environ=self.expected_environment, run_command=run),
                "unknown",
            )

        decode_failure = self.command_runner(
            crontab=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            daemon=(0, "active\n", ""),
        )
        with (
            patch("runtime_scheduler_observation.pwd.getpwuid", return_value=SimpleNamespace(pw_name="ubuntu")),
            patch("runtime_scheduler_observation.os.stat", return_value=SimpleNamespace(st_mode=0o100755)),
            patch("runtime_scheduler_observation.os.access", return_value=True),
        ):
            self.assertEqual(
                observe_runtime_scheduler_state(
                    environ=self.expected_environment,
                    run_command=decode_failure,
                ),
                "unknown",
            )

    def test_contradictory_daemon_exit_and_state_are_unknown(self):
        exact = "0 * * * * /home/ubuntu/binance-quant/ops/dispatch-runtime.sh\n"
        for daemon in ((0, "inactive\n", ""), (3, "active\n", "")):
            with self.subTest(daemon=daemon):
                run = self.command_runner(crontab=(0, exact, ""), daemon=daemon)
                with (
                    patch(
                        "runtime_scheduler_observation.pwd.getpwuid",
                        return_value=SimpleNamespace(pw_name="ubuntu"),
                    ),
                    patch("runtime_scheduler_observation.os.stat", return_value=SimpleNamespace(st_mode=0o100755)),
                    patch("runtime_scheduler_observation.os.access", return_value=True),
                ):
                    state = observe_runtime_scheduler_state(
                        environ=self.expected_environment,
                        run_command=run,
                    )
                self.assertEqual(state, "unknown")

    def test_unreviewed_host_context_does_not_run_commands(self):
        contexts = []
        for field in self.expected_environment:
            environment = dict(self.expected_environment)
            environment[field] = "unexpected"
            contexts.append(environment)

        for environment, user, is_file, executable in (
            *((context, "ubuntu", True, True) for context in contexts),
            (self.expected_environment, "other", True, True),
            (self.expected_environment, "ubuntu", False, True),
            (self.expected_environment, "ubuntu", True, False),
        ):
            with self.subTest(environment=environment, user=user, is_file=is_file, executable=executable):
                calls = []

                def run(*args, **kwargs):
                    calls.append((args, kwargs))
                    raise AssertionError("command must not run")

                mode = 0o100755 if is_file else 0o040755
                with (
                    patch(
                        "runtime_scheduler_observation.pwd.getpwuid",
                        return_value=SimpleNamespace(pw_name=user),
                    ),
                    patch("runtime_scheduler_observation.os.stat", return_value=SimpleNamespace(st_mode=mode)),
                    patch("runtime_scheduler_observation.os.access", return_value=executable),
                ):
                    state = observe_runtime_scheduler_state(environ=environment, run_command=run)
                self.assertEqual(state, "unknown")
                self.assertEqual(calls, [])
