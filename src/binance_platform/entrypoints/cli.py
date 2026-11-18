"""CLI entrypoint helpers for BinancePlatform."""

from __future__ import annotations

from binance_platform.application.cycle_service import run_live_cycle


def run_cli_entrypoint(
    *,
    runtime_builder,
    execute_cycle,
    output_printer=print,
    exit_fn=None,
):
    return run_live_cycle(
        runtime_builder=runtime_builder,
        execute_cycle=execute_cycle,
        output_printer=output_printer,
        exit_fn=exit_fn,
    )

