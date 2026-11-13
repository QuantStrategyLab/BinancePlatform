"""BinancePlatform live strategy orchestration entrypoint."""

import http_routes as _http_routes
import notifications_runtime as _notifications_runtime
import runtime_wiring as _runtime_wiring
import strategy_plugins_runtime as _strategy_plugins_runtime

def _export(module):
    globals().update({key: value for key, value in vars(module).items() if not key.startswith("__")})

_export(_runtime_wiring)
_export(_notifications_runtime)
_export(_strategy_plugins_runtime)
_export(_http_routes)

if __name__ == "__main__":
    main()
