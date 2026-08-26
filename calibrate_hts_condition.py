from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from pykrx import stock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Point-in-time calibration runner for Kiwoom HTS condition performance validation. "
            "It selects every stock matching the reproducible condition on one date, then "
            "measures the return after a fixed number of trading days."
        )
    )
    parser.add_argument("--date", required=True, help="Signal/search date, YYYY-MM-DD")
    parser.add_argument("--hold-days", type=int, default=20, help="Trading days after the signal date")
    parser.add_argument("--price-min", type=float, default=0.0)
    parser.add_argument("--price-max", type=float, default=100_000.0)
    parser.add_argument("--market-cap-min", type=float, default=300_000_000_000.0)
    parser.add_argument("--market-cap-max", type=float, default=None)
    parser.add_argument("--daily-return-min", type=float, default=-7.0)
    parser.add_argument("--daily-return-max", type=float, default=-1.0)
    parser.add_argument("--trading-value-min", type=float, default=300_000_000.0)
    parser.add_argument("--trading-value-max", type=float, default=100_000_000_000.0)
    parser.add_argument("--ma-long", type=int, default=240)
    parser.add_argument("--ma-mid", type=int, default=120)
    parser.add_argument("--ma-short", type=int, default=60)
    parser.add_argument("--envelope-period", type=int, default=20)
    parser.add_argument("--envelope-percent", type=float, default=6.0)
    parser.add_argument("--markets", default="KOSPI,KOSDAQ")
    parser.add_argument("--output-dir", default="results/hts_calibration")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.hold_days < 1:
        raise ValueError("hold-days must be >= 1")
    if not (args.ma_long > args.ma_mid > args.ma_short >= 1):
        raise ValueError("MA periods must satisfy ma-long > ma-mid > ma-short >= 1")
    if args.envelope_period < 1:
        raise ValueError("envelope-period must be >= 1")
    if not 0 < args.envelope_percent < 100:
        raise ValueError("envelope-percent must be between 0 and 100")


def _load_frame(args: argparse.Namespace) -> pd.DataFrame:
    if not hasattr(stock, "_slice_years"):
        raise RuntimeError("This runner requires the repository's local marcap-backed pykrx shim.")

    signal_date = pd.Timestamp(args.date).normalize()
    warmup_period = max(args.ma_long, args.envelope_period)
    warmup_start = signal_date - pd.DateOffset(days=max(800, warmup_period * 3))
    future_end = signal_date + pd.DateOffset(days=max(60, args.hold_days * 4))

    frame = stock._slice_years(  # type: ignore[attr-defined]
        warmup_start.strftime("%Y%m%d"), future_end.strftime("%Y%m%d")
    ).copy()
    if frame.empty:
        raise RuntimeError("No marcap rows found for the requested period.")

    markets = {x.strip().upper() for x in args.markets.split(",") if x.strip()}
    if markets and "Market" in frame.columns:
        frame = frame[frame["Market"].astype(str).str.upper().isin(markets)].copy()

    ratio = stock._ratio_column(frame)  # type: ignore[attr-defined]
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
    periods = {args.ma_long, args.ma_mid, args.ma_short, args.envelope_period}
    for days in sorted(periods):
        frame[f"ma_{days}"] = grouped_close.transform(
            lambda series, n=days: series.rolling(n, min_periods=n).mean()
        )

    return frame


def _select_candidates(frame: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    signal_date = pd.Timestamp(args.date).normalize()
    day = frame[frame["Date"] == signal_date].copy()
    if day.empty:
        available = frame.loc[frame["Date"] <= signal_date, "Date"]
        nearest = available.max() if not available.empty else None
        suffix = f" Nearest prior trading day: {nearest.date()}." if pd.notna(nearest) else ""
        raise RuntimeError(f"{args.date} is not present in the market data.{suffix}")

    lower_envelope = day[f"ma_{args.envelope_period}"] * (1.0 - args.envelope_percent / 100.0)
    eligible = (
        (day["close"] >= args.price_min)
        & (day["close"] <= args.price_max)
        & (day["market_cap"] >= args.market_cap_min)
        & (day["daily_return"] >= args.daily_return_min)
        & (day["daily_return"] <= args.daily_return_max)
        & (day["trading_value"] >= args.trading_value_min)
        & (day["trading_value"] <= args.trading_value_max)
        & (day[f"ma_{args.ma_long}"] < day[f"ma_{args.ma_mid}"])
        & (day[f"ma_{args.ma_mid}"] < day[f"ma_{args.ma_short}"])
        & (day["close"] <= lower_envelope)
    )
    if args.market_cap_max is not None:
        eligible &= day["market_cap"] <= args.market_cap_max

    selected = day[eligible].copy()
    selected["envelope_lower"] = lower_envelope.loc[selected.index]
    return selected


def _attach_forward_returns(
    frame: pd.DataFrame,
    candidates: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    signal_date = pd.Timestamp(args.date).normalize()

    for row in candidates.itertuples(index=False):
        history = frame[(frame["ticker"] == row.ticker) & (frame["Date"] >= signal_date)].sort_values("Date")
        if history.empty:
            continue

        future = history[history["Date"] > signal_date]
        if len(future) < args.hold_days:
            exit_date = pd.NaT
            exit_close = float("nan")
            return_pct = float("nan")
        else:
            exit_row = future.iloc[args.hold_days - 1]
            exit_date = pd.Timestamp(exit_row["Date"])
            exit_close = float(exit_row["close"])
            return_pct = (exit_close / float(row.close) - 1.0) * 100.0

        name = getattr(row, "Name", None)
        market = getattr(row, "Market", None)
        rows.append(
            {
                "ticker": row.ticker,
                "name": str(name) if name is not None else row.ticker,
                "market": str(market) if market is not None else "",
                "signal_date": signal_date.strftime("%Y-%m-%d"),
                "signal_close": float(row.close),
                "exit_date": exit_date.strftime("%Y-%m-%d") if pd.notna(exit_date) else "",
                "exit_close": exit_close,
                "return_pct": return_pct,
                "daily_return_pct": float(row.daily_return),
                "market_cap_krw": float(row.market_cap),
                "trading_value_krw": float(row.trading_value),
                f"ma_{args.ma_long}": float(getattr(row, f"ma_{args.ma_long}")),
                f"ma_{args.ma_mid}": float(getattr(row, f"ma_{args.ma_mid}")),
                f"ma_{args.ma_short}": float(getattr(row, f"ma_{args.ma_short}")),
                "envelope_lower": float(row.envelope_lower),
            }
        )

    return pd.DataFrame(rows)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _validate_args(args)

    frame = _load_frame(args)
    candidates = _select_candidates(frame, args)
    results = _attach_forward_returns(frame, candidates, args)

    valid = results.dropna(subset=["return_pct"]) if not results.empty else results
    rising = int((valid["return_pct"] > 0).sum()) if not valid.empty else 0
    flat = int((valid["return_pct"] == 0).sum()) if not valid.empty else 0
    falling = int((valid["return_pct"] < 0).sum()) if not valid.empty else 0

    by_market: dict[str, dict[str, float | int]] = {}
    if not valid.empty and "market" in valid.columns:
        for market, group in valid.groupby("market", dropna=False):
            by_market[str(market)] = {
                "count": int(len(group)),
                "mean_return_pct": round(float(group["return_pct"].mean()), 4),
            }

    summary = {
        "signal_date": args.date,
        "hold_days": args.hold_days,
        "candidate_count": int(len(results)),
        "completed_return_count": int(len(valid)),
        "rising_count": rising,
        "flat_count": flat,
        "falling_count": falling,
        "rising_ratio_pct": round(rising / len(valid) * 100.0, 4) if len(valid) else None,
        "mean_return_pct": round(float(valid["return_pct"].mean()), 4) if len(valid) else None,
        "median_return_pct": round(float(valid["return_pct"].median()), 4) if len(valid) else None,
        "by_market": by_market,
        "condition": {
            "price": [args.price_min, args.price_max],
            "market_cap_min": args.market_cap_min,
            "market_cap_max": args.market_cap_max,
            "daily_return": [args.daily_return_min, args.daily_return_max],
            "trading_value": [args.trading_value_min, args.trading_value_max],
            "ma_order": [args.ma_long, args.ma_mid, args.ma_short],
            "ma_relation": "<",
            "envelope_period": args.envelope_period,
            "envelope_percent": args.envelope_percent,
            "envelope_relation": "close<=lower_band",
            "orderbook_ratio": "NOT_AVAILABLE_IN_HISTORICAL_MARCAP_DATA",
        },
        "return_definition": "signal-day close to close after N subsequent trading days",
    }

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = pd.Timestamp(args.date).strftime("%Y%m%d")
    csv_path = out / f"calibration_{stem}_{args.hold_days}d.csv"
    json_path = out / f"calibration_{stem}_{args.hold_days}d_summary.json"
    results.to_csv(csv_path, index=False, encoding="utf-8-sig")
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print("=== HTS condition calibration ===")
    print(f"signal date: {args.date}")
    print(f"hold: {args.hold_days} subsequent trading days")
    print(f"candidates: {len(results)}")
    print(f"completed returns: {len(valid)}")
    print(f"rising / flat / falling: {rising} / {flat} / {falling}")
    if len(valid):
        print(f"mean return: {summary['mean_return_pct']:+.4f}%")
        print(f"median return: {summary['median_return_pct']:+.4f}%")
        for market, stats in by_market.items():
            print(f"{market or 'UNKNOWN'}: {stats['count']} stocks / mean {stats['mean_return_pct']:+.4f}%")
    print("order-book ratio condition: omitted (historical data unavailable)")
    print(f"saved: {csv_path}")
    print(f"saved: {json_path}")


if __name__ == "__main__":
    main()
