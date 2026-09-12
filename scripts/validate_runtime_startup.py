"""Load actual runtime configuration and strategy, without execution ports."""
from __future__ import annotations

import json
import os
import copy
import re
from collections.abc import Mapping
from pathlib import Path
import sys


_SAFE_STARTUP_REASONS = {
    "runtime_recovery_not_active": "runtime_recovery_not_active",
    "recovery_control_state_invalid": "recovery_control_state_invalid",
    "recovery_active_binding_invalid": "recovery_active_binding_invalid",
    "STRATEGY_PROFILE does not match RUNTIME_TARGET_JSON.strategy_profile": "startup_strategy_profile_conflict",
    "BINANCE_DRY_RUN does not match RUNTIME_TARGET_JSON.dry_run_only": "startup_dry_run_conflict",
    "full_cycle_requires_disabled_runtime": "full_cycle_requires_disabled_runtime",
    "full_cycle_risk_authority_missing": "full_cycle_risk_authority_missing",
    "full_cycle_state_loader_missing": "full_cycle_state_loader_missing",
}


class FullCycleValidationError(RuntimeError):
    def __init__(self, reason_code, *, failure_stage="result", error_type="RuntimeError"):
        self.reason_code = str(reason_code)
        self.failure_stage = str(failure_stage)
        self.error_type = str(error_type)
        super().__init__(self.reason_code)


_AUTHORITY_REASON_CODES = (
    ("authority source parameters are incomplete", "authority_source_incomplete"),
    ("authority file digest mismatch", "authority_file_digest_mismatch"),
    ("authority file must be a regular non-symlink file", "authority_file_invalid"),
    ("authority file size is invalid", "authority_file_invalid"),
    ("authority file cannot be read", "authority_file_unreadable"),
    ("authority file changed while reading", "authority_file_changed"),
    ("invalid authority JSON", "authority_json_invalid"),
    ("duplicate authority field", "authority_duplicate_field"),
    ("non-finite JSON value", "authority_non_finite_value"),
    ("authority fields are unsupported or incomplete", "authority_fields_invalid"),
    ("authority decision is not APPROVE", "authority_decision_not_approve"),
    ("authority scope is not LIVE", "authority_scope_not_live"),
    ("installed strategy revision is unavailable", "strategy_revision_unavailable"),
    ("strategy revision mismatch", "strategy_revision_mismatch"),
    ("runner revision is unavailable", "runner_revision_unavailable"),
    ("runner revision mismatch", "runner_revision_mismatch"),
    ("runner checkout has tracked modifications", "runner_checkout_dirty"),
    ("config digest mismatch", "config_digest_mismatch"),
    ("runtime target is missing", "runtime_target_missing"),
    ("runtime target identity is incomplete", "runtime_target_identity_incomplete"),
    ("runtime target account identity is ambiguous", "runtime_target_identity_ambiguous"),
    ("runtime target mismatch", "runtime_target_mismatch"),
    ("authority expired or not yet effective", "authority_expired_or_not_effective"),
)


def _authority_reason_code(exc):
    """Return only a fixed authority reason; never expose provider text."""
    try:
        from live_risk_authority import LiveRiskAuthorityError
    except ImportError:
        return None
    if not isinstance(exc, LiveRiskAuthorityError):
        return None
    text = str(exc)
    for marker, reason_code in _AUTHORITY_REASON_CODES:
        if marker in text:
            return reason_code
    return "authority_material_invalid"


def _error_type_name(exc):
    if isinstance(exc, ValueError):
        return "ValueError"
    if isinstance(exc, KeyError):
        return "KeyError"
    if isinstance(exc, TypeError):
        return "TypeError"
    if isinstance(exc, OSError):
        return "OSError"
    if isinstance(exc, RuntimeError):
        return "RuntimeError"
    return "RuntimeError"


def _wrap_full_cycle_error(exc, *, failure_stage, fallback_reason):
    if isinstance(exc, FullCycleValidationError):
        return exc
    reason_code = _authority_reason_code(exc)
    if reason_code is None and type(exc) is ValueError:
        reason_code = _SAFE_STARTUP_REASONS.get(str(exc))
    return FullCycleValidationError(
        reason_code or fallback_reason,
        failure_stage=failure_stage,
        error_type=_error_type_name(exc),
    )


def _full_cycle_failure_reason(report):
    """Project a cycle report to one stable, non-sensitive validator reason."""
    if report.get("risk_outcome") == "REJECT":
        return "full_cycle_risk_rejected"
    if report.get("status") == "aborted":
        return "full_cycle_aborted"
    failure = report.get("diagnostics", {}).get("cycle_failure", {})
    stage = failure.get("stage")
    if stage in {"client_connect", "market_snapshot"}:
        return "full_cycle_broker_read_failed"
    if stage in {"fuel_execution", "trend_execution", "btc_execution", "earn_execution"}:
        return "full_cycle_funding_or_execution_blocked"
    if report.get("status") == "error":
        return "full_cycle_cycle_error"
    return "full_cycle_not_complete"


class _ReadOnlyBroker:
    """Expose only the broker reads needed by one validation cycle."""

    _MARGIN_READS = frozenset({
        "capital/deposit/hisrec",
        "capital/withdraw/history",
    })

    def __init__(self, client, *, symbols):
        self._client = client
        self._symbols = frozenset(symbols)
        self.read_counts = {}

    def _read(self, method, *args, **payload):
        self.read_counts[method] = self.read_counts.get(method, 0) + 1
        return getattr(self._client, method)(*args, **payload)

    def _asset(self, asset):
        if type(asset) is not str or asset not in self._assets:
            raise RuntimeError("full_cycle_broker_asset_forbidden")

    @property
    def _assets(self):
        assets = {"USDT", "BTC", "BNB"}
        assets.update(symbol.removesuffix("USDT") for symbol in self._symbols if symbol.endswith("USDT"))
        return assets

    def ping(self):
        return self._read("ping")

    def get_server_time(self):
        return self._read("get_server_time")

    def get_account(self):
        return self._read("get_account")

    def get_asset_balance(self, *, asset):
        self._asset(asset)
        return self._read("get_asset_balance", asset=asset)

    def get_simple_earn_flexible_product_position(self, *, asset=None, current=1, size=100):
        if asset is not None:
            self._asset(asset)
        if current != 1 or size != 100:
            raise RuntimeError("full_cycle_broker_checkpoint_forbidden")
        payload = {"current": current, "size": size}
        if asset is not None:
            payload["asset"] = asset
        return self._read("get_simple_earn_flexible_product_position", **payload)

    def get_simple_earn_flexible_product_list(self, *, asset):
        self._asset(asset)
        return self._read("get_simple_earn_flexible_product_list", asset=asset)

    def get_avg_price(self, *, symbol):
        if symbol not in self._symbols:
            raise RuntimeError("full_cycle_broker_symbol_forbidden")
        return self._read("get_avg_price", symbol=symbol)

    def get_symbol_info(self, symbol):
        if symbol not in self._symbols:
            raise RuntimeError("full_cycle_broker_symbol_forbidden")
        return self._read("get_symbol_info", symbol=symbol)

    def get_historical_klines(self, symbol, interval, lookback):
        if symbol not in self._symbols or interval != "1d":
            raise RuntimeError("full_cycle_broker_market_read_forbidden")
        match = re.fullmatch(r"([0-9]+) days ago UTC", str(lookback))
        if match is None or not 0 < int(match.group(1)) <= 420:
            raise RuntimeError("full_cycle_broker_market_window_forbidden")
        return self._read("get_historical_klines", symbol, interval, lookback)

    def _request_margin_api(self, method, path, *, signed, data):
        if method != "get" or signed is not True or path not in self._MARGIN_READS:
            raise RuntimeError("full_cycle_broker_request_forbidden")
        return self._read("_request_margin_api", method, path, signed=signed, data=dict(data))

    def __getattr__(self, _name):
        raise RuntimeError("full_cycle_broker_method_forbidden")


class _ForbiddenWrite:
    def __init__(self, reason):
        self.reason = reason
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        raise RuntimeError(self.reason)


class _SuppressedPerformanceMonitor:
    def __init__(self):
        self.calls = 0

    def record(self, *_args, **_kwargs):
        self.calls += 1


def validate_full_cycle(*, runtime_builder=None, client_connector=None):
    """Run one real-input cycle with every mutation port closed."""
    import importlib

    try:
        import main
    except Exception as exc:
        raise _wrap_full_cycle_error(
            exc, failure_stage="import", fallback_reason="full_cycle_import_failed"
        ) from None

    try:
        runtime = (runtime_builder or main.build_live_runtime)()
    except Exception as exc:
        raise _wrap_full_cycle_error(
            exc, failure_stage="build", fallback_reason="full_cycle_build_failed"
        ) from None
    if getattr(runtime, "standard_execution_permitted", True):
        raise FullCycleValidationError(
            "full_cycle_requires_disabled_runtime", failure_stage="build", error_type="ValueError"
        )
    if getattr(runtime, "risk_authority", None) is None:
        raise FullCycleValidationError(
            "full_cycle_risk_authority_missing", failure_stage="build", error_type="ValueError"
        )
    if not callable(getattr(runtime, "state_loader", None)):
        raise FullCycleValidationError(
            "full_cycle_state_loader_missing", failure_stage="state", error_type="ValueError"
        )

    try:
        snapshot = copy.deepcopy(runtime.state_loader(normalize=False))
    except Exception:
        raise FullCycleValidationError(
            "full_cycle_state_snapshot_unavailable", failure_stage="state"
        ) from None
    if not isinstance(snapshot, Mapping):
        raise FullCycleValidationError(
            "full_cycle_state_snapshot_unavailable", failure_stage="state"
        )

    runtime.dry_run = True
    runtime.state_loader = lambda *, normalize=False: copy.deepcopy(snapshot)
    runtime.state_writer = _ForbiddenWrite("full_cycle_state_write_forbidden")
    runtime.notifier = _ForbiddenWrite("full_cycle_notification_forbidden")
    runtime.state_owner_claim = _ForbiddenWrite("full_cycle_owner_claim_forbidden")
    runtime.state_owner_release = _ForbiddenWrite("full_cycle_owner_release_forbidden")

    mandate = getattr(runtime.risk_authority, "mandate", {})
    symbols = set(mandate.get("allowed_nonzero_assets", ())) if isinstance(mandate, Mapping) else set()
    symbols.update({"BTCUSDT", "BNBUSDT"})
    try:
        raw_client = (client_connector or main.qpk_connect_client)(
            runtime.api_key, runtime.api_secret, timeout=30,
        )
    except Exception:
        raise FullCycleValidationError(
            "full_cycle_broker_connect_failed", failure_stage="connect"
        ) from None
    broker = _ReadOnlyBroker(raw_client, symbols=symbols)
    runtime.client = broker

    try:
        common = importlib.import_module("crypto_strategies.entrypoints._common")
    except Exception as exc:
        raise _wrap_full_cycle_error(
            exc, failure_stage="import", fallback_reason="full_cycle_import_failed"
        ) from None
    import builtins
    previous_monitor = getattr(common, "_performance_monitor", None)
    had_health_monitor = "_qsl_health_monitor" in builtins.__dict__
    previous_health_monitor = builtins.__dict__.get("_qsl_health_monitor")
    monitor = _SuppressedPerformanceMonitor()
    common._performance_monitor = monitor
    builtins.__dict__.pop("_qsl_health_monitor", None)
    try:
        try:
            report = main.execute_cycle(runtime)
        except Exception as exc:
            raise _wrap_full_cycle_error(
                exc, failure_stage="cycle", fallback_reason="full_cycle_cycle_failed"
            ) from None
    finally:
        common._performance_monitor = previous_monitor
        if had_health_monitor:
            builtins.__dict__["_qsl_health_monitor"] = previous_health_monitor

    intents = report.get("state_write_intents")
    cycle_complete = isinstance(intents, list) and any(
        isinstance(item, Mapping) and item.get("reason") == "cycle_complete" for item in intents
    )
    if not cycle_complete:
        raise FullCycleValidationError(_full_cycle_failure_reason(report), failure_stage="result")
    if report.get("status") != "ok" or report.get("risk_outcome") != "APPROVE":
        raise FullCycleValidationError(_full_cycle_failure_reason(report), failure_stage="result")
    if report.get("execution_blocked_reason") not in {None, "runtime_target_disabled"}:
        raise FullCycleValidationError("full_cycle_execution_blocked", failure_stage="result")
    if report.get("side_effect_summary", {}).get("executed_call_count") != 0:
        raise FullCycleValidationError("full_cycle_side_effect_executed", failure_stage="result")
    if any(port.calls for port in (runtime.state_writer, runtime.notifier, runtime.state_owner_claim, runtime.state_owner_release)):
        raise FullCycleValidationError("full_cycle_write_port_called", failure_stage="result")
    return {
        "status": "passed",
        "validation_scope": "full_cycle_no_submit",
        "cycle_complete": True,
        "risk_outcome": "APPROVE",
        "broker_read_count": sum(broker.read_counts.values()),
        "suppressed_side_effect_count": report.get("side_effect_summary", {}).get("suppressed_call_count", 0),
        "suppressed_strategy_record_count": monitor.calls,
        "execution_permitted": False,
    }


def validate_startup():
    if os.getenv("RUNTIME_TARGET_ENABLED") != "false":
        raise ValueError("startup_validation_requires_disabled_runtime")
    if os.getenv("BINANCE_API_KEY") or os.getenv("BINANCE_API_SECRET"):
        raise ValueError("startup_validation_broker_credentials_forbidden")
    import main

    runtime = main.build_live_runtime()
    if runtime.standard_execution_permitted:
        raise ValueError("startup_validation_execution_permitted")
    memory_writes = []
    runtime.state_writer = lambda state: memory_writes.append(dict(state)) or True
    report = main.build_execution_report(runtime)
    cycle_state = main._load_cycle_state(runtime, report, False)
    if cycle_state is None:
        raise ValueError("startup_validation_state_load_failed")
    _state, resolution, runtime_trend_universe, _allow_new = cycle_state
    candidate_count = sum(
        not meta.get("valuation_only")
        for meta in runtime_trend_universe.values()
    )
    valuation_only_count = len(runtime_trend_universe) - candidate_count
    mandate = getattr(runtime, "mandate_provenance", None)
    candidate_identity = getattr(runtime, "candidate_risk_identity", None)
    risk_materials_present = bool(
        isinstance(mandate, Mapping) and candidate_identity is not None
    )
    return {"status": "passed", "strategy_profile": runtime.strategy_profile,
            "execution_permitted": False, "validation_only": True,
            "risk_materials_present": risk_materials_present,
            "validation_scope": "startup_only",
            "recovery_ready": False,
            "state_load_checked": True,
            "source_pool_symbol_count": len(resolution.get("symbols") or ()),
            "managed_asset_count": len(runtime_trend_universe) + 3,
            "strategy_candidate_count": candidate_count,
            "valuation_only_count": valuation_only_count}


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    full_cycle = "--full-cycle" in sys.argv[1:]
    try:
        result = validate_full_cycle() if full_cycle else validate_startup()
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        kind = _error_type_name(exc)
        # Exact application reasons only; never expose provider messages or values.
        reason = "full_cycle_validation_failed" if full_cycle else "runtime_startup_validation_failed"
        failure_stage = None
        if isinstance(exc, FullCycleValidationError):
            reason = exc.reason_code
            failure_stage = exc.failure_stage
            kind = exc.error_type
        if type(exc) is ValueError:
            reason = _SAFE_STARTUP_REASONS.get(str(exc), reason)
        output = {
            "status": "failed",
            "stage": "full_cycle_validation" if full_cycle else "runtime_startup_validation",
            "error_type": kind,
            "reason_code": reason,
        }
        if failure_stage is not None:
            output["failure_stage"] = failure_stage
        print(json.dumps(output, sort_keys=True))
        raise SystemExit(1) from None
