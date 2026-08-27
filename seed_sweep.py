from __future__ import annotations

import argparse
import hashlib
import json
import math
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


@dataclass
class Position:
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


def kiwoom_sell_tax_rate(day: str | pd.Timestamp) -> float:
    d = pd.Timestamp(day)
    if d < pd.Timestamp("2023-01-01"):
        return 0.0023
    if d < pd.Timestamp("2024-01-01"):
        return 0.0020
    if d < pd.Timestamp("2025-01-01"):
        return 0.0018
    return 0.0015


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "All-in-one stocksim 100-seed runner. Conditions are opt-in: "
            "if a setting is omitted, that condition is not applied."
        )
    )
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--seed-start", type=int, default=1)
    p.add_argument("--seed-end", type=int, default=100)
    p.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    p.add_argument("--daily-buy-count", type=int, default=5)
    p.add_argument("--initial-capital", type=int, default=1_000_000)
    p.add_argument("--position-size", type=int, default=100_000)
    p.add_argument("--no-reentry", action="store_true")
    p.add_argument("--markets", default="KOSPI,KOSDAQ")

    p.add_argument("--price-min", type=float, default=None)
    p.add_argument("--price-max", type=float, default=None)
    p.add_argument("--market-cap-min", type=float, default=None)
    p.add_argument("--market-cap-max", type=float, default=None)
    p.add_argument("--daily-return-min", type=float, default=None)
    p.add_argument("--daily-return-max", type=float, default=None)
    p.add_argument("--trading-value-min", type=float, default=None)
    p.add_argument("--trading-value-max", type=float, default=None)

    p.add_argument("--ma-long", type=int, default=None)
    p.add_argument("--ma-mid", type=int, default=None)
    p.add_argument("--ma-short", type=int, default=None)

    p.add_argument("--envelope-period", type=int, default=None)
    p.add_argument("--envelope-percent", type=float, default=None)
    p.add_argument(
        "--envelope-filter",
        choices=("below", "recent-low-cross", "recent-close-cross"),
        default=None,
    )
    p.add_argument("--envelope-cross-lookback-days", type=int, default=3)

    p.add_argument(
        "--target-mode",
        choices=("envelope-center", "envelope-mid-upper", "fixed-return", "none"),
        default="none",
    )
    p.add_argument("--fixed-take-profit", type=float, default=None)
    p.add_argument("--planned-target-return-min", type=float, default=None)
    p.add_argument("--planned-target-return-max", type=float, default=None)

    p.add_argument(
        "--hold-mode",
        choices=("recent-target-touch", "fixed", "none"),
        default="none",
    )
    p.add_argument("--hold-period-ratio", type=float, default=1.0)
    p.add_argument("--fixed-hold-days", type=int, default=None)
    p.add_argument("--touch-lookback-days", type=int, default=None)
    p.add_argument("--max-hold-days", type=int, default=None)

    p.add_argument(
        "--stop-mode",
        choices=("target-gap", "fixed-pct", "none"),
        default="none",
    )
    p.add_argument("--stop-gap-ratio", type=float, default=None)
    p.add_argument("--fixed-stop-loss-pct", type=float, default=None)
    p.add_argument(
        "--compare-fixed-stops",
        default=None,
        help="Comma-separated decimal stops, e.g. 0.03,0.04",
    )

    p.add_argument("--commission-rate", type=float, default=0.00015)
    p.add_argument("--sell-tax-rate", type=float, default=0.0)
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.add_argument("--cache-dir", default=".cache/stocksim")
    p.add_argument("--output-dir", default="results/seed_sweep")
    p.add_argument("--rebuild-prepared-cache", action="store_true")
    p.add_argument("--no-trade-diagnostics", action="store_true")
    return p


def parse_compare_stops(text: str) -> list[float]:
    return [float(x.strip()) for x in text.split(",") if x.strip()]


def validate_args(a: argparse.Namespace) -> None:
    if pd.Timestamp(a.start) > pd.Timestamp(a.end):
        raise ValueError("start must be on or before end")
    if a.seed_end < a.seed_start:
        raise ValueError("seed-end must be >= seed-start")
    if a.workers < 1 or a.daily_buy_count < 1:
        raise ValueError("workers and daily-buy-count must be >= 1")
    if a.initial_capital <= 0 or a.position_size <= 0:
        raise ValueError("capital values must be > 0")

    ma = (a.ma_long, a.ma_mid, a.ma_short)
    if any(x is not None for x in ma):
        if any(x is None for x in ma):
            raise ValueError("ma-long, ma-mid, ma-short must be supplied together")
        if not (a.ma_long > a.ma_mid > a.ma_short >= 1):
            raise ValueError("MA periods must satisfy long > mid > short >= 1")

    env = (a.envelope_period, a.envelope_percent)
    if any(x is not None for x in env):
        if any(x is None for x in env):
            raise ValueError("envelope-period and envelope-percent must be supplied together")
        if a.envelope_period < 1 or not 0 < a.envelope_percent < 100:
            raise ValueError("invalid Envelope settings")
    if a.envelope_filter is not None and a.envelope_period is None:
        raise ValueError("envelope-filter requires Envelope settings")
    if a.target_mode.startswith("envelope") and a.envelope_period is None:
        raise ValueError("Envelope target mode requires Envelope settings")
    if a.target_mode == "fixed-return" and (a.fixed_take_profit is None or a.fixed_take_profit <= 0):
        raise ValueError("fixed-return target requires --fixed-take-profit > 0")

    if a.hold_mode == "fixed" and (a.fixed_hold_days is None or a.fixed_hold_days < 1):
        raise ValueError("fixed hold requires --fixed-hold-days >= 1")
    if a.hold_mode == "recent-target-touch" and a.target_mode == "none":
        raise ValueError("recent-target-touch hold requires a target")
    if a.hold_period_ratio <= 0:
        raise ValueError("hold-period-ratio must be > 0")
    if a.max_hold_days is not None and a.max_hold_days < 1:
        raise ValueError("max-hold-days must be >= 1")
    if a.touch_lookback_days is not None and a.touch_lookback_days < 1:
        raise ValueError("touch-lookback-days must be >= 1")

    if a.stop_mode == "target-gap" and (a.stop_gap_ratio is None or a.stop_gap_ratio <= 0):
        raise ValueError("target-gap stop requires --stop-gap-ratio > 0")
    if a.stop_mode == "fixed-pct" and (a.fixed_stop_loss_pct is None or not 0 < a.fixed_stop_loss_pct < 1):
        raise ValueError("fixed-pct stop requires --fixed-stop-loss-pct between 0 and 1")
    if a.compare_fixed_stops:
        for value in parse_compare_stops(a.compare_fixed_stops):
            if not 0 < value < 1:
                raise ValueError("compare-fixed-stops values must be decimals between 0 and 1")

    if a.planned_target_return_min is not None and a.planned_target_return_min < 0:
        raise ValueError("planned-target-return-min must be >= 0")
    if a.planned_target_return_max is not None and a.planned_target_return_max <= 0:
        raise ValueError("planned-target-return-max must be > 0")
    if (
        a.planned_target_return_min is not None
        and a.planned_target_return_max is not None
        and a.planned_target_return_max < a.planned_target_return_min
    ):
        raise ValueError("planned target max must be >= min")


def config_dict(a: argparse.Namespace) -> dict[str, Any]:
    keys = [
        "price_min", "price_max", "market_cap_min", "market_cap_max",
        "daily_return_min", "daily_return_max", "trading_value_min", "trading_value_max",
        "ma_long", "ma_mid", "ma_short", "envelope_period", "envelope_percent",
        "envelope_filter", "envelope_cross_lookback_days", "target_mode",
        "fixed_take_profit", "planned_target_return_min", "planned_target_return_max",
        "hold_mode", "hold_period_ratio", "fixed_hold_days", "touch_lookback_days",
        "max_hold_days", "stop_mode", "stop_gap_ratio", "fixed_stop_loss_pct",
    ]
    return {key: getattr(a, key) for key in keys}


def _cache_path(a: argparse.Namespace) -> Path:
    cfg = config_dict(a)
    cfg["stop_mode"] = None
    cfg["stop_gap_ratio"] = None
    cfg["fixed_stop_loss_pct"] = None
    key = {"start": a.start, "end": a.end, "markets": a.markets, **cfg, "format_version": 3}
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
    root = Path(a.cache_dir) / "prepared_allinone"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.pkl"


def _apply_range(mask: pd.Series, series: pd.Series, lo: float | None, hi: float | None) -> pd.Series:
    if lo is not None:
        mask &= series >= lo
    if hi is not None:
        mask &= series <= hi
    return mask


def prepare(a: argparse.Namespace) -> tuple[dict[str, Any], Path, bool]:
    cache_path = _cache_path(a)
    if cache_path.exists() and not a.rebuild_prepared_cache:
        with cache_path.open("rb") as fh:
            return pickle.load(fh), cache_path, True

    if not hasattr(stock, "_slice_years"):
        raise RuntimeError("This runner requires the repository's local marcap-backed pykrx shim")

    start = pd.Timestamp(a.start).normalize()
    end = pd.Timestamp(a.end).normalize()
    periods = [x for x in (a.ma_long, a.ma_mid, a.ma_short, a.envelope_period) if x]
    warmup_days = max([1100, *(int(x) * 3 for x in periods)])
    if a.touch_lookback_days is not None:
        warmup_days = max(warmup_days, a.touch_lookback_days * 2)
    warmup_start = start - pd.DateOffset(days=warmup_days)

    today = pd.Timestamp.today().normalize()
    future_days = max(90, (a.max_hold_days or a.fixed_hold_days or 240) * 3)
    future_end = min(today, end + pd.DateOffset(days=future_days))
    if future_end < end:
        future_end = end

    frame = stock._slice_years(
        warmup_start.strftime("%Y%m%d"), future_end.strftime("%Y%m%d")
    ).copy()
    if frame.empty:
        raise RuntimeError("No marcap rows found")

    markets = {x.strip().upper() for x in a.markets.split(",") if x.strip()}
    if markets and "Market" in frame.columns:
        frame = frame[frame["Market"].astype(str).str.upper().isin(markets)].copy()

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
    for n in sorted(set(int(x) for x in periods)):
        frame[f"ma_{n}"] = grouped_close.transform(
            lambda series, n=n: series.rolling(n, min_periods=n).mean()
        )

    if a.envelope_period is not None:
        frame["env_center"] = frame[f"ma_{a.envelope_period}"]
        frame["env_upper"] = frame["env_center"] * (1 + a.envelope_percent / 100.0)
        frame["env_lower"] = frame["env_center"] * (1 - a.envelope_percent / 100.0)
        grouped = frame.groupby("ticker", sort=False)
        prev_low = grouped["low"].shift(1)
        prev_close = grouped["close"].shift(1)
        prev_lower = grouped["env_lower"].shift(1)
        frame["low_cross"] = (
            (prev_low > prev_lower) & (frame["low"] <= frame["env_lower"])
        ).fillna(False)
        frame["close_cross"] = (
            (prev_close > prev_lower) & (frame["close"] <= frame["env_lower"])
        ).fillna(False)
        for col in ("low_cross", "close_cross"):
            frame[f"recent_{col}"] = frame.groupby("ticker", sort=False)[col].transform(
                lambda series: series.astype(int)
                .rolling(a.envelope_cross_lookback_days, min_periods=1)
                .max()
                .astype(bool)
            )

    dates = sorted(pd.Timestamp(x) for x in frame["Date"].drop_duplicates())
    date_index = {d: i for i, d in enumerate(dates)}
    signal_dates = {d for d in dates if start <= d <= end}
    if not signal_dates:
        raise RuntimeError("No trading days in requested period")

    prices: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    for d, day in frame.groupby("Date", sort=True):
        prices[pd.Timestamp(d).strftime("%Y-%m-%d")] = {
            row.ticker: (float(row.open), float(row.high), float(row.low), float(row.close))
            for row in day[["ticker", "open", "high", "low", "close"]].itertuples(index=False)
            if float(row.close) > 0
        }

    signal_frame = frame[frame["Date"].isin(signal_dates)].copy()
    eligible = pd.Series(True, index=signal_frame.index)
    eligible = _apply_range(eligible, signal_frame["close"], a.price_min, a.price_max)
    eligible = _apply_range(eligible, signal_frame["market_cap"], a.market_cap_min, a.market_cap_max)
    eligible = _apply_range(eligible, signal_frame["daily_return"], a.daily_return_min, a.daily_return_max)
    eligible = _apply_range(eligible, signal_frame["trading_value"], a.trading_value_min, a.trading_value_max)

    if a.ma_long is not None:
        eligible &= signal_frame[f"ma_{a.ma_long}"] < signal_frame[f"ma_{a.ma_mid}"]
        eligible &= signal_frame[f"ma_{a.ma_mid}"] < signal_frame[f"ma_{a.ma_short}"]

    if a.envelope_filter == "below":
        eligible &= signal_frame["close"] <= signal_frame["env_lower"]
    elif a.envelope_filter == "recent-low-cross":
        eligible &= signal_frame["recent_low_cross"]
    elif a.envelope_filter == "recent-close-cross":
        eligible &= signal_frame["recent_close_cross"]

    if a.target_mode == "envelope-center":
        signal_frame["signal_target"] = signal_frame["env_center"]
    elif a.target_mode == "envelope-mid-upper":
        signal_frame["signal_target"] = (
            signal_frame["env_center"] + signal_frame["env_upper"]
        ) / 2.0
    else:
        signal_frame["signal_target"] = float("nan")

    signal_frame = signal_frame[eligible].copy()
    by_ticker = {
        ticker: group.reset_index(drop=True)
        for ticker, group in frame.groupby("ticker", sort=False)
    }
    candidates: dict[str, list[str]] = {}
    strategy: dict[str, dict[str, dict[str, Any]]] = {}
    skipped_no_touch = 0

    for signal_date, ticker, signal_target in signal_frame[
        ["Date", "ticker", "signal_target"]
    ].itertuples(index=False, name=None):
        signal_date = pd.Timestamp(signal_date)
        ticker = str(ticker)
        target = None if pd.isna(signal_target) else float(signal_target)
        recent_touch_date = None
        raw_hold_days = None
        hold_days = None

        if a.hold_mode == "recent-target-touch":
            hist = by_ticker[ticker]
            before = hist[hist["Date"] < signal_date]
            if a.touch_lookback_days is not None:
                before = before.tail(a.touch_lookback_days)
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
            hold_days = max(1, int(math.ceil(raw_hold_days * a.hold_period_ratio)))
        elif a.hold_mode == "fixed":
            hold_days = a.fixed_hold_days

        if hold_days is not None and a.max_hold_days is not None:
            hold_days = min(hold_days, a.max_hold_days)

        day_key = signal_date.strftime("%Y-%m-%d")
        candidates.setdefault(day_key, []).append(ticker)
        strategy.setdefault(day_key, {})[ticker] = {
            "signal_target": target,
            "recent_touch_date": recent_touch_date,
            "raw_hold_days": raw_hold_days,
            "max_hold_days": hold_days,
        }

    for day_key in candidates:
        candidates[day_key] = sorted(candidates[day_key])

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


def _pct(num: float, den: float) -> float | None:
    return None if den == 0 else num / den * 100.0


def simulate_seed(seed: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if _PREPARED is None or _ARGS is None:
        raise RuntimeError("worker not initialized")

    prepared = _PREPARED
    a = _ARGS
    rng = random.Random(seed)
    cash = float(a["initial_capital"])
    positions: list[Position] = []
    pending: list[tuple[str, str, dict[str, Any]]] = []
    trades: list[dict[str, Any]] = []
    realized = commission_total = tax_total = 0.0
    skipped_target_below_entry = skipped_target_return_range = 0
    exits = {"target": 0, "stop": 0, "hold_exit": 0}
    winners = 0

    for idx, day_key in enumerate(prepared["dates"]):
        day_prices = prepared["prices"].get(day_key, {})
        entering, pending = pending, []
        for ticker, signal_date, meta in entering:
            px = day_prices.get(ticker)
            if px is None:
                continue
            if a["no_reentry"] and any(pos.ticker == ticker for pos in positions):
                continue
            raw_open = px[0]
            if raw_open <= 0:
                continue
            entry = raw_open * (1 + a["slippage_bps"] / 10_000.0)

            target = meta.get("signal_target")
            if a["target_mode"] == "fixed-return":
                target = entry * (1 + a["fixed_take_profit"])
            elif a["target_mode"] == "none":
                target = None
            if target is not None and target <= entry:
                skipped_target_below_entry += 1
                continue

            planned_target_pct = None if target is None else (target / entry - 1) * 100.0
            if planned_target_pct is not None:
                if a["planned_target_return_min"] is not None and planned_target_pct < a["planned_target_return_min"]:
                    skipped_target_return_range += 1
                    continue
                if a["planned_target_return_max"] is not None and planned_target_pct > a["planned_target_return_max"]:
                    skipped_target_return_range += 1
                    continue

            stop = None
            if a["stop_mode"] == "target-gap":
                if target is None:
                    raise RuntimeError("target-gap stop requires a target")
                stop = entry - a["stop_gap_ratio"] * (target - entry)
            elif a["stop_mode"] == "fixed-pct":
                stop = entry * (1 - a["fixed_stop_loss_pct"])

            qty = int(min(a["position_size"], cash) // (entry * (1 + a["commission_rate"])))
            if qty < 1:
                continue
            gross = qty * entry
            buy_commission = gross * a["commission_rate"]
            invested = gross + buy_commission
            cash -= invested
            commission_total += buy_commission
            positions.append(
                Position(
                    ticker=ticker,
                    signal_date=signal_date,
                    entry_date=day_key,
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

        survivors: list[Position] = []
        for pos in positions:
            px = day_prices.get(pos.ticker)
            if px is None:
                survivors.append(pos)
                continue
            _, high, low, close = px
            pos.holding_days += 1
            reason = None
            exit_price = None
            if pos.target_price is not None and high >= pos.target_price:
                reason, exit_price = "target", pos.target_price
            elif pos.stop_price is not None and low <= pos.stop_price:
                reason, exit_price = "stop", pos.stop_price
            elif pos.max_hold_days is not None and pos.holding_days >= pos.max_hold_days:
                reason, exit_price = "hold_exit", close

            if reason is None:
                survivors.append(pos)
                continue

            gross_sale = pos.quantity * float(exit_price)
            sell_commission = gross_sale * a["commission_rate"]
            sell_tax = gross_sale * (kiwoom_sell_tax_rate(day_key) + a["sell_tax_rate"])
            sell_slippage = gross_sale * a["slippage_bps"] / 10_000.0
            proceeds = gross_sale - sell_commission - sell_tax - sell_slippage
            pnl = proceeds - pos.invested
            cash += proceeds
            realized += pnl
            commission_total += sell_commission
            tax_total += sell_tax
            winners += int(pnl > 0)
            exits[reason] += 1

            target_return = None if pos.target_price is None else _pct(
                pos.target_price - pos.entry_price, pos.entry_price
            )
            stop_return = None if pos.stop_price is None else _pct(
                pos.stop_price - pos.entry_price, pos.entry_price
            )
            trades.append(
                {
                    "seed": seed,
                    "ticker": pos.ticker,
                    "signal_date": pos.signal_date,
                    "entry_date": pos.entry_date,
                    "exit_date": day_key,
                    "recent_touch_date": pos.recent_touch_date,
                    "entry_price": round(pos.entry_price, 4),
                    "target_price": round(pos.target_price, 4) if pos.target_price is not None else None,
                    "stop_price": round(pos.stop_price, 4) if pos.stop_price is not None else None,
                    "exit_price": round(float(exit_price), 4),
                    "planned_target_return_pct": round(target_return, 4) if target_return is not None else None,
                    "planned_stop_loss_pct": round(-stop_return, 4) if stop_return is not None else None,
                    "raw_hold_days": pos.raw_hold_days,
                    "planned_hold_days": pos.max_hold_days,
                    "actual_holding_days": pos.holding_days,
                    "exit_reason": reason,
                    "quantity": pos.quantity,
                    "invested_krw": round(pos.invested, 2),
                    "buy_commission_krw": round(pos.quantity * pos.entry_price * a["commission_rate"], 2),
                    "sell_commission_krw": round(sell_commission, 2),
                    "sell_tax_krw": round(sell_tax, 2),
                    "pnl_krw": round(pnl, 2),
                    "return_pct": round(_pct(pnl, pos.invested) or 0.0, 4),
                }
            )
        positions = survivors

        if prepared["signal_start"] <= day_key <= prepared["signal_end"]:
            candidates = list(prepared["candidates"].get(day_key, ()))
            if a["no_reentry"]:
                active = {pos.ticker for pos in positions}
                candidates = [ticker for ticker in candidates if ticker not in active]
            rng.shuffle(candidates)
            for ticker in candidates[: a["daily_buy_count"]]:
                if idx + 1 < len(prepared["dates"]):
                    meta = prepared["strategy"].get(day_key, {}).get(ticker)
                    if meta is not None:
                        pending.append((ticker, day_key, meta))

        if day_key > prepared["signal_end"] and not positions and not pending:
            break

    final_equity = cash
    if positions:
        last_prices = prepared["prices"].get(prepared["dates"][-1], {})
        for pos in positions:
            px = last_prices.get(pos.ticker)
            final_equity += pos.quantity * (px[3] if px else pos.entry_price)

    count = len(trades)
    return (
        {
            "seed": seed,
            "total_return_pct": round((final_equity / a["initial_capital"] - 1) * 100.0, 4),
            "final_equity_krw": round(final_equity, 2),
            "total_realized_pnl_krw": round(realized, 2),
            "trade_count": count,
            "win_rate_pct": round(winners / count * 100.0, 4) if count else 0.0,
            "target_count": exits["target"],
            "stop_count": exits["stop"],
            "hold_exit_count": exits["hold_exit"],
            "open_position_count": len(positions),
            "skipped_target_below_entry": skipped_target_below_entry,
            "skipped_target_return_range": skipped_target_return_range,
            "total_commission_krw": round(commission_total, 2),
            "total_sell_tax_krw": round(tax_total, 2),
        },
        trades,
    )


def _series_stats(series: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "min": None,
            "p05": None,
            "p25": None,
            "p75": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": int(len(values)),
        "mean": round(float(values.mean()), 4),
        "median": round(float(values.median()), 4),
        "min": round(float(values.min()), 4),
        "p05": round(float(values.quantile(0.05)), 4),
        "p25": round(float(values.quantile(0.25)), 4),
        "p75": round(float(values.quantile(0.75)), 4),
        "p95": round(float(values.quantile(0.95)), 4),
        "max": round(float(values.max()), 4),
    }


def summarize(
    results: pd.DataFrame,
    trades: pd.DataFrame,
    a: argparse.Namespace,
    prepared: dict[str, Any],
) -> dict[str, Any]:
    returns = results["total_return_pct"].astype(float).tolist()
    wins = results["win_rate_pct"].astype(float).tolist()
    trade_diag: dict[str, Any] = {}
    if not a.no_trade_diagnostics and not trades.empty:
        target_rows = trades[trades["exit_reason"] == "target"]
        hold_rows = trades[trades["exit_reason"] == "hold_exit"]
        trade_diag = {
            "planned_target_return_distribution_pct": _series_stats(trades["planned_target_return_pct"]),
            "planned_stop_loss_distribution_pct": _series_stats(trades["planned_stop_loss_pct"]),
            "planned_hold_days_distribution": _series_stats(trades["planned_hold_days"]),
            "actual_holding_days_distribution": _series_stats(trades["actual_holding_days"]),
            "target_hit_average_holding_days": (
                round(float(target_rows["actual_holding_days"].mean()), 4)
                if not target_rows.empty
                else None
            ),
            "hold_exit_average_final_return_pct": (
                round(float(hold_rows["return_pct"].mean()), 4)
                if not hold_rows.empty
                else None
            ),
            "return_by_exit_reason_pct": {
                reason: _series_stats(group["return_pct"])
                for reason, group in trades.groupby("exit_reason", sort=True)
            },
        }

    return {
        "seed_start": a.seed_start,
        "seed_end": a.seed_end,
        "seed_count": len(results),
        "config": config_dict(a),
        "skipped_no_recent_target_touch": int(prepared.get("skipped_no_touch", 0)),
        "return_stats_pct": {
            "mean": round(statistics.fmean(returns), 4),
            "median": round(statistics.median(returns), 4),
            "stdev": round(statistics.pstdev(returns), 4) if len(returns) > 1 else 0.0,
            "min": round(min(returns), 4),
            "p05": round(float(pd.Series(returns).quantile(0.05)), 4),
            "p25": round(float(pd.Series(returns).quantile(0.25)), 4),
            "p75": round(float(pd.Series(returns).quantile(0.75)), 4),
            "p95": round(float(pd.Series(returns).quantile(0.95)), 4),
            "max": round(max(returns), 4),
            "positive_seed_rate_pct": round(
                sum(value > 0 for value in returns) / len(returns) * 100.0, 4
            ),
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
            "commission_rate_each_side": a.commission_rate,
            "extra_sell_tax_rate": a.sell_tax_rate,
            "slippage_bps": a.slippage_bps,
            "kiwoom_historical_sell_tax": True,
        },
        "trade_diagnostics": trade_diag,
    }


def run_one(a: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    prepared, prepared_path, reused = prepare(a)
    print(f"prepared cache: {prepared_path} ({'reused' if reused else 'built'})")
    print(json.dumps(config_dict(a), ensure_ascii=False, indent=2))
    seeds = list(range(a.seed_start, a.seed_end + 1))
    workers = min(a.workers, len(seeds), os.cpu_count() or 1)
    args_dict = vars(a).copy()
    rows: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []

    if workers == 1:
        _init_worker(str(prepared_path), args_dict)
        iterator = (simulate_seed(seed) for seed in seeds)
        for n, (row, seed_trades) in enumerate(iterator, 1):
            rows.append(row)
            all_trades.extend(seed_trades)
            print(f"[{n}/{len(seeds)}] seed {row['seed']} complete")
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(str(prepared_path), args_dict),
        ) as pool:
            for n, (row, seed_trades) in enumerate(pool.map(simulate_seed, seeds), 1):
                rows.append(row)
                all_trades.extend(seed_trades)
                print(f"[{n}/{len(seeds)}] seed {row['seed']} complete")

    output_dir.mkdir(parents=True, exist_ok=True)
    results = pd.DataFrame(rows).sort_values("seed").reset_index(drop=True)
    trades = pd.DataFrame(all_trades)
    if not trades.empty:
        trades = trades.sort_values(["seed", "entry_date", "ticker"]).reset_index(drop=True)

    results.to_csv(output_dir / "seed_results.csv", index=False, encoding="utf-8-sig")
    if not a.no_trade_diagnostics:
        trades.to_csv(output_dir / "trade_diagnostics.csv", index=False, encoding="utf-8-sig")

    summary = summarize(results, trades, a, prepared)
    summary["workers"] = workers
    summary["prepared_cache"] = str(prepared_path)
    with (output_dir / "seed_summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print(json.dumps(summary["return_stats_pct"], ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    a = build_parser().parse_args()
    validate_args(a)
    root = Path(a.output_dir)

    if a.compare_fixed_stops:
        comparison = []
        for stop in parse_compare_stops(a.compare_fixed_stops):
            variant = argparse.Namespace(**vars(a))
            variant.compare_fixed_stops = None
            variant.stop_mode = "fixed-pct"
            variant.fixed_stop_loss_pct = stop
            variant.stop_gap_ratio = None
            subdir = root / f"stop_{stop * 100:g}pct"
            print(f"\n=== fixed stop {stop * 100:g}% ===")
            summary = run_one(variant, subdir)
            stats = summary["return_stats_pct"]
            comparison.append(
                {
                    "fixed_stop_loss_pct": stop,
                    "mean_return_pct": stats["mean"],
                    "median_return_pct": stats["median"],
                    "positive_seed_rate_pct": stats["positive_seed_rate_pct"],
                    "mean_trade_count": summary["mean_counts"]["trades"],
                    "mean_win_rate_pct": summary["win_rate_stats_pct"]["mean"],
                }
            )
        root.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(comparison).to_csv(root / "comparison.csv", index=False, encoding="utf-8-sig")
        with (root / "comparison.json").open("w", encoding="utf-8") as fh:
            json.dump(comparison, fh, ensure_ascii=False, indent=2)
    else:
        run_one(a, root)


if __name__ == "__main__":
    main()
