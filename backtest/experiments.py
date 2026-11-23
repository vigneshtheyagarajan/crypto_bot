"""
Run the bot's current settings plus a handful of alternative exits/timeframes,
split into an in-sample window (used to compare variants) and a later
out-of-sample window (never used to pick anything).

    python3 backtest/experiments.py > backtest/results.json
"""
import json
import os
import sys
from dataclasses import replace

from backtest import Params, load_all, run, summarize

IS_START, SPLIT, OOS_END = "2025-11-01", "2026-07-01", "2026-11-01"
PAPER_START, PAPER_END = "2026-05-13T17:45", "2026-05-18T15:05"

BASE = Params()
VARIANTS = {
    "A. current settings, bot's paper accounting (no fees, exact fills)": replace(BASE, mode="paper"),
    "B. current settings, realistic (0.1% fee + 0.05% slippage per side)": BASE,
    "C. current settings, best case costs (0.075% BNB fee, no slippage)": replace(BASE, fee=0.00075, slippage=0.0),
    "D. no trailing stop (pure 1% stop / 2% target)": replace(BASE, use_trail=False),
    "E. wider trail (arm at +1%, trail 1%)": replace(BASE, trail_dist=0.01),
    "F. 1.5% stop / 3% target, trail arms +1.5% trails 1%": replace(BASE, sl_pct=0.015, tp_pct=0.03, trail_trigger=0.015, trail_dist=0.01),
    "G. 15m candles, current exits": replace(BASE, timeframe_min=15),
    "H. 15m candles, 2% stop / 4% target, no trail": replace(BASE, timeframe_min=15, sl_pct=0.02, tp_pct=0.04, use_trail=False),
    "I. 1h candles, 3% stop / 6% target, no trail": replace(BASE, timeframe_min=60, sl_pct=0.03, tp_pct=0.06, use_trail=False),
}


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "backtest_data")
    data = load_all(data_dir)
    out = {"symbols_loaded": sorted(data), "in_sample": [IS_START, SPLIT],
           "out_of_sample": [SPLIT, OOS_END], "results": []}

    # Sanity check: replay the exact week the bot paper-traded.
    for mode in ("paper", "realistic"):
        p = replace(BASE, mode=mode)
        tr = run(data, p, PAPER_START, PAPER_END)
        out.setdefault("paper_week_replay", []).append(summarize(tr, p, f"13-18 May 2026, {mode}"))

    # Month-by-month for the current settings, realistic costs.
    tr = run(data, BASE, IS_START, OOS_END)
    if not tr.empty:
        m = tr.groupby(tr["exit_time"].dt.strftime("%Y-%m"))["pnl"].agg(["count", "sum"])
        out["current_realistic_by_month"] = {k: {"trades": int(r["count"]), "pnl": round(float(r["sum"]), 2)}
                                             for k, r in m.iterrows()}
        out["current_realistic_by_symbol"] = {
            s: {"trades": int(len(g)), "win_rate": round(float((g["pnl"] > 0).mean()), 3),
                "pnl": round(float(g["pnl"].sum()), 2)}
            for s, g in tr.groupby("symbol")}
        tr.to_csv(os.path.join(data_dir, "trades_current_realistic.csv"), index=False)

    for name, p in VARIANTS.items():
        row = {"variant": name}
        for label, (s, e) in {"in_sample": (IS_START, SPLIT),
                              "out_of_sample": (SPLIT, OOS_END),
                              "full_year": (IS_START, OOS_END)}.items():
            row[label] = summarize(run(data, p, s, e), p, label)
        out["results"].append(row)
        print(f"done: {name}", file=sys.stderr)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
