import pandas as pd
import ta
from binance.client import Client
from config import API_KEY, API_SECRET

client = Client(API_KEY, API_SECRET)

# ── Strategy settings ─────────────────────────────────────────────────────────
MIN_EMA_GAP_PCT = 0.0005  # EMA9 must differ from EMA21 by at least 0.05%
CROSS_LOOKBACK  = 5       # crossover valid if it happened within last N candles

RSI_BUY_MIN  = 35
RSI_BUY_MAX  = 65
RSI_SELL_MIN = 35
RSI_SELL_MAX = 65


def get_historical_data(symbol, interval=Client.KLINE_INTERVAL_5MINUTE, limit=100):
    """Fetch OHLCV klines from Binance for a spot symbol (e.g. BTCUSDT)."""
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df  = pd.DataFrame(raw, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["Close"] = df["Close"].astype(float)
    df.dropna(inplace=True)
    return df


def add_indicators(df):
    """Add EMA9, EMA21 and RSI14."""
    df["ema9"]  = ta.trend.ema_indicator(df["Close"], window=9)
    df["ema21"] = ta.trend.ema_indicator(df["Close"], window=21)
    df["rsi"]   = ta.momentum.rsi(df["Close"], window=14)
    return df


def get_signal(symbol):
    """
    Analyse a symbol and return BUY / SELL / HOLD signal.

    Signal logic:
    ─────────────
    BUY  : EMA9 crossed ABOVE EMA21 within last 3 candles AND gap ≥ 0.1% AND RSI 35–65
    SELL : EMA9 crossed BELOW EMA21 within last 3 candles AND gap ≥ 0.1% AND RSI 35–65
    """
    try:
        df = get_historical_data(symbol)

        if len(df) < 22:
            return {"symbol": symbol, "error": "Not enough data", "signal": "HOLD"}

        df = add_indicators(df)
        df.dropna(inplace=True)

        if len(df) < 2:
            return {"symbol": symbol, "error": "Not enough data after indicators", "signal": "HOLD"}

        latest   = df.iloc[-1]
        previous = df.iloc[-2]

        price = round(float(latest["Close"]), 6)
        ema9  = round(float(latest["ema9"]),  6)
        ema21 = round(float(latest["ema21"]), 6)
        rsi   = round(float(latest["rsi"]),   2)

        # ── EMA gap filter: trend must be meaningful now ──────────────────────
        ema_gap_pct = abs(ema9 - ema21) / ema21 if ema21 > 0 else 0
        if ema_gap_pct < MIN_EMA_GAP_PCT:
            return {
                "symbol": symbol, "price": price,
                "ema9": ema9, "ema21": ema21, "rsi": rsi,
                "signal": "HOLD", "hold_reason": "ema_gap"
            }

        # ── EMA crossover detection (lookback = CROSS_LOOKBACK candles) ───────
        # A crossover is valid if it occurred within the last N candles so the
        # gap filter and crossover check don't cancel each other out. At the
        # exact crossover candle the gap is ~0; we allow a small window for the
        # gap to widen before the signal is confirmed.
        tail = df.iloc[-(CROSS_LOOKBACK + 1):]  # N+1 rows → N pairs to compare
        bull_cross = any(
            tail["ema9"].iloc[i - 1] < tail["ema21"].iloc[i - 1]
            and tail["ema9"].iloc[i] > tail["ema21"].iloc[i]
            for i in range(1, len(tail))
        )
        bear_cross = any(
            tail["ema9"].iloc[i - 1] > tail["ema21"].iloc[i - 1]
            and tail["ema9"].iloc[i] < tail["ema21"].iloc[i]
            for i in range(1, len(tail))
        )

        # ── Signal with RSI confirmation ──────────────────────────────────────
        if bull_cross and ema9 > ema21 and RSI_BUY_MIN < rsi < RSI_BUY_MAX:
            signal = "BUY"
        elif bear_cross and ema9 < ema21 and RSI_SELL_MIN < rsi < RSI_SELL_MAX:
            signal = "SELL"
        else:
            signal = "HOLD"

        return {
            "symbol":      symbol,
            "price":       price,
            "ema9":        ema9,
            "ema21":       ema21,
            "rsi":         rsi,
            "ema_gap_pct": round(ema_gap_pct * 100, 3),
            "signal":      signal,
        }

    except Exception as e:
        return {"symbol": symbol, "error": str(e), "signal": "HOLD"}
