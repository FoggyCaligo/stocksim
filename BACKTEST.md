# Configurable swing backtest

This backtester approximates the MTS condition shown in the conversation while **excluding the order-book balance ratio**, because historical bid/ask queue totals are not available from ordinary daily OHLCV data.

## Default screening rules

The defaults mirror the displayed condition as closely as practical with `pykrx` daily data:

- close: 0 ~ 100,000 KRW
- market cap: at least 300 billion KRW
- daily return: -7% ~ -1%
- trading value: 300 million ~ 100 billion KRW
- moving average: 20-day MA > 60-day MA
- order-book balance ratio: intentionally excluded

All numeric values can be changed with command-line options.

## Trading rules

- Evaluate the condition after each trading day's close.
- Randomly select up to 5 matching stocks (configurable).
- Buy at the **next trading day's open**. This avoids using the same day's close to both discover and retrospectively buy a stock.
- If the daily high reaches the configured profit target within the holding period, assume a resting limit order fills exactly at the target price.
- During the first 4 holding days, exit at the close when 2 consecutive down moves occur. By default a down move means `today close < previous trading-day close`.
- If neither exit occurs, sell at the close on the 7th holding day.
- If several exit conditions are true on one day, take-profit has priority because the target order is assumed to have been resting intraday.

The entry day is counted as holding day 1. For the default close-to-close stop, the first entry-day comparison uses the signal day's close as the previous close.

## Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

## Basic run

```bash
python backtest.py --start 2025-01-01 --end 2025-12-31
```

## Example with custom values

```bash
python backtest.py \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --daily-buy-count 5 \
  --price-max 100000 \
  --market-cap-min 300000000000 \
  --daily-return-min -7 \
  --daily-return-max -1 \
  --trading-value-min 300000000 \
  --trading-value-max 100000000000 \
  --short-ma 20 \
  --long-ma 60 \
  --take-profit 0.05 \
  --early-stop-window 4 \
  --consecutive-down-days 2 \
  --max-hold-days 7 \
  --initial-capital 100000000 \
  --position-size 1000000 \
  --seed 42
```

## Customizable options

| Option | Default | Meaning |
|---|---:|---|
| `--daily-buy-count` | 5 | Random stocks bought per signal day |
| `--price-min` | 0 | Minimum close price, KRW |
| `--price-max` | 100000 | Maximum close price, KRW |
| `--market-cap-min` | 300000000000 | Minimum market cap, KRW |
| `--market-cap-max` | none | Optional maximum market cap, KRW |
| `--daily-return-min` | -7 | Minimum daily return, percent |
| `--daily-return-max` | -1 | Maximum daily return, percent |
| `--trading-value-min` | 300000000 | Minimum daily trading value, KRW |
| `--trading-value-max` | 100000000000 | Maximum daily trading value, KRW |
| `--short-ma` | 20 | Short moving-average period |
| `--long-ma` | 60 | Long moving-average period |
| `--take-profit` | 0.05 | Profit target; 0.05 means +5% |
| `--early-stop-window` | 4 | Number of early holding days in which the consecutive-down stop is active |
| `--consecutive-down-days` | 2 | Consecutive down moves required for stop |
| `--max-hold-days` | 7 | Maximum holding days |
| `--down-bar-mode` | close_to_close | `close_to_close` or `red_candle` |
| `--initial-capital` | 100000000 | Starting cash, KRW |
| `--position-size` | 1000000 | Maximum cash allocated to each entry |
| `--no-reentry` | off | Prevent buying a ticker already held |
| `--seed` | 42 | Reproducible random selection seed |
| `--commission-rate` | 0 | Commission as decimal rate |
| `--sell-tax-rate` | 0 | Sell-side tax as decimal rate |
| `--slippage-bps` | 0 | Slippage in basis points on both sides |
| `--markets` | KOSPI,KOSDAQ | Markets to include |

## Outputs

Results are written under `results/` by default:

- `summary.json`: final return, realized P/L, trade count, win rate, exit counts, and the full configuration
- `trades.csv`: one row per completed position
- `signals.csv`: number of eligible names and the randomly selected tickers for each signal day
- `daily_equity.csv`: daily cash, market value, equity, daily return, and cumulative return

Set `--output-dir` to write somewhere else.

## Reproducibility and cache

The random draw is deterministic for the same `--seed` and the same historical data. Daily `pykrx` market snapshots are cached in `.cache/stocksim/` so repeated parameter tests do not need to download the same dates again.

## Important interpretation notes

1. The historical order-book condition is not used.
2. The MTS trading-value fields are represented here directly in KRW. The defaults assume the displayed 300 ~ 100,000 corresponds to 300 million ~ 100 billion KRW; change the two trading-value options if your MTS unit differs.
3. Corporate actions and data-provider adjustments can affect historical price comparisons. The backtest is a research approximation, not an exact replay of the Kiwoom matching engine.
4. With the default 100 million KRW capital and 1 million KRW position size, there is room for many overlapping 7-day positions. If available cash is insufficient, the simulator skips entries it cannot fund.
