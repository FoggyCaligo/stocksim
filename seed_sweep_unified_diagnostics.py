from __future__ import annotations

import json
import os
import random
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

import seed_sweep_unified as unified
from seed_sweep_kiwoom import kiwoom_sell_tax_rate


@dataclass
class DiagnosticPosition:
    ticker: str
    signal_date: str
    entry_date: str
    entry_price: float
    quantity: int
    invested: float
    target_price: float | None
    stop_price: float | None
    max_hold_days: int | None
    recent_touch_date: str | None
    raw_hold_days: int | None
    holding_days: int = 0


_PREPARED: dict[str, Any] | None = None
_ARGS: dict[str, Any] | None = None


def _init_worker(prepared_path: str, args_dict: dict[str, Any]) -> None:
    global _PREPARED, _ARGS
    with Path(prepared_path).open("rb") as fh:
        import pickle
        _PREPARED = pickle.load(fh)
    _ARGS = args_dict


def _pct(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator * 100.0


def _simulate_seed(seed: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if _PREPARED is None or _ARGS is None:
        raise RuntimeError("Worker was not initialized")

    prepared = _PREPARED
    a = _ARGS
    rng = random.Random(seed)
    dates: list[str] = prepared["dates"]
    prices = prepared["prices"]
    candidates_by_date = prepared["candidates"]
    strategy_by_date = prepared["strategy"]

    cash = float(a["initial_capital"])
    positions: list[DiagnosticPosition] = []
    pending: list[tuple[str, str, dict[str, Any]]] = []
    realized_pnl = total_commission = total_sell_tax = 0.0
    trade_count = winners = skipped_target_below_entry = 0
    exits = {"target": 0, "stop": 0, "hold_exit": 0}
    trades: list[dict[str, Any]] = []

    for idx, d in enumerate(dates):
        day_prices = prices.get(d, {})

        entering, pending = pending, []
        for ticker, signal_date, meta in entering:
            px = day_prices.get(ticker)
            if px is None:
                continue
            if a["no_reentry"] and any(p.ticker == ticker for p in positions):
                continue
            raw_open = px[0]
            if raw_open <= 0:
                continue
            entry = raw_open * (1.0 + a["slippage_bps"] / 10_000.0)

            target = meta.get("target_price")
            if target is None and a["fixed_take_profit"] is not None:
                target = entry * (1.0 + a["fixed_take_profit"])
            if target is not None and target <= entry:
                skipped_target_below_entry += 1
                continue

            stop = None
            if target is not None and a["stop_gap_ratio"] is not None:
                stop = entry - a["stop_gap_ratio"] * (target - entry)
            elif a["fixed_stop_loss_pct"] is not None:
                stop = entry * (1.0 - a["fixed_stop_loss_pct"])

            qty = int(min(a["position_size"], cash) // (entry * (1.0 + a["commission_rate"])))
            if qty < 1:
                continue
            gross = qty * entry
            buy_commission = gross * a["commission_rate"]
            invested = gross + buy_commission
            cash -= invested
            total_commission += buy_commission
            positions.append(
                DiagnosticPosition(
                    ticker=ticker,
                    signal_date=signal_date,
                    entry_date=d,
                    entry_price=entry,
                    quantity=qty,
                    invested=invested,
                    target_price=target,
                    stop_price=stop,
                    max_hold_days=meta.get("max_hold_days"),
                    recent_touch_date=meta.get("recent_touch_date"),
                    raw_hold_days=meta.get("raw_hold_days"),
                )
            )

        survivors: list[DiagnosticPosition] = []
        for p in positions:
            px = day_prices.get(p.ticker)
            if px is None:
                survivors.append(p)
                continue
            _, high, low, close = px
            p.holding_days += 1

            reason: str | None = None
            exit_price: float | None = None
            if p.target_price is not None and high >= p.target_price:
                reason, exit_price = "target", p.target_price
            elif p.stop_price is not None and low <= p.stop_price:
                reason, exit_price = "stop", p.stop_price
            elif p.max_hold_days is not None and p.holding_days >= p.max_hold_days:
                reason, exit_price = "hold_exit", close

            if reason is None or exit_price is None:
                survivors.append(p)
                continue

            gross_sale = p.quantity * float(exit_price)
            sell_commission = gross_sale * a["commission_rate"]
            sell_tax = gross_sale * (kiwoom_sell_tax_rate(d) + a["sell_tax_rate"])
            sell_slippage = gross_sale * a["slippage_bps"] / 10_000.0
            proceeds = gross_sale - sell_commission - sell_tax - sell_slippage
            pnl = proceeds - p.invested
            trade_return_pct = _pct(pnl, p.invested) or 0.0
            target_return_pct = (
                _pct(p.target_price - p.entry_price, p.entry_price)
                if p.target_price is not None
                else None
            )
            stop_return_pct = (
                _pct(p.stop_price - p.entry_price, p.entry_price)
                if p.stop_price is not None
                else None
            )

            cash += proceeds
            realized_pnl += pnl
            total_commission += sell_commission
            total_sell_tax += sell_tax
            trade_count += 1
            winners += int(pnl > 0)
            exits[reason] += 1
            trades.append(
                {
                    "seed": seed,
                    "ticker": p.ticker,
                    "signal_date": p.signal_date,
                    "entry_date": p.entry_date,
                    "exit_date": d,
                    "recent_touch_date": p.recent_touch_date,
                    "entry_price": round(p.entry_price, 4),
                    "target_price": round(p.target_price, 4) if p.target_price is not None else None,
                    "stop_price": round(p.stop_price, 4) if p.stop_price is not None else None,
                    "exit_price": round(float(exit_price), 4),
                    "planned_target_return_pct": round(target_return_pct, 4) if target_return_pct is not None else None,
                    "planned_stop_return_pct": round(stop_return_pct, 4) if stop_return_pct is not None else None,
                    "planned_stop_loss_pct": round(-stop_return_pct, 4) if stop_return_pct is not None else None,
                    "raw_hold_days": p.raw_hold_days,
                    "planned_hold_days": p.max_hold_days,
                    "actual_holding_days": p.holding_days,
                    "exit_reason": reason,
                    "quantity": p.quantity,
                    "invested_krw": round(p.invested, 2),
                    "gross_sale_krw": round(gross_sale, 2),
                    "buy_commission_krw": round(p.quantity * p.entry_price * a["commission_rate"], 2),
                    "sell_commission_krw": round(sell_commission, 2),
                    "sell_tax_krw": round(sell_tax, 2),
                    "pnl_krw": round(pnl, 2),
                    "return_pct": round(trade_return_pct, 4),
                }
            )
        positions = survivors

        if prepared["signal_start"] <= d <= prepared["signal_end"]:
            candidates = list(candidates_by_date.get(d, ()))
            if a["no_reentry"]:
                active = {p.ticker for p in positions}
                candidates = [t for t in candidates if t not in active]
            rng.shuffle(candidates)
            for ticker in candidates[: a["daily_buy_count"]]:
                if idx + 1 >= len(dates):
                    continue
                meta = strategy_by_date.get(d, {}).get(ticker)
                if meta is not None:
                    pending.append((ticker, d, meta))

        if d > prepared["signal_end"] and not positions and not pending:
            break

    final_equity = cash
    if positions:
        last_prices = prices.get(dates[-1], {})
        for p in positions:
            px = last_prices.get(p.ticker)
            final_equity += p.quantity * (px[3] if px else p.entry_price)

    row = {
        "seed": seed,
        "total_return_pct": round((final_equity / a["initial_capital"] - 1.0) * 100.0, 4),
        "final_equity_krw": round(final_equity, 2),
        "total_realized_pnl_krw": round(realized_pnl, 2),
        "trade_count": trade_count,
        "win_rate_pct": round(winners / trade_count * 100.0, 4) if trade_count else 0.0,
        "target_count": exits["target"],
        "stop_count": exits["stop"],
        "hold_exit_count": exits["hold_exit"],
        "open_position_count": len(positions),
        "skipped_target_below_entry": skipped_target_below_entry,
        "total_commission_krw": round(total_commission, 2),
        "total_sell_tax_krw": round(total_sell_tax, 2),
    }
    return row, trades


def _series_stats(series: pd.Series) -> dict[str, float | int | None]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {"count": 0, "mean": None, "median": None, "min": None, "p05": None, "p25": None, "p75": None, "p95": None, "max": None}
    return {
        "count": int(len(s)),
        "mean": round(float(s.mean()), 4),
        "median": round(float(s.median()), 4),
        "min": round(float(s.min()), 4),
        "p05": round(float(s.quantile(0.05)), 4),
        "p25": round(float(s.quantile(0.25)), 4),
        "p75": round(float(s.quantile(0.75)), 4),
        "p95": round(float(s.quantile(0.95)), 4),
        "max": round(float(s.max()), 4),
    }


def build_trade_diagnostics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "average_planned_target_return_pct": None,
            "average_planned_stop_loss_pct": None,
            "target_hit_average_holding_days": None,
            "hold_exit_average_final_return_pct": None,
            "planned_hold_days_distribution": _series_stats(pd.Series(dtype=float)),
        }

    targets = trades[trades["exit_reason"] == "target"]
    hold_exits = trades[trades["exit_reason"] == "hold_exit"]
    return {
        "average_planned_target_return_pct": round(float(pd.to_numeric(trades["planned_target_return_pct"], errors="coerce").mean()), 4),
        "average_planned_stop_loss_pct": round(float(pd.to_numeric(trades["planned_stop_loss_pct"], errors="coerce").mean()), 4),
        "target_hit_average_holding_days": round(float(pd.to_numeric(targets["actual_holding_days"], errors="coerce").mean()), 4) if not targets.empty else None,
        "hold_exit_average_final_return_pct": round(float(pd.to_numeric(hold_exits["return_pct"], errors="coerce").mean()), 4) if not hold_exits.empty else None,
        "planned_hold_days_distribution": _series_stats(trades["planned_hold_days"]),
        "actual_holding_days_distribution": _series_stats(trades["actual_holding_days"]),
        "return_by_exit_reason_pct": {
            reason: _series_stats(group["return_pct"])
            for reason, group in trades.groupby("exit_reason", sort=True)
        },
        "planned_target_return_distribution_pct": _series_stats(trades["planned_target_return_pct"]),
        "planned_stop_loss_distribution_pct": _series_stats(trades["planned_stop_loss_pct"]),
    }


def main() -> None:
    parser = unified.build_parser()
    parser.description = "Unified stocksim runner with trade-level diagnostics across the full seed sweep."
    args = parser.parse_args()
    unified.validate_args(args)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("[1/2] preparing unified candidate set...")
    prepared, prepared_path, reused = unified.prepare(args)
    print(f"prepared cache: {prepared_path} ({'reused' if reused else 'built'})")
    print("filters:")
    print(json.dumps(unified._filter_dict(args), ensure_ascii=False, indent=2))

    seeds = list(range(args.seed_start, args.seed_end + 1))
    workers = min(args.workers, len(seeds), os.cpu_count() or 1)
    args_dict = vars(args).copy()
    print(f"[2/2] running {len(seeds)} seeds with {workers} process(es)...")

    rows: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    if workers == 1:
        _init_worker(str(prepared_path), args_dict)
        iterator = (_simulate_seed(seed) for seed in seeds)
        for n, (row, trades) in enumerate(iterator, 1):
            rows.append(row)
            all_trades.extend(trades)
            print(f"[{n}/{len(seeds)}] seed {row['seed']} complete / trades {len(trades)}")
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(str(prepared_path), args_dict),
        ) as pool:
            for n, (row, trades) in enumerate(pool.map(_simulate_seed, seeds), 1):
                rows.append(row)
                all_trades.extend(trades)
                print(f"[{n}/{len(seeds)}] seed {row['seed']} complete / trades {len(trades)}")

    results = pd.DataFrame(rows).sort_values("seed").reset_index(drop=True)
    trades_df = pd.DataFrame(all_trades)
    if not trades_df.empty:
        trades_df = trades_df.sort_values(["seed", "entry_date", "ticker"]).reset_index(drop=True)

    results.to_csv(out / "seed_results.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(out / "trade_diagnostics.csv", index=False, encoding="utf-8-sig")

    summary = unified.summarize(results, args, prepared)
    summary["workers"] = workers
    summary["prepared_cache"] = str(prepared_path)
    summary["trade_diagnostics"] = build_trade_diagnostics(trades_df)
    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print("\n=== Unified diagnostic sweep summary ===")
    print(json.dumps(summary["return_stats_pct"], ensure_ascii=False, indent=2))
    print("\n=== Trade diagnostics ===")
    print(json.dumps(summary["trade_diagnostics"], ensure_ascii=False, indent=2))
    print(f"saved: {out / 'seed_results.csv'}")
    print(f"saved: {out / 'trade_diagnostics.csv'}")
    print(f"saved: {out / 'seed_summary.json'}")


if __name__ == "__main__":
    main()
