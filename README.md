# Crypto Trading Bot

Automated trading bot for Binance Spot using an EMA crossover + RSI strategy. Supports paper trading (simulated) and live trading, with trailing stops, state persistence across restarts, and Telegram notifications.

## Strategy

- **Signal**: EMA9 crosses above/below EMA21 within the last 5 candles
- **Filters**: EMA gap must be ≥ 0.05% (trend strength) and RSI must be between 35–65 (not overbought/oversold)
- **Timeframe**: 5-minute candles
- **Live mode**: BUY signals only (Binance Spot cannot short without holding the asset)
- **Paper mode**: Both BUY and SELL signals are simulated

## Risk Management

| Parameter | Value |
|---|---|
| Capital per trade | 25% max ($125 of $500) |
| Stop loss | 1% from entry |
| Take profit | 2% from entry (2:1 reward/risk) |
| Trailing stop trigger | 0.5% profit |
| Trailing stop distance | 0.3% from current price |
| Max concurrent positions | 5 |
| Daily loss limit | 3% of capital |
| Symbol cooldown | 30 min after a trade |

## Watchlist

24 pairs across three tiers:

- **Tier 1** (highest liquidity): BTC, ETH, BNB, SOL, XRP, DOGE, ADA, TRX, SHIB, PEPE
- **Tier 2** (large-cap alts): AVAX, DOT, LINK, LTC, ATOM, NEAR, UNI, AAVE, TON
- **Tier 3** (L2 / newer alts): ARB, OP, SUI, APT, POL

## Project Structure

```
crypto_bot/
├── main.py          # Main bot loop, trade execution, state management
├── strategy.py      # EMA/RSI signal generation
├── risk_manager.py  # Position sizing, trailing stop, circuit breaker
├── tracker.py       # Trade logging, P&L tracking, win rate report
├── notifier.py      # Telegram notifications
├── config.py        # Loads API keys and capital from .env
├── .env             # API keys (not committed)
└── logs/            # Per-session logs, trade CSV, state.json (not committed)
```

## Setup

1. **Install dependencies**

   ```bash
   pip3 install python-binance python-dotenv pandas ta
   ```

2. **Create `.env`**

   ```
   BINANCE_API_KEY=your_api_key
   BINANCE_API_SECRET=your_api_secret
   CAPITAL=500
   TELEGRAM_BOT_TOKEN=your_bot_token   # optional
   TELEGRAM_CHAT_ID=your_chat_id       # optional
   ```

3. **Run in paper mode** (default — no real orders)

   ```bash
   python3 main.py
   ```

4. **Switch to live mode**

   In `main.py`, change:
   ```python
   IS_LIVE = False  →  IS_LIVE = True
   ```

## State Persistence

The bot saves open positions, deployed capital, and daily P&L to `logs/state.json` on every state change. On restart it automatically restores the session (same-day only — previous-day state is discarded).

## Logs

Each run creates:
- `logs/bot_YYYY-MM-DD_HH-MM-SS.log` — full terminal output
- `logs/trade_log_YYYY-MM-DD_HH-MM-SS.csv` — closed trade history with P&L
- `logs/state.json` — live state snapshot for restart recovery

## Backtesting

`backtest/` replays the bot's entry and exit rules on historical Binance 1-minute data, with fees, slippage and the 60-second polling of stops.

```bash
python3 backtest/fetch_data.py --start 2025-11 --end 2026-10   # public data, no API key
python3 backtest/experiments.py > backtest_data/results.json
```

See `backtest/RESULTS.md` for the current bot and `backtest/STRATEGIES.md` for lower-frequency alternatives (`python3 backtest/strategies.py backtest_data`).
