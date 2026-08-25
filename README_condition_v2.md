# Updated MTS condition sweep

`seed_sweep_condition_v2.py` matches the updated MTS screenshot:

- Close: 0 to 100,000 KRW
- Market cap: at least 300 billion KRW
- Daily return: -7% to -1%
- Trading value: 300 to 100,000 in the existing mapped units
- MA120 < MA60
- MA60 > MA3 < MA10

The runner keeps the percentage stop-loss behavior from `seed_sweep_percent_stop.py` and reuses multiprocessing/prepared-cache mechanics. Moving-average periods and the percentage stop are CLI-configurable.
