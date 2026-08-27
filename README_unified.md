# Unified seed sweep

`seed_sweep_unified.py` replaces the strategy-specific sweep variants with one configurable runner.

## Rules

- `--seed-start` / `--seed-end` default to `1..100`.
- Screening arguments are optional. If a min/max or indicator filter is omitted, that screening condition is not applied.
- `--ma-long`, `--ma-mid`, and `--ma-short` must be supplied together. When present, the condition is `MA long < MA mid < MA short`.
- `--envelope-period` and `--envelope-percent` must be supplied together. When present:
  - candidate condition: close <= Envelope lower band
  - target: midpoint between Envelope center and upper band
  - holding period: number of trading sessions since the most recent historical candle whose high/low range touched the target
- `--stop-gap-ratio 0.5` means `stop = entry - 0.5 * (target - entry)`.
- `--touch-lookback-days` omitted means no additional tail cutoff is applied inside the historical data loaded by the runner.
- `--max-hold-days` omitted means the recent-touch-derived holding period is not capped.
- Daily OHLC cannot reveal whether target or stop was touched first. As in the previous sweep convention, target has priority if both are inside the same daily range.

## Current SwingDanta-style experiment

This reproduces the current condition without the order-book balance ratio and adds the Envelope-lower condition:

```bash
python seed_sweep_unified.py \
  --start 2022-01-01 \
  --end 2025-07-31 \
  --workers 4 \
  --daily-buy-count 5 \
  --price-min 0 \
  --price-max 100000 \
  --market-cap-min 300000000000 \
  --daily-return-min -7 \
  --daily-return-max -1 \
  --trading-value-min 300000000 \
  --trading-value-max 100000000000 \
  --ma-long 240 \
  --ma-mid 120 \
  --ma-short 60 \
  --envelope-period 20 \
  --envelope-percent 6 \
  --stop-gap-ratio 0.5 \
  --initial-capital 1000000 \
  --position-size 100000 \
  --no-reentry
```

No seed arguments are necessary for the normal run because `1..100` is the default.

Outputs are written to `results/seed_sweep_unified/seed_results.csv` and `seed_summary.json` unless `--output-dir` is specified.
