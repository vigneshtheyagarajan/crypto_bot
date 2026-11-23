"""
Lower-frequency candidate strategies, compared against holding BTC and against cash.

Every strategy produces target portfolio weights decided at a bar's CLOSE and
held over the NEXT bar, so nothing uses information from the bar it trades on.
Costs: (fee + slippage) charged on every unit of turnover, same as backtest.py
(0.1% + 0.05% per side by default).

Each strategy family has a small parameter grid. The best grid point is picked
on the in-sample window only, then reported on the out-of-sample window.
The out-of-sample spread across ALL grid points is reported too, so a lucky
pick is visible.

    python3 backtest/strategies.py backtest_data > backtest_data/strategies.json
"""
import json
import os
import sys
from itertools import product

import numpy as np
import pandas as pd

from backtest import load_all, rsi

COST = 0.001 + 0.0005            # per side, per unit of turnover
IS_START, SPLIT, OOS_END = "2025-11-01", "2026-07-01", "2026-11-01"


# ── data ──────────────────────────────────────────────────────────────────────

def closes(data, rule):
    """Close prices per symbol at a bar size ('1D' or '4h'). NaN where a
    symbol has no data (e.g. TONUSDT after its delisting)."""
    cols = {}
    for s, d in data.items():
        cols[s] = d["c"].resample(rule, label="left", closed="left").last()
    return pd.DataFrame(cols).sort_index()


# ── engine ────────────────────────────────────────────────────────────────────

def simulate(w, px, start, end):
    """w: target weights decided at bar t's close (rows sum <= 1, rest is cash).
    Returns per-bar portfolio returns net of costs, plus turnover."""
    w = w.reindex(px.index).fillna(0.0)
    r = px.pct_change().shift(-1)                 # return from t to t+1
    r = r.where(px.notna() & px.shift(-1).notna(), 0.0)
    # a symbol with no next price can't be held
    w = w.where(px.shift(-1).notna(), 0.0)
    mask = (w.index >= pd.Timestamp(start, tz="UTC")) & (w.index < pd.Timestamp(end, tz="UTC"))
    w, r = w[mask], r[mask]
    turnover = w.diff().abs().sum(axis=1)
    turnover.iloc[0] = w.iloc[0].abs().sum()       # entering from cash
    gross = (w * r).sum(axis=1)
    net = gross - turnover * COST
    return net, turnover


def stats(net, turnover, bars_per_day):
    eq = (1 + net).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    daily = (1 + net).groupby(net.index.floor("1D")).prod() - 1
    monthly = (1 + net).groupby(net.index.strftime("%Y-%m")).prod() - 1
    sd = daily.std()
    return {
        "return_pct": round((eq.iloc[-1] - 1) * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "sharpe_ann": round(daily.mean() / sd * np.sqrt(365), 2) if sd > 0 else None,
        "pct_days_positive": round((daily > 0).mean() * 100, 1),
        "worst_day_pct": round(daily.min() * 100, 2),
        "worst_month_pct": round(monthly.min() * 100, 2),
        "months_positive": f"{int((monthly > 0).sum())}/{len(monthly)}",
        "turnover_x": round(float(turnover.sum()), 1),
        "cost_drag_pct": round(float(turnover.sum() * COST * 100), 2),
    }


def evaluate(w, px, bars_per_day):
    out = {}
    for label, (s, e) in {"in_sample": (IS_START, SPLIT), "out_of_sample": (SPLIT, OOS_END),
                          "full_year": (IS_START, OOS_END)}.items():
        net, to = simulate(w, px, s, e)
        st = stats(net, to, bars_per_day)
        ww = w.reindex(px.index).fillna(0.0)
        ww = ww[(ww.index >= pd.Timestamp(s, tz="UTC")) & (ww.index < pd.Timestamp(e, tz="UTC"))]
        st["avg_exposure_pct"] = round(float(ww.sum(axis=1).mean()) * 100, 1)
        out[label] = st
    return out


# ── strategies (all return a weights DataFrame) ───────────────────────────────

def cash(px):
    return pd.DataFrame(0.0, index=px.index, columns=px.columns)


def hold_btc(px):
    w = cash(px)
    w["BTCUSDT"] = 1.0
    return w


def hold_basket(px, rebalance="7D"):
    """Equal weight across every coin that has data, rebalanced weekly."""
    avail = px.notna().astype(float)
    w = avail.div(avail.sum(axis=1), axis=0)
    keep = np.arange(len(w)) % int(pd.Timedelta(rebalance) / pd.Timedelta("1D")) == 0
    w.loc[~keep] = np.nan
    return w.ffill().fillna(0) * avail


def btc_trend(px, n):
    """Hold BTC while its daily close is above its n-day SMA, else cash."""
    b = px["BTCUSDT"]
    w = cash(px)
    w["BTCUSDT"] = (b > b.rolling(n).mean()).astype(float)
    return w


def basket_trend(px, n, regime_n):
    """Each coin gets 1/N of capital while above its own n-day SMA,
    and only while BTC is above its regime_n-day SMA."""
    b = px["BTCUSDT"]
    regime = b > b.rolling(regime_n).mean()
    on = (px > px.rolling(n).mean()) & px.notna()
    w = on.astype(float).div(px.notna().sum(axis=1), axis=0)
    return w.mul(regime.astype(float), axis=0)


def momentum_rotation(px, lookback, top, regime_n, rebalance_days=7):
    """Every `rebalance_days`, hold the `top` coins with the best `lookback`-day
    return (only if that return is positive), equal weight. Cash while BTC is
    below its regime_n-day SMA."""
    b = px["BTCUSDT"]
    regime = b > b.rolling(regime_n).mean()
    mom = px / px.shift(lookback) - 1
    w = cash(px)
    last = None
    for i, t in enumerate(px.index):
        if last is None or i - last >= rebalance_days or not regime.iloc[i]:
            row = pd.Series(0.0, index=px.columns)
            if regime.iloc[i]:
                m = mom.iloc[i].dropna()
                m = m[m > 0].nlargest(top)
                if len(m):
                    row[m.index] = 1.0 / top
            w.iloc[i] = row
            last = i if regime.iloc[i] else None
        else:
            w.iloc[i] = w.iloc[i - 1]
    return w


def mean_reversion(px4, band_n, k, regime_n_days, slots=5):
    """4h bars. Buy a coin when it closes below its band_n-bar mean minus k std
    (an oversold dip), only while BTC's daily trend is not down. Exit when it
    closes back above the mean, or after 30 bars (5 days). Each position is
    1/slots of capital."""
    mu = px4.rolling(band_n).mean()
    sd = px4.rolling(band_n).std()
    z = (px4 - mu) / sd
    b = px4["BTCUSDT"]
    regime = (b > b.rolling(regime_n_days * 6).mean()).to_numpy()
    zv, pv, mv = z.to_numpy(), px4.to_numpy(), mu.to_numpy()
    W = np.zeros_like(pv)
    held = {}
    for i in range(len(px4)):
        for j in list(held):
            if np.isnan(pv[i, j]) or pv[i, j] > mv[i, j] or i - held[j] >= 30:
                del held[j]
        if regime[i]:
            cands = [(zv[i, j], j) for j in range(pv.shape[1])
                     if j not in held and not np.isnan(zv[i, j]) and zv[i, j] < -k]
            for _, j in sorted(cands)[:max(0, slots - len(held))]:
                held[j] = i
        for j in held:
            W[i, j] = 1.0 / slots
    return pd.DataFrame(W, index=px4.index, columns=px4.columns)


def placebo(fn, params, px, n=50, seed=0):
    """Re-run a strategy on n copies of the data whose bar returns are shuffled
    in time (same shuffle for every coin, so volatility and cross-correlation
    stay but any trend or reversal pattern is destroyed). Tells you how big a
    full-year return pure luck produces with this much data."""
    rng = np.random.default_rng(seed)
    r = px.pct_change().iloc[1:]
    outs = []
    for _ in range(n):
        rr = r.iloc[rng.permutation(len(r))].set_axis(r.index)
        fake = (1 + rr.fillna(0)).cumprod() * px.iloc[0].fillna(1.0)
        fake = pd.concat([px.iloc[:1].fillna(1.0), fake]).where(px.notna())
        net, _ = simulate(fn(fake, *params), fake, IS_START, OOS_END)
        outs.append(float(((1 + net).prod() - 1) * 100))
    return outs


# ── run ───────────────────────────────────────────────────────────────────────

def family(name, px, bars_per_day, fn, grid):
    runs = []
    for params in grid:
        w = fn(px, *params)
        runs.append({"params": params, **evaluate(w, px, bars_per_day)})
    best = max(runs, key=lambda r: (r["in_sample"]["sharpe_ann"] or -99))
    oos = [r["out_of_sample"]["return_pct"] for r in runs]
    pl = placebo(fn, best["params"], px)
    real = best["full_year"]["return_pct"]
    return {"strategy": name, "grid_size": len(runs), "picked_in_sample": best["params"],
            "picked": {k: best[k] for k in ("in_sample", "out_of_sample", "full_year")},
            "oos_return_range_all_params": [min(oos), round(float(np.median(oos)), 2), max(oos)],
            "full_year_return_range_all_params": [min(r["full_year"]["return_pct"] for r in runs),
                                                  max(r["full_year"]["return_pct"] for r in runs)],
            "placebo_full_year_return_pct": {"p5": round(float(np.percentile(pl, 5)), 1),
                                             "median": round(float(np.median(pl)), 1),
                                             "p95": round(float(np.percentile(pl, 95)), 1),
                                             "share_of_placebos_beating_real": round(float(np.mean(np.array(pl) >= real)), 2)}}


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "backtest_data")
    data = load_all(data_dir)
    d1 = closes(data, "1D")
    h4 = closes(data, "4h")

    out = {"cost_per_side": COST, "in_sample": [IS_START, SPLIT], "out_of_sample": [SPLIT, OOS_END],
           "benchmarks": {
               "cash": evaluate(cash(d1), d1, 1),
               "hold_btc": evaluate(hold_btc(d1), d1, 1),
               "hold_equal_weight_basket": evaluate(hold_basket(d1), d1, 1),
           },
           "btc_price": {"start": float(d1["BTCUSDT"].loc[IS_START:].iloc[0]),
                         "split": float(d1["BTCUSDT"].loc[SPLIT:].iloc[0]),
                         "end": float(d1["BTCUSDT"].dropna().iloc[-1])},
           "strategies": []}

    out["strategies"].append(family("BTC trend (daily, SMA filter)", d1, 1, btc_trend,
                                     [(n,) for n in (10, 20, 50, 100)]))
    print("done btc_trend", file=sys.stderr)
    out["strategies"].append(family("Basket trend (daily, each coin above its SMA, BTC regime)", d1, 1,
                                    basket_trend, list(product((10, 20, 50), (20, 50)))))
    print("done basket_trend", file=sys.stderr)
    out["strategies"].append(family("Momentum rotation (weekly, top-k by past return, BTC regime)", d1, 1,
                                    momentum_rotation, list(product((7, 14, 30), (3, 5), (20, 50)))))
    print("done momentum", file=sys.stderr)
    out["strategies"].append(family("Mean reversion (4h, buy oversold dips, BTC regime)", h4, 6,
                                    mean_reversion, list(product((20, 42), (1.5, 2.0, 2.5), (20, 50)))))
    print("done mean_reversion", file=sys.stderr)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
