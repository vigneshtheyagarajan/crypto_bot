"""
Event-driven backtest of the live bot's rules on historical 1-minute Binance data.

What it mirrors from main.py / strategy.py / risk_manager.py
  - EMA9/EMA21 cross within the last 5 candles, EMA gap >= 0.05%, close > EMA50,
    35 < RSI14 < 65, on 5-minute candles. BUY only (live is spot BUY-only).
  - BTC filter: BTCUSDT EMA9 > EMA21 on 15-minute candles.
  - $500 static capital, $125 per trade (the 25% cap always binds), entry only
    while free capital >= $10, at most 5 open positions, 30 min symbol cooldown,
    3% daily realised-loss circuit breaker, counters reset at UTC midnight.
  - Exits: 1% stop, 2% target, trailing stop that arms at +1.0% and trails 0.3%
    below price. Exits are checked once a minute on the last price, like the
    bot's 60 s polling loop.

Where it deliberately differs (and why)
  - Signals are computed on CLOSED 5m candles and entered at the next minute's
    open. The live bot uses the still-forming candle from get_klines, which can
    repaint; a backtest can't reproduce that honestly, so the closed-candle
    version is the conservative stand-in.
  - Indicators run over full history, not a fresh 100-candle window each scan.
    EMA50 differs very slightly early in each window; the effect is minor.

Cost models (pick with --mode)
  paper     : the bot's own paper accounting. Fills exactly at stop/target, no fees.
  realistic : fills at the polled price (1m close) after it crosses the level,
              plus slippage and taker fees on both sides.
"""
import argparse
import glob
import gzip
import json
import os
import sys
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "TRXUSDT", "SHIBUSDT", "PEPEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT",
    "ATOMUSDT", "NEARUSDT", "UNIUSDT", "AAVEUSDT", "TONUSDT", "ARBUSDT", "OPUSDT",
    "SUIUSDT", "APTUSDT", "POLUSDT",
]


@dataclass
class Params:
    # strategy (strategy.py)
    timeframe_min: int = 5
    min_ema_gap: float = 0.0005
    cross_lookback: int = 5
    rsi_min: float = 35
    rsi_max: float = 65
    trend_ema: int = 50
    use_btc_filter: bool = True
    # risk (risk_manager.py / main.py)
    capital: float = 500.0
    trade_usdt: float = 125.0
    min_usdt: float = 10.0
    max_open: int = 5
    cooldown_min: int = 30
    daily_loss_pct: float = 0.03
    sl_pct: float = 0.01
    tp_pct: float = 0.02
    trail_trigger: float = 0.010
    trail_dist: float = 0.003
    use_trail: bool = True
    max_hold_min: int = 0          # 0 = no time stop
    # costs
    mode: str = "realistic"        # "paper" or "realistic"
    fee: float = 0.001             # per side (Binance spot taker, no BNB discount)
    slippage: float = 0.0005       # per side, applied against you


# ── data ──────────────────────────────────────────────────────────────────────

def load_1m(path):
    df = pd.read_csv(path, header=None, usecols=range(6),
                     names=["t", "o", "h", "l", "c", "v"],
                     compression="gzip" if path.endswith(".gz") else None)
    t = df["t"].astype("int64")
    # Binance spot dumps switched from milliseconds to microseconds in 2025.
    t = np.where(t > 10**14, t // 1000, t)
    df.index = pd.to_datetime(t, unit="ms", utc=True).floor("min")
    df = df[~df.index.duplicated()].sort_index()
    return df[["o", "h", "l", "c"]].astype("float64")


def resample(df, minutes):
    r = df.resample(f"{minutes}min", label="left", closed="left")
    out = pd.DataFrame({"o": r["o"].first(), "h": r["h"].max(),
                        "l": r["l"].min(), "c": r["c"].last()}).dropna()
    return out


def ema(s, n):
    return s.ewm(span=n, min_periods=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def signals(df1m, p: Params):
    """Boolean Series indexed by the time the signal becomes actionable
    (the close of the candle that produced it)."""
    c = resample(df1m, p.timeframe_min)["c"]
    e9, e21, e50, r = ema(c, 9), ema(c, 21), ema(c, p.trend_ema), rsi(c)
    above = e9 > e21
    cross_up = above & ~above.shift(1, fill_value=True)
    recent_cross = cross_up.rolling(p.cross_lookback, min_periods=1).max().astype(bool)
    gap = (e9 - e21).abs() / e21
    sig = (recent_cross & above & (gap >= p.min_ema_gap) & (c > e50)
           & (r > p.rsi_min) & (r < p.rsi_max))
    sig.index = sig.index + pd.Timedelta(minutes=p.timeframe_min)
    return sig.fillna(False)


def btc_uptrend(btc1m):
    c = resample(btc1m, 15)["c"]
    up = ema(c, 9) > ema(c, 21)
    up.index = up.index + pd.Timedelta(minutes=15)
    return up


# ── simulation ────────────────────────────────────────────────────────────────

def run(data, p: Params, start=None, end=None):
    syms = [s for s in WATCHLIST if s in data]
    grid = pd.date_range(min(d.index[0] for d in data.values()),
                         max(d.index[-1] for d in data.values()), freq="1min")
    if start is not None:
        grid = grid[grid >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        grid = grid[grid < pd.Timestamp(end, tz="UTC")]

    O, H, L, C, S = {}, {}, {}, {}, {}
    for s in syms:
        d = data[s].reindex(grid)
        O[s], H[s], L[s], C[s] = (d[k].to_numpy() for k in "ohlc")
        S[s] = signals(data[s], p).reindex(grid, fill_value=False).to_numpy(bool)
    if p.use_btc_filter and "BTCUSDT" in data:
        btc_ok = btc_uptrend(data["BTCUSDT"]).reindex(grid, method="ffill").fillna(False).to_numpy(bool)
    else:
        btc_ok = np.ones(len(grid), bool)

    any_sig = np.zeros(len(grid), bool)
    for s in syms:
        any_sig |= S[s]
    day = grid.normalize().asi8
    open_t, trades = {}, []
    deployed = daily_pnl = 0.0
    cooldown = {}
    halted = False                 # circuit breaker stays on until UTC midnight
    cur_day = day[0] if len(day) else 0
    realistic = p.mode == "realistic"
    sl, tp, fee, slip = p.sl_pct, p.tp_pct, p.fee, p.slippage

    def close(sym, i, raw_px, reason):
        nonlocal deployed, daily_pnl
        t = open_t.pop(sym)
        if realistic:
            px = raw_px * (1 - slip)
            pnl = t["qty"] * px * (1 - fee) - t["cost"]
        else:
            px = raw_px
            pnl = t["qty"] * (px - t["entry"])
        deployed -= p.trade_usdt
        daily_pnl += pnl
        trades.append({"symbol": sym, "entry_time": grid[t["i"]], "exit_time": grid[i],
                       "entry": t["entry"], "exit": px, "reason": reason, "pnl": pnl,
                       "hold_min": i - t["i"]})

    for i in range(len(grid)):
        if day[i] != cur_day:
            cur_day, daily_pnl, cooldown, halted = day[i], 0.0, {}, False

        # exits: one poll per minute on last price (1m close)
        for sym in list(open_t):
            px = C[sym][i]
            if np.isnan(px):
                continue
            t = open_t[sym]
            if p.use_trail and (px - t["entry"]) / t["entry"] > p.trail_trigger:
                ns = px * (1 - p.trail_dist)
                if ns > t["stop"]:
                    t["stop"], t["trailing"] = ns, True
            if px >= t["target"]:
                close(sym, i, px if realistic else t["target"], "target")
            elif px <= t["stop"]:
                close(sym, i, px if realistic else t["stop"],
                      "trail" if t["trailing"] else "stop")
            elif p.max_hold_min and i - t["i"] >= p.max_hold_min:
                close(sym, i, px, "time")

        # entries
        if daily_pnl <= -p.capital * p.daily_loss_pct:
            halted = True
        if halted or not btc_ok[i] or not any_sig[i]:
            continue
        for sym in syms:
            if not S[sym][i] or sym in open_t:
                continue
            if len(open_t) >= p.max_open or p.capital - deployed < p.min_usdt:
                break
            if sym in cooldown and i - cooldown[sym] < p.cooldown_min:
                continue
            raw = O[sym][i]
            if np.isnan(raw):
                continue
            if realistic:
                entry = raw * (1 + slip)
                qty = p.trade_usdt * (1 - fee) / entry
            else:
                entry, qty = raw, p.trade_usdt / raw
            open_t[sym] = {"i": i, "entry": entry, "qty": qty, "cost": p.trade_usdt,
                           "stop": entry * (1 - sl), "target": entry * (1 + tp),
                           "trailing": False}
            deployed += p.trade_usdt
            cooldown[sym] = i

    # mark anything still open to the last price
    for sym in list(open_t):
        last = C[sym][~np.isnan(C[sym])]
        if len(last):
            close(sym, len(grid) - 1, last[-1], "end")
    return pd.DataFrame(trades)


def summarize(tr: pd.DataFrame, p: Params, label=""):
    if tr.empty:
        return {"label": label, "trades": 0}
    pnl = tr["pnl"]
    eq = pnl.cumsum()
    dd = (eq - eq.cummax().clip(lower=0)).min()
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    days = max(1.0, (tr["exit_time"].max() - tr["entry_time"].min()).total_seconds() / 86400)
    n = len(tr)
    wr = len(wins) / n
    se = (wr * (1 - wr) / n) ** 0.5
    return {
        "label": label,
        "trades": n,
        "win_rate": round(wr, 4),
        "win_rate_95ci": [round(max(0, wr - 1.96 * se), 4), round(min(1, wr + 1.96 * se), 4)],
        "total_pnl": round(pnl.sum(), 2),
        "return_pct": round(pnl.sum() / p.capital * 100, 2),
        "avg_trade": round(pnl.mean(), 4),
        "avg_win": round(wins.mean(), 4) if len(wins) else 0,
        "avg_loss": round(losses.mean(), 4) if len(losses) else 0,
        "profit_factor": round(wins.sum() / -losses.sum(), 3) if losses.sum() < 0 else None,
        "max_drawdown": round(dd, 2),
        "max_drawdown_pct": round(dd / p.capital * 100, 2),
        "days": round(days, 1),
        "trades_per_day": round(n / days, 2),
        "exit_reasons": tr["reason"].value_counts().to_dict(),
        "fees_paid_est": round(n * p.trade_usdt * p.fee * 2, 2) if p.mode == "realistic" else 0,
    }


def load_all(data_dir):
    data = {}
    for s in WATCHLIST:
        files = glob.glob(os.path.join(data_dir, f"{s}_1m.csv*"))
        if files:
            data[s] = load_1m(files[0])
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "backtest_data"))
    ap.add_argument("--mode", default="realistic", choices=["paper", "realistic"])
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--trades-out")
    a = ap.parse_args()
    data = load_all(a.data)
    if not data:
        sys.exit(f"No data in {a.data}. Run backtest/fetch_data.py first.")
    p = Params(mode=a.mode)
    tr = run(data, p, a.start, a.end)
    print(json.dumps(summarize(tr, p, f"current settings, {a.mode}"), indent=2, default=str))
    if a.trades_out:
        tr.to_csv(a.trades_out, index=False)
