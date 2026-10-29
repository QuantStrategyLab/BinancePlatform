# Exact-pin CI contract

The `exact-pin` job installs only `uv sync --frozen --extra test` in a separate
GitHub-hosted runner. It does not check out sibling repositories or apply editable
overrides. The existing `test` job remains the candidate integration check,
including its same-named sibling branch support.

The opt-in installed-dependency test checks both QPK and CryptoStrategies:

- The declared full SHA agrees with the resolved `uv.lock` Git source.
- The real installed `direct_url.json` names that repository and commit, with no
  editable/path install.
- The imported module belongs to the distribution's installed file list, so a
  sibling source checkout cannot stand in for the pinned package.

`BINANCE_EXACT_PIN_TESTS=1` disables the runtime test module's local sibling-path
convenience. The job reuses the real catalog/runtime tests, including member-risk
approval and rejection for candidate mismatch, incomplete portfolio snapshots,
expired mandates and research-only mandates in live mode. All inputs are
synthetic; no broker account, cloud secret, execution grant or deployment is used.
These tests establish the current pinned member-risk semantics. They do not
claim that a legacy 20% cost-loss stop is enforced under member-risk approval.
Observation-only reporting added in a separate PR is outside this check's scope.

Local reproduction in a clean checkout:

```sh
uv sync --frozen --extra test
BINANCE_EXACT_PIN_TESTS=1 PYTHONPATH=. uv run --no-sync python -m pytest -q \
  tests/test_exact_pin_install.py tests/test_strategy_runtime.py
```

The installed-dependency test deliberately skips in ordinary candidate tests,
where editable dependency overrides are permitted. Missing or different Git
provenance fails the exact-pin job instead of being treated as verified.
