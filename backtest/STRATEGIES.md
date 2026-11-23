# Lower-frequency strategies vs holding, Nov 2025 to Oct 2026

Run on 23 Nov 2026 with `python3 backtest/strategies.py backtest_data`.

**Setup**

- **Data:** the same 12 months of Binance 1-minute data, resampled to daily and 4h bars.
- **Costs:** 0.15% per side (0.1% fee plus 0.05% slippage), charged on all turnover.
- **Trading timing:** decisions use a bar's close and trade over the next bar.
- **Parameter choice:** each family's parameters were picked on the in-sample window (Nov 2025 to Jun 2026) and then scored on the out-of-sample window (Jul to Oct 2026).

**The market in this window.** BTC went from $110,098 down to $60,024 by 1 July (-45%), then recovered to $77,582. That's one long crash followed by one rally, so every result below comes from a single market cycle.

## Results (full year unless noted)

| | Return | Max drawdown | In-sample | Out-of-sample | Days positive | Months positive | Cost drag | Placebos beating it |
|---|---|---|---|---|---|---|---|---|
| Cash | 0% | 0% | 0% | 0% | - | - | 0% | - |
| Hold BTC | -29.7% | -47.0% | -45.6% | +28.9% | 48% | 6/12 | 0.3% | - |
| Hold 24-coin basket | -39.2% | -58.5% | -57.3% | +42.3% | 48% | 6/12 | 0.3% | - |
| BTC trend (above 20-day SMA) | +11.4% | -15.1% | -2.4% | +14.1% | 21% | 4/12 | 5.7% | 4% |
| Basket trend (coin > 20d SMA, BTC > 50d SMA) | +11.4% | -20.4% | -7.5% | +20.3% | 24% | 3/12 | 4.3% | 4% |
| Momentum rotation (top 5 by 7d return, weekly) | +15.9% | -27.1% | -11.8% | +31.4% | 22% | 3/12 | 6.5% | 10% |
| Mean reversion (4h, buy 2-sd dips) | +3.1% | -16.2% | -4.6% | +8.0% | 23% | 6/12 | 6.1% | 26% |

"Placebos beating it" is the share of 50 runs where the bar returns were shuffled in time and the strategy did at least as well. The shuffle keeps volatility but destroys trends.

## What this does and doesn't show

- **Every strategy lost money in-sample.** During the crash, the trend filters lost 2% to 12% while holding lost 46% to 57%. They protect capital; they did not make money.
- **All the full-year gains came in the Jul to Oct rally.** In that rally, holding BTC made +28.9%, more than BTC trend (+14.1%) or basket trend (+20.3%). Only momentum rotation kept up, with the deepest drawdown.
- **The placebo test mostly measures one event.** The real price order had one long, persistent crash that a trend filter could step aside from. The test shows the strategies exploited that crash better than chance. It can't show they will work in the next cycle.
- **Momentum rotation depends on its settings.** Full-year results ranged from -23.6% to +15.9% across its 12 settings.
- **Mean reversion can't be told apart from luck.** 26% of placebos beat it, and its +3.1% is smaller than the 6.1% it paid in costs.
- **Multiple testing:** 4 families and 34 parameter settings were tested. Some apparent wins at the 5% level are expected by chance alone.
- **"Daily profits" is not on offer from any of these.** The best strategies made money on only about 1 day in 5 and in 3 or 4 months out of 12. They spend most of the time in cash.

## Approximations

- Weights are held constant between rebalances, so drift isn't modelled. The cost of rebalancing back to target is not charged.
- Only one year of data is used. Daily-bar strategies make just 10 to 40 round trips a year, so these samples are small.
