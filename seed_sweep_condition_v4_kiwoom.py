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

from backtest import Config, buy_price, validate
from seed_sweep_condition_v4 import prepare_condition
from seed_sweep_fast import FastPosition, build_parser, config_from_args, summarize
from seed_sweep_kiwoom import kiwoom_sell_tax_rate

_PREPARED: dict[str, Any] | None = None
_CONFIG: Config | None = None
_STOP_LOSS_PCT = 0.03
_TAKE_PROFIT_ENABLED = True
_STOP_LOSS_ENABLED = True


def _init_worker(
    prepared_path: str,
    config_dict: dict[str, Any],
    stop_loss_pct: float,
    take_profit_enabled: bool,
    stop_loss_enabled: bool,
) -> None:
    global _PREPARED, _CONFIG, _STOP_LOSS_PCT, _TAKE_PROFIT_ENABLED, _STOP_LOSS_ENABLED
    with Path(prepared_path).open("rb") as fh:
        _PREPARED = pickle.load(fh)
    config_dict = dict(config_dict)
    config_dict["markets"] = tuple(config_dict["markets"])
    _CONFIG = Config(**config_dict)
    _STOP_LOSS_PCT = stop_loss_pct
    _TAKE_PROFIT_ENABLED = take_profit_enabled
    _STOP_LOSS_ENABLED = stop_loss_enabled


def _simulate_seed(seed: int) -> dict[str, Any]:
    if _PREPARED is None or _CONFIG is None:
        raise RuntimeError("Worker was not initialized.")

    prepared = _PREPARED
    config = _CONFIG
    rng = random.Random(seed)
    dates: list[str] = prepared["dates"]
    prices: dict[str, dict[str, tuple[float, float, float]]] = prepared["prices"]
    candidates_by_date: dict[str, list[str]] = prepared["candidates"]
    signal_start: str = prepared["signal_start"]
    signal_end: str = prepared["signal_end"]

    cash = float(config.initial_capital)
    positions: list[FastPosition] = []
    pending: list[tuple[str, float]] = []
    trade_count = winners = 0
    realized_pnl = total_commission = total_sell_tax = 0.0
    exits = {"take_profit": 0, "percent_stop": 0, "max_hold_exit": 0}

    for idx, d in enumerate(dates):
        day_prices = prices.get(d, {})

        entering, pending = pending, []
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
            qty = int(min(config.position_size, cash) // (entry * (1.0 + config.commission_rate)))
            if qty < 1:
                continue
            gross = qty * entry
            buy_commission = gross * config.commission_rate
            invested = gross + buy_commission
            cash -= invested
            total_commission += buy_commission
            positions.append(FastPosition(ticker, entry, qty, invested, signal_close))

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
            stop_price = p.entry_price * (1.0 - _STOP_LOSS_PCT)
            reason: str | None = None
            exit_price: float | None = None

            # Same daily-bar convention as the existing sweep: target hit has priority.
            if (
                _TAKE_PROFIT_ENABLED
                and p.holding_days <= config.max_hold_days
                and current_high >= target
            ):
                reason, exit_price = "take_profit", target
            elif _STOP_LOSS_ENABLED and current_close <= stop_price:
                reason, exit_price = "percent_stop", current_close
            elif p.holding_days >= config.max_hold_days:
                reason, exit_price = "max_hold_exit", current_close

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
                px = day_prices.get(ticker)
                if px is not None and idx + 1 < len(dates):
                    pending.append((ticker, px[2]))

        if d > signal_end and not positions and not pending:
            break

    final_equity = cash
    if positions:
        last_prices = prices.get(dates[-1], {})
        for p in positions:
            px = last_prices.get(p.ticker)
            final_equity += p.quantity * (px[2] if px else p.entry_price)

    return {
        "seed": seed,
        "total_return_pct": round((final_equity / config.initial_capital - 1.0) * 100.0, 4),
        "final_equity_krw": round(final_equity, 2),
        "total_realized_pnl_krw": round(realized_pnl, 2),
        "trade_count": trade_count,
        "win_rate_pct": round(winners / trade_count * 100.0, 4) if trade_count else 0.0,
        "take_profit_count": exits["take_profit"],
        "percent_stop_count": exits["percent_stop"],
        "max_hold_exit_count": exits["max_hold_exit"],
        "total_commission_krw": round(total_commission, 2),
        "total_sell_tax_krw": round(total_sell_tax, 2),
    }


def main() -> None:
    parser = build_parser()
    parser.description = (
        "MA240 < MA120 < MA60 plus Envelope lower-band sweep with Kiwoom fees, "
        "historical sell tax, and optional take-profit/percent-stop exits."
    )
    parser.add_argument("--stop-loss-pct", type=float, default=0.03)
    parser.add_argument("--no-take-profit", action="store_true")
    parser.add_argument("--no-stop-loss", action="store_true")
    parser.add_argument("--ma-long", type=int, default=240)
    parser.add_argument("--ma-mid", type=int, default=120)
    parser.add_argument("--ma-short", type=int, default=60)
    parser.add_argument("--envelope-period", type=int, default=20)
    parser.add_argument(
        "--envelope-percent",
        type=float,
        default=6.0,
        help="Envelope width in percent; lower band = SMA * (1 - percent / 100).",
    )
    parser.set_defaults(
        take_profit=0.12,
        max_hold_days=20,
        commission_rate=0.00015,
        sell_tax_rate=0.0,
        output_dir="results/seed_sweep_condition_v4_envelope_kiwoom",
    )
    args = parser.parse_args()

    if args.seed_end < args.seed_start:
        raise ValueError("seed-end must be >= seed-start")
    if args.workers < 1:
        raise ValueError("workers must be >= 1")
    if not args.no_stop_loss and not 0 < args.stop_loss_pct < 1:
        raise ValueError("stop-loss-pct must be between 0 and 1")
    if not (args.ma_long > args.ma_mid > args.ma_short >= 1):
        raise ValueError("MA periods must satisfy ma-long > ma-mid > ma-short >= 1")
    if args.envelope_period < 1:
        raise ValueError("envelope-period must be >= 1")
    if not 0 < args.envelope_percent < 100:
        raise ValueError("envelope-percent must be between 0 and 100")

    config = config_from_args(args)
    validate(config)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    take_profit_enabled = not args.no_take_profit
    stop_loss_enabled = not args.no_stop_loss
    effective_stop_loss_pct = args.stop_loss_pct if stop_loss_enabled else 0.0

    print("[1/2] preparing MA-order + Envelope lower-band candidates once...")
    prepared, prepared_path, reused = prepare_condition(
        config,
        args.ma_long,
        args.ma_mid,
        args.ma_short,
        args.rebuild_prepared_cache,
        envelope_period=args.envelope_period,
        envelope_percent=args.envelope_percent,
    )
    print(f"prepared cache: {prepared_path} ({'reused' if reused else 'built'}) / {len(prepared['dates'])} trading days")
    print(
        f"condition: MA{args.ma_long} < MA{args.ma_mid} < MA{args.ma_short} / "
        f"close <= Envelope({args.envelope_period}, {args.envelope_percent:g}%) lower band"
    )
    tp_text = f"+{config.take_profit * 100:.2f}%" if take_profit_enabled else "OFF"
    stop_text = f"-{args.stop_loss_pct * 100:.2f}% close" if stop_loss_enabled else "OFF"
    print(
        f"strategy: TP {tp_text} / stop {stop_text} / "
        f"max hold {config.max_hold_days} days"
    )
    print("Kiwoom KRX commission: 0.015% each side")
    print("Sell tax: 2022 0.23%, 2023 0.20%, 2024 0.18%, 2025 0.15%, 2026 0.20%")

    seeds = list(range(args.seed_start, args.seed_end + 1))
    workers = min(args.workers, len(seeds), os.cpu_count() or 1)
    print(f"[2/2] running {len(seeds)} seeds with {workers} process(es)...")
    config_dict = asdict(config)
    rows: list[dict[str, Any]] = []
    worker_args = (
        str(prepared_path),
        config_dict,
        effective_stop_loss_pct,
        take_profit_enabled,
        stop_loss_enabled,
    )

    if workers == 1:
        _init_worker(*worker_args)
        for n, seed in enumerate(seeds, 1):
            rows.append(_simulate_seed(seed))
            print(f"[{n}/{len(seeds)}] seed {seed} complete")
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=worker_args,
        ) as pool:
            for n, row in enumerate(pool.map(_simulate_seed, seeds), 1):
                rows.append(row)
                print(f"[{n}/{len(seeds)}] seed {row['seed']} complete")

    results = pd.DataFrame(rows).sort_values("seed").reset_index(drop=True)
    results.to_csv(out / "seed_results.csv", index=False, encoding="utf-8-sig")
    aggregate = summarize(results, config, args.seed_start, args.seed_end)
    aggregate["workers"] = workers
    aggregate["prepared_cache"] = str(prepared_path)
    aggregate["condition"] = {
        "ma_order": [args.ma_long, args.ma_mid, args.ma_short],
        "relation": "<",
        "envelope": {
            "period": args.envelope_period,
            "percent": args.envelope_percent,
            "relation": "close<=lower_band",
        },
    }
    aggregate["take_profit_enabled"] = take_profit_enabled
    aggregate["stop_loss_enabled"] = stop_loss_enabled
    aggregate["stop_loss_pct"] = args.stop_loss_pct if stop_loss_enabled else None
    aggregate["cost_model"] = {
        "broker": "Kiwoom Securities",
        "commission_rate_each_side": config.commission_rate,
        "historical_sell_tax_rates": {"2022": 0.0023, "2023": 0.0020, "2024": 0.0018, "2025": 0.0015, "2026": 0.0020},
    }
    aggregate["cost_stats_krw"] = {
        "mean_commission": round(float(results["total_commission_krw"].mean()), 2),
        "mean_sell_tax": round(float(results["total_sell_tax_krw"].mean()), 2),
        "median_commission": round(float(results["total_commission_krw"].median()), 2),
        "median_sell_tax": round(float(results["total_sell_tax_krw"].median()), 2),
    }
    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(aggregate, fh, ensure_ascii=False, indent=2)

    print("\n=== Condition v4 + Envelope + Kiwoom cost summary ===")
    print(json.dumps(aggregate["return_stats_pct"], ensure_ascii=False, indent=2))
    print(f"saved: {out / 'seed_results.csv'}")
    print(f"saved: {out / 'seed_summary.json'}")


if __name__ == "__main__":
    main()
