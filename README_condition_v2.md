# Condition sweep runner

`seed_sweep_condition_v4_kiwoom.py` reproduces the historical parts of the Kiwoom condition filter and can additionally require the close to be at or below the Envelope lower band.

For a Kiwoom-style 1-month hold comparison with no early exits, use `--no-take-profit --no-stop-loss --max-hold-days 20`.

Example:

```bash
python seed_sweep_condition_v4_kiwoom.py \
  --start 2022-01-01 \
  --end 2026-07-31 \
  --seed-start 1 \
  --seed-end 100 \
  --workers 4 \
  --daily-buy-count 5 \
  --max-hold-days 20 \
  --no-take-profit \
  --no-stop-loss
```

Envelope defaults to 20 periods and 6%. Override with `--envelope-period` and `--envelope-percent`.
