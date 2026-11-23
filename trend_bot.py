"""
Daily BTC trend bot. PAPER TRADING ONLY in this version.

Rule (from backtest/strategies.py, "BTC trend"):
    Once a day, after the UTC daily candle closes:
      - if BTC's close is above its 20-day simple moving average, hold BTC
      - otherwise hold USDT (cash)

This is a drawdown-limiting rule, not a daily-profit machine. In the
Nov 2025 to Oct 2026 backtest it made money on about 1 day in 5 and spent
over half the year in cash. Judge it on how much it limits losses compared
with simply holding BTC; the daily summary reports both.

Safety:
  - Paper fills include 0.1% fees and 0.05% slippage per side.
  - State (balances, position, last decision, peak equity) is saved
    atomically after every change and kept across days and restarts.
  - Drawdown kill switch: if equity falls more than MAX_DRAWDOWN_PCT below
    its peak, the bot moves to cash and stops until you run --reset-halt.
  - Create the file logs/STOP to pause all decisions without killing the
    process; delete it to resume.
  - Any error in one check is logged and alerted; the loop keeps running.
  - No data or a stale candle means no trade (fails closed).

Run:
    python3 trend_bot.py                # paper trading loop
    python3 trend_bot.py --status       # print the saved state and exit
    python3 trend_bot.py --reset-halt   # clear the kill switch after reviewing
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

from execution import PaperBroker, OrderError, retry, FEE, SLIPPAGE

try:  # load .env before reading settings below
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SYMBOL = "BTCUSDT"
SMA_DAYS = int(os.getenv("TREND_SMA_DAYS", "20"))
CHECK_INTERVAL = int(os.getenv("TREND_CHECK_INTERVAL", "300"))      # seconds
MAX_DRAWDOWN_PCT = float(os.getenv("TREND_MAX_DRAWDOWN_PCT", "0.25"))
START_CAPITAL = float(os.getenv("TREND_CAPITAL", os.getenv("CAPITAL", "500")))
TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()

LOG_DIR = os.getenv("TREND_LOG_DIR", "logs")
STATE_FILE = os.path.join(LOG_DIR, "trend_state.json")
TRADES_FILE = os.path.join(LOG_DIR, "trend_trades.csv")
STOP_FILE = os.path.join(LOG_DIR, "STOP")

LIVE_DISABLED_MSG = (
    "Live trading is disabled in this version. Paper-trade the trend bot for at "
    "least 3 months, then review the results before any live code path is enabled."
)


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}", flush=True)


# ── state ─────────────────────────────────────────────────────────────────────

def new_state(capital, price):
    return {
        "mode": "paper",
        "symbol": SYMBOL,
        "usdt": capital,
        "base": 0.0,
        "in_position": False,
        "entry_price": None,
        "last_decision_day": None,
        "peak_equity": capital,
        "halted": False,
        "halt_reason": None,
        "start_time": now_utc().isoformat(),
        "start_equity": capital,
        "start_price": price,
        "fees_paid_usdt": 0.0,
        "trade_count": 0,
        "consecutive_errors": 0,
        "saved_at": None,
    }


def load_state(path=None):
    path = path or STATE_FILE
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_state(state, path=None):
    """Write to a temp file and rename, so a crash mid-write can't corrupt it."""
    path = path or STATE_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    state["saved_at"] = now_utc().isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def log_trade(fill, state, reason, path=None):
    path = path or TRADES_FILE
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "side", "symbol", "base_qty", "quote_usdt", "avg_price",
                        "fee_usdt", "reason", "usdt_after", "base_after", "order_id"])
        w.writerow([datetime.fromtimestamp(fill.time, timezone.utc).isoformat(), fill.side,
                    fill.symbol, f"{fill.base_qty:.8f}", f"{fill.quote_qty:.2f}",
                    f"{fill.avg_price:.2f}", f"{fill.fee_usdt:.4f}", reason,
                    f"{state['usdt']:.2f}", f"{state['base']:.8f}", fill.client_order_id])


# ── market data ───────────────────────────────────────────────────────────────

def daily_signal(client, now_ms=None, n=SMA_DAYS):
    """Use CLOSED daily candles only. Returns dict with the decision and the
    UTC date of the candle it was based on, or raises if data is unusable."""
    now_ms = now_ms or int(time.time() * 1000)
    kl = retry(lambda: client.get_klines(symbol=SYMBOL, interval="1d", limit=n + 5))
    closed = [k for k in kl if int(k[6]) < now_ms]
    if len(closed) < n:
        raise ValueError(f"only {len(closed)} closed daily candles, need {n}")
    last = closed[-1]
    day = datetime.fromtimestamp(int(last[0]) / 1000, timezone.utc).date().isoformat()
    # The newest closed candle must be yesterday's (UTC), else data is stale.
    age_h = (now_ms - int(last[6])) / 3_600_000
    if age_h > 26:
        raise ValueError(f"latest closed daily candle {day} is {age_h:.0f}h old")
    closes = [float(k[4]) for k in closed[-n:]]
    sma = sum(closes) / n
    return {"day": day, "close": closes[-1], "sma": sma, "hold": closes[-1] > sma}


def get_price(client, symbol=SYMBOL):
    t = retry(lambda: client.get_symbol_ticker(symbol=symbol))
    return float(t["price"])


# ── one check ─────────────────────────────────────────────────────────────────

def equity(state, price):
    return state["usdt"] + state["base"] * price


def _apply(state, fill, reason, notify):
    if fill.side == "BUY":
        state["usdt"] -= fill.quote_qty
        state["base"] += fill.base_qty
        state["in_position"] = True
        state["entry_price"] = fill.avg_price
    else:
        state["usdt"] += fill.quote_qty
        state["base"] -= fill.base_qty
        if state["base"] < 1e-12:
            state["base"] = 0.0
        state["in_position"] = state["base"] > 0
        state["entry_price"] = None if not state["in_position"] else state["entry_price"]
    state["fees_paid_usdt"] += fill.fee_usdt
    state["trade_count"] += 1
    save_state(state)
    log_trade(fill, state, reason)
    log(f"{fill.side} {fill.base_qty:.6f} {fill.symbol} @ ${fill.avg_price:,.2f} "
        f"(fee ${fill.fee_usdt:.2f}) - {reason}")
    notify(f"{'🟢' if fill.side == 'BUY' else '🔴'} PAPER {fill.side} {fill.symbol}\n"
           f"{fill.base_qty:.6f} @ ${fill.avg_price:,.2f}\nFee: ${fill.fee_usdt:.2f}\n"
           f"Reason: {reason}")


def check(state, client, broker, notify, now_ms=None):
    """One pass of the bot. Safe to call any number of times a day: it trades
    at most once per closed daily candle."""
    if os.path.exists(STOP_FILE):
        log("STOP file present - paused, no decisions")
        return state

    price = get_price(client)
    eq = equity(state, price)
    state["peak_equity"] = max(state["peak_equity"], eq)
    dd = eq / state["peak_equity"] - 1

    # Kill switch
    if not state["halted"] and dd <= -MAX_DRAWDOWN_PCT:
        state["halted"] = True
        state["halt_reason"] = (f"equity ${eq:,.2f} is {dd:.1%} below peak "
                                f"${state['peak_equity']:,.2f}")
        save_state(state)
        notify(f"🛑 KILL SWITCH: {state['halt_reason']}. Moving to cash and stopping. "
               f"Run trend_bot.py --reset-halt after reviewing.")
    if state["halted"] and state["base"] > 0:
        # Keeps retrying every check until the position is actually closed.
        _apply(state, broker.sell(SYMBOL, state["base"]), "kill switch", notify)
    if state["halted"]:
        log(f"halted: {state['halt_reason']}")
        return state

    sig = daily_signal(client, now_ms)
    if state["last_decision_day"] == sig["day"]:
        log(f"price ${price:,.2f} | equity ${eq:,.2f} | dd {dd:.1%} | "
            f"{'BTC' if state['in_position'] else 'cash'} | waiting for next daily close")
        return state

    log(f"new daily close {sig['day']}: close ${sig['close']:,.2f} vs "
        f"SMA{SMA_DAYS} ${sig['sma']:,.2f} -> {'hold BTC' if sig['hold'] else 'cash'}")
    reason = (f"{sig['day']} close ${sig['close']:,.0f} "
              f"{'above' if sig['hold'] else 'below'} SMA{SMA_DAYS} ${sig['sma']:,.0f}")
    if sig["hold"] and not state["in_position"]:
        _apply(state, broker.buy(SYMBOL, state["usdt"]), reason, notify)
    elif not sig["hold"] and state["in_position"]:
        _apply(state, broker.sell(SYMBOL, state["base"]), reason, notify)

    # Only mark the day done once any trade it needed has succeeded.
    state["last_decision_day"] = sig["day"]
    save_state(state)
    send_summary(state, price, notify)
    return state


def send_summary(state, price, notify):
    eq = equity(state, price)
    ret = eq / state["start_equity"] - 1
    hold = price / state["start_price"] - 1 if state.get("start_price") else 0
    dd = eq / state["peak_equity"] - 1
    notify(f"📋 Trend bot daily summary (PAPER)\n"
           f"Position: {'BTC' if state['in_position'] else 'cash'}\n"
           f"Equity: ${eq:,.2f} ({ret:+.1%} since start)\n"
           f"Holding BTC instead: {hold:+.1%}\n"
           f"Drawdown from peak: {dd:.1%}\n"
           f"Fees paid: ${state['fees_paid_usdt']:.2f} over {state['trade_count']} trades")


# ── main ──────────────────────────────────────────────────────────────────────

def _make_client():
    from binance.client import Client
    # Paper mode only needs public market data; keys are optional.
    return Client(os.getenv("BINANCE_API_KEY") or None, os.getenv("BINANCE_API_SECRET") or None)


def _notifier():
    try:
        from notifier import _send
        return _send
    except Exception:
        return lambda msg: None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset-halt", action="store_true")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    a = ap.parse_args(argv)

    if TRADING_MODE != "paper":
        sys.exit(LIVE_DISABLED_MSG)

    os.makedirs(LOG_DIR, exist_ok=True)
    state = load_state()

    if a.status:
        print(json.dumps(state, indent=2) if state else "No saved state yet.")
        return
    if a.reset_halt:
        if state:
            state["halted"], state["halt_reason"] = False, None
            save_state(state)
            print("Kill switch cleared.")
        return

    client = _make_client()
    notify = _notifier()
    broker = PaperBroker(lambda s: get_price(client, s), fee=FEE, slippage=SLIPPAGE)

    if state is None:
        state = new_state(START_CAPITAL, get_price(client))
        save_state(state)
        log(f"new paper account: ${START_CAPITAL:,.2f} USDT")
    else:
        log(f"restored state: usdt ${state['usdt']:,.2f}, {SYMBOL} {state['base']:.6f}, "
            f"last decision {state['last_decision_day']}, halted={state['halted']}")
    notify(f"🤖 Trend bot started (PAPER). SMA{SMA_DAYS} daily filter on {SYMBOL}.")

    while True:
        try:
            state = check(state, client, broker, notify)
            state["consecutive_errors"] = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
            log(f"check failed ({state['consecutive_errors']} in a row): {e!r}")
            if state["consecutive_errors"] in (3, 12, 48):
                notify(f"⚠️ Trend bot: {state['consecutive_errors']} failed checks in a row. "
                       f"Last error: {str(e)[:200]}")
            save_state(state)
        if a.once:
            return
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped manually")
