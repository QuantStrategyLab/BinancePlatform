import math
import time


def get_total_balance(client, asset, *, log_buffer=None, append_log_fn=None, balance_error_cls=RuntimeError):
    total = 0.0
    spot_error = None
    try:
        spot_info = client.get_asset_balance(asset=asset)
        total += float(spot_info["free"]) + float(spot_info["locked"])
    except Exception as exc:
        spot_error = exc
        if append_log_fn is not None:
            append_log_fn(log_buffer, f"⚠️ {asset} spot balance lookup failed: {exc}")
    try:
        earn_positions = client.get_simple_earn_flexible_product_position(asset=asset)
        if earn_positions and "rows" in earn_positions and len(earn_positions["rows"]) > 0:
            total += float(earn_positions["rows"][0]["totalAmount"])
    except Exception as exc:
        if append_log_fn is not None:
            append_log_fn(log_buffer, f"⚠️ {asset} earn balance lookup failed: {exc}")
    if spot_error is not None:
        raise balance_error_cls(f"{asset} spot balance unavailable: {spot_error}")
    return total


def ensure_asset_available(
    client,
    asset,
    required_amount,
    *,
    tg_token,
    tg_chat_id,
    send_tg_msg_fn,
    sleep_fn=time.sleep,
):
    try:
        spot_free = float(client.get_asset_balance(asset=asset)["free"])
        if spot_free >= required_amount:
            return True

        shortfall = required_amount - spot_free
        earn_positions = client.get_simple_earn_flexible_product_position(asset=asset)

        if earn_positions and "rows" in earn_positions and len(earn_positions["rows"]) > 0:
            row = earn_positions["rows"][0]
            product_id = row["productId"]
            earn_free = float(row["totalAmount"])

            if earn_free > 0:
                redeem_amt = min(shortfall * 1.001, earn_free)
                redeem_amt = round(redeem_amt, 8)
                client.redeem_simple_earn_flexible_product(productId=product_id, amount=redeem_amt)
                send_tg_msg_fn(tg_token, tg_chat_id, f"🔄 [Execution] spot {asset} short, redeeming from earn: {redeem_amt}")
                sleep_fn(3)
                return True
    except Exception as exc:
        send_tg_msg_fn(tg_token, tg_chat_id, f"⚠️ [Execution] {asset} redeem failed: {exc}")
    return False


def manage_usdt_earn_buffer(client, target_buffer, *, tg_token, tg_chat_id, log_buffer, append_log_fn):
    try:
        asset = "USDT"
        spot_free = float(client.get_asset_balance(asset=asset)["free"])

        earn_list = client.get_simple_earn_flexible_product_list(asset=asset)
        if not earn_list or "rows" not in earn_list or len(earn_list["rows"]) == 0:
            return
        product_id = earn_list["rows"][0]["productId"]

        if spot_free > target_buffer + 5.0:
            excess = round(spot_free - target_buffer, 4)
            if excess >= 0.1:
                client.subscribe_simple_earn_flexible_product(productId=product_id, amount=excess)
                append_log_fn(log_buffer, f"📥 [Cash manager] excess spot balance subscribed to earn: ${excess:.2f}")
        elif spot_free < target_buffer - 5.0:
            shortfall = round(target_buffer - spot_free, 4)
            earn_positions = client.get_simple_earn_flexible_product_position(asset=asset)
            if earn_positions and "rows" in earn_positions and len(earn_positions["rows"]) > 0:
                earn_free = float(earn_positions["rows"][0]["totalAmount"])
                if earn_free > 0:
                    redeem_amt = min(shortfall, earn_free)
                    redeem_amt = round(redeem_amt, 8)
                    client.redeem_simple_earn_flexible_product(productId=product_id, amount=redeem_amt)
                    append_log_fn(log_buffer, f"📤 [Cash manager] spot buffer low, redeeming to spot: ${redeem_amt:.2f}")
    except Exception as exc:
        append_log_fn(log_buffer, f"⚠️ USDT earn buffer maintenance failed: {exc}")


def format_qty(client, symbol, qty):
    try:
        info = client.get_symbol_info(symbol)
        step_size = float([f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"][0])
        precision = int(round(-math.log(step_size, 10), 0))
        return round(math.floor(qty / step_size) * step_size, precision)
    except Exception:
        return round(math.floor(qty * 10000) / 10000, 4)
