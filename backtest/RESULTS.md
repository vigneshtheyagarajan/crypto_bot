# Backtest results: Nov 2025 to Oct 2026

Run on 23 Nov 2026 with `python3 backtest/experiments.py backtest_data`, on 12 months of Binance 1-minute spot data for all 24 watchlist pairs. TONUSDT data stops in late June 2026, so it has no trades after that.

- **In-sample window:** 1 Nov 2025 to 30 Jun 2026.
- **Out-of-sample window:** 1 Jul 2026 to 31 Oct 2026. It was never used to choose a variant.
- **Account model:** $500 of static capital and $125 per trade, as in the bot. Losses are not compounded. That's why some totals exceed -100%: the account would really be empty well before the year ends.

## Headline

With realistic costs, the bot's current settings lose **$2,186 over the year on a $500 account**. That's about -$0.47 per trade across 4,608 trades (12.6 per day).

- **Every month and every symbol loses money.**
- At this rate the $500 would be gone during January 2026, less than 3 months in.
- With the bot's own paper accounting (no fees, fills exactly at stop and target), the same trades come to -$3.94 for the year. In other words, **the strategy has no edge even before costs, and costs make it lose money.**

## Sanity check against the real paper week (13 to 18 May 2026)

| | Trades | Win rate | P&L |
|---|---|---|---|
| Bot's actual paper log | 64 | 28% | -$33.16 |
| Backtest, paper accounting | 51 | 37% | -$14.28 |
| Backtest, realistic costs | 52 | 37% | -$40.65 |

The backtest decides on closed candles. The live bot fires on the still-forming candle, which can repaint. The closed-candle version did somewhat better than the live bot over the same week, so the backtest is, if anything, kind to the live behaviour.

## Variants (realistic costs unless noted)

| Variant | Full-year trades | Full-year P&L | Profit factor | In-sample P&L | Out-of-sample P&L |
|---|---|---|---|---|---|
| A. Current settings, paper accounting | 4,692 | -$3.94 | 1.00 | -$74 | +$72 |
| **B. Current settings, 0.1% fee + 0.05% slippage** | 4,608 | **-$2,186** | 0.49 | -$1,523 | -$658 |
| C. Current settings, 0.075% BNB fee, no slippage | 4,645 | -$1,343 | 0.65 | -$961 | -$379 |
| D. No trailing stop (1% stop / 2% target) | 3,261 | -$1,636 | 0.58 | -$1,170 | -$462 |
| E. Wider trail (arm +1%, trail 1%) | 4,048 | -$2,038 | 0.48 | -$1,413 | -$620 |
| F. 1.5% stop / 3% target, wider trail | 2,480 | -$1,364 | 0.57 | -$1,048 | -$417 |
| G. 15m candles, current exits | 3,866 | -$1,871 | 0.48 | -$1,276 | -$593 |
| H. 15m candles, 2% stop / 4% target | 1,165 | -$825 | 0.67 | -$743 | -$77 |
| I. 1h candles, 3% stop / 6% target | 489 | -$301 | 0.79 | -$341 | +$55 |

No variant makes money over the full year.

Variant I is positive out-of-sample (+$55 on 156 trades, profit factor 1.14), but it lost $341 in-sample. Its 95% win-rate range out-of-sample is 32% to 47%, which overlaps comfortably with losing. Picking it because it won the last four months would be curve-fitting on the out-of-sample window, so it doesn't count as evidence of an edge.

## Where the cost comes from (variant B)

- **Fees:** $0.25 per round trip on $125, so $1,152 for the year.
- **Slippage:** an assumed 0.05% per side, about $0.125 per round trip.
- **Polling overshoot:** exits happen at the first one-minute price past the level, not at the level itself. Average loss is -$1.68 against the -$1.25 the paper log books.
- **Small wins:** the trailing stop caps the average win at about $1.03 after costs.

## Caveats

- Signals are computed on closed 5-minute candles, with entry at the next minute's open. This is not the live bot's forming-candle behaviour; see the sanity check above.
- Indicators are computed over full history, not a fresh 100-candle window per scan.
- Capital is static, as in the bot's `.env`. Nothing is compounded and nothing stops trading at $0.
- The 3-scan BTC confirmation is approximated by "BTC 15m EMA9 > EMA21 on the last closed 15m candle".
