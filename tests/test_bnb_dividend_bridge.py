from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from application.broker_reconciliation import collect_bnb_dividend_quantity
from application.earn_accrual import prepare_forward_earn_state
from tests.test_forward_earn_accounting import materials


START = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 12, 14, 1, tzinfo=timezone.utc)


def _row(**changes):
    row = {
        "id": 7,
        "tranId": 8,
        "asset": "BNB",
        "divTime": int(END.timestamp() * 1000),
        "amount": "0.00000001",
        "direction": 1,
    }
    row.update(changes)
    return row


class Client:
    def __init__(self, response):
        self.response = response

    def _request_margin_api(self, method, path, **kwargs):
        assert method == "get"
        assert path == "asset/assetDividend"
        assert kwargs["signed"] is True
        return self.response


def test_dividend_bridge_accepts_right_boundary_and_returns_private_quantity():
    result = collect_bnb_dividend_quantity(
        Client({"total": 1, "rows": [_row()]}), start=START, end=END
    )

    assert result["quantity"] == Decimal("0.00000001")
    assert result["record_count"] == 1
    assert result["direction_values"] == (1,)
    assert result["identities"] == ((7, 8, int(END.timestamp() * 1000)),)


@pytest.mark.parametrize(
    "changes",
    [
        {"divTime": int(START.timestamp() * 1000)},
        {"asset": "USDT"},
        {"direction": 2},
        {"direction": True},
        {"amount": "0"},
        {"id": 7, "tranId": 8},
    ],
)
def test_dividend_bridge_rejects_boundary_wrong_asset_direction_amount_and_duplicate(changes):
    rows = [_row(**changes)]
    if changes == {"id": 7, "tranId": 8}:
        rows = [_row(), _row()]
    with pytest.raises(ValueError, match="bnb_dividend"):
        collect_bnb_dividend_quantity(Client({"total": len(rows), "rows": rows}), start=START, end=END)


@pytest.mark.parametrize(
    "response",
    [
        {"rows": [_row()]},
        {"total": 500, "rows": [_row()] * 500},
        {"total": 2, "rows": [_row()]},
    ],
)
def test_dividend_bridge_rejects_missing_or_incomplete_page(response):
    with pytest.raises(ValueError, match="bnb_dividend"):
        collect_bnb_dividend_quantity(Client(response), start=START, end=END)


def test_prepare_forward_earn_state_consumes_only_verified_dividend_quantity():
    state, current, cash = materials()
    current["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.1"
    cash["bnb_dividend_quantity"] = "0.00000001"

    updated = prepare_forward_earn_state(state, current, cash)

    assert updated["earn_accounted_net_changes"] == {"USDT": "0", "BNB": "0"}
    assert updated.get("daily_external_principal_usdt", 0) == 0


def test_unmatched_dividend_quantity_does_not_consume_forward_state():
    state, current, cash = materials()
    current["assets"]["BNB"]["products"]["BNB001"]["realtime_rewards"] = "0.1"
    current["assets"]["BNB"]["products"]["BNB001"]["total"] = "2.00000002"
    current["assets"]["BNB"]["quantity"] = "3.00000002"
    cash["bnb_dividend_quantity"] = "0.00000001"
    before = deepcopy(state)

    with pytest.raises(ValueError, match="earn_quantity_change_unexplained"):
        prepare_forward_earn_state(state, current, cash)

    assert state == before
