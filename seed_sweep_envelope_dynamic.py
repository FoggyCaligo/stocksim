from __future__ import annotations

import json
import os
import pickle
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from backtest import Config, buy_price, validate
from pykrx import stock
from seed_sweep_fast import build_parser, config_from_args, summarize
from seed_sweep_kiwoom import kiwoom_sell_tax_rate


@dataclass
class DynamicPosition:
    ticker: str
    entry_price: float
    quantity: int
    invested: float
    target_price: float
    stop_price: float
    max_hold_days: int
    holding_days: int = 0


_PREPARED: dict[str, Any] | None = None
_CONFIG: Config | None = None


def _cache_path(config: Config, ma_long: int, ma_mid: int, ma_short: int, envelope_period: int,
                envelope_percent: float, stop_gap_ratio: float, touch_lookback_days: int) -> Path:
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
        "ma_order": [ma_long, ma_mid, ma_short],
        "envelope_period": envelope_period,
        "envelope_percent": envelope_percent,
        "stop_gap_ratio": stop_gap_ratio,
        "touch_lookback_days": touch_lookback_days,
        "strategy": "envelope_mid_target_recent_touch_hold_half_gap_stop",
        "format_version": 1,
    }
    import hashlib
    digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=list).encode("utf-8")).hexdigest()[:16]
    root = Path(config.cache_dir) / "prepared_envelope_dynamic"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.pkl"


def _prepare(
    config: Config,
    ma_long: int,
    ma_mid: int,
    ma_short: int,
    envelope_period: int,
    envelope_percent: float,
    stop_gap_ratio: float,
    touch_lookback_days: int,
    rebuild: bool,
) -> tuple[dict[str, Any], Path, bool]:
    cache_path = _cache_path(
        config, ma_long, ma_mid, ma_short, envelope_period, envelope_percent,
        stop_gap_ratio, touch_lookback_days,
    )
    if cache_path.exists() and not rebuild:
        with cache_path.open("rb") as fh:
            return pickle.load(fh), cache_path, True

    if not hasattr(stock, "_slice_years"):
        raise RuntimeError("This runner requires the repository's local marcap-backed pykrx shim.")

    start = pd.Timestamp(config.start).normalize()
    end = pd.Timestamp(config.end).normalize()

    # Warm up both the moving averages and the historical target-touch search.
    warmup_days = max(800, ma_long * 3, envelope_period * 3, touch_lookback_days * 2)
    warmup_start = start - pd.DateOffset(days=warmup_days)

    # Dynamic holding periods can be much longer than the older fixed 20-day sweep.
    # config.max_hold_days is used only as a safety/data horizon cap here.
    future_days = max(30, config.max_hold_days * 3)
    future_end = end + pd.DateOffset(days=future_days)

    frame = stock._slice_years(
        warmup_start.strftime("%Y%m%d"), future_end.strftime("%Y%m%d")
    ).copy()
    if frame.empty:
        raise RuntimeError("No marcap rows found for the requested period.")

    if config.markets and "Market" in frame.columns:
        wanted = {m.upper() for m in config.markets}
        frame = frame[frame["Market"].astype(str).str.upper().isin(wanted)].copy()

    ratio = stock._ratio_column(frame)
    frame = frame.assign(
        ticker=frame["Code"].astype(str).str.zfill(6),
        open=pd.to_numeric(frame["Open"], errors="coerce").fillna(0.0),
        high=pd.to_numeric(frame["High"], errors="coerce").fillna(0.0),
        low=pd.to_numeric(frame["Low"], errors="coerce").fillna(0.0),
        close=pd.to_numeric(frame["Close"], errors="coerce").fillna(0.0),
        trading_value=pd.to_numeric(frame["Amount"], errors="coerce").fillna(0.0),
        daily_return=pd.to_numeric(ratio, errors="coerce").fillna(0.0),
        market_cap=pd.to_numeric(frame["Marcap"], errors="coerce").fillna(0.0),
    )
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.normalize()
    frame = frame.sort_values(["ticker", "Date"]).reset_index(drop=True)

    grouped_close = frame.groupby("ticker", sort=False)["close"]
    for days in sorted({ma_long, ma_mid, ma_short, envelope_period}):
        frame[f"ma_{days}"] = grouped_close.transform(
            lambda s, n=days: s.rolling(n, min_periods=n).mean()
        )

    dates = sorted(pd.Timestamp(x) for x in frame["Date"].drop_duplicates().tolist())
    date_strings = [d.strftime("%Y-%m-%d") for d in dates]
    signal_date_set = {d for d in dates if start <= d <= end}
    if not signal_date_set:
        raise RuntimeError("The requested period contains no trading days.")

    prices: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    for d, day in frame.groupby("Date", sort=True):
        prices[pd.Timestamp(d).strftime("%Y-%m-%d")] = {
            row.ticker: (float(row.open), float(row.high), float(row.low), float(row.close))
            for row in day[["ticker", "open", "high", "low", "close"]].itertuples(index=False)
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

    envelope_mid = signal_frame[f"ma_{envelope_period}"]
    envelope_lower = envelope_mid * (1.0 - envelope_percent / 100.0)
    eligible &= signal_frame["close"] <= envelope_lower
    signal_frame = signal_frame[eligible].copy()

    # Strategy metadata is defined at the SIGNAL close. The actual entry is the next trading day's open.
    # target = signal-day Envelope center (SMA)
    # max hold = number of trading sessions since the most recent historical candle whose range touched
    #            that fixed target price. A safety cap of config.max_hold_days is applied.
    # stop = entry - stop_gap_ratio * (target - entry), calculated after the next-day entry is known.
    by_ticker = {ticker: grp.reset_index(drop=True) for ticker, grp in frame.groupby("ticker", sort=False)}
    date_index = {d: i for i, d in enumerate(dates)}
    strategy_by_date: dict[str, dict[str, dict[str, float | int | str | None]]] = {}
    candidates: dict[str, list[str]] = {}

    for row in signal_frame[["Date", "ticker", f"ma_{envelope_period}"]].itertuples(index=False, name=None):
        signal_date, ticker, target_raw = row
        signal_date = pd.Timestamp(signal_date)
        target = float(target_raw)
        if target <= 0:
            continue

        hist = by_ticker[str(ticker)]
        before = hist[hist["Date"] < signal_date]
        if touch_lookback_days > 0:
            before = before.tail(touch_lookback_days)
        touched = before[(before["low"] <= target) & (before["high"] >= target)]
        if touched.empty:
            continue

        touch_date = pd.Timestamp(touched.iloc[-1]["Date"])
        raw_hold_days = date_index[signal_date] - date_index[touch_date]
        if raw_hold_days < 1:
            continue
        hold_days = min(raw_hold_days, config.max_hold_days)

        d = signal_date.strftime("%Y-%m-%d")
        candidates.setdefault(d, []).append(str(ticker))
        strategy_by_date.setdefault(d, {})[str(ticker)] = {
            "target_price": target,
            "recent_touch_date": touch_date.strftime("%Y-%m-%d"),
            "raw_hold_days": int(raw_hold_days),
            "max_hold_days": int(hold_days),
        }

    for d in list(candidates):
        candidates[d] = sorted(candidates[d])

    prepared = {
        "dates": date_strings,
        "signal_start": start.strftime("%Y-%m-%d"),
        "signal_end": end.strftime("%Y-%m-%d"),
        "prices": prices,
        "candidates": candidates,
        "strategy": strategy_by_date,
        "stop_gap_ratio": stop_gap_ratio,
    }
    with cache_path.open("wb") as fh:
        pickle.dump(prepared, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return prepared, cache_path, False


def _init_worker(prepared_path: str, config_dict: dict[str, Any]) -> None:
    global _PREPARED, _CONFIG
    with Path(prepared_path).open("rb") as fh:
        _PREPARED = pickle.load(fh)
    config_dict = dict(config_dict)
    config_dict["markets"] = tuple(config_dict["markets"])
    _CONFIG = Config(**config_dict)


def _simulate_seed(seed: int) -> dict[str, Any]:
    if _PREPARED is None or _CONFIG is None:
        raise RuntimeError("Worker was not initialized.")

    prepared = _PREPARED
    config = _CONFIG
    rng = random.Random(seed)
    dates: list[str] = prepared["dates"]
    prices: dict[str, dict[str, tuple[float, float, float, float]]] = prepared["prices"]
    candidates_by_date: dict[str, list[str]] = prepared["candidates"]
    strategy_by_date: dict[str, dict[str, dict[str, Any]]] = prepared["strategy"]
    signal_start: str = prepared["signal_start"]
    signal_end: str = prepared["signal_end"]
    stop_gap_ratio = float(prepared["stop_gap_ratio"])

    cash = float(config.initial_capital)
    positions: list[DynamicPosition] = []
    pending: list[tuple[str, dict[str, Any]]] = []
    trade_count = winners = skipped_target_below_entry = 0
    realized_pnl = total_commission = total_sell_tax = 0.0
    exits = {"target": 0, "dynamic_stop": 0, "dynamic_hold_exit": 0}

    for idx, d in enumerate(dates):
        day_prices = prices.get(d, {})

        entering, pending = pending, []
        for ticker, meta in entering:
            px = day_prices.get(ticker)
            if px is None:
                continue
            if (not config.allow_reentry) and any(p.ticker == ticker for p in positions):
                continue
            raw_open = px[0]
            if raw_open <= 0:
                continue
            entry = buy_price(raw_open, config)
            target = float(meta["target_price"])
            # If the next-day gap-up already puts the entry at/above the signal-day center line,
            # the intended mean-reversion trade no longer has positive target distance.
            if target <= entry:
                skipped_target_below_entry += 1
                continue
            stop = entry - stop_gap_ratio * (target - entry)
            qty = int(min(config.position_size, cash) // (entry * (1.0 + config.commission_rate)))
            if qty < 1:
                continue
            gross = qty * entry
            buy_commission = gross * config.commission_rate
            invested = gross + buy_commission
            cash -= invested
            total_commission += buy_commission
            positions.append(
                DynamicPosition(
                    ticker=ticker,
                    entry_price=entry,
                    quantity=qty,
                    invested=invested,
                    target_price=target,
                    stop_price=stop,
                    max_hold_days=int(meta["max_hold_days"]),
                )
            )

        survivors: list[DynamicPosition] = []
        for p in positions:
            px = day_prices.get(p.ticker)
            if px is None:
                survivors.append(p)
                continue
            _, current_high, current_low, current_close = px
            p.holding_days += 1

            reason: str | None = None
            exit_price: float | None = None

            # Daily OHLC cannot tell which intraday level came first. Keep the existing sweep convention:
            # target has priority when both target and stop were inside the day's range.
            if current_high >= p.target_price:
                reason, exit_price = "target", p.target_price
            elif current_low <= p.stop_price:
                reason, exit_price = "dynamic_stop", p.stop_price
            elif p.holding_days >= p.max_hold_days:
                reason, exit_price = "dynamic_hold_exit", current_close

            if reason is None or exit_price is None:
                survivors.append(p)
                continue

            gross_sale = p.quantity * exit_price
            sell_commission = gross_sale * config.commission_rate
            sell_tax = gross_sale * (kiwoom_sell_tax_rate(d) + config.sell_tax_rate)
            slippage = gross_sale * config.slippage_bps / 10_000.0
            proceeds = gross_sale - sell_commission - sell_tax - slippage
            pnl = proceeds - p.invested
            cash += proceeds
            realized_pnl += pnl
            total_commission += sell_commission
            total_sell_tax += sell_tax
            trade_count += 1
            winners += int(pnl > 0)
            exits[reason] += 1
        positions = survivors

        if signal_start <= d <= signal_end:
            candidates = list(candidates_by_date.get(d, ()))
            if not config.allow_reentry:
                active = {p.ticker for p in positions}
                candidates = [t for t in candidates if t not in active]
            rng.shuffle(candidates)
            for ticker in candidates[: config.daily_buy_count]:
                if idx + 1 >= len(dates):
                    continue
                meta = strategy_by_date.get(d, {}).get(ticker)
                if meta is not None:
                    pending.append((ticker, meta))

        if d > signal_end and not positions and not pending:
            break

    final_equity = cash
    if positions:
        last_prices = prices.get(dates[-1], {})
        for p in positions:
            px = last_prices.get(p.ticker)
            final_equity += p.quantity * (px[3] if px else p.entry_price)

    return {
        "seed": seed,
        "total_return_pct": round((final_equity / config.initial_capital - 1.0) * 100.0, 4),
        "final_equity_krw": round(final_equity, 2),
        "total_realized_pnl_krw": round(realized_pnl, 2),
        "trade_count": trade_count,
        "win_rate_pct": round(winners / trade_count * 100.0, 4) if trade_count else 0.0,
        "target_count": exits["target"],
        "dynamic_stop_count": exits["dynamic_stop"],
        "dynamic_hold_exit_count": exits["dynamic_hold_exit"],
        "skipped_target_below_entry": skipped_target_below_entry,
        "total_commission_krw": round(total_commission, 2),
        "total_sell_tax_krw": round(total_sell_tax, 2),
    }


def main() -> None:
    parser = build_parser()
    parser.description = (
        "Kiwoom-style MA-order + Envelope lower-band sweep with dynamic Envelope-center target, "
        "recent-touch holding period, and half-target-gap stop. Historical order-book ratio is omitted."
    )
    parser.add_argument("--ma-long", type=int, default=240)
    parser.add_argument("--ma-mid", type=int, default=120)
    parser.add_argument("--ma-short", type=int, default=60)
    parser.add_argument("--envelope-period", type=int, default=20)
    parser.add_argument("--envelope-percent", type=float, default=6.0)
    parser.add_argument(
        "--stop-gap-ratio",
        type=float,
        default=0.5,
        help="Stop distance below entry as a fraction of (target-entry). 0.5 means half the upside gap.",
    )
    parser.add_argument(
        "--touch-lookback-days",
        type=int,
        default=240,
        help="Maximum number of prior trading rows searched for the latest candle touching the fixed target price.",
    )
    parser.set_defaults(
        # Screenshot condition: 0~100,000 KRW, market cap >= 300 billion KRW,
        # -7~-1% daily return, trading value 300 million~100 billion KRW.
        price_min=0,
        price_max=100_000,
        market_cap_min=300_000_000_000,
        daily_return_min=-7.0,
        daily_return_max=-1.0,
        trading_value_min=300_000_000,
        trading_value_max=100_000_000_000,
        # This is a safety/data-horizon cap; each trade gets its own recent-touch-derived hold period.
        max_hold_days=240,
        commission_rate=0.00015,
        sell_tax_rate=0.0,
        output_dir="results/seed_sweep_envelope_dynamic",
    )
    args = parser.parse_args()

    if args.seed_end < args.seed_start:
        raise ValueError("seed-end must be >= seed-start")
    if args.workers < 1:
        raise ValueError("workers must be >= 1")
    if not (args.ma_long > args.ma_mid > args.ma_short >= 1):
        raise ValueError("MA periods must satisfy ma-long > ma-mid > ma-short >= 1")
    if args.envelope_period < 1:
        raise ValueError("envelope-period must be >= 1")
    if not 0 < args.envelope_percent < 100:
        raise ValueError("envelope-percent must be between 0 and 100")
    if not 0 < args.stop_gap_ratio <= 2:
        raise ValueError("stop-gap-ratio must be > 0 and <= 2")
    if args.touch_lookback_days < 1:
        raise ValueError("touch-lookback-days must be >= 1")

    config = config_from_args(args)
    validate(config)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("[1/2] preparing condition candidates and dynamic target/hold metadata...")
    prepared, prepared_path, reused = _prepare(
        config,
        args.ma_long,
        args.ma_mid,
        args.ma_short,
        args.envelope_period,
        args.envelope_percent,
        args.stop_gap_ratio,
        args.touch_lookback_days,
        args.rebuild_prepared_cache,
    )
    candidate_count = sum(len(v) for v in prepared["candidates"].values())
    print(f"prepared cache: {prepared_path} ({'reused' if reused else 'built'})")
    print(f"candidate signals: {candidate_count}")
    print(
        f"condition: price {config.price_min:,}~{config.price_max:,} / market cap >= {config.market_cap_min:,} / "
        f"daily return {config.daily_return_min:g}~{config.daily_return_max:g}% / "
        f"trading value {config.trading_value_min:,}~{config.trading_value_max:,} / "
        f"MA{args.ma_long}<MA{args.ma_mid}<MA{args.ma_short} / "
        f"close<=Envelope({args.envelope_period},{args.envelope_percent:g}%) lower"
    )
    print("order-book ratio: omitted (historical marcap data has no comparable order-book snapshot)")
    print(
        f"exit: target=signal-day Envelope center / stop=entry-{args.stop_gap_ratio:g}*(target-entry) / "
        f"hold=days since most recent target-price touch (cap {config.max_hold_days} trading days)"
    )

    seeds = list(range(args.seed_start, args.seed_end + 1))
    workers = min(args.workers, len(seeds), os.cpu_count() or 1)
    print(f"[2/2] running {len(seeds)} seeds with {workers} process(es)...")
    config_dict = asdict(config)
    rows: list[dict[str, Any]] = []

    if workers == 1:
        _init_worker(str(prepared_path), config_dict)
        for n, seed in enumerate(seeds, 1):
            rows.append(_simulate_seed(seed))
            print(f"[{n}/{len(seeds)}] seed {seed} complete")
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(str(prepared_path), config_dict),
        ) as pool:
            for n, row in enumerate(pool.map(_simulate_seed, seeds), 1):
                rows.append(row)
                print(f"[{n}/{len(seeds)}] seed {row['seed']} complete")

    results = pd.DataFrame(rows).sort_values("seed").reset_index(drop=True)
    results.to_csv(out / "seed_results.csv", index=False, encoding="utf-8-sig")
    aggregate = summarize(results, config, args.seed_start, args.seed_end)
    aggregate["workers"] = workers
    aggregate["prepared_cache"] = str(prepared_path)
    aggregate["candidate_signal_count"] = candidate_count
    aggregate["condition"] = {
        "order_book_ratio": "omitted",
        "ma_order": [args.ma_long, args.ma_mid, args.ma_short],
        "relation": "<",
        "envelope": {
            "period": args.envelope_period,
            "percent": args.envelope_percent,
            "relation": "close<=lower_band",
        },
    }
    aggregate["dynamic_exit"] = {
        "target": "signal_day_envelope_center",
        "hold": "trading_days_since_latest_prior_candle_range_touching_fixed_target",
        "hold_cap_days": config.max_hold_days,
        "touch_lookback_days": args.touch_lookback_days,
        "stop": "entry - stop_gap_ratio * (target - entry)",
        "stop_gap_ratio": args.stop_gap_ratio,
        "same_day_target_stop_priority": "target",
    }
    aggregate["cost_model"] = {
        "broker": "Kiwoom Securities",
        "commission_rate_each_side": config.commission_rate,
        "historical_sell_tax_rates": {
            "2022": 0.0023,
            "2023": 0.0020,
            "2024": 0.0018,
            "2025": 0.0015,
            "2026": 0.0020,
        },
    }
    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(aggregate, fh, ensure_ascii=False, indent=2)

    print("\n=== Dynamic Envelope strategy summary ===")
    print(json.dumps(aggregate["return_stats_pct"], ensure_ascii=False, indent=2))
    print(f"saved: {out / 'seed_results.csv'}")
    print(f"saved: {out / 'seed_summary.json'}")


if __name__ == "__main__":
    main()
