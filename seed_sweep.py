from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
from dataclasses import replace
from pathlib import Path

import pandas as pd

from backtest import Config, run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the same backtest across many random seeds and summarize robustness."
    )
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--seed-start", type=int, default=1)
    p.add_argument("--seed-end", type=int, default=100)

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
    p.add_argument("--output-dir", default="results/seed_sweep")
    p.add_argument(
        "--keep-seed-results",
        action="store_true",
        help="Keep each seed's trades/signals/equity files under output-dir/seeds/.",
    )
    return p


def base_config(args: argparse.Namespace) -> Config:
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


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(pd.Series(values, dtype="float64").quantile(q))


def main() -> None:
    args = build_parser().parse_args()
    if args.seed_end < args.seed_start:
        raise ValueError("seed-end must be >= seed-start")

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    seed_root = out / "seeds"
    if args.keep_seed_results:
        seed_root.mkdir(parents=True, exist_ok=True)

    config_template = base_config(args)
    rows: list[dict] = []

    for seed in range(args.seed_start, args.seed_end + 1):
        if args.keep_seed_results:
            run_output = seed_root / f"seed_{seed:04d}"
            run_output.mkdir(parents=True, exist_ok=True)
            temp_ctx = None
        else:
            temp_ctx = tempfile.TemporaryDirectory(prefix=f"stocksim_seed_{seed}_")
            run_output = Path(temp_ctx.name)

        config = replace(config_template, seed=seed, output_dir=str(run_output))
        print(f"[{seed}/{args.seed_end}] running seed {seed}...")
        summary = run(config)
        exits = summary.get("exit_counts", {})
        rows.append(
            {
                "seed": seed,
                "total_return_pct": float(summary.get("total_return_pct", 0.0)),
                "final_equity_krw": float(summary.get("final_equity_krw", 0.0)),
                "total_realized_pnl_krw": float(summary.get("total_realized_pnl_krw", 0.0)),
                "trade_count": int(summary.get("trade_count", 0)),
                "win_rate_pct": float(summary.get("win_rate_pct", 0.0)),
                "take_profit_count": int(exits.get("take_profit", 0)),
                "consecutive_down_stop_count": int(exits.get("consecutive_down_stop", 0)),
                "max_hold_exit_count": int(exits.get("max_hold_exit", 0)),
            }
        )

        if temp_ctx is not None:
            temp_ctx.cleanup()

    results = pd.DataFrame(rows).sort_values("seed").reset_index(drop=True)
    results.to_csv(out / "seed_results.csv", index=False, encoding="utf-8-sig")

    returns = results["total_return_pct"].astype(float).tolist()
    wins = results["win_rate_pct"].astype(float).tolist()
    best_row = results.loc[results["total_return_pct"].idxmax()].to_dict()
    worst_row = results.loc[results["total_return_pct"].idxmin()].to_dict()

    aggregate = {
        "seed_start": args.seed_start,
        "seed_end": args.seed_end,
        "seed_count": len(results),
        "base_config": {
            key: value
            for key, value in config_template.__dict__.items()
            if key not in {"seed", "output_dir"}
        },
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

    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(aggregate, fh, ensure_ascii=False, indent=2)

    print("\n=== Seed sweep summary ===")
    print(json.dumps(aggregate["return_stats_pct"], ensure_ascii=False, indent=2))
    print(f"best seed: {int(best_row['seed'])} / {best_row['total_return_pct']:.4f}%")
    print(f"worst seed: {int(worst_row['seed'])} / {worst_row['total_return_pct']:.4f}%")
    print(f"saved: {out / 'seed_results.csv'}")
    print(f"saved: {out / 'seed_summary.json'}")


if __name__ == "__main__":
    main()
