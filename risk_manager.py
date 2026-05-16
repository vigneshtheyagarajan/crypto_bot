from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from config import API_KEY, API_SECRET, CAPITAL

client = Client(API_KEY, API_SECRET)

# ── Risk settings ─────────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT      = 0.03   # 3% of capital = max daily loss
RISK_PER_TRADE_PCT        = 0.01   # 1% of capital = max risk per trade
SL_PCT                    = 0.01   # 1% stop loss distance from entry
TARGET_RATIO              = 2.0    # 2:1 reward to risk
MAX_CAPITAL_PER_TRADE_PCT = 0.25   # max 25% of total capital in one trade
TRAIL_STOP_TRIGGER_PCT    = 0.010  # start trailing at 1.0% profit (raised from 0.5%)
TRAIL_STOP_DISTANCE_PCT   = 0.003  # trail at 0.3% distance from current price

# ── Capital ───────────────────────────────────────────────────────────────────

def get_capital():
    """Read capital from .env (USDT). Update CAPITAL in .env when balance changes."""
    return float(CAPITAL)


# ── Position sizing ───────────────────────────────────────────────────────────

def calculate_position(capital, entry_price, signal="BUY"):
    """
    Calculate position size with two caps:

    1. Risk cap   — qty limited so max loss = 1% of capital
    2. Capital cap — qty limited so capital deployed ≤ 25% of total capital

    Returns qty in base asset units (e.g. BTC), plus stop/target prices.
    For spot trading, SELL signal means selling coins you bought earlier —
    this function sizes a BUY entry; SELL sizing mirrors it direction-aware.
    """
    if entry_price <= 0:
        return {"qty": 0.0, "stop_loss": 0.0, "target": 0.0,
                "risk_amount": 0.0, "usdt_amount": 0.0}

    # Risk-based qty
    risk_amount = capital * RISK_PER_TRADE_PCT
    sl_amount   = entry_price * SL_PCT
    risk_qty    = risk_amount / sl_amount if sl_amount > 0 else 0

    # Capital cap qty
    max_capital = capital * MAX_CAPITAL_PER_TRADE_PCT
    cap_qty     = max_capital / entry_price if entry_price > 0 else 0

    qty         = min(risk_qty, cap_qty)
    usdt_amount = round(qty * entry_price, 2)

    if signal == "BUY":
        stop_loss = round(entry_price * (1 - SL_PCT), 6)
        target    = round(entry_price * (1 + SL_PCT * TARGET_RATIO), 6)
    else:  # SELL (short-side simulation in paper; or sell existing holdings in live)
        stop_loss = round(entry_price * (1 + SL_PCT), 6)
        target    = round(entry_price * (1 - SL_PCT * TARGET_RATIO), 6)

    actual_risk = round(qty * sl_amount, 4)

    return {
        "qty":         round(qty, 6),
        "stop_loss":   stop_loss,
        "target":      target,
        "risk_amount": actual_risk,
        "usdt_amount": usdt_amount,
    }


# ── Order placement ───────────────────────────────────────────────────────────

def place_order(symbol, side, usdt_amount):
    """
    Place a live market order on Binance Spot.

    Uses quoteOrderQty so you spend exactly `usdt_amount` USDT —
    Binance calculates the base qty automatically.
    """
    order = client.create_order(
        symbol=symbol,
        side=SIDE_BUY if side == "BUY" else SIDE_SELL,
        type=ORDER_TYPE_MARKET,
        quoteOrderQty=usdt_amount,
    )
    return order


# ── Trailing stop ─────────────────────────────────────────────────────────────

def update_trailing_stop(trade: dict, current_price: float) -> dict:
    """
    Move stop loss in the direction of profit to lock in gains.

    Rules:
    - Only activates when unrealised P&L > 0.5% of entry
    - BUY  : stop moves UP  only
    - SELL : stop moves DOWN only
    - Trail distance = 0.3% of current price
    """
    entry          = trade.get("entry", 0)
    signal         = trade.get("signal", "BUY")
    current_stop   = trade.get("stop_loss", 0)
    trail_distance = round(current_price * TRAIL_STOP_DISTANCE_PCT, 6)

    if signal == "BUY":
        pnl_pct = (current_price - entry) / entry if entry > 0 else 0
        if pnl_pct > TRAIL_STOP_TRIGGER_PCT:
            new_stop = round(current_price - trail_distance, 6)
            if new_stop > current_stop:
                trade["stop_loss"] = new_stop
                trade["trailing"]  = True
                print(f"      🔒 Trail stop ▲ ${current_stop} → ${new_stop} "
                      f"(price:${current_price})")

    elif signal == "SELL":
        pnl_pct = (entry - current_price) / entry if entry > 0 else 0
        if pnl_pct > TRAIL_STOP_TRIGGER_PCT:
            new_stop = round(current_price + trail_distance, 6)
            if new_stop < current_stop:
                trade["stop_loss"] = new_stop
                trade["trailing"]  = True
                print(f"      🔒 Trail stop ▼ ${current_stop} → ${new_stop} "
                      f"(price:${current_price})")

    return trade


# ── Circuit breaker ───────────────────────────────────────────────────────────

def is_circuit_breaker_hit(capital, daily_pnl):
    limit = capital * DAILY_LOSS_LIMIT_PCT
    if daily_pnl <= -limit:
        print(f"🔴 Circuit breaker: daily loss ${abs(daily_pnl):.2f} "
              f"exceeded limit ${limit:.2f}")
        return True
    return False
