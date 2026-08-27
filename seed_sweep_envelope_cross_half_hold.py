from __future__ import annotations

import hashlib
import json
import math
import pickle
from pathlib import Path
from typing import Any

import pandas as pd

import seed_sweep_unified as unified
import seed_sweep_unified_diagnostics as diagnostics
from pykrx import stock


_BASE_BUILD_PARSER = unified.build_parser
_BASE_VALIDATE_ARGS = unified.validate_args
_BASE_FILTER_DICT = unified._filter_dict


def build_parser():
    parser = _BASE_BUILD_PARSER()
    parser.description = (
        "Unified diagnostic sweep with a recent LOW-based Envelope lower-band "
        "downward-cross condition and scaled recent-touch holding period."
    )
    parser.add_argument(
        "--envelope-cross-lookback-days",
        type=int,
        default=3,
        help=(
            "Require a LOW-based downward cross of the Envelope lower band within "
            "the most recent N trading sessions. Default: 3."
        ),
    )
    parser.add_argument(
        "--hold-period-ratio",
        type=float,
        default=0.5,
        help=(
            "Multiply the original recent-target-touch holding period by this ratio. "
            "Fractional days are rounded up. Default: 0.5."
        ),
    )
    parser.set_defaults(output_dir="results/seed_sweep_envelope_cross_half_hold")
    return parser


def validate_args(args) -> None:
    _BASE_VALIDATE_ARGS(args)
    if args.envelope_cross_lookback_days < 1:
        raise ValueError("envelope-cross-lookback-days must be >= 1")
    if not 0 < args.hold_period_ratio <= 1:
        raise ValueError("hold-period-ratio must be > 0 and <= 1")
    if args.envelope_period is None:
        raise ValueError(
            "This experiment requires --envelope-period and --envelope-percent."
        )


def filter_dict(args) -> dict[str, Any]:
    out = _BASE_FILTER_DICT(args)
    out["envelope_cross_basis"] = "low"
    out["envelope_cross_lookback_days"] = args.envelope_cross_lookback_days
    out["hold_period_ratio"] = args.hold_period_ratio
    return out


def _cache_path(args) -> Path:
    key = {
        "start": args.start,
        "end": args.end,
        "markets": args.markets,
        **filter_dict(args),
        "strategy": "low_envelope_down_cross_recent_scaled_hold",
        "format_version": 1,
    }
    digest = hashlib.sha256(
        json.dumps(key, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    root = Path(args.cache_dir) / "prepared_envelope_cross_half_hold"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.pkl"


def prepare(args) -> tuple[dict[str, Any], Path, bool]:
    cache_path = _cache_path(args)
    if cache_path.exists() and not args.rebuild_prepared_cache:
        with cache_path.open("rb") as fh:
            return pickle.load(fh), cache_path, True

    if not hasattr(stock, "_slice_years"):
        raise RuntimeError(
            "This runner requires the repository's local marcap-backed pykrx shim."
        )

    start = pd.Timestamp(args.start).normalize()
    end = pd.Timestamp(args.end).normalize()
    ma_periods = [
        x
        for x in (args.ma_long, args.ma_mid, args.ma_short, args.envelope_period)
        if x
    ]
    warmup_days = max([800, *(int(x) * 3 for x in ma_periods)])
    if args.touch_lookback_days is not None:
        warmup_days = max(warmup_days, args.touch_lookback_days * 2)
    else:
        warmup_days = max(warmup_days, 1100)
    warmup_start = start - pd.DateOffset(days=warmup_days)

    today = pd.Timestamp.today().normalize()
    if args.max_hold_days is not None:
        future_end = min(
            today, end + pd.DateOffset(days=max(30, args.max_hold_days * 3))
        )
    else:
        future_end = min(today, end + pd.DateOffset(days=1100))
    if future_end < end:
        future_end = end

    frame = stock._slice_years(
        warmup_start.strftime("%Y%m%d"), future_end.strftime("%Y%m%d")
    ).copy()
    if frame.empty:
        raise RuntimeError("No marcap rows found for the requested period.")

    markets = tuple(
        x.strip().upper() for x in args.markets.split(",") if x.strip()
    )
    if markets and "Market" in frame.columns:
        frame = frame[
            frame["Market"].astype(str).str.upper().isin(set(markets))
        ].copy()

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

    center_col = f"ma_{args.envelope_period}"
    frame["envelope_center"] = frame[center_col]
    frame["envelope_upper"] = frame["envelope_center"] * (
        1.0 + args.envelope_percent / 100.0
    )
    frame["envelope_lower"] = frame["envelope_center"] * (
        1.0 - args.envelope_percent / 100.0
    )

    grouped = frame.groupby("ticker", sort=False)
    prev_low = grouped["low"].shift(1)
    prev_lower = grouped["envelope_lower"].shift(1)

    # LOW-based downward cross:
    # previous session low was above its lower band, and today's low is at/below
    # today's lower band. This intentionally does not use the close.
    frame["envelope_low_down_cross"] = (
        (prev_low > prev_lower) & (frame["low"] <= frame["envelope_lower"])
    ).fillna(False)
    frame["envelope_low_down_cross_recent"] = frame.groupby(
        "ticker", sort=False
    )["envelope_low_down_cross"].transform(
        lambda s: s.astype(int)
        .rolling(args.envelope_cross_lookback_days, min_periods=1)
        .max()
        .astype(bool)
    )

    dates = sorted(pd.Timestamp(x) for x in frame["Date"].drop_duplicates().tolist())
    signal_dates = {d for d in dates if start <= d <= end}
    if not signal_dates:
        raise RuntimeError("The requested period contains no trading days.")
    date_index = {d: i for i, d in enumerate(dates)}

    prices: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    for d, day in frame.groupby("Date", sort=True):
        prices[pd.Timestamp(d).strftime("%Y-%m-%d")] = {
            row.ticker: (
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
            )
            for row in day[["ticker", "open", "high", "low", "close"]].itertuples(
                index=False
            )
            if float(row.close) > 0
        }

    signal_frame = frame[frame["Date"].isin(signal_dates)].copy()
    eligible = pd.Series(True, index=signal_frame.index)
    eligible = unified._apply_optional_filter(
        eligible, signal_frame["close"], args.price_min, args.price_max
    )
    eligible = unified._apply_optional_filter(
        eligible, signal_frame["market_cap"], args.market_cap_min, args.market_cap_max
    )
    eligible = unified._apply_optional_filter(
        eligible,
        signal_frame["daily_return"],
        args.daily_return_min,
        args.daily_return_max,
    )
    eligible = unified._apply_optional_filter(
        eligible,
        signal_frame["trading_value"],
        args.trading_value_min,
        args.trading_value_max,
    )

    if args.ma_long is not None:
        eligible &= (
            signal_frame[f"ma_{args.ma_long}"]
            < signal_frame[f"ma_{args.ma_mid}"]
        )
        eligible &= (
            signal_frame[f"ma_{args.ma_mid}"]
            < signal_frame[f"ma_{args.ma_short}"]
        )

    # Changed Envelope condition: a LOW-based lower-band downward cross must have
    # occurred during the current session or the previous N-1 trading sessions.
    eligible &= signal_frame["envelope_low_down_cross_recent"]

    # Target remains the midpoint between the center and upper Envelope bands.
    signal_frame["dynamic_target"] = (
        signal_frame["envelope_center"] + signal_frame["envelope_upper"]
    ) / 2.0
    signal_frame = signal_frame[eligible].copy()

    by_ticker = {
        ticker: grp.reset_index(drop=True)
        for ticker, grp in frame.groupby("ticker", sort=False)
    }
    candidates: dict[str, list[str]] = {}
    strategy: dict[str, dict[str, dict[str, Any]]] = {}
    skipped_no_touch = 0

    for signal_date, ticker, target_raw in signal_frame[
        ["Date", "ticker", "dynamic_target"]
    ].itertuples(index=False, name=None):
        signal_date = pd.Timestamp(signal_date)
        ticker = str(ticker)
        target = float(target_raw)

        hist = by_ticker[ticker]
        before = hist[hist["Date"] < signal_date]
        if args.touch_lookback_days is not None:
            before = before.tail(args.touch_lookback_days)
        touched = before[(before["low"] <= target) & (before["high"] >= target)]
        if touched.empty:
            skipped_no_touch += 1
            continue

        touch_date = pd.Timestamp(touched.iloc[-1]["Date"])
        raw_hold_days = date_index[signal_date] - date_index[touch_date]
        if raw_hold_days < 1:
            skipped_no_touch += 1
            continue

        scaled_hold_days = max(
            1, int(math.ceil(raw_hold_days * args.hold_period_ratio))
        )
        if args.max_hold_days is not None:
            scaled_hold_days = min(scaled_hold_days, args.max_hold_days)

        d = signal_date.strftime("%Y-%m-%d")
        candidates.setdefault(d, []).append(ticker)
        strategy.setdefault(d, {})[ticker] = {
            "target_price": target,
            "recent_touch_date": touch_date.strftime("%Y-%m-%d"),
            "raw_hold_days": int(raw_hold_days),
            "scaled_hold_days": int(scaled_hold_days),
            "max_hold_days": int(scaled_hold_days),
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
        "envelope_cross_rule": {
            "basis": "low",
            "lookback_trading_days": args.envelope_cross_lookback_days,
            "definition": "prev_low > prev_lower_band AND current_low <= current_lower_band",
        },
        "hold_period_ratio": args.hold_period_ratio,
    }
    with cache_path.open("wb") as fh:
        pickle.dump(prepared, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return prepared, cache_path, False


def main() -> None:
    # Reuse the existing diagnostic engine and cost/seed mechanics, but swap in
    # the experimental parser, validation, filter metadata, and preparation.
    unified.build_parser = build_parser
    unified.validate_args = validate_args
    unified._filter_dict = filter_dict
    unified.prepare = prepare
    diagnostics.main()


if __name__ == "__main__":
    main()
