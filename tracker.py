import csv
import os
from datetime import datetime, timezone
from notifier import _send

os.makedirs("logs", exist_ok=True)
_run_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
TRADE_LOG_FILE = f"logs/trade_log_{_run_timestamp}.csv"


def init_log():
    if not os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "date", "symbol", "signal", "entry",
                "stop", "target", "exit_price",
                "outcome", "pnl_usdt", "qty", "rsi",
                "usdt_used", "time"
            ])
        print(f"📁 Trade log created: {TRADE_LOG_FILE}")


def log_trade(symbol: str, signal: str, entry: float, stop: float,
              target: float, exit_price: float, outcome: str,
              pnl: float, qty: float, rsi: float, usdt_used: float = 0.0):
    os.makedirs("logs", exist_ok=True)
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            symbol, signal,
            round(entry, 6), round(stop, 6), round(target, 6),
            round(exit_price, 6), outcome,
            round(pnl, 4), round(qty, 6), round(rsi, 1),
            round(usdt_used, 2),
            datetime.now(timezone.utc).strftime("%H:%M:%S")
        ])
    print(f"📝 Trade logged: {outcome} | {symbol} | P&L: ${pnl:+.4f} | USDT used: ${usdt_used:.2f}")


def _get_all_trade_files():
    os.makedirs("logs", exist_ok=True)
    return sorted([
        os.path.join("logs", f)
        for f in os.listdir("logs")
        if f.startswith("trade_log_") and f.endswith(".csv")
    ])


def check_open_trades(open_trades: dict, get_price_fn,
                      is_live: bool = False) -> tuple:
    """
    Check all open trades against current price.
    Closes trade when price hits target or stop.
    For spot BUY trades in live mode, places a market sell order to exit.

    Returns (updated_open_trades, usdt_freed, closed_pnl).
    """
    from risk_manager import place_order

    closed      = []
    usdt_freed  = 0.0
    closed_pnl  = 0.0

    for symbol, trade in open_trades.items():
        try:
            current_price = get_price_fn(symbol)
            if current_price is None:
                continue

            entry     = trade.get("entry", 0)
            stop      = trade["stop_loss"]
            target    = trade["target"]
            qty       = trade["qty"]
            signal    = trade["signal"]
            rsi       = trade.get("rsi", 0)
            usdt_used = trade.get("usdt_used", qty * entry)

            exit_price = None

            if signal == "BUY":
                if current_price >= target:
                    exit_price = target
                elif current_price <= stop:
                    exit_price = stop
            elif signal == "SELL":
                if current_price <= target:
                    exit_price = target
                elif current_price >= stop:
                    exit_price = stop

            if exit_price is not None:
                pnl     = ((exit_price - entry) * qty if signal == "BUY"
                           else (entry - exit_price) * qty)
                outcome = "WIN" if pnl > 0 else "LOSS"

                if is_live and signal == "BUY":
                    try:
                        order = place_order(symbol, "SELL", usdt_used)
                        print(f"   ✅ LIVE sell order placed: {symbol} | ID:{order['orderId']}")
                    except Exception as e:
                        print(f"   ❌ Failed to place sell order for {symbol}: {e}")

                log_trade(symbol, signal, entry, stop, target,
                          exit_price, outcome, pnl, qty, rsi, usdt_used)
                _notify_outcome(symbol, signal, outcome, entry,
                                exit_price, pnl, qty)

                usdt_freed += usdt_used
                closed_pnl += pnl
                closed.append(symbol)

        except Exception as e:
            print(f"⚠️  Error checking {symbol}: {e}")

    for symbol in closed:
        del open_trades[symbol]

    return open_trades, usdt_freed, closed_pnl


def _notify_outcome(symbol: str, signal: str, outcome: str,
                    entry: float, exit_price: float,
                    pnl: float, qty: float):
    emoji = "🏆" if outcome == "WIN" else "💸"
    _send(
        f"{emoji} <b>TRADE CLOSED — {outcome}</b>\n"
        f"─────────────────────────\n"
        f"📌 Symbol  : {symbol}\n"
        f"📊 Signal  : {signal}\n"
        f"💵 Entry   : ${entry:.6g}\n"
        f"🚪 Exit    : ${exit_price:.6g}\n"
        f"📦 Qty     : {qty:.6g} coins\n"
        f"{'🟢' if pnl >= 0 else '🔴'} P&L    : ${pnl:+.4f}\n"
        f"🕐 Time    : {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )


def print_win_rate():
    files = _get_all_trade_files()
    if not files:
        print("📭 No trades logged yet.")
        return

    wins = 0; losses = 0; total_pnl = 0.0
    for filepath in files:
        with open(filepath, "r") as f:
            for row in csv.DictReader(f):
                if row["outcome"] == "WIN":   wins += 1
                elif row["outcome"] == "LOSS": losses += 1
                try: total_pnl += float(row["pnl_usdt"])
                except Exception: pass

    total    = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0

    print("\n" + "═" * 45)
    print("📊  WIN RATE REPORT  (all sessions)")
    print("═" * 45)
    print(f"   Sessions      : {len(files)}")
    print(f"   Total trades  : {total}")
    print(f"   Wins          : {wins}")
    print(f"   Losses        : {losses}")
    print(f"   Win rate      : {win_rate:.1f}%")
    print(f"   Total P&L     : ${total_pnl:+.4f} USDT")
    if total > 0:
        print(f"   Avg per trade : ${(total_pnl/total):+.4f}")
    print("\n   Assessment:")
    if total < 10:
        print("   ⏳ Need more trades for reliable assessment (aim for 20+)")
    elif win_rate >= 45:
        print("   ✅ Strategy looks viable — consider going live")
    elif win_rate >= 35:
        print("   ⚠️  Marginal — observe more trades before going live")
    else:
        print("   ❌ Win rate too low — strategy needs tuning")
    print("═" * 45 + "\n")


def send_win_rate_to_telegram():
    files = _get_all_trade_files()
    if not files:
        _send("📭 No trades logged yet.")
        return

    wins = 0; losses = 0; total_pnl = 0.0
    for filepath in files:
        with open(filepath, "r") as f:
            for row in csv.DictReader(f):
                if row["outcome"] == "WIN":    wins += 1
                elif row["outcome"] == "LOSS": losses += 1
                try: total_pnl += float(row["pnl_usdt"])
                except Exception: pass

    total    = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0

    if total < 10:       assess = "⏳ Need 20+ trades for reliable assessment"
    elif win_rate >= 45: assess = "✅ Viable — consider going live"
    elif win_rate >= 35: assess = "⚠️ Marginal — observe more trades"
    else:                assess = "❌ Needs tuning"

    _send(
        f"📊 <b>Win rate report — all sessions</b>\n"
        f"─────────────────────────\n"
        f"📁 Sessions     : {len(files)}\n"
        f"📈 Total trades : {total}\n"
        f"🏆 Wins         : {wins}\n"
        f"💸 Losses       : {losses}\n"
        f"🎯 Win rate     : {win_rate:.1f}%\n"
        f"💰 Total P&L    : ${total_pnl:+.4f} USDT\n"
        f"📋 Assessment   : {assess}"
    )
