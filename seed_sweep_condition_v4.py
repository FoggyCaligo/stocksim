from __future__ import annotations

import hashlib
import json
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from backtest import Config, validate
from pykrx import stock
from seed_sweep_fast import build_parser, config_from_args
from seed_sweep_percent_stop import _init_worker, _simulate_seed, _summarize


def _cache_path(config: Config, ma_long: int, ma_mid: int, ma_short: int) -> Path:
    key = {
        "start": config.start,
        "end": config.end,
        "price_min": config.price_min,
        "price_max": config.price_max,
        "market_cap_min": config.market_cap_min,
        "market_cap_max": config.market_cap_max,
        "daily_return_min": config.daily_return_min,
        "daily_return_max": config.daily_return_max,
        "trading_value_min": config.trading_value_min,
        "trading_value_max": config.trading_value_max,
        "markets": config.markets,
        "ma_long": ma_long,
        "ma_mid": ma_mid,
        "ma_short": ma_short,
        "condition": "ma_long<ma_mid<ma_short",
        "format_version": 1,
    }
    digest = hashlib.sha256(
        json.dumps(key, sort_keys=True, default=list).encode("utf-8")
    ).hexdigest()[:16]
    root = Path(config.cache_dir) / "prepared_condition_v4"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.pkl"


def prepare_condition(
    config: Config,
    ma_long: int,
    ma_mid: int,
    ma_short: int,
    rebuild: bool = False,
) -> tuple[dict[str, Any], Path, bool]:
    cache_path = _cache_path(config, ma_long, ma_mid, ma_short)
    if cache_path.exists() and not rebuild:
        with cache_path.open("rb") as fh:
            return pickle.load(fh), cache_path, True

    if not hasattr(stock, "_slice_years"):
        raise RuntimeError("This runner requires the repository's local marcap-backed pykrx shim.")

    start = pd.Timestamp(config.start).normalize()
    end = pd.Timestamp(config.end).normalize()
    warmup_start = start - pd.DateOffset(days=max(800, ma_long * 3))
    future_end = end + pd.DateOffset(days=max(30, config.max_hold_days * 3))

    frame = stock._slice_years(  # type: ignore[attr-defined]
        warmup_start.strftime("%Y%m%d"), future_end.strftime("%Y%m%d")
    ).copy()
    if frame.empty:
        raise RuntimeError("No marcap rows found for the requested period.")

    if config.markets and "Market" in frame.columns:
        wanted = {m.upper() for m in config.markets}
        frame = frame[frame["Market"].astype(str).str.upper().isin(wanted)].copy()

    ratio = stock._ratio_column(frame)  # type: ignore[attr-defined]
    frame = frame.assign(
        ticker=frame["Code"].astype(str).str.zfill(6),
        open=pd.to_numeric(frame["Open"], errors="coerce").fillna(0.0),
        high=pd.to_numeric(frame["High"], errors="coerce").fillna(0.0),
        close=pd.to_numeric(frame["Close"], errors="coerce").fillna(0.0),
        trading_value=pd.to_numeric(frame["Amount"], errors="coerce").fillna(0.0),
        daily_return=pd.to_numeric(ratio, errors="coerce").fillna(0.0),
        market_cap=pd.to_numeric(frame["Marcap"], errors="coerce").fillna(0.0),
    )
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.normalize()
    frame = frame.sort_values(["ticker", "Date"]).reset_index(drop=True)

    grouped_close = frame.groupby("ticker", sort=False)["close"]
    for days in sorted({ma_long, ma_mid, ma_short}):
        frame[f"ma_{days}"] = grouped_close.transform(
            lambda s, n=days: s.rolling(n, min_periods=n).mean()
        )

    dates = sorted(pd.Timestamp(x) for x in frame["Date"].drop_duplicates().tolist())
    signal_date_set = {d for d in dates if start <= d <= end}
    if not signal_date_set:
        raise RuntimeError("The requested period contains no trading days.")

    prices: dict[str, dict[str, tuple[float, float, float]]] = {}
    for d, day in frame.groupby("Date", sort=True):
        prices[pd.Timestamp(d).strftime("%Y-%m-%d")] = {
            row.ticker: (float(row.open), float(row.high), float(row.close))
            for row in day[["ticker", "open", "high", "close"]].itertuples(index=False)
            if float(row.close) > 0
        }

    signal_frame = frame[frame["Date"].isin(signal_date_set)].copy()
    eligible = (
        (signal_frame["close"] >= config.price_min)
        & (signal_frame["close"] <= config.price_max)
        & (signal_frame["market_cap"] >= config.market_cap_min)
        & (signal_frame["daily_return"] >= config.daily_return_min)
        & (signal_frame["daily_return"] <= config.daily_return_max)
        & (signal_frame["trading_value"] >= config.trading_value_min)
        & (signal_frame["trading_value"] <= config.trading_value_max)
        & (signal_frame[f"ma_{ma_long}"] < signal_frame[f"ma_{ma_mid}"])
        & (signal_frame[f"ma_{ma_mid}"] < signal_frame[f"ma_{ma_short}"])
    )
    if config.market_cap_max is not None:
        eligible &= signal_frame["market_cap"] <= config.market_cap_max
    signal_frame = signal_frame[eligible]

    candidates = {
        pd.Timestamp(d).strftime("%Y-%m-%d"): sorted(day["ticker"].astype(str).tolist())
        for d, day in signal_frame.groupby("Date", sort=True)
    }

    prepared = {
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "signal_start": start.strftime("%Y-%m-%d"),
        "signal_end": end.strftime("%Y-%m-%d"),
        "prices": prices,
        "candidates": candidates,
    }
    with cache_path.open("wb") as fh:
        pickle.dump(prepared, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return prepared, cache_path, False


def main() -> None:
    parser = build_parser()
    parser.description = "Multi-seed sweep for MTS condition MA240 < MA120 < MA60."
    parser.add_argument("--stop-loss-pct", type=float, default=0.05)
    parser.add_argument("--ma-long", type=int, default=240)
    parser.add_argument("--ma-mid", type=int, default=120)
    parser.add_argument("--ma-short", type=int, default=60)
    parser.set_defaults(output_dir="results/seed_sweep_condition_v4")
    args = parser.parse_args()

    if args.seed_end < args.seed_start:
        raise ValueError("seed-end must be >= seed-start")
    if args.workers < 1:
        raise ValueError("workers must be >= 1")
    if not 0 < args.stop_loss_pct < 1:
        raise ValueError("stop-loss-pct must be between 0 and 1")
    if not (args.ma_long > args.ma_mid > args.ma_short >= 1):
        raise ValueError("MA periods must satisfy ma-long > ma-mid > ma-short >= 1")

    config = config_from_args(args)
    validate(config)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("[1/2] preparing MA240 < MA120 < MA60 candidates once...")
    prepared, prepared_path, reused = prepare_condition(
        config, args.ma_long, args.ma_mid, args.ma_short, args.rebuild_prepared_cache
    )
    print(
        f"prepared cache: {prepared_path} "
        f"({'reused' if reused else 'built'}) / {len(prepared['dates'])} trading days"
    )
    print(f"condition: MA{args.ma_long} < MA{args.ma_mid} < MA{args.ma_short}")
    print(f"percent stop: -{args.stop_loss_pct * 100:.2f}% based on daily close")

    seeds = list(range(args.seed_start, args.seed_end + 1))
    worker_count = min(args.workers, len(seeds), os.cpu_count() or 1)
    print(f"[2/2] running {len(seeds)} seeds with {worker_count} process(es)...")

    config_dict = asdict(config)
    rows: list[dict[str, Any]] = []
    if worker_count == 1:
        _init_worker(str(prepared_path), config_dict, args.stop_loss_pct)
        for n, seed in enumerate(seeds, start=1):
            rows.append(_simulate_seed(seed))
            print(f"[{n}/{len(seeds)}] seed {seed} complete")
    else:
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_init_worker,
            initargs=(str(prepared_path), config_dict, args.stop_loss_pct),
        ) as pool:
            for n, row in enumerate(pool.map(_simulate_seed, seeds), start=1):
                rows.append(row)
                print(f"[{n}/{len(seeds)}] seed {row['seed']} complete")

    results = pd.DataFrame(rows).sort_values("seed").reset_index(drop=True)
    results.to_csv(out / "seed_results.csv", index=False, encoding="utf-8-sig")
    aggregate = _summarize(results, config, args.seed_start, args.seed_end, args.stop_loss_pct)
    aggregate["condition"] = {
        "ma_order": [args.ma_long, args.ma_mid, args.ma_short],
        "relation": "<",
    }
    aggregate["workers"] = worker_count
    aggregate["prepared_cache"] = str(prepared_path)
    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(aggregate, fh, ensure_ascii=False, indent=2)

    print("\n=== MA240 < MA120 < MA60 summary ===")
    print(json.dumps(aggregate["return_stats_pct"], ensure_ascii=False, indent=2))
    print(f"saved: {out / 'seed_results.csv'}")
    print(f"saved: {out / 'seed_summary.json'}")


if __name__ == "__main__":
    main()
