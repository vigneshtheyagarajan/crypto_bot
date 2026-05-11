import time
import sys
import os
import json
from datetime import datetime, timezone

# ── Tee: write to both terminal and log file simultaneously ───────────────────
class _Tee:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            s.write(data)
            s.flush()
    def flush(self):
        for s in self._streams:
            s.flush()
    def fileno(self):
        return self._streams[0].fileno()

os.makedirs("logs", exist_ok=True)
_run_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
_log_path      = f"logs/bot_{_run_timestamp}.log"
_log_file      = open(_log_path, "a", buffering=1)
sys.stdout     = _Tee(sys.__stdout__, _log_file)
sys.stderr     = _Tee(sys.__stderr__, _log_file)

from binance.client import Client
from config import API_KEY, API_SECRET
from strategy import get_signal

_binance = Client(API_KEY, API_SECRET)
from risk_manager import (get_capital, calculate_position,
                          update_trailing_stop,
                          is_circuit_breaker_hit, record_loss, reset_daily_loss,
                          place_order)
from notifier import (notify_bot_started, notify_paper_trade, notify_order_placed,
                      notify_circuit_breaker, notify_max_trades,
                      notify_daily_summary, notify_crash)
from tracker import (init_log, check_open_trades, log_trade,
                     print_win_rate, send_win_rate_to_telegram)

# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST = [
    # ── Tier 1: highest liquidity ─────────────────────────────────────────────
    "BTCUSDT",    # Bitcoin
    "ETHUSDT",    # Ethereum
    "BNBUSDT",    # BNB
    "SOLUSDT",    # Solana
    "XRPUSDT",    # XRP
    "DOGEUSDT",   # Dogecoin
    "ADAUSDT",    # Cardano
    "TRXUSDT",    # Tron
    "SHIBUSDT",   # Shiba Inu
    "PEPEUSDT",   # PEPE
    # ── Tier 2: large-cap alts ────────────────────────────────────────────────
    "AVAXUSDT",   # Avalanche
    "DOTUSDT",    # Polkadot
    "LINKUSDT",   # Chainlink
    "LTCUSDT",    # Litecoin
    "ATOMUSDT",   # Cosmos
    "NEARUSDT",   # NEAR Protocol
    "UNIUSDT",    # Uniswap
    "AAVEUSDT",   # AAVE
    "TONUSDT",    # Toncoin
    # ── Tier 3: newer / L2 alts ──────────────────────────────────────────────
    "ARBUSDT",    # Arbitrum
    "OPUSDT",     # Optimism
    "SUIUSDT",    # Sui
    "APTUSDT",    # Aptos
    "POLUSDT",    # Polygon
]

# ── Bot settings ──────────────────────────────────────────────────────────────
SCAN_INTERVAL    = 60    # seconds between scans
MAX_TRADES       = 5     # max concurrent open trades
MIN_USDT_TRADE   = 10    # minimum USDT per trade (Binance minimum is ~$10)
COOLDOWN_MINUTES = 30    # don't re-trade same symbol within this many mins

# ── Trading mode ──────────────────────────────────────────────────────────────
# IS_LIVE = False  →  paper trading (no real orders placed)
# IS_LIVE = True   →  live trading  (real orders on Binance)
IS_LIVE = False

STATE_FILE = "logs/state.json"

# ── State ─────────────────────────────────────────────────────────────────────
open_trades        = {}
trades_today       = 0
daily_pnl          = 0.0
deployed_usdt      = 0.0
max_trades_alerted = False
symbol_cooldown    = {}


def now_utc():
    return datetime.now(timezone.utc)


# ── Capital helpers ───────────────────────────────────────────────────────────

def available_capital(total_capital):
    return max(0.0, total_capital - deployed_usdt)


def is_affordable(usdt_amount, free_capital):
    return free_capital >= MIN_USDT_TRADE and usdt_amount >= MIN_USDT_TRADE


def is_on_cooldown(symbol):
    last = symbol_cooldown.get(symbol)
    if not last:
        return False, 0
    elapsed = (now_utc() - last).seconds / 60
    if elapsed < COOLDOWN_MINUTES:
        return True, int(COOLDOWN_MINUTES - elapsed)
    return False, 0


# ── State persistence ─────────────────────────────────────────────────────────

def save_state():
    state = {
        "open_trades":    open_trades,
        "deployed_usdt":  deployed_usdt,
        "trades_today":   trades_today,
        "daily_pnl":      daily_pnl,
        "symbol_cooldown": {
            sym: ts.isoformat()
            for sym, ts in symbol_cooldown.items()
        },
        "saved_at": now_utc().isoformat(),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_state():
    global open_trades, deployed_usdt, trades_today, daily_pnl, symbol_cooldown
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        saved_at = datetime.fromisoformat(state["saved_at"])
        if saved_at.date() != now_utc().date():
            print("📂 State file is from a previous day — starting fresh.")
            return
        open_trades   = state.get("open_trades", {})
        deployed_usdt = state.get("deployed_usdt", 0.0)
        trades_today  = state.get("trades_today", 0)
        daily_pnl     = state.get("daily_pnl", 0.0)
        symbol_cooldown = {
            sym: datetime.fromisoformat(ts)
            for sym, ts in state.get("symbol_cooldown", {}).items()
        }
        if open_trades:
            print(f"📂 Restored {len(open_trades)} open trade(s): "
                  f"{list(open_trades.keys())}")
        print(f"   open_positions={trades_today}  deployed=${deployed_usdt:.2f}  "
              f"daily_pnl=${daily_pnl:+.4f}")
    except Exception as e:
        print(f"⚠️  Could not load state file: {e} — starting fresh.")


# ── Price fetch ───────────────────────────────────────────────────────────────

def get_current_price(symbol):
    try:
        ticker = _binance.get_symbol_ticker(symbol=symbol)
        return round(float(ticker["price"]), 6)
    except Exception:
        return None


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(capital):
    print("\n" + "─" * 55)
    print("📋 SESSION SUMMARY")
    print(f"   Mode             : {'🔴 LIVE' if IS_LIVE else '📝 PAPER'}")
    print(f"   Capital          : ${capital:,.2f} USDT")
    print(f"   Deployed         : ${deployed_usdt:,.2f} USDT")
    print(f"   Available        : ${available_capital(capital):,.2f} USDT")
    print(f"   Open positions   : {trades_today} / {MAX_TRADES}")
    print(f"   Daily P&L        : ${daily_pnl:+.4f} USDT")
    print(f"   Open positions   : {list(open_trades.keys()) or 'None'}")
    print(f"   Log file         : {_log_path}")
    print("─" * 55)
    print_win_rate()


# ── Main bot loop ─────────────────────────────────────────────────────────────

def run_bot():
    global trades_today, open_trades, daily_pnl, deployed_usdt
    global max_trades_alerted, symbol_cooldown

    print("\n" + "═" * 55)
    print(f"🤖  CRYPTO BOT STARTED  "
          f"[{'🔴 LIVE MODE' if IS_LIVE else '📝 PAPER MODE'}]")
    print(f"📁  Log : {_log_path}")
    print("═" * 55)

    init_log()
    reset_daily_loss()

    trades_today       = 0
    open_trades        = {}
    daily_pnl          = 0.0
    deployed_usdt      = 0.0
    max_trades_alerted = False
    symbol_cooldown    = {}

    load_state()

    capital = get_capital()

    print(f"💰 Total capital        : ${capital:,.2f} USDT")
    print(f"📋 Watchlist            : {len(WATCHLIST)} pairs")
    print(f"🔁 Scan interval        : every {SCAN_INTERVAL}s")
    print(f"🛑 Max concurrent pos   : {MAX_TRADES}")
    print(f"💵 Min USDT per trade   : ${MIN_USDT_TRADE}")
    print(f"⏳ Symbol cooldown      : {COOLDOWN_MINUTES} mins")
    print(f"💼 Max capital/trade    : 25% = ${capital*0.25:,.2f}")
    print(f"⚡ Daily loss limit     : ${capital * 0.03:,.2f}  (3%)")
    print(f"⚡ Risk per trade       : ${capital * 0.01:,.2f}  (1%)")
    print(f"📊 EMA gap filter       : 0.05% minimum")
    print(f"📊 RSI for BUY          : 35 – 65")
    print(f"📊 RSI for SELL         : 35 – 65")
    print(f"🔒 Trail stop trigger   : 0.5% profit from entry")
    print(f"🔒 Trail stop distance  : 0.3% below/above current price")
    print(f"🕐 Current UTC time     : {now_utc().strftime('%H:%M:%S')}\n")

    notify_bot_started(capital, WATCHLIST)

    last_reset_day = now_utc().day

    # ── Main loop ─────────────────────────────────────────────────────────────
    while True:
        now   = now_utc()
        avail = available_capital(capital)

        # 1. Daily reset at UTC midnight
        if now.day != last_reset_day:
            print(f"\n🔄 UTC midnight — resetting daily counters")
            notify_daily_summary(capital, trades_today,
                                 list(open_trades.keys()), daily_pnl)
            send_win_rate_to_telegram()
            print_summary(capital)

            trades_today       = len(open_trades)   # keep concurrent count accurate
            daily_pnl          = 0.0
            max_trades_alerted = len(open_trades) >= MAX_TRADES
            symbol_cooldown    = {}
            last_reset_day     = now.day
            reset_daily_loss()
            save_state()
            # Note: open_trades are NOT reset — crypto holds positions across days

        # 2. Circuit breaker (3% daily loss limit)
        if is_circuit_breaker_hit(capital):
            print("🔴 CIRCUIT BREAKER — All new trades stopped for today.")
            notify_circuit_breaker(capital, daily_pnl, trades_today)
            print_summary(capital)
            # Don't break — keep monitoring open trades, just stop new entries
            while True:
                if now_utc().day != last_reset_day:
                    break   # wait for midnight reset
                if open_trades:
                    open_trades, freed, pnl_delta = check_open_trades(
                        open_trades, get_current_price, is_live=IS_LIVE)
                    if freed > 0:
                        deployed_usdt = max(0.0, deployed_usdt - freed)
                        daily_pnl    += pnl_delta
                        save_state()
                time.sleep(SCAN_INTERVAL)
            continue

        # 3. Max trades — monitor only
        if trades_today >= MAX_TRADES:
            if not max_trades_alerted:
                notify_max_trades(MAX_TRADES)
                max_trades_alerted = True
            print(f"⏸  [{now.strftime('%H:%M')} UTC] "
                  f"Max {MAX_TRADES} concurrent positions — monitoring only | "
                  f"Available:${avail:,.2f}")
            if open_trades:
                for symbol in list(open_trades.keys()):
                    price = get_current_price(symbol)
                    if price:
                        open_trades[symbol] = update_trailing_stop(
                            open_trades[symbol], price)
                        trade     = open_trades[symbol]
                        entry     = trade.get("entry", 0)
                        stop      = trade.get("stop_loss", 0)
                        target    = trade.get("target", 0)
                        signal    = trade.get("signal", "BUY")
                        qty       = trade.get("qty", 0)
                        trailing  = trade.get("trailing", False)
                        pnl       = ((price - entry) * qty if signal == "BUY"
                                     else (entry - price) * qty)
                        direction = "▲" if price > entry else "▼"
                        trail_tag = " 🔒" if trailing else ""
                        print(f"   {symbol:14s} — 🔄 {signal}{trail_tag} | "
                              f"Entry:${entry:.6g}  Now:${price:.6g} {direction} | "
                              f"P&L:${pnl:+.4f} | "
                              f"Stop:${stop:.6g}  Target:${target:.6g}")
                prev_count  = len(open_trades)
                open_trades, freed, pnl_delta = check_open_trades(
                    open_trades, get_current_price, is_live=IS_LIVE)
                closed_count = prev_count - len(open_trades)
                if freed > 0:
                    deployed_usdt = max(0.0, deployed_usdt - freed)
                    trades_today  = max(0, trades_today - closed_count)
                    daily_pnl    += pnl_delta
                    save_state()
                    print(f"   💰 Capital freed: ${freed:,.2f} | Now available: "
                          f"${available_capital(capital):,.2f}")
            time.sleep(SCAN_INTERVAL)
            continue

        # 4. Monitor open trades — trailing stop + target/stop check
        if open_trades:
            print(f"\n👁  Monitoring {len(open_trades)} open trade(s) | "
                  f"Deployed:${deployed_usdt:,.2f} | "
                  f"Available:${avail:,.2f} ...")

            for symbol in list(open_trades.keys()):
                price = get_current_price(symbol)
                if price:
                    open_trades[symbol] = update_trailing_stop(
                        open_trades[symbol], price)
                    trade     = open_trades[symbol]
                    entry     = trade.get("entry", 0)
                    stop      = trade.get("stop_loss", 0)
                    target    = trade.get("target", 0)
                    signal    = trade.get("signal", "BUY")
                    qty       = trade.get("qty", 0)
                    trailing  = trade.get("trailing", False)
                    pnl       = ((price - entry) * qty if signal == "BUY"
                                 else (entry - price) * qty)
                    direction = "▲" if price > entry else "▼"
                    trail_tag = " 🔒" if trailing else ""
                    print(f"   {symbol:14s} — 🔄 {signal}{trail_tag} | "
                          f"Entry:${entry:.6g}  Now:${price:.6g} {direction} | "
                          f"P&L:${pnl:+.4f} | "
                          f"Stop:${stop:.6g}  Target:${target:.6g}")

            open_trades, freed, pnl_delta = check_open_trades(
                open_trades, get_current_price, is_live=IS_LIVE)
            if freed > 0:
                deployed_usdt = max(0.0, deployed_usdt - freed)
                daily_pnl    += pnl_delta
                avail         = available_capital(capital)
                save_state()
                print(f"   💰 Capital freed: ${freed:,.2f} | Now available: ${avail:,.2f}")

        # 5. Skip scan if no capital
        if avail < MIN_USDT_TRADE:
            print(f"⏸  [{now.strftime('%H:%M')} UTC] "
                  f"No free capital — waiting | Deployed:${deployed_usdt:,.2f}")
            time.sleep(SCAN_INTERVAL)
            continue

        # 6. Scan watchlist
        print(f"\n🔍 Scanning {len(WATCHLIST)} pairs at "
              f"{now.strftime('%H:%M:%S')} UTC "
              f"[open:{trades_today}/{MAX_TRADES} | "
              f"available:${avail:,.2f}] ...")

        for symbol in WATCHLIST:

            # ── Already in trade ──────────────────────────────────────────────
            if symbol in open_trades:
                trade = open_trades[symbol]
                price = get_current_price(symbol)
                if price:
                    entry    = trade.get("entry", 0)
                    stop     = trade.get("stop_loss", 0)
                    target   = trade.get("target", 0)
                    signal   = trade.get("signal", "BUY")
                    qty      = trade.get("qty", 0)
                    trailing = trade.get("trailing", False)
                    pnl      = ((price - entry) * qty if signal == "BUY"
                                else (entry - price) * qty)
                    direction    = "▲" if price > entry else "▼"
                    trail_tag    = " 🔒" if trailing else ""
                    print(f"   {symbol:14s} — 🔄 {signal}{trail_tag} | "
                          f"Entry:${entry:.6g}  Now:${price:.6g} {direction} | "
                          f"P&L:${pnl:+.4f} | "
                          f"Stop:${stop:.6g}  Target:${target:.6g}")
                else:
                    print(f"   {symbol:14s} — 🔄 in trade (price unavailable)")
                continue

            # ── Cooldown check ────────────────────────────────────────────────
            on_cd, mins_left = is_on_cooldown(symbol)
            if on_cd:
                print(f"   {symbol:14s} — ⏳ cooldown ({mins_left}min left)")
                continue

            avail = available_capital(capital)
            if avail < MIN_USDT_TRADE:
                print(f"   {symbol:14s} — ⏭  no free capital remaining")
                continue

            # ── Get signal ────────────────────────────────────────────────────
            result = get_signal(symbol)

            if "error" in result:
                print(f"   {symbol:14s} — ⚠️  Error: {result['error']}")
                continue

            # ── Affordability check ───────────────────────────────────────────
            pos = calculate_position(capital, result["price"], result.get("signal", "HOLD"))
            if not is_affordable(pos["usdt_amount"], avail):
                print(f"   {symbol:14s} — ⏭  ${pos['usdt_amount']:.2f} exceeds "
                      f"free capital ${avail:.2f}")
                continue

            # ── Print scan result ─────────────────────────────────────────────
            arrow   = ("🟢" if result["signal"] == "BUY"
                       else "🔴" if result["signal"] == "SELL"
                       else "⚪")
            gap_str = (f" gap:{result.get('ema_gap_pct', 0):.3f}%"
                       if result["signal"] != "HOLD" else "")
            print(f"   {symbol:14s} | ${result['price']:<12.6g} | "
                  f"EMA9:{result['ema9']:.6g}  "
                  f"EMA21:{result['ema21']:.6g}  "
                  f"RSI:{result['rsi']:>5.1f}{gap_str} | "
                  f"{arrow} {result['signal']}")

            # ── Act on signal ─────────────────────────────────────────────────
            if IS_LIVE and result["signal"] == "SELL":
                print(f"   {symbol:14s} — ⏭  SELL skipped (spot-only live mode)")
                continue

            if result["signal"] in ("BUY", "SELL"):
                if pos["qty"] <= 0 or pos["usdt_amount"] < MIN_USDT_TRADE:
                    print(f"   ⚠️  {symbol} — position too small, skipping")
                    continue

                print(f"\n   {'─'*51}")
                print(f"   💡 SIGNAL: {result['signal']} {symbol}")
                print(f"      Entry    : ${result['price']:.6g}")
                print(f"      Qty      : {pos['qty']:.6g} coins")
                print(f"      USDT     : ${pos['usdt_amount']:.2f}")
                print(f"      Stop     : ${pos['stop_loss']:.6g}"
                      f"  (max loss ${pos['risk_amount']:.4f})")
                print(f"      Target   : ${pos['target']:.6g}"
                      f"  (expected profit ~${pos['risk_amount']*2:.4f})")
                print(f"      EMA gap  : {result.get('ema_gap_pct',0):.3f}%  "
                      f"RSI: {result['rsi']}")
                print(f"   {'─'*51}\n")

                if IS_LIVE:
                    order = place_order(symbol, result["signal"], pos["usdt_amount"])
                    order_id = str(order["orderId"])
                    notify_order_placed(symbol, result["signal"], pos["qty"],
                                        result["price"], pos["usdt_amount"], order_id)
                    open_trades[symbol] = {
                        "order_id": order_id,
                        "signal":   result["signal"],
                        "entry":    result["price"],
                        "rsi":      result["rsi"],
                        "usdt_used": pos["usdt_amount"],
                        "trailing": False,
                        **pos
                    }
                else:
                    notify_paper_trade(symbol, result["signal"], result["price"],
                                       pos["qty"], pos["stop_loss"], pos["target"],
                                       pos["risk_amount"], pos["usdt_amount"])
                    open_trades[symbol] = {
                        "signal":    result["signal"],
                        "entry":     result["price"],
                        "rsi":       result["rsi"],
                        "usdt_used": pos["usdt_amount"],
                        "trailing":  False,
                        **pos
                    }
                    print(f"      📝 PAPER TRADE — tracking outcome automatically")

                deployed_usdt          += pos["usdt_amount"]
                avail                   = available_capital(capital)
                symbol_cooldown[symbol] = now_utc()
                trades_today           += 1
                save_state()

                print(f"      💸 Deployed:${deployed_usdt:,.2f} | Remaining:${avail:,.2f}")

                if trades_today >= MAX_TRADES:
                    print(f"   🛑 Max {MAX_TRADES} concurrent positions reached — stopping scan")
                    break

                if avail < MIN_USDT_TRADE:
                    print(f"   💰 No free capital remaining — stopping scan")
                    break

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\n\n⛔ Bot stopped manually (Ctrl+C)")
        capital = get_capital()
        print_summary(capital)
        notify_daily_summary(capital, trades_today,
                             list(open_trades.keys()), daily_pnl)
        send_win_rate_to_telegram()
    except Exception as e:
        print(f"\n❌ Bot crashed: {e}")
        notify_crash(str(e))
        raise
