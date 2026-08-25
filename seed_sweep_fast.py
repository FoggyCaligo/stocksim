from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import random
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from backtest import Config, buy_price, net_sell, validate
from pykrx import stock


@dataclass
class FastPosition:
    ticker: str
    entry_price: float
    quantity: int
    invested: float
    prev_close: float
    consecutive_down: int = 0
    holding_days: int = 0


_PREPARED: dict[str, Any] | None = None
_WORKER_CONFIG: Config | None = None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fast multi-seed sweep: precompute market/filter data once, then run seeds in parallel."
    )
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--seed-start", type=int, default=1)
    p.add_argument("--seed-end", type=int, default=100)
    p.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))

    p.add_argument("--daily-buy-count", type=int, default=5)
    p.add_argument("--price-min", type=int, default=0)
    p.add_argument("--price-max", type=int, default=100_000)
    p.add_argument("--market-cap-min", type=int, default=300_000_000_000)
    p.add_argument("--market-cap-max", type=int, default=None)
    p.add_argument("--daily-return-min", type=float, default=-7.0)
    p.add_argument("--daily-return-max", type=float, default=-1.0)
    p.add_argument("--trading-value-min", type=int, default=300_000_000)
    p.add_argument("--trading-value-max", type=int, default=100_000_000_000)
    p.add_argument("--short-ma", type=int, default=20)
    p.add_argument("--long-ma", type=int, default=60)
    p.add_argument("--take-profit", type=float, default=0.05)
    p.add_argument("--early-stop-window", type=int, default=2)
    p.add_argument("--consecutive-down-days", type=int, default=3)
    p.add_argument("--max-hold-days", type=int, default=7)
    p.add_argument(
        "--down-bar-mode",
        choices=["close_to_close", "red_candle"],
        default="close_to_close",
    )
    p.add_argument("--initial-capital", type=int, default=1_000_000)
    p.add_argument("--position-size", type=int, default=100_000)
    p.add_argument("--no-reentry", action="store_true")
    p.add_argument("--commission-rate", type=float, default=0.0)
    p.add_argument("--sell-tax-rate", type=float, default=0.0)
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.add_argument("--markets", default="KOSPI,KOSDAQ")
    p.add_argument("--cache-dir", default=".cache/stocksim")
    p.add_argument("--output-dir", default="results/seed_sweep_fast")
    p.add_argument(
        "--rebuild-prepared-cache",
        action="store_true",
        help="Ignore a matching precomputed sweep cache and rebuild it.",
    )
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    return Config(
        start=args.start,
        end=args.end,
        daily_buy_count=args.daily_buy_count,
        price_min=args.price_min,
        price_max=args.price_max,
        market_cap_min=args.market_cap_min,
        market_cap_max=args.market_cap_max,
        daily_return_min=args.daily_return_min,
        daily_return_max=args.daily_return_max,
        trading_value_min=args.trading_value_min,
        trading_value_max=args.trading_value_max,
        short_ma=args.short_ma,
        long_ma=args.long_ma,
        take_profit=args.take_profit,
        early_stop_window=args.early_stop_window,
        consecutive_down_days=args.consecutive_down_days,
        max_hold_days=args.max_hold_days,
        down_bar_mode=args.down_bar_mode,
        initial_capital=args.initial_capital,
        position_size=args.position_size,
        allow_reentry=not args.no_reentry,
        seed=args.seed_start,
        commission_rate=args.commission_rate,
        sell_tax_rate=args.sell_tax_rate,
        slippage_bps=args.slippage_bps,
        markets=tuple(x.strip().upper() for x in args.markets.split(",") if x.strip()),
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
    )


def _prepared_cache_path(config: Config) -> Path:
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
        "short_ma": config.short_ma,
        "long_ma": config.long_ma,
        "max_hold_days": config.max_hold_days,
        "markets": config.markets,
        "format_version": 1,
    }
    digest = hashlib.sha256(
        json.dumps(key, sort_keys=True, default=list).encode("utf-8")
    ).hexdigest()[:16]
    root = Path(config.cache_dir) / "prepared_seed_sweeps"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.pkl"


def _load_raw_period(config: Config) -> pd.DataFrame:
    if not hasattr(stock, "_slice_years"):
        raise RuntimeError(
            "Fast sweep requires the repository's local marcap-backed pykrx shim. "
            "Run `git pull` and try again."
        )

    start = pd.Timestamp(config.start).normalize()
    end = pd.Timestamp(config.end).normalize()
    warmup_days = int(max(120, config.long_ma * 3))
    future_days = int(max(30, config.max_hold_days * 3))
    warmup_start = start - pd.DateOffset(days=warmup_days)
    future_end = end + pd.DateOffset(days=future_days)

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
    frame["short_ma_value"] = grouped_close.transform(
        lambda s: s.rolling(config.short_ma, min_periods=config.short_ma).mean()
    )
    frame["long_ma_value"] = grouped_close.transform(
        lambda s: s.rolling(config.long_ma, min_periods=config.long_ma).mean()
    )
    return frame


def prepare(config: Config, rebuild: bool = False) -> tuple[dict[str, Any], Path, bool]:
    cache_path = _prepared_cache_path(config)
    if cache_path.exists() and not rebuild:
        with cache_path.open("rb") as fh:
            return pickle.load(fh), cache_path, True

    frame = _load_raw_period(config)
    start = pd.Timestamp(config.start).normalize()
    end = pd.Timestamp(config.end).normalize()

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
        & (signal_frame["short_ma_value"] > signal_frame["long_ma_value"])
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


def _init_worker(prepared_path: str, config_dict: dict[str, Any]) -> None:
    global _PREPARED, _WORKER_CONFIG
    with Path(prepared_path).open("rb") as fh:
        _PREPARED = pickle.load(fh)
    config_dict = dict(config_dict)
    config_dict["markets"] = tuple(config_dict["markets"])
    _WORKER_CONFIG = Config(**config_dict)


def _simulate_seed(seed: int) -> dict[str, Any]:
    if _PREPARED is None or _WORKER_CONFIG is None:
        raise RuntimeError("Worker was not initialized.")
    prepared = _PREPARED
    config = _WORKER_CONFIG
    rng = random.Random(seed)

    dates: list[str] = prepared["dates"]
    prices: dict[str, dict[str, tuple[float, float, float]]] = prepared["prices"]
    candidates_by_date: dict[str, list[str]] = prepared["candidates"]
    signal_start: str = prepared["signal_start"]
    signal_end: str = prepared["signal_end"]

    cash = float(config.initial_capital)
    positions: list[FastPosition] = []
    pending: list[tuple[str, float]] = []
    trade_count = 0
    winners = 0
    realized_pnl = 0.0
    exit_counts = {
        "take_profit": 0,
        "consecutive_down_stop": 0,
        "max_hold_exit": 0,
    }

    for idx, d in enumerate(dates):
        day_prices = prices.get(d, {})

        if pending:
            new_pending = pending
            pending = []
            for ticker, signal_close in new_pending:
                px = day_prices.get(ticker)
                if px is None:
                    continue
                if (not config.allow_reentry) and any(p.ticker == ticker for p in positions):
                    continue
                raw_open = px[0]
                if raw_open <= 0:
                    continue
                entry = buy_price(raw_open, config)
                qty = int(
                    min(config.position_size, cash)
                    // (entry * (1.0 + config.commission_rate))
                )
                if qty < 1:
                    continue
                gross = qty * entry
                invested = gross + gross * config.commission_rate
                cash -= invested
                positions.append(
                    FastPosition(
                        ticker=ticker,
                        entry_price=entry,
                        quantity=qty,
                        invested=invested,
                        prev_close=signal_close,
                    )
                )

        survivors: list[FastPosition] = []
        for p in positions:
            px = day_prices.get(p.ticker)
            if px is None:
                survivors.append(p)
                continue
            current_open, current_high, current_close = px
            p.holding_days += 1

            if config.down_bar_mode == "close_to_close":
                is_down = current_close < p.prev_close
            else:
                is_down = current_close < current_open
            p.consecutive_down = p.consecutive_down + 1 if is_down else 0
            p.prev_close = current_close

            target = p.entry_price * (1.0 + config.take_profit)
            reason: str | None = None
            exit_price: float | None = None
            if p.holding_days <= config.max_hold_days and current_high >= target:
                reason = "take_profit"
                exit_price = target
            elif (
                p.holding_days <= config.early_stop_window
                and p.consecutive_down >= config.consecutive_down_days
            ):
                reason = "consecutive_down_stop"
                exit_price = current_close
            elif p.holding_days >= config.max_hold_days:
                reason = "max_hold_exit"
                exit_price = current_close

            if reason is None or exit_price is None:
                survivors.append(p)
                continue

            proceeds = net_sell(p.quantity * exit_price, config)
            pnl = proceeds - p.invested
            cash += proceeds
            realized_pnl += pnl
            trade_count += 1
            winners += int(pnl > 0)
            exit_counts[reason] += 1
        positions = survivors

        if signal_start <= d <= signal_end:
            candidates = list(candidates_by_date.get(d, ()))
            if not config.allow_reentry:
                active = {p.ticker for p in positions}
                candidates = [ticker for ticker in candidates if ticker not in active]
            rng.shuffle(candidates)
            selected = candidates[: config.daily_buy_count]
            if idx + 1 < len(dates):
                for ticker in selected:
                    px = day_prices.get(ticker)
                    if px is not None:
                        pending.append((ticker, px[2]))

        if d > signal_end and not positions and not pending:
            break

    final_equity = cash
    if positions:
        last_prices = prices.get(dates[-1], {})
        for p in positions:
            px = last_prices.get(p.ticker)
            final_equity += p.quantity * (px[2] if px else p.entry_price)

    total_return = (final_equity / config.initial_capital - 1.0) * 100.0
    return {
        "seed": seed,
        "total_return_pct": round(total_return, 4),
        "final_equity_krw": round(final_equity, 2),
        "total_realized_pnl_krw": round(realized_pnl, 2),
        "trade_count": trade_count,
        "win_rate_pct": round(winners / trade_count * 100.0, 4) if trade_count else 0.0,
        "take_profit_count": exit_counts["take_profit"],
        "consecutive_down_stop_count": exit_counts["consecutive_down_stop"],
        "max_hold_exit_count": exit_counts["max_hold_exit"],
    }


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(pd.Series(values, dtype="float64").quantile(q))


def summarize(results: pd.DataFrame, config: Config, seed_start: int, seed_end: int) -> dict[str, Any]:
    returns = results["total_return_pct"].astype(float).tolist()
    wins = results["win_rate_pct"].astype(float).tolist()
    best_row = results.loc[results["total_return_pct"].idxmax()].to_dict()
    worst_row = results.loc[results["total_return_pct"].idxmin()].to_dict()
    return {
        "seed_start": seed_start,
        "seed_end": seed_end,
        "seed_count": len(results),
        "base_config": {k: v for k, v in asdict(config).items() if k not in {"seed", "output_dir"}},
        "return_stats_pct": {
            "mean": round(statistics.fmean(returns), 4),
            "median": round(statistics.median(returns), 4),
            "stdev": round(statistics.pstdev(returns), 4) if len(returns) > 1 else 0.0,
            "min": round(min(returns), 4),
            "p05": round(percentile(returns, 0.05), 4),
            "p25": round(percentile(returns, 0.25), 4),
            "p75": round(percentile(returns, 0.75), 4),
            "p95": round(percentile(returns, 0.95), 4),
            "max": round(max(returns), 4),
            "positive_seed_rate_pct": round(sum(x > 0 for x in returns) / len(returns) * 100.0, 4),
            "over_10pct_seed_rate_pct": round(sum(x >= 10 for x in returns) / len(returns) * 100.0, 4),
            "over_20pct_seed_rate_pct": round(sum(x >= 20 for x in returns) / len(returns) * 100.0, 4),
        },
        "win_rate_stats_pct": {
            "mean": round(statistics.fmean(wins), 4),
            "median": round(statistics.median(wins), 4),
            "min": round(min(wins), 4),
            "max": round(max(wins), 4),
        },
        "best_seed": best_row,
        "worst_seed": worst_row,
    }


def main() -> None:
    args = build_parser().parse_args()
    if args.seed_end < args.seed_start:
        raise ValueError("seed-end must be >= seed-start")
    if args.workers < 1:
        raise ValueError("workers must be >= 1")

    config = config_from_args(args)
    validate(config)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("[1/2] preparing market data and daily candidate sets once...")
    prepared, prepared_path, reused = prepare(config, args.rebuild_prepared_cache)
    print(
        f"prepared cache: {prepared_path} "
        f"({'reused' if reused else 'built'}) / "
        f"{len(prepared['dates'])} trading days"
    )

    seeds = list(range(args.seed_start, args.seed_end + 1))
    worker_count = min(args.workers, len(seeds))
    print(f"[2/2] running {len(seeds)} seeds with {worker_count} process(es)...")

    config_dict = asdict(config)
    rows: list[dict[str, Any]] = []
    if worker_count == 1:
        _init_worker(str(prepared_path), config_dict)
        for n, seed in enumerate(seeds, start=1):
            rows.append(_simulate_seed(seed))
            print(f"[{n}/{len(seeds)}] seed {seed} complete")
    else:
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_init_worker,
            initargs=(str(prepared_path), config_dict),
        ) as pool:
            for n, row in enumerate(pool.map(_simulate_seed, seeds), start=1):
                rows.append(row)
                print(f"[{n}/{len(seeds)}] seed {row['seed']} complete")

    results = pd.DataFrame(rows).sort_values("seed").reset_index(drop=True)
    results.to_csv(out / "seed_results.csv", index=False, encoding="utf-8-sig")
    aggregate = summarize(results, config, args.seed_start, args.seed_end)
    aggregate["workers"] = worker_count
    aggregate["prepared_cache"] = str(prepared_path)
    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(aggregate, fh, ensure_ascii=False, indent=2)

    stats = aggregate["return_stats_pct"]
    print("\n=== Fast seed sweep summary ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(
        f"best seed: {int(aggregate['best_seed']['seed'])} / "
        f"{aggregate['best_seed']['total_return_pct']:.4f}%"
    )
    print(
        f"worst seed: {int(aggregate['worst_seed']['seed'])} / "
        f"{aggregate['worst_seed']['total_return_pct']:.4f}%"
    )
    print(f"saved: {out / 'seed_results.csv'}")
    print(f"saved: {out / 'seed_summary.json'}")


if __name__ == "__main__":
    main()
