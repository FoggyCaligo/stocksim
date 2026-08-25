from __future__ import annotations

import json
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

import seed_sweep_fast as fast
from backtest import Config, buy_price, validate


_PREPARED: dict[str, Any] | None = None
_WORKER_CONFIG: Config | None = None


def kiwoom_sell_tax_rate(trade_date: str) -> float:
    """Historical total sell-side tax for KOSPI/KOSDAQ ordinary shares.

    KOSPI totals include the rural special tax where applicable.
    Rates are expressed as decimal fractions (0.0023 = 0.23%).
    """
    year = pd.Timestamp(trade_date).year
    if year <= 2022:
        return 0.0023
    if year == 2023:
        return 0.0020
    if year == 2024:
        return 0.0018
    if year == 2025:
        return 0.0015
    return 0.0020


def _init_worker(prepared_path: str, config_dict: dict[str, Any]) -> None:
    global _PREPARED, _WORKER_CONFIG
    with Path(prepared_path).open("rb") as fh:
        import pickle

        _PREPARED = pickle.load(fh)
    config_dict = dict(config_dict)
    config_dict["markets"] = tuple(config_dict["markets"])
    _WORKER_CONFIG = Config(**config_dict)


def _net_sell_kiwoom(gross: float, trade_date: str, config: Config) -> float:
    commission = gross * config.commission_rate
    historical_tax = gross * kiwoom_sell_tax_rate(trade_date)
    extra_tax = gross * config.sell_tax_rate
    slippage = gross * config.slippage_bps / 10_000.0
    return gross - commission - historical_tax - extra_tax - slippage


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
    positions: list[fast.FastPosition] = []
    pending: list[tuple[str, float]] = []
    trade_count = 0
    winners = 0
    realized_pnl = 0.0
    total_commission = 0.0
    total_sell_tax = 0.0
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
                buy_commission = gross * config.commission_rate
                invested = gross + buy_commission
                cash -= invested
                total_commission += buy_commission
                positions.append(
                    fast.FastPosition(
                        ticker=ticker,
                        entry_price=entry,
                        quantity=qty,
                        invested=invested,
                        prev_close=signal_close,
                    )
                )

        survivors: list[fast.FastPosition] = []
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

            gross_sale = p.quantity * exit_price
            sell_commission = gross_sale * config.commission_rate
            sell_tax = gross_sale * (kiwoom_sell_tax_rate(d) + config.sell_tax_rate)
            proceeds = _net_sell_kiwoom(gross_sale, d, config)
            pnl = proceeds - p.invested

            cash += proceeds
            realized_pnl += pnl
            total_commission += sell_commission
            total_sell_tax += sell_tax
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
        "total_commission_krw": round(total_commission, 2),
        "total_sell_tax_krw": round(total_sell_tax, 2),
    }


def main() -> None:
    parser = fast.build_parser()
    parser.description = (
        "Fast multi-seed sweep with Kiwoom KRX online commission and historical Korean sell taxes."
    )
    parser.set_defaults(
        commission_rate=0.00015,
        sell_tax_rate=0.0,
        output_dir="results/seed_sweep_kiwoom",
    )
    args = parser.parse_args()

    if args.seed_end < args.seed_start:
        raise ValueError("seed-end must be >= seed-start")
    if args.workers < 1:
        raise ValueError("workers must be >= 1")

    config = fast.config_from_args(args)
    validate(config)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("[1/2] preparing market data and daily candidate sets once...")
    prepared, prepared_path, reused = fast.prepare(config, args.rebuild_prepared_cache)
    print(
        f"prepared cache: {prepared_path} "
        f"({'reused' if reused else 'built'}) / "
        f"{len(prepared['dates'])} trading days"
    )

    seeds = list(range(args.seed_start, args.seed_end + 1))
    worker_count = min(args.workers, len(seeds))
    print(f"[2/2] running {len(seeds)} seeds with {worker_count} process(es)...")
    print("Kiwoom KRX commission: 0.015% on buys and sells")
    print("Historical sell tax: 2022 0.23%, 2023 0.20%, 2024 0.18%, 2025 0.15%, 2026 0.20%")

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

    aggregate = fast.summarize(results, config, args.seed_start, args.seed_end)
    aggregate["workers"] = worker_count
    aggregate["prepared_cache"] = str(prepared_path)
    aggregate["cost_model"] = {
        "broker": "Kiwoom Securities",
        "venue": "KRX",
        "commission_rate_each_side": 0.00015,
        "historical_sell_tax_rates": {
            "2022": 0.0023,
            "2023": 0.0020,
            "2024": 0.0018,
            "2025": 0.0015,
            "2026": 0.0020,
        },
        "note": "KOSPI rates include rural special tax; KOSDAQ total sell-side tax is the same in these years.",
    }
    aggregate["cost_stats_krw"] = {
        "mean_commission": round(float(results["total_commission_krw"].mean()), 2),
        "mean_sell_tax": round(float(results["total_sell_tax_krw"].mean()), 2),
        "median_commission": round(float(results["total_commission_krw"].median()), 2),
        "median_sell_tax": round(float(results["total_sell_tax_krw"].median()), 2),
    }

    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(aggregate, fh, ensure_ascii=False, indent=2)

    stats = aggregate["return_stats_pct"]
    print("\n=== Kiwoom-cost seed sweep summary ===")
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
