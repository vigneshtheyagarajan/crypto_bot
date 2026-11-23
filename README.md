# Crypto Trading Bot

> **Status (Nov 2026):** the original EMA crossover bot (`main.py`) lost money in every month of a one-year backtest after fees, so live trading is blocked for it (see `backtest/RESULTS.md`). The current candidate is the **daily BTC trend bot** (`trend_bot.py`), which runs in **paper mode only**.

## Daily BTC trend bot (paper only)

Once a day, after the UTC daily candle closes, the bot checks BTC's close against its 20-day average. If the close is above the average, it holds BTC. Otherwise it holds USDT.

In the Nov 2025 to Oct 2026 backtest this rule made +11% for the year, while holding BTC lost 30%. It still lost 2% during the crash and made money on only about 1 day in 5. It limits losses; it doesn't produce daily profits. See `backtest/STRATEGIES.md`.

```bash
python3 trend_bot.py              # run the paper loop (checks every 5 minutes, trades at most once a day)
python3 trend_bot.py --status     # show balances, position, peak equity, fees
python3 trend_bot.py --reset-halt # clear the drawdown kill switch after reviewing
touch logs/STOP                   # pause decisions; delete the file to resume
```

**Safety features**

- Paper fills include a 0.1% fee and 0.05% slippage per side.
- State lives in `logs/trend_state.json` and trades in `logs/trend_trades.csv`. Both persist across days and restarts, and the state file is written atomically.
- A drawdown kill switch moves the bot to cash and stops it when equity falls 25% below its peak. Set the threshold with `TREND_MAX_DRAWDOWN_PCT`.
- An error in one check is logged and alerted on Telegram after 3 in a row, and the loop keeps running.
- Missing or stale data means no trade.
- `TRADING_MODE=live` is refused in this version.

**Settings** (env or `.env`): `TREND_CAPITAL` (default `CAPITAL` or 500), `TREND_SMA_DAYS` (20), `TREND_CHECK_INTERVAL` (300 seconds), `TREND_MAX_DRAWDOWN_PCT` (0.25).

Run it on an always-on machine. On a laptop that sleeps, the bot can miss a daily close and will only act once it wakes. Run the tests with `python3 -m pytest -q`.

## Original EMA crossover bot (live trading blocked)

Automated trading bot for Binance Spot using an EMA crossover + RSI strategy. Supports paper trading (simulated), with trailing stops, state persistence across restarts, and Telegram notifications.

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
   TELEGRAM_TOKEN=your_bot_token       # optional (TELEGRAM_BOT_TOKEN also works)
   TELEGRAM_CHAT_ID=your_chat_id       # optional
   ```

3. **Run in paper mode** (default — no real orders)

   ```bash
   python3 main.py
   ```

4. **Live mode is blocked.** Setting `IS_LIVE = True` makes the bot exit with an explanation, because the backtest shows this strategy loses money after fees.

## State Persistence

The bot saves open positions, deployed capital, and daily P&L to `logs/state.json` on every state change. On restart it restores open positions whatever day they were saved. Only the daily counters reset on a new UTC day.

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
