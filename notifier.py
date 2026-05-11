import requests
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_URL     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def _send(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram not configured — check TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env")
        return False
    try:
        resp = requests.post(TELEGRAM_URL, data={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code == 200:
            return True
        print(f"⚠️  Telegram error {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        print(f"⚠️  Telegram send failed: {e}")
        return False


def _now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


# ── Alert types ───────────────────────────────────────────────────────────────

def notify_bot_started(capital: float, watchlist: list):
    _send(
        f"🤖 <b>Crypto bot started</b>\n"
        f"─────────────────────────\n"
        f"💰 Capital   : ${capital:,.2f} USDT\n"
        f"📋 Watchlist : {len(watchlist)} pairs\n"
        f"🌐 Market    : 24/7 Binance Spot\n"
        f"📅 Date      : {datetime.now(timezone.utc).strftime('%d %b %Y')}"
    )


def notify_paper_trade(symbol: str, signal: str, price: float,
                       qty: float, stop: float, target: float,
                       risk: float, usdt_amount: float):
    emoji = "🟢" if signal == "BUY" else "🔴"
    _send(
        f"{emoji} <b>PAPER TRADE — {signal} {symbol}</b>\n"
        f"─────────────────────────\n"
        f"💵 Entry   : ${price:.6g}\n"
        f"📦 Qty     : {qty:.6g} coins\n"
        f"💸 USDT    : ${usdt_amount:.2f}\n"
        f"🛑 Stop    : ${stop:.6g}  (max loss ${risk:.2f})\n"
        f"🎯 Target  : ${target:.6g}  (profit ~${risk*2:.2f})\n"
        f"📝 No real order placed\n"
        f"🕐 Time    : {_now()}"
    )


def notify_order_placed(symbol: str, signal: str, qty: float,
                        price: float, usdt_amount: float, order_id: str):
    emoji = "✅"
    _send(
        f"{emoji} <b>ORDER PLACED — {signal} {symbol}</b>\n"
        f"─────────────────────────\n"
        f"📦 Qty      : {qty:.6g} coins @ ${price:.6g}\n"
        f"💸 USDT     : ${usdt_amount:.2f}\n"
        f"🔖 Order ID : {order_id}\n"
        f"🕐 Time     : {_now()}"
    )


def notify_circuit_breaker(capital: float, loss: float, trades: int):
    _send(
        f"🔴 <b>CIRCUIT BREAKER TRIGGERED</b>\n"
        f"─────────────────────────\n"
        f"💸 Loss today  : ${abs(loss):.2f}\n"
        f"💰 Capital     : ${capital:,.2f} USDT\n"
        f"📊 Trades done : {trades}\n"
        f"⛔ All trading stopped for today\n"
        f"🕐 Time        : {_now()}"
    )


def notify_max_trades(max_trades: int):
    _send(
        f"⏸ <b>Max trades reached — {max_trades} trades done</b>\n"
        f"Bot is now in monitor-only mode for today.\n"
        f"🕐 {_now()}"
    )


def notify_daily_summary(capital: float, trades: int,
                         open_positions: list, daily_pnl: float):
    pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
    _send(
        f"📋 <b>Daily reset summary</b>\n"
        f"─────────────────────────\n"
        f"💰 Capital       : ${capital:,.2f} USDT\n"
        f"📊 Trades today  : {trades}\n"
        f"{pnl_emoji} Daily P&L     : ${daily_pnl:+.2f}\n"
        f"📂 Open positions: {', '.join(open_positions) if open_positions else 'None'}\n"
        f"📅 Date          : {datetime.now(timezone.utc).strftime('%d %b %Y')}"
    )


def notify_crash(error: str):
    _send(
        f"❌ <b>BOT CRASHED</b>\n"
        f"─────────────────────────\n"
        f"🐛 Error : {error[:200]}\n"
        f"🕐 Time  : {_now()}\n"
        f"👉 Check logs and restart main.py"
    )
