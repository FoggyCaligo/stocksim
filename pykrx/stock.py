from __future__ import annotations

from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

_BASE_URL = "https://raw.githubusercontent.com/FinanceData/marcap/master/data"
_CACHE_DIR = Path(".cache/stocksim/marcap")
_YEAR_CACHE: dict[int, pd.DataFrame] = {}


def _download_year(year: int) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"marcap-{year}.parquet"
    if path.exists() and path.stat().st_size > 0:
        return path

    url = f"{_BASE_URL}/marcap-{year}.parquet"
    print(f"marcap {year} 데이터 다운로드 중...")
    req = Request(url, headers={"User-Agent": "stocksim/1.0"})
    try:
        with urlopen(req, timeout=120) as response, path.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.index.name == "Date" and "Date" not in frame.columns:
        frame = frame.reset_index()
    elif "Date" not in frame.columns:
        frame = frame.reset_index()
        if "Date" not in frame.columns:
            frame = frame.rename(columns={frame.columns[0]: "Date"})

    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.normalize()
    frame["Code"] = frame["Code"].astype(str).str.zfill(6)

    # FinanceData/marcap has historically used raw KRW for Marcap, while some
    # documentation labels the field as million KRW. Detect the latter safely.
    if "Marcap" in frame.columns and not frame.empty:
        max_marcap = pd.to_numeric(frame["Marcap"], errors="coerce").max()
        if pd.notna(max_marcap) and max_marcap < 10_000_000_000:
            frame["Marcap"] = pd.to_numeric(frame["Marcap"], errors="coerce") * 1_000_000

    return frame


def _load_year(year: int) -> pd.DataFrame:
    if year not in _YEAR_CACHE:
        path = _download_year(year)
        try:
            frame = pd.read_parquet(path)
        except ImportError as exc:
            raise RuntimeError(
                "Parquet 데이터를 읽으려면 pyarrow가 필요합니다. "
                "`pip install -r requirements.txt`를 다시 실행해 주세요."
            ) from exc
        _YEAR_CACHE[year] = _normalize(frame)
    return _YEAR_CACHE[year]


def _slice_years(start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    parts: list[pd.DataFrame] = []
    for year in range(start_ts.year, end_ts.year + 1):
        frame = _load_year(year)
        part = frame[(frame["Date"] >= start_ts) & (frame["Date"] <= end_ts)]
        if not part.empty:
            parts.append(part)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def _market_filter(frame: pd.DataFrame, market: str) -> pd.DataFrame:
    if frame.empty or market.upper() == "ALL" or "Market" not in frame.columns:
        return frame
    return frame[frame["Market"].astype(str).str.upper() == market.upper()]


def _ratio_column(frame: pd.DataFrame) -> pd.Series:
    for name in ("ChagesRatio", "ChangesRatio", "ChangeRatio"):
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce").fillna(0.0)

    changes = pd.to_numeric(frame.get("Changes", 0), errors="coerce").fillna(0.0)
    close = pd.to_numeric(frame["Close"], errors="coerce").fillna(0.0)
    prev_close = close - changes
    return (changes / prev_close.where(prev_close != 0) * 100).fillna(0.0)


def get_market_ohlcv_by_date(
    fromdate: str,
    todate: str,
    ticker: str,
    adjusted: bool = True,
) -> pd.DataFrame:
    del adjusted
    frame = _slice_years(fromdate, todate)
    if frame.empty:
        return pd.DataFrame(columns=["시가", "고가", "저가", "종가", "거래량", "거래대금", "등락률"])

    frame = frame[frame["Code"] == str(ticker).zfill(6)].copy()
    if frame.empty:
        return pd.DataFrame(columns=["시가", "고가", "저가", "종가", "거래량", "거래대금", "등락률"])

    frame["등락률"] = _ratio_column(frame)
    out = frame.rename(
        columns={
            "Open": "시가",
            "High": "고가",
            "Low": "저가",
            "Close": "종가",
            "Volume": "거래량",
            "Amount": "거래대금",
        }
    )
    return out.set_index("Date")[["시가", "고가", "저가", "종가", "거래량", "거래대금", "등락률"]].sort_index()


def get_market_ohlcv_by_ticker(
    date: str,
    market: str = "KOSPI",
    alternative: bool = False,
) -> pd.DataFrame:
    del alternative
    day = pd.Timestamp(date).normalize()
    frame = _load_year(day.year)
    frame = frame[frame["Date"] == day].copy()
    frame = _market_filter(frame, market)
    if frame.empty:
        return pd.DataFrame(columns=["시가", "고가", "저가", "종가", "거래량", "거래대금", "등락률"])

    frame["등락률"] = _ratio_column(frame)
    out = frame.rename(
        columns={
            "Open": "시가",
            "High": "고가",
            "Low": "저가",
            "Close": "종가",
            "Volume": "거래량",
            "Amount": "거래대금",
        }
    )
    return out.set_index("Code")[["시가", "고가", "저가", "종가", "거래량", "거래대금", "등락률"]]


def get_market_cap_by_ticker(date: str, market: str = "KOSPI") -> pd.DataFrame:
    day = pd.Timestamp(date).normalize()
    frame = _load_year(day.year)
    frame = frame[frame["Date"] == day].copy()
    frame = _market_filter(frame, market)
    if frame.empty:
        return pd.DataFrame(columns=["시가총액"])

    frame["시가총액"] = pd.to_numeric(frame["Marcap"], errors="coerce").fillna(0)
    return frame.set_index("Code")[["시가총액"]]
