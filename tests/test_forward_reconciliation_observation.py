"""Forward report regressions; every identity and observation is invented."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

import pytest

def build_forward_report(*args, **kwargs):
    # Import inside each test so an unimplemented report path records individual
    # adverse failures, rather than preventing the entire module's collection.
    from application.forward_reconciliation import build_forward_report as build

    return build(*args, **kwargs)


START = "2026-11-03T12:00:00Z"
END = "2026-11-04T12:00:00Z"
WALLETS = ("spot", "funding", "flexible_earn", "locked_earn")
SURFACES = ("fills", "deposits", "withdrawals", "rewards", "convert", "dust",
            "transfers", "flexible_lifecycle", "locked_lifecycle")


def receipt(at, count=1):
    return {"binding": "synthetic-source-1", "started_at": at, "finished_at": at,
            "source_as_of": at, "stable_revision": "synthetic-revision-1",
            "pages": [{"number": 1, "size": 100, "count": count, "total": count,
                       "binding": "synthetic-source-1", "revision": "synthetic-revision-1",
                       "started_at": at, "finished_at": at}]}


def snapshot(at):
    return {"account_id": "binance-paper-mock-001", "binding": "synthetic-source-1",
            "as_of": at, "wallets": {
                w: {"balances": {"BTC": "0", "USDT": "100" if w == "spot" else "0"},
                    "receipt": receipt(at, 2)} for w in WALLETS}}


def seal_receipts(f):
    """Simulate source receipt creation; call only when constructing new evidence."""
    import hashlib

    def seal(r, rows):
        offset = 0
        for page in r["pages"]:
            page_rows = rows[offset:offset + page["count"]]
            offset += page["count"]
            page["content_sha256"] = hashlib.sha256(json.dumps(
                page_rows, sort_keys=True, separators=(",", ":"),
                ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()

    for name in ("opening", "closing"):
        if f[name]:
            for w in f[name]["wallets"].values():
                if w:
                    rows = w.get("native_rows")
                    if rows is None:
                        rows = [{"asset": a, "quantity": q} for a, q in sorted(w["balances"].items())]
                    seal(w["receipt"], rows)
    surfaces = {"fill": "fills", "deposit": "deposits", "withdrawal": "withdrawals",
                "reward": "rewards", "transfer": "transfers", "convert": "convert", "dust": "dust",
                "flexible_subscription": "flexible_lifecycle", "flexible_redemption": "flexible_lifecycle",
                "locked_subscription": "locked_lifecycle", "locked_redemption": "locked_lifecycle"}
    for surface, r in f["event_receipts"].items():
        seal(r, [e for e in f["events"] if surfaces.get(e["kind"]) == surface])
    return f


def fixture():
    return seal_receipts({"environment": "synthetic", "account_id": "binance-paper-mock-001",
            "binding": "synthetic-source-1", "anchor_at": START,
            "previous_cutoff": START, "cutoff": END,
            "opening": snapshot(START), "closing": snapshot(END),
            "events": [], "event_receipts": {
                s: {**receipt(END, 0), "start_exclusive": START, "end_inclusive": END,
                    "universe": "all_assets"} for s in SURFACES}})


def event(kind, *, asset="USDT", quantity="10", **fields):
    return {"id": "synthetic-event-1", "kind": kind, "at": "2026-11-04T01:00:00Z",
            "status": "SUCCESS", "asset": asset, "quantity": quantity, **fields}


def transfer(f):
    f["events"] = [event("transfer", source_wallet="spot", destination_wallet="funding")]
    f["closing"]["wallets"]["spot"]["balances"]["USDT"] = "90"
    f["closing"]["wallets"]["funding"]["balances"]["USDT"] = "10"
    f["event_receipts"]["transfers"] = {**f["event_receipts"]["transfers"],
                                          "pages": receipt(END)["pages"]}
    seal_receipts(f)


def test_complete_synthetic_match_is_only_informational():
    f = fixture()
    original = deepcopy(f)
    report = build_forward_report(f)
    assert report["status"] == "MATCHED_SYNTHETIC"
    assert report["runtime_status"] == "NOT_PROVEN"
    assert report["policy"] == {"purpose": "observation_only", "no_order": True,
                                 "execution_authority_granted": False}
    assert report["halt_required"] is False
    assert f == original
    assert report["wallets"]["funding"]["opening"] == {"BTC": "0", "USDT": "0"}


@pytest.mark.parametrize("wallet", ["funding", "locked_earn"])
def test_unknown_wallet_is_null_unreconciled_never_zero(wallet):
    f = fixture()
    f["opening"]["wallets"][wallet] = None
    report = build_forward_report(f)
    assert report["status"] == "UNRECONCILED"
    assert report["wallets"][wallet]["opening"] is None
    assert report["wallets"][wallet]["status"] == "UNRECONCILED"
    assert report["combined"] is None


def test_internal_transfer_has_two_native_legs_and_no_external_inflow():
    f = fixture()
    transfer(f)
    report = build_forward_report(f)
    assert report["status"] == "MATCHED_SYNTHETIC"
    assert report["combined"]["USDT"]["movement"] == "0"
    assert sorted(x["quantity"] for x in report["legs"]) == ["-10", "10"]


@pytest.mark.parametrize("problem", ["duplicate", "missing_leg", "unknown_destination",
                                    "pending", "outside_window", "count_missing"])
def test_transfer_adversaries_cannot_match(problem):
    f = fixture()
    transfer(f)
    if problem == "duplicate":
        f["events"].append(deepcopy(f["events"][0]))
        f["event_receipts"]["transfers"]["pages"][0].update(count=2, total=2)
    elif problem == "missing_leg":
        f["closing"]["wallets"]["funding"]["balances"]["USDT"] = "0"
    elif problem == "unknown_destination":
        f["events"][0]["destination_wallet"] = "margin"
    elif problem == "pending":
        f["events"][0]["status"] = "PENDING"
    elif problem == "outside_window":
        f["events"][0]["at"] = START
    else:
        del f["event_receipts"]["transfers"]
    assert build_forward_report(f)["status"] == "UNRECONCILED"


@pytest.mark.parametrize("kind", ["flexible_subscription", "flexible_redemption",
                                  "locked_subscription", "locked_redemption"])
def test_earn_lifecycle_is_paired_once(kind):
    f = fixture()
    wallet = "flexible_earn" if kind.startswith("flexible") else "locked_earn"
    source, destination = ("spot", wallet) if kind.endswith("subscription") else (wallet, "spot")
    f["opening"]["wallets"][source]["balances"]["USDT"] = "100"
    f["closing"]["wallets"][source]["balances"]["USDT"] = "90"
    f["closing"]["wallets"][destination]["balances"]["USDT"] = str(
        int(f["opening"]["wallets"][destination]["balances"]["USDT"]) + 10)
    f["events"] = [event(kind, source_wallet=source, destination_wallet=destination)]
    surface = "flexible_lifecycle" if kind.startswith("flexible") else "locked_lifecycle"
    f["event_receipts"][surface]["pages"] = receipt(END)["pages"]
    seal_receipts(f)
    report = build_forward_report(f)
    assert report["status"] == "MATCHED_SYNTHETIC"
    assert report["combined"]["USDT"]["movement"] == "0"


@pytest.mark.parametrize("kind,surface", [("fill", "fills"), ("convert", "convert"), ("dust", "dust")])
def test_exchange_uses_exact_quote_and_third_asset_fee(kind, surface):
    f = fixture()
    f["events"] = [{"id": "synthetic-trade-1", "kind": kind, "at": "2026-11-04T01:00:00Z",
                    "status": "SUCCESS", "wallet": "spot", "from_asset": "USDT",
                    "from_quantity": "10.000000000000000001", "to_asset": "BTC",
                    "to_quantity": "0.000000000000000001", "fee_asset": "BNB",
                    "fee_quantity": "0.000000000000000001", "fee_semantics": "additional_debit"}]
    for name in ("opening", "closing"):
        for w in WALLETS:
            f[name]["wallets"][w]["balances"]["BNB"] = "1" if w == "spot" else "0"
            f[name]["wallets"][w]["receipt"]["pages"][0].update(count=3, total=3)
    f["closing"]["wallets"]["spot"]["balances"].update(
        USDT="89.999999999999999999", BTC="0.000000000000000001", BNB="0.999999999999999999")
    f["event_receipts"][surface]["pages"] = receipt(END)["pages"]
    seal_receipts(f)
    report = build_forward_report(f)
    assert report["status"] == "MATCHED_SYNTHETIC"
    assert len(report["legs"]) == 3


def test_new_asset_requires_explicit_opening_coverage_and_retains_zero():
    f = fixture()
    f["events"] = [event("deposit", asset="NEW", wallet="spot")]
    f["event_receipts"]["deposits"]["pages"] = receipt(END)["pages"]
    f["closing"]["wallets"]["spot"]["balances"]["NEW"] = "10"
    f["closing"]["wallets"]["spot"]["receipt"]["pages"][0].update(count=3, total=3)
    assert build_forward_report(f)["status"] == "UNRECONCILED"
    for name in ("opening", "closing"):
        for w in WALLETS:
            f[name]["wallets"][w]["balances"].setdefault("NEW", "0")
            f[name]["wallets"][w]["receipt"]["pages"][0].update(count=3, total=3)
    seal_receipts(f)
    assert build_forward_report(f)["status"] == "MATCHED_SYNTHETIC"


@pytest.mark.parametrize("problem", ["missing", "full", "changing", "source_time", "mixed_time",
                                    "page_binding", "page_count", "partial", "universe", "digest_missing",
                                    "digest_changed"])
def test_missing_or_mixed_page_provenance_stays_unreconciled(problem):
    f = fixture()
    r = f["event_receipts"]["fills"]
    if problem == "missing":
        r["pages"] = []
    elif problem == "full":
        r["pages"][0].update(count=100, total=101)
    elif problem == "changing":
        r["pages"][0]["revision"] = "synthetic-changed"
    elif problem == "source_time":
        r["source_as_of"] = None
    elif problem == "mixed_time":
        f["closing"]["wallets"]["spot"]["receipt"]["source_as_of"] = START
    elif problem == "page_binding":
        r["pages"][0]["binding"] = "synthetic-other-source"
    elif problem == "page_count":
        r["pages"][0]["count"] = True
    elif problem == "partial":
        del f["event_receipts"]["locked_lifecycle"]
    elif problem == "universe":
        r["universe"] = "strategy_symbols"
    elif problem == "digest_missing":
        del r["pages"][0]["content_sha256"]
    else:
        r["pages"][0]["content_sha256"] = "0" * 64
    assert build_forward_report(f)["status"] == "UNRECONCILED"


def test_forward_anchor_needs_no_history_and_never_claims_midnight():
    f = fixture()
    report = build_forward_report(f)
    assert report["status"] == "MATCHED_SYNTHETIC"
    assert report["anchor_at"] == START
    assert "opening_before_anchor_missing" not in report["reason_codes"]
    f["anchor_at"] = "2026-11-02T12:00:00Z"
    assert build_forward_report(f)["status"] == "UNRECONCILED"


@pytest.mark.parametrize("change", ["anchor", "opening", "binding", "account", "withdrawal", "unsupported"])
def test_missing_inputs_identity_and_unsupported_semantics_fail_closed(change):
    f = fixture()
    if change == "anchor":
        f["anchor_at"] = None
    elif change == "opening":
        f["opening"] = None
    elif change == "binding":
        f["closing"]["binding"] = "synthetic-other-source"
    elif change == "account":
        f["closing"]["account_id"] = "binance-paper-mock-other"
    elif change == "withdrawal":
        f["events"] = [event("withdrawal", wallet="spot", fee_semantics="unknown")]
    else:
        f["events"] = [event("unsupported_locked_product")]
    report = build_forward_report(f)
    assert report["status"] == "UNRECONCILED"
    assert report["halt_required"] is False
    assert report["policy"]["execution_authority_granted"] is False


def test_replay_recomputes_from_current_inputs_and_does_not_trust_saved_match():
    f = fixture()
    assert build_forward_report(f)["status"] == "MATCHED_SYNTHETIC"
    f["status"] = "MATCHED_SYNTHETIC"
    f["closing"]["wallets"]["funding"] = None
    assert build_forward_report(json.loads(json.dumps(f)))["status"] == "UNRECONCILED"


def test_public_envelope_omits_all_private_inputs():
    from application.forward_reconciliation import forward_envelope
    f = fixture()
    f["closing"]["wallets"]["spot"]["receipt"]["pages"][0]["revision"] = "sensitive-private-value"
    text = json.dumps(forward_envelope(build_forward_report(f)))
    for private in (f["account_id"], f["binding"], "sensitive-private-value", "balances", "100"):
        assert private not in text
    assert "UNRECONCILED" in text


def test_cli_forward_fixture_is_offline_create_only_and_redacted(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(fixture()))
    command = [sys.executable, "scripts/collect_reconciliation_observation.py", "--forward-fixture",
               str(source), "--report-dir", str(tmp_path / "reports")]
    run = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    envelope = json.loads(run.stdout)
    assert envelope["status"] == "MATCHED_SYNTHETIC"
    assert "binance-paper-mock-001" not in run.stdout
    report = tmp_path / "reports" / "forward-synthetic-report.json"
    saved = report.read_bytes()
    again = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert again.returncode != 0
    assert report.read_bytes() == saved


def test_non_synthetic_input_is_rejected_without_echoing_identity():
    f = fixture()
    f["environment"] = "live"
    with pytest.raises(ValueError, match="^forward_synthetic_input_required$"):
        build_forward_report(f)


def test_private_api_requires_separate_binding_and_never_grants_authority():
    f = fixture()
    f["environment"] = "private_observation"
    with pytest.raises(ValueError, match="^forward_private_binding_required$"):
        build_forward_report(f)
    with pytest.raises(ValueError, match="^forward_private_binding_required$"):
        build_forward_report(f, expected_account_id="binance-paper-mock-other", expected_binding=f["binding"])
    report = build_forward_report(f, expected_account_id=f["account_id"], expected_binding=f["binding"])
    assert report["status"] == "MATCHED_OBSERVATION"
    assert report["policy"]["execution_authority_granted"] is False
    assert report["runtime_status"] == "NOT_PROVEN"


@pytest.mark.parametrize("wallet,rows", [
    ("spot", [{"asset": "BTC", "free": "0", "locked": "0"},
              {"asset": "USDT", "free": "90", "locked": "10"}]),
    ("funding", [{"asset": "BTC", "free": "0", "locked": "0", "freeze": "0", "withdrawing": "0"},
                 {"asset": "USDT", "free": "5", "locked": "2", "freeze": "3", "withdrawing": "0"}]),
    ("flexible_earn", [{"asset": "USDT", "productId": "synthetic-product-1", "totalAmount": "7"},
                       {"asset": "USDT", "productId": "synthetic-product-2", "totalAmount": "3"}]),
    ("locked_earn", [{"asset": "USDT", "positionId": "synthetic-position-1", "amount": "7"},
                     {"asset": "USDT", "positionId": "synthetic-position-2", "amount": "3"}])])
def test_native_inventory_keeps_components_positions_and_zero_rows(wallet, rows):
    f = fixture()
    for name, at in (("opening", START), ("closing", END)):
        f[name]["wallets"][wallet] = {"native_rows": deepcopy(rows), "receipt": receipt(at, len(rows))}
    seal_receipts(f)
    report = build_forward_report(f)
    assert report["wallets"][wallet]["opening"]["USDT"] == ("100" if wallet == "spot" else "10")
    assert report["inputs"]["opening"]["wallets"][wallet]["native_rows"] == rows
    if wallet in ("spot", "funding"):
        assert report["wallets"][wallet]["opening"]["BTC"] == "0"
    else:
        # A missing asset is not an observed zero, even on an otherwise complete page.
        assert report["wallets"][wallet]["status"] == "UNRECONCILED"


@pytest.mark.parametrize("problem", ["duplicate", "partial", "withdrawing", "unsupported"])
def test_native_inventory_unknown_semantics_are_not_zero(problem):
    f = fixture()
    wallet = "locked_earn" if problem == "unsupported" else "funding"
    rows = ([{"asset": "USDT", "positionId": "synthetic-unsupported", "amount": "1", "unsupported": True}]
            if problem == "unsupported" else
            [{"asset": "USDT", "free": "1", "locked": "0", "freeze": "0", "withdrawing": "0"}])
    if problem == "duplicate":
        rows *= 2
    elif problem == "partial":
        del rows[0]["freeze"]
    elif problem == "withdrawing":
        rows[0]["withdrawing"] = "1"
    f["opening"]["wallets"][wallet] = {"native_rows": rows, "receipt": receipt(START, len(rows))}
    report = build_forward_report(f)
    assert report["wallets"][wallet]["opening"] is None
    assert report["status"] == "UNRECONCILED"


def test_spot_fill_projection_requires_native_quote_and_preserves_symbol_identity():
    from application.forward_reconciliation import project_spot_fill
    row = {"symbol": "BTCUSDT", "id": 1, "time": 1793754000000, "isBuyer": True,
           "qty": "0.01", "quoteQty": "10.001", "price": "1000",
           "commission": "0.001", "commissionAsset": "BNB"}
    projected = project_spot_fill(row, base_asset="BTC", quote_asset="USDT")
    assert projected["from_quantity"] == "10.001"
    assert projected["to_quantity"] == "0.01"
    assert projected["fee_asset"] == "BNB"
    assert projected["id"] == "BTCUSDT:1"
    row["symbol"] = "BTCUSDC"
    assert project_spot_fill(row, base_asset="BTC", quote_asset="USDC")["id"] != projected["id"]
    del row["quoteQty"]
    with pytest.raises(ValueError, match="quantity_invalid"):
        project_spot_fill(row, base_asset="BTC", quote_asset="USDC")


def test_concurrent_processes_have_one_create_only_winner_and_restart_does_not_overwrite(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(fixture()))
    command = [sys.executable, "scripts/collect_reconciliation_observation.py", "--forward-fixture",
               str(source), "--report-dir", str(tmp_path / "reports")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _: subprocess.run(command, cwd=root, capture_output=True, text=True), range(2)))
    assert sorted(run.returncode for run in runs) == [0, 2]
    report = tmp_path / "reports" / "forward-synthetic-report.json"
    original = report.read_bytes()
    f = fixture()
    f["closing"]["wallets"]["spot"]["balances"]["USDT"] = "999"
    source.write_text(json.dumps(f))
    run = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert run.returncode == 2
    assert report.read_bytes() == original
    assert run.stderr.strip() == "mock_input_invalid"


def test_cli_blocks_runtime_imports_and_network_and_retains_no_authority(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(fixture()))
    code = '''
import importlib.abc, runpy, socket, sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'binance', 'google', 'quant_platform_kit', 'main', 'infra', 'runtime_support', 'live_services'} or fullname in {'application.broker_reconciliation', 'application.reconciliation_recovery'}:
            raise AssertionError('runtime import forbidden')
sys.meta_path.insert(0, NoRuntime())
def blocked(*args, **kwargs):
    raise AssertionError('network forbidden')
socket.socket.connect = blocked
socket.create_connection = blocked
sys.argv = ['collect_reconciliation_observation.py', '--forward-fixture', sys.argv[1], '--report-dir', sys.argv[2]]
runpy.run_path('scripts/collect_reconciliation_observation.py', run_name='__main__')
'''
    run = subprocess.run([sys.executable, "-c", code, str(source), str(tmp_path / "reports")],
                         cwd=root, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["policy"]["execution_authority_granted"] is False
    assert payload["halt_required"] is False


@pytest.mark.parametrize("problem", ["duplicate_key", "private_cli", "wrong_type", "bad_json"])
def test_cli_invalid_inputs_fail_nonzero_with_fixed_diagnostics(problem, tmp_path):
    root = Path(__file__).resolve().parents[1]
    f = fixture()
    if problem == "private_cli":
        f["environment"] = "private_observation"
    raw = json.dumps(f)
    if problem == "duplicate_key":
        raw = raw.replace('"USDT": "100"', '"USDT": "100", "USDT": "0"')
    elif problem == "wrong_type":
        raw = '[]'
    elif problem == "bad_json":
        raw = '{"sensitive-private-value":'
    source = tmp_path / "synthetic.json"
    source.write_text(raw)
    run = subprocess.run([sys.executable, "scripts/collect_reconciliation_observation.py", "--forward-fixture",
                          str(source), "--report-dir", str(tmp_path / "reports")],
                         cwd=root, capture_output=True, text=True)
    assert run.returncode == 2
    assert run.stdout == ""
    assert run.stderr.strip() == "mock_input_invalid"


def test_multiple_pages_bind_every_row_and_reject_extra_or_reordered_pages():
    f = fixture()
    for name, at in (("opening", START), ("closing", END)):
        r = f[name]["wallets"]["spot"]["receipt"]
        page = r["pages"][0]
        page.update(size=1, count=1)
        r["pages"].append({**page, "number": 2})
    seal_receipts(f)
    assert build_forward_report(f)["status"] == "MATCHED_SYNTHETIC"
    f["closing"]["wallets"]["spot"]["receipt"]["pages"].reverse()
    assert build_forward_report(f)["status"] == "UNRECONCILED"


def test_inventory_content_tampering_does_not_reuse_coverage_receipt():
    f = fixture()
    f["opening"]["wallets"]["funding"]["balances"]["USDT"] = "10"
    f["closing"]["wallets"]["funding"]["balances"]["USDT"] = "10"
    report = build_forward_report(f)
    assert report["wallets"]["funding"]["opening"] is None
    assert report["status"] == "UNRECONCILED"


@pytest.mark.parametrize("kind,surface", [("deposit", "deposits"), ("reward", "rewards"),
                                        ("withdrawal", "withdrawals")])
def test_native_external_movements_include_explicit_withdrawal_fee(kind, surface):
    f = fixture()
    withdrawal = kind == "withdrawal"
    f["events"] = [event(kind, wallet="spot", fee_asset="USDT", fee_quantity="1",
                         fee_semantics="additional_debit")]
    f["closing"]["wallets"]["spot"]["balances"]["USDT"] = "89" if withdrawal else "110"
    f["event_receipts"][surface]["pages"] = receipt(END)["pages"]
    seal_receipts(f)
    report = build_forward_report(f)
    assert report["status"] == "MATCHED_SYNTHETIC"
    assert report["combined"]["USDT"]["movement"] == ("-11" if withdrawal else "10")
