from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import random
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from pykrx import stock
from seed_sweep_kiwoom import kiwoom_sell_tax_rate


@dataclass
class Position:
    ticker: str
    entry_price: float
    quantity: int
    invested: float
    target_price: float | None
    stop_price: float | None
    max_hold_days: int | None
    holding_days: int = 0


_PREPARED: dict[str, Any] | None = None
_ARGS: dict[str, Any] | None = None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Unified 100-seed stocksim runner. Candidate filters are optional: "
            "when a filter argument is omitted, that filter is not applied."
        )
    )
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")

    # Seed sweep / portfolio mechanics.
    p.add_argument("--seed-start", type=int, default=1)
    p.add_argument("--seed-end", type=int, default=100)
    p.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    p.add_argument("--daily-buy-count", type=int, default=5)
    p.add_argument("--initial-capital", type=int, default=1_000_000)
    p.add_argument("--position-size", type=int, default=100_000)
    p.add_argument("--no-reentry", action="store_true")
    p.add_argument("--markets", default="KOSPI,KOSDAQ")

    # Optional screening filters. None means disabled.
    p.add_argument("--price-min", type=float, default=None)
    p.add_argument("--price-max", type=float, default=None)
    p.add_argument("--market-cap-min", type=float, default=None)
    p.add_argument("--market-cap-max", type=float, default=None)
    p.add_argument("--daily-return-min", type=float, default=None)
    p.add_argument("--daily-return-max", type=float, default=None)
    p.add_argument("--trading-value-min", type=float, default=None)
    p.add_argument("--trading-value-max", type=float, default=None)

    # Optional MA-order filter. Supply all three or none.
    p.add_argument("--ma-long", type=int, default=None)
    p.add_argument("--ma-mid", type=int, default=None)
    p.add_argument("--ma-short", type=int, default=None)

    # Optional Envelope lower-band filter and dynamic target source.
    # When both values are supplied, candidates must close <= lower band.
    p.add_argument("--envelope-period", type=int, default=None)
    p.add_argument("--envelope-percent", type=float, default=None)

    # Exit rules. Omitted values are disabled / uncapped.
    # With Envelope enabled, target = midpoint(center, upper).
    # stop-gap-ratio 0.5 means stop = entry - 0.5 * (target - entry).
    p.add_argument("--stop-gap-ratio", type=float, default=None)
    p.add_argument("--fixed-take-profit", type=float, default=None)
    p.add_argument("--fixed-stop-loss-pct", type=float, default=None)
    p.add_argument(
        "--touch-lookback-days",
        type=int,
        default=None,
        help="Maximum prior trading sessions searched for the most recent target touch; omitted = unlimited history loaded.",
    )
    p.add_argument(
        "--max-hold-days",
        type=int,
        default=None,
        help="Optional safety cap. With Envelope target, actual hold = min(recent-touch age, this cap).",
    )

    # Cost / runtime settings.
    p.add_argument("--commission-rate", type=float, default=0.00015)
    p.add_argument("--sell-tax-rate", type=float, default=0.0)
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.add_argument("--cache-dir", default=".cache/stocksim")
    p.add_argument("--output-dir", default="results/seed_sweep_unified")
    p.add_argument("--rebuild-prepared-cache", action="store_true")
    return p


def validate_args(args: argparse.Namespace) -> None:
    if pd.Timestamp(args.start) > pd.Timestamp(args.end):
        raise ValueError("start must be on or before end")
    if args.seed_end < args.seed_start:
        raise ValueError("seed-end must be >= seed-start")
    if args.workers < 1:
        raise ValueError("workers must be >= 1")
    if args.daily_buy_count < 1:
        raise ValueError("daily-buy-count must be >= 1")
    if args.initial_capital <= 0 or args.position_size <= 0:
        raise ValueError("capital values must be > 0")

    ma_values = (args.ma_long, args.ma_mid, args.ma_short)
    if any(v is not None for v in ma_values):
        if any(v is None for v in ma_values):
            raise ValueError("ma-long, ma-mid, and ma-short must be supplied together")
        if not (args.ma_long > args.ma_mid > args.ma_short >= 1):
            raise ValueError("MA periods must satisfy ma-long > ma-mid > ma-short >= 1")

    env_values = (args.envelope_period, args.envelope_percent)
    if any(v is not None for v in env_values):
        if any(v is None for v in env_values):
            raise ValueError("envelope-period and envelope-percent must be supplied together")
        if args.envelope_period < 1:
            raise ValueError("envelope-period must be >= 1")
        if not 0 < args.envelope_percent < 100:
            raise ValueError("envelope-percent must be between 0 and 100")

    for name in ("stop_gap_ratio", "fixed_take_profit", "fixed_stop_loss_pct"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            raise ValueError(f"{name.replace('_', '-')} must be > 0")
    if args.fixed_stop_loss_pct is not None and args.fixed_stop_loss_pct >= 1:
        raise ValueError("fixed-stop-loss-pct must be < 1")
    if args.touch_lookback_days is not None and args.touch_lookback_days < 1:
        raise ValueError("touch-lookback-days must be >= 1")
    if args.max_hold_days is not None and args.max_hold_days < 1:
        raise ValueError("max-hold-days must be >= 1")


def _filter_dict(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "price_min": args.price_min,
        "price_max": args.price_max,
        "market_cap_min": args.market_cap_min,
        "market_cap_max": args.market_cap_max,
        "daily_return_min": args.daily_return_min,
        "daily_return_max": args.daily_return_max,
        "trading_value_min": args.trading_value_min,
        "trading_value_max": args.trading_value_max,
        "ma_long": args.ma_long,
        "ma_mid": args.ma_mid,
        "ma_short": args.ma_short,
        "envelope_period": args.envelope_period,
        "envelope_percent": args.envelope_percent,
        "touch_lookback_days": args.touch_lookback_days,
        "max_hold_days": args.max_hold_days,
    }


def _cache_path(args: argparse.Namespace) -> Path:
    key = {
        "start": args.start,
        "end": args.end,
        "markets": args.markets,
        **_filter_dict(args),
        "strategy": "unified_optional_filters_envelope_mid_upper_target",
        "format_version": 1,
    }
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    root = Path(args.cache_dir) / "prepared_unified"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.pkl"


def _apply_optional_filter(mask: pd.Series, series: pd.Series, low: float | None, high: float | None) -> pd.Series:
    if low is not None:
        mask &= series >= low
    if high is not None:
        mask &= series <= high
    return mask


def prepare(args: argparse.Namespace) -> tuple[dict[str, Any], Path, bool]:
    cache_path = _cache_path(args)
    if cache_path.exists() and not args.rebuild_prepared_cache:
        with cache_path.open("rb") as fh:
            return pickle.load(fh), cache_path, True

    if not hasattr(stock, "_slice_years"):
        raise RuntimeError("This runner requires the repository's local marcap-backed pykrx shim.")

    start = pd.Timestamp(args.start).normalize()
    end = pd.Timestamp(args.end).normalize()
    ma_periods = [x for x in (args.ma_long, args.ma_mid, args.ma_short, args.envelope_period) if x]
    warmup_days = max([800, *(int(x) * 3 for x in ma_periods)])
    if args.touch_lookback_days is not None:
        warmup_days = max(warmup_days, args.touch_lookback_days * 2)
    else:
        # Unlimited touch lookup still needs finite data. Load roughly 3 years before the test.
        warmup_days = max(warmup_days, 1100)
    warmup_start = start - pd.DateOffset(days=warmup_days)

    # Load future bars only up to data that can exist today. If a position cannot finish,
    # it remains open and is marked to market at the final available bar.
    today = pd.Timestamp.today().normalize()
    if args.max_hold_days is not None:
        future_end = min(today, end + pd.DateOffset(days=max(30, args.max_hold_days * 3)))
    else:
        future_end = min(today, end + pd.DateOffset(days=1100))
    if future_end < end:
        future_end = end

    frame = stock._slice_years(
        warmup_start.strftime("%Y%m%d"), future_end.strftime("%Y%m%d")
    ).copy()
    if frame.empty:
        raise RuntimeError("No marcap rows found for the requested period.")

    markets = tuple(x.strip().upper() for x in args.markets.split(",") if x.strip())
    if markets and "Market" in frame.columns:
        frame = frame[frame["Market"].astype(str).str.upper().isin(set(markets))].copy()

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
    for days in sorted(set(int(x) for x in ma_periods)):
        frame[f"ma_{days}"] = grouped_close.transform(
            lambda s, n=days: s.rolling(n, min_periods=n).mean()
        )

    dates = sorted(pd.Timestamp(x) for x in frame["Date"].drop_duplicates().tolist())
    signal_dates = {d for d in dates if start <= d <= end}
    if not signal_dates:
        raise RuntimeError("The requested period contains no trading days.")
    date_index = {d: i for i, d in enumerate(dates)}

    prices: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    for d, day in frame.groupby("Date", sort=True):
        prices[pd.Timestamp(d).strftime("%Y-%m-%d")] = {
            row.ticker: (float(row.open), float(row.high), float(row.low), float(row.close))
            for row in day[["ticker", "open", "high", "low", "close"]].itertuples(index=False)
            if float(row.close) > 0
        }

    signal_frame = frame[frame["Date"].isin(signal_dates)].copy()
    eligible = pd.Series(True, index=signal_frame.index)
    eligible = _apply_optional_filter(eligible, signal_frame["close"], args.price_min, args.price_max)
    eligible = _apply_optional_filter(eligible, signal_frame["market_cap"], args.market_cap_min, args.market_cap_max)
    eligible = _apply_optional_filter(eligible, signal_frame["daily_return"], args.daily_return_min, args.daily_return_max)
    eligible = _apply_optional_filter(eligible, signal_frame["trading_value"], args.trading_value_min, args.trading_value_max)

    if args.ma_long is not None:
        eligible &= signal_frame[f"ma_{args.ma_long}"] < signal_frame[f"ma_{args.ma_mid}"]
        eligible &= signal_frame[f"ma_{args.ma_mid}"] < signal_frame[f"ma_{args.ma_short}"]

    target_column = None
    if args.envelope_period is not None:
        center = signal_frame[f"ma_{args.envelope_period}"]
        upper = center * (1.0 + args.envelope_percent / 100.0)
        lower = center * (1.0 - args.envelope_percent / 100.0)
        eligible &= signal_frame["close"] <= lower
        signal_frame["dynamic_target"] = (center + upper) / 2.0
        target_column = "dynamic_target"

    signal_frame = signal_frame[eligible].copy()
    by_ticker = {ticker: grp.reset_index(drop=True) for ticker, grp in frame.groupby("ticker", sort=False)}

    candidates: dict[str, list[str]] = {}
    strategy: dict[str, dict[str, dict[str, Any]]] = {}
    skipped_no_touch = 0

    target_cols = ["Date", "ticker"] + ([target_column] if target_column else [])
    for row in signal_frame[target_cols].itertuples(index=False, name=None):
        signal_date = pd.Timestamp(row[0])
        ticker = str(row[1])
        target = float(row[2]) if target_column else None
        max_hold = args.max_hold_days
        recent_touch_date = None
        raw_hold_days = None

        if target is not None:
            hist = by_ticker[ticker]
            before = hist[hist["Date"] < signal_date]
            if args.touch_lookback_days is not None:
                before = before.tail(args.touch_lookback_days)
            touched = before[(before["low"] <= target) & (before["high"] >= target)]
            if touched.empty:
                skipped_no_touch += 1
                continue
            touch_date = pd.Timestamp(touched.iloc[-1]["Date"])
            recent_touch_date = touch_date.strftime("%Y-%m-%d")
            raw_hold_days = date_index[signal_date] - date_index[touch_date]
            if raw_hold_days < 1:
                skipped_no_touch += 1
                continue
            max_hold = raw_hold_days if max_hold is None else min(raw_hold_days, max_hold)

        d = signal_date.strftime("%Y-%m-%d")
        candidates.setdefault(d, []).append(ticker)
        strategy.setdefault(d, {})[ticker] = {
            "target_price": target,
            "recent_touch_date": recent_touch_date,
            "raw_hold_days": raw_hold_days,
            "max_hold_days": max_hold,
        }

    for d in candidates:
        candidates[d] = sorted(candidates[d])

    prepared = {
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "signal_start": start.strftime("%Y-%m-%d"),
        "signal_end": end.strftime("%Y-%m-%d"),
        "prices": prices,
        "candidates": candidates,
        "strategy": strategy,
        "skipped_no_touch": skipped_no_touch,
    }
    with cache_path.open("wb") as fh:
        pickle.dump(prepared, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return prepared, cache_path, False


def _init_worker(prepared_path: str, args_dict: dict[str, Any]) -> None:
    global _PREPARED, _ARGS
    with Path(prepared_path).open("rb") as fh:
        _PREPARED = pickle.load(fh)
    _ARGS = args_dict


def _simulate_seed(seed: int) -> dict[str, Any]:
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
    positions: list[Position] = []
    pending: list[tuple[str, dict[str, Any]]] = []
    realized_pnl = total_commission = total_sell_tax = 0.0
    trade_count = winners = skipped_target_below_entry = 0
    exits = {"target": 0, "stop": 0, "hold_exit": 0}

    for idx, d in enumerate(dates):
        day_prices = prices.get(d, {})

        entering, pending = pending, []
        for ticker, meta in entering:
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
                Position(
                    ticker=ticker,
                    entry_price=entry,
                    quantity=qty,
                    invested=invested,
                    target_price=target,
                    stop_price=stop,
                    max_hold_days=meta.get("max_hold_days"),
                )
            )

        survivors: list[Position] = []
        for p in positions:
            px = day_prices.get(p.ticker)
            if px is None:
                survivors.append(p)
                continue
            _, high, low, close = px
            p.holding_days += 1

            reason = None
            exit_price = None
            # Preserve the existing daily-bar convention: target wins if both levels are touched.
            if p.target_price is not None and high >= p.target_price:
                reason, exit_price = "target", p.target_price
            elif p.stop_price is not None and low <= p.stop_price:
                reason, exit_price = "stop", p.stop_price
            elif p.max_hold_days is not None and p.holding_days >= p.max_hold_days:
                reason, exit_price = "hold_exit", close

            if reason is None:
                survivors.append(p)
                continue

            gross_sale = p.quantity * float(exit_price)
            sell_commission = gross_sale * a["commission_rate"]
            sell_tax = gross_sale * (kiwoom_sell_tax_rate(d) + a["sell_tax_rate"])
            sell_slippage = gross_sale * a["slippage_bps"] / 10_000.0
            proceeds = gross_sale - sell_commission - sell_tax - sell_slippage
            pnl = proceeds - p.invested
            cash += proceeds
            realized_pnl += pnl
            total_commission += sell_commission
            total_sell_tax += sell_tax
            trade_count += 1
            winners += int(pnl > 0)
            exits[reason] += 1
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
                    pending.append((ticker, meta))

        if d > prepared["signal_end"] and not positions and not pending:
            break

    final_equity = cash
    if positions:
        last_prices = prices.get(dates[-1], {})
        for p in positions:
            px = last_prices.get(p.ticker)
            final_equity += p.quantity * (px[3] if px else p.entry_price)

    return {
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


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(pd.Series(values, dtype="float64").quantile(q))


def summarize(results: pd.DataFrame, args: argparse.Namespace, prepared: dict[str, Any]) -> dict[str, Any]:
    returns = results["total_return_pct"].astype(float).tolist()
    wins = results["win_rate_pct"].astype(float).tolist()
    return {
        "seed_start": args.seed_start,
        "seed_end": args.seed_end,
        "seed_count": len(results),
        "filters": _filter_dict(args),
        "target_rule": (
            "midpoint_between_envelope_center_and_upper"
            if args.envelope_period is not None
            else (f"fixed_{args.fixed_take_profit}" if args.fixed_take_profit is not None else None)
        ),
        "stop_gap_ratio": args.stop_gap_ratio,
        "fixed_stop_loss_pct": args.fixed_stop_loss_pct,
        "skipped_no_recent_target_touch": int(prepared.get("skipped_no_touch", 0)),
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
        },
        "win_rate_stats_pct": {
            "mean": round(statistics.fmean(wins), 4),
            "median": round(statistics.median(wins), 4),
            "min": round(min(wins), 4),
            "max": round(max(wins), 4),
        },
        "mean_counts": {
            "trades": round(float(results["trade_count"].mean()), 2),
            "target": round(float(results["target_count"].mean()), 2),
            "stop": round(float(results["stop_count"].mean()), 2),
            "hold_exit": round(float(results["hold_exit_count"].mean()), 2),
            "open_positions": round(float(results["open_position_count"].mean()), 2),
        },
        "cost_model": {
            "commission_rate_each_side": args.commission_rate,
            "extra_sell_tax_rate": args.sell_tax_rate,
            "slippage_bps": args.slippage_bps,
            "kiwoom_historical_sell_tax": True,
        },
    }


def main() -> None:
    args = build_parser().parse_args()
    validate_args(args)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print("[1/2] preparing unified candidate set...")
    prepared, prepared_path, reused = prepare(args)
    print(f"prepared cache: {prepared_path} ({'reused' if reused else 'built'})")
    print("filters:")
    print(json.dumps(_filter_dict(args), ensure_ascii=False, indent=2))
    if args.envelope_period is not None:
        print(
            "target: midpoint(center, upper) = "
            f"Envelope center * (1 + {args.envelope_percent:g}% / 2)"
        )
        print("hold: trading days since the most recent historical candle touching that target")
    if args.stop_gap_ratio is not None:
        print(f"stop: entry - {args.stop_gap_ratio:g} * (target - entry)")

    seeds = list(range(args.seed_start, args.seed_end + 1))
    workers = min(args.workers, len(seeds), os.cpu_count() or 1)
    args_dict = vars(args).copy()
    print(f"[2/2] running {len(seeds)} seeds with {workers} process(es)...")

    rows: list[dict[str, Any]] = []
    if workers == 1:
        _init_worker(str(prepared_path), args_dict)
        for n, seed in enumerate(seeds, 1):
            rows.append(_simulate_seed(seed))
            print(f"[{n}/{len(seeds)}] seed {seed} complete")
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(str(prepared_path), args_dict),
        ) as pool:
            for n, row in enumerate(pool.map(_simulate_seed, seeds), 1):
                rows.append(row)
                print(f"[{n}/{len(seeds)}] seed {row['seed']} complete")

    results = pd.DataFrame(rows).sort_values("seed").reset_index(drop=True)
    results.to_csv(out / "seed_results.csv", index=False, encoding="utf-8-sig")
    summary = summarize(results, args, prepared)
    summary["workers"] = workers
    summary["prepared_cache"] = str(prepared_path)
    with (out / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print("\n=== Unified sweep summary ===")
    print(json.dumps(summary["return_stats_pct"], ensure_ascii=False, indent=2))
    print(f"saved: {out / 'seed_results.csv'}")
    print(f"saved: {out / 'seed_summary.json'}")


if __name__ == "__main__":
    main()
