from __future__ import annotations

import json
import os
import pickle
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from backtest import Config, buy_price, net_sell, validate
from seed_sweep_fast import FastPosition, build_parser, config_from_args, percentile, prepare

_PREPARED: dict[str, Any] | None = None
_WORKER_CONFIG: Config | None = None
_STOP_LOSS_PCT: float = 0.05


def _init_worker(prepared_path: str, config_dict: dict[str, Any], stop_loss_pct: float) -> None:
    global _PREPARED, _WORKER_CONFIG, _STOP_LOSS_PCT
    with Path(prepared_path).open("rb") as fh:
        _PREPARED = pickle.load(fh)
    config_dict = dict(config_dict)
    config_dict["markets"] = tuple(config_dict["markets"])
    _WORKER_CONFIG = Config(**config_dict)
    _STOP_LOSS_PCT = stop_loss_pct


def _simulate_seed(seed: int) -> dict[str, Any]:
    if _PREPARED is None or _WORKER_CONFIG is None:
        raise RuntimeError("Worker was not initialized.")

    prepared = _PREPARED
    config = _WORKER_CONFIG
    stop_loss_pct = _STOP_LOSS_PCT
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
        "percent_stop": 0,
        "max_hold_exit": 0,
    }

    for idx, d in enumerate(dates):
        day_prices = prices.get(d, {})

        if pending:
            entering = pending
            pending = []
            for ticker, signal_close in entering:
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

            _, current_high, current_close = px
            p.holding_days += 1
            p.prev_close = current_close

            target = p.entry_price * (1.0 + config.take_profit)
            stop_price = p.entry_price * (1.0 - stop_loss_pct)
            reason: str | None = None
            exit_price: float | None = None

            # Preserve the existing backtest's take-profit priority. The percent
            # stop is evaluated on the daily close because the prepared fast-path
            # cache stores open/high/close rather than intraday order or lows.
            if p.holding_days <= config.max_hold_days and current_high >= target:
                reason = "take_profit"
                exit_price = target
            elif current_close <= stop_price:
                reason = "percent_stop"
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
        "percent_stop_count": exit_counts["percent_stop"],
        "max_hold_exit_count": exit_counts["max_hold_exit"],
    }


def _summarize(results: pd.DataFrame, config: Config, seed_start: int, seed_end: int, stop_loss_pct: float) -> dict[str, Any]:
    returns = results["total_return_pct"].astype(float).tolist()
    wins = results["win_rate_pct"].astype(float).tolist()
    best_row = results.loc[results["total_return_pct"].idxmax()].to_dict()
    worst_row = results.loc[results["total_return_pct"].idxmin()].to_dict()
    return {
        "seed_start": seed_start,
        "seed_end": seed_end,
        "seed_count": len(results),
        "stop_loss_pct": stop_loss_pct,
        "stop_loss_trigger": "daily_close",
        "base_config": {k: v for k, v in asdict(config).items() if k not in {"seed", "output_dir"}},
        "return_stats_pct": {
            "mean": round(sum(returns) / len(returns), 4),
            "median": round(float(pd.Series(returns).median()), 4),
            "stdev": round(float(pd.Series(returns).std(ddof=0)), 4),
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
            "mean": round(sum(wins) / len(wins), 4),
            "median": round(float(pd.Series(wins).median()), 4),
            "min": round(min(wins), 4),
            "max": round(max(wins), 4),
        },
        "mean_percent_stop_count": round(float(results["percent_stop_count"].mean()), 2),
        "best_seed": best_row,
        "worst_seed": worst_row,
    }


def main() -> None:
    parser = build_parser()
    parser.description = "Fast multi-seed sweep with a configurable percentage stop-loss."
    parser.add_argument(
        "--stop-loss-pct",
        type=float,
        default=0.05,
        help="Stop when daily close is this fraction below entry; 0.05 means -5%%.",
    )
    parser.set_defaults(output_dir="results/seed_sweep_percent_stop")
    args = parser.parse_args()

    if args.seed_end < args.seed_start:
        raise ValueError("seed-end must be >= seed-start")
    if args.workers < 1:
        raise ValueError("workers must be >= 1")
    if not 0 < args.stop_loss_pct < 1:
        raise ValueError("stop-loss-pct must be between 0 and 1")

    config = config_from_args(args)
    validate(config)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("[1/2] preparing market data and daily candidate sets once...")
    prepared, prepared_path, reused = prepare(config, args.rebuild_prepared_cache)
    print(
        f"prepared cache: {prepared_path} "
        f"({'reused' if reused else 'built'}) / {len(prepared['dates'])} trading days"
    )
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
    aggregate["workers"] = worker_count
    aggregate["prepared_cache"] = str(prepared_path)
    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(aggregate, fh, ensure_ascii=False, indent=2)

    stats = aggregate["return_stats_pct"]
    print("\n=== Percent-stop seed sweep summary ===")
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
