from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from pykrx import stock


@dataclass
class Config:
    start: str
    end: str
    daily_buy_count: int = 5
    price_min: int = 0
    price_max: int = 100_000
    market_cap_min: int = 300_000_000_000
    market_cap_max: int | None = None
    daily_return_min: float = -7.0
    daily_return_max: float = -1.0
    trading_value_min: int = 300_000_000
    trading_value_max: int = 100_000_000_000
    short_ma: int = 20
    long_ma: int = 60
    take_profit: float = 0.05
    early_stop_window: int = 4
    consecutive_down_days: int = 2
    max_hold_days: int = 7
    down_bar_mode: str = "close_to_close"
    initial_capital: int = 100_000_000
    position_size: int = 1_000_000
    allow_reentry: bool = True
    seed: int = 42
    commission_rate: float = 0.0
    sell_tax_rate: float = 0.0
    slippage_bps: float = 0.0
    markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")
    cache_dir: str = ".cache/stocksim"
    output_dir: str = "results"


@dataclass
class Position:
    id: int
    ticker: str
    signal_date: str
    entry_date: str
    entry_price: float
    quantity: int
    invested: float
    prev_close: float
    consecutive_down: int = 0
    holding_days: int = 0


class PykrxProvider:
    def __init__(self, config: Config):
        self.config = config
        self.cache_dir = Path(config.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _fmt(d: pd.Timestamp | datetime | date | str) -> str:
        return pd.Timestamp(d).strftime("%Y%m%d")

    def trading_dates(self, start: str | pd.Timestamp, end: str | pd.Timestamp) -> list[pd.Timestamp]:
        # Samsung Electronics is used only as a stable Korean-market trading-calendar proxy.
        frame = stock.get_market_ohlcv_by_date(self._fmt(start), self._fmt(end), "005930")
        return [pd.Timestamp(x) for x in frame.index]

    def day(self, d: pd.Timestamp) -> pd.DataFrame:
        key = d.strftime("%Y%m%d")
        cache_file = self.cache_dir / f"{key}.pkl"
        if cache_file.exists():
            return pd.read_pickle(cache_file)

        parts: list[pd.DataFrame] = []
        for market in self.config.markets:
            ohlcv = stock.get_market_ohlcv_by_ticker(key, market=market)
            if ohlcv.empty:
                continue
            caps = stock.get_market_cap_by_ticker(key, market=market)
            if "시가총액" in caps.columns:
                frame = ohlcv.join(caps[["시가총액"]], how="left")
            else:
                frame = ohlcv.copy()
                frame["시가총액"] = 0
            frame = frame.reset_index().rename(
                columns={
                    "티커": "ticker",
                    "시가": "open",
                    "고가": "high",
                    "저가": "low",
                    "종가": "close",
                    "거래량": "volume",
                    "거래대금": "trading_value",
                    "등락률": "daily_return",
                    "시가총액": "market_cap",
                }
            )
            if "ticker" not in frame.columns:
                frame = frame.rename(columns={frame.columns[0]: "ticker"})
            frame["market"] = market
            parts.append(frame)

        if not parts:
            return pd.DataFrame(
                columns=[
                    "ticker", "open", "high", "low", "close", "volume",
                    "trading_value", "daily_return", "market_cap", "market",
                ]
            )

        out = pd.concat(parts, ignore_index=True)
        out["ticker"] = out["ticker"].astype(str).str.zfill(6)
        out.to_pickle(cache_file)
        return out


def validate(config: Config) -> None:
    if pd.Timestamp(config.start) > pd.Timestamp(config.end):
        raise ValueError("start must be on or before end")
    if config.daily_buy_count < 1:
        raise ValueError("daily_buy_count must be >= 1")
    if config.short_ma < 1 or config.long_ma < 2 or config.short_ma >= config.long_ma:
        raise ValueError("moving averages must satisfy 1 <= short_ma < long_ma")
    if config.max_hold_days < 1:
        raise ValueError("max_hold_days must be >= 1")
    if not 1 <= config.early_stop_window <= config.max_hold_days:
        raise ValueError("early_stop_window must be between 1 and max_hold_days")
    if config.consecutive_down_days < 1:
        raise ValueError("consecutive_down_days must be >= 1")
    if config.take_profit <= 0:
        raise ValueError("take_profit must be > 0")
    if config.position_size <= 0 or config.initial_capital <= 0:
        raise ValueError("capital values must be > 0")
    if config.down_bar_mode not in {"close_to_close", "red_candle"}:
        raise ValueError("down_bar_mode must be close_to_close or red_candle")


def screen(frame: pd.DataFrame, histories: dict[str, deque[float]], config: Config) -> pd.DataFrame:
    """Approximate the MTS condition set, intentionally excluding order-book balance ratio."""
    if frame.empty:
        return frame.copy()

    f = frame.copy()
    f = f[(f["close"] >= config.price_min) & (f["close"] <= config.price_max)]
    f = f[f["market_cap"] >= config.market_cap_min]
    if config.market_cap_max is not None:
        f = f[f["market_cap"] <= config.market_cap_max]
    f = f[(f["daily_return"] >= config.daily_return_min) & (f["daily_return"] <= config.daily_return_max)]
    f = f[(f["trading_value"] >= config.trading_value_min) & (f["trading_value"] <= config.trading_value_max)]

    def ma_ok(ticker: str) -> bool:
        values = histories.get(ticker)
        if values is None or len(values) < config.long_ma:
            return False
        seq = list(values)
        short = sum(seq[-config.short_ma:]) / config.short_ma
        long = sum(seq[-config.long_ma:]) / config.long_ma
        return short > long

    return f[f["ticker"].map(ma_ok)].copy()


def buy_price(raw_price: float, config: Config) -> float:
    return raw_price * (1.0 + config.slippage_bps / 10_000.0)


def net_sell(gross: float, config: Config) -> float:
    fees = gross * (config.commission_rate + config.sell_tax_rate)
    slip = gross * config.slippage_bps / 10_000.0
    return gross - fees - slip


def run(config: Config) -> dict:
    validate(config)
    provider = PykrxProvider(config)
    rng = random.Random(config.seed)

    start = pd.Timestamp(config.start).normalize()
    end = pd.Timestamp(config.end).normalize()
    warmup_start = start - pd.Timedelta(days=max(120, config.long_ma * 3))
    future_end = end + pd.Timedelta(days=max(30, config.max_hold_days * 3))

    dates = provider.trading_dates(warmup_start, future_end)
    if not dates:
        raise RuntimeError("No trading dates returned by pykrx.")

    histories: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=config.long_ma))
    cash = float(config.initial_capital)
    positions: list[Position] = []
    pending: dict[pd.Timestamp, list[tuple[str, str, float]]] = defaultdict(list)
    trades: list[dict] = []
    signals: list[dict] = []
    equity_rows: list[dict] = []
    next_id = 1

    signal_dates = [d for d in dates if start <= d <= end]
    if not signal_dates:
        raise RuntimeError("The requested period contains no trading days.")
    last_signal_date = signal_dates[-1]

    for i, d in enumerate(dates):
        frame = provider.day(d)
        if frame.empty:
            continue
        by_ticker = frame.set_index("ticker", drop=False)

        # Today's close is included in today's 20/60-day moving averages.
        for row in frame.itertuples(index=False):
            if pd.notna(row.close) and row.close > 0:
                histories[row.ticker].append(float(row.close))

        # The condition is evaluated after day D closes; purchases execute at D+1 open.
        if d in pending:
            for ticker, signal_date, signal_close in pending.pop(d):
                if ticker not in by_ticker.index:
                    continue
                if (not config.allow_reentry) and any(p.ticker == ticker for p in positions):
                    continue
                row = by_ticker.loc[ticker]
                raw = float(row["open"])
                if raw <= 0:
                    continue
                entry = buy_price(raw, config)
                qty = int(min(config.position_size, cash) // (entry * (1.0 + config.commission_rate)))
                if qty < 1:
                    continue
                gross = qty * entry
                buy_fee = gross * config.commission_rate
                invested = gross + buy_fee
                cash -= invested
                positions.append(
                    Position(
                        id=next_id,
                        ticker=ticker,
                        signal_date=signal_date,
                        entry_date=d.strftime("%Y-%m-%d"),
                        entry_price=entry,
                        quantity=qty,
                        invested=invested,
                        prev_close=float(signal_close),
                    )
                )
                next_id += 1

        survivors: list[Position] = []
        for p in positions:
            if p.ticker not in by_ticker.index:
                survivors.append(p)
                continue

            row = by_ticker.loc[p.ticker]
            current_close = float(row["close"])
            current_high = float(row["high"])
            current_open = float(row["open"])
            p.holding_days += 1

            if config.down_bar_mode == "close_to_close":
                is_down = current_close < p.prev_close
            else:
                is_down = current_close < current_open
            p.consecutive_down = p.consecutive_down + 1 if is_down else 0
            p.prev_close = current_close

            target = p.entry_price * (1.0 + config.take_profit)
            exit_reason = None
            exit_price = None

            # A resting +N% target order is assumed to fill if the daily high touches it.
            if p.holding_days <= config.max_hold_days and current_high >= target:
                exit_reason = "take_profit"
                exit_price = target
            elif (
                p.holding_days <= config.early_stop_window
                and p.consecutive_down >= config.consecutive_down_days
            ):
                exit_reason = "consecutive_down_stop"
                exit_price = current_close
            elif p.holding_days >= config.max_hold_days:
                exit_reason = "max_hold_exit"
                exit_price = current_close

            if exit_reason is None:
                survivors.append(p)
                continue

            gross_sale = p.quantity * float(exit_price)
            proceeds = net_sell(gross_sale, config)
            cash += proceeds
            pnl = proceeds - p.invested
            ret = pnl / p.invested if p.invested else 0.0
            trades.append(
                {
                    "position_id": p.id,
                    "ticker": p.ticker,
                    "signal_date": p.signal_date,
                    "entry_date": p.entry_date,
                    "exit_date": d.strftime("%Y-%m-%d"),
                    "entry_price": round(p.entry_price, 4),
                    "exit_price": round(float(exit_price), 4),
                    "quantity": p.quantity,
                    "holding_days": p.holding_days,
                    "exit_reason": exit_reason,
                    "pnl_krw": round(pnl, 2),
                    "return_pct": round(ret * 100.0, 4),
                }
            )
        positions = survivors

        # Randomly choose up to N names from today's matching universe for tomorrow's open.
        if start <= d <= end:
            eligible = screen(frame, histories, config)
            candidates = eligible["ticker"].tolist()
            if not config.allow_reentry:
                active = {p.ticker for p in positions}
                candidates = [ticker for ticker in candidates if ticker not in active]
            rng.shuffle(candidates)
            selected = candidates[: config.daily_buy_count]
            signals.append(
                {
                    "signal_date": d.strftime("%Y-%m-%d"),
                    "eligible_count": len(candidates),
                    "selected": ",".join(selected),
                }
            )
            if i + 1 < len(dates):
                next_date = dates[i + 1]
                for ticker in selected:
                    signal_close = float(by_ticker.loc[ticker]["close"])
                    pending[next_date].append((ticker, d.strftime("%Y-%m-%d"), signal_close))

        market_value = 0.0
        for p in positions:
            if p.ticker in by_ticker.index:
                market_value += p.quantity * float(by_ticker.loc[p.ticker]["close"])
            else:
                market_value += p.invested
        equity = cash + market_value
        equity_rows.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "cash": round(cash, 2),
                "market_value": round(market_value, 2),
                "equity": round(equity, 2),
                "open_positions": len(positions),
            }
        )

        if d > last_signal_date and not positions and not pending:
            break

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_rows)
    signals_df = pd.DataFrame(signals)

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    signals_df.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")

    if not equity_df.empty:
        equity_df["daily_return_pct"] = equity_df["equity"].pct_change().fillna(0.0) * 100.0
        equity_df["cumulative_return_pct"] = (
            equity_df["equity"] / float(config.initial_capital) - 1.0
        ) * 100.0
    equity_df.to_csv(output_dir / "daily_equity.csv", index=False, encoding="utf-8-sig")

    winners = int((trades_df["pnl_krw"] > 0).sum()) if not trades_df.empty else 0
    final_equity = float(equity_df.iloc[-1]["equity"]) if not equity_df.empty else float(config.initial_capital)
    summary = {
        "config": asdict(config),
        "trade_count": int(len(trades_df)),
        "win_rate_pct": round(winners / len(trades_df) * 100.0, 4) if len(trades_df) else 0.0,
        "total_realized_pnl_krw": round(float(trades_df["pnl_krw"].sum()), 2) if not trades_df.empty else 0.0,
        "final_equity_krw": round(final_equity, 2),
        "total_return_pct": round((final_equity / config.initial_capital - 1.0) * 100.0, 4),
        "exit_counts": trades_df["exit_reason"].value_counts().to_dict() if not trades_df.empty else {},
    }
    with (output_dir / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Configurable Korean-stock swing backtester")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
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
    p.add_argument("--take-profit", type=float, default=0.05, help="0.05 = +5%%")
    p.add_argument("--early-stop-window", type=int, default=4)
    p.add_argument("--consecutive-down-days", type=int, default=2)
    p.add_argument("--max-hold-days", type=int, default=7)
    p.add_argument("--down-bar-mode", choices=["close_to_close", "red_candle"], default="close_to_close")
    p.add_argument("--initial-capital", type=int, default=100_000_000)
    p.add_argument("--position-size", type=int, default=1_000_000)
    p.add_argument("--no-reentry", action="store_true", help="Do not buy a ticker already held.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--commission-rate", type=float, default=0.0)
    p.add_argument("--sell-tax-rate", type=float, default=0.0)
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.add_argument("--markets", default="KOSPI,KOSDAQ")
    p.add_argument("--cache-dir", default=".cache/stocksim")
    p.add_argument("--output-dir", default="results")
    return p


def main() -> None:
    args = build_parser().parse_args()
    config = Config(
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
        seed=args.seed,
        commission_rate=args.commission_rate,
        sell_tax_rate=args.sell_tax_rate,
        slippage_bps=args.slippage_bps,
        markets=tuple(x.strip().upper() for x in args.markets.split(",") if x.strip()),
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
    )
    summary = run(config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
