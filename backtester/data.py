"""Market data: Binance public REST API with a local CSV cache, plus fast per-day access to
intraday bars for the execution model."""
import os
import time
import numpy as np
import pandas as pd
import requests

from . import config as C

INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
               "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000}
BASE_URLS = ["https://api.binance.com", "https://data-api.binance.vision"]


def _to_ms(ts: pd.Timestamp) -> int:
    return int((ts - pd.Timestamp("1970-01-01")) / pd.Timedelta(milliseconds=1))


def _get_json(path: str, params: dict, tries: int = 6):
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(BASE_URLS[attempt % len(BASE_URLS)] + path, params=params, timeout=15)
            if r.status_code in (418, 429):                      # rate limited
                time.sleep(int(r.headers.get("Retry-After", 10)))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"Cannot reach Binance market data ({last}).")


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Download candles from Binance, 1,000 per request."""
    rows, cursor, n = [], start_ms, 0
    while cursor < end_ms:
        batch = _get_json("/api/v3/klines", {"symbol": symbol, "interval": interval,
                                             "startTime": cursor, "endTime": end_ms, "limit": 1000})
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + 1
        n += 1
        if n % 25 == 0 or len(batch) < 1000:
            print(f"  {symbol} {interval}: {len(rows):,} bars, reached {pd.to_datetime(batch[-1][0], unit='ms')}")
        if len(batch) < 1000:
            break
        time.sleep(0.1)
    now_ms = int(time.time() * 1000)
    rows = [r for r in rows if r[6] < now_ms]                    # drop the still-forming candle
    df = pd.DataFrame([r[:6] for r in rows], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp").astype(float)


def load_data(symbol: str, interval: str, start, end=None) -> pd.DataFrame:
    """Candles from the local cache, downloading only what is missing. If Binance is unreachable
    but a cache exists, the cached data is used with a warning."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) if end else None
    step, now_ms = INTERVAL_MS[interval], int(time.time() * 1000)
    target_end_ms = _to_ms(end_ts) if end_ts is not None else now_ms

    path = os.path.join(C.CACHE_DIR, f"{symbol}_{interval}.csv")
    cached = pd.read_csv(path, index_col=0, parse_dates=True) if os.path.exists(path) else None
    need = []
    if cached is None or cached.empty:
        need.append((_to_ms(start_ts), target_end_ms))
    else:
        first_ms, last_ms = _to_ms(cached.index[0]), _to_ms(cached.index[-1])
        if _to_ms(start_ts) < first_ms - step:
            need.append((_to_ms(start_ts), first_ms - 1))
        if last_ms + step < target_end_ms - step:
            need.append((last_ms + 1, target_end_ms))

    parts = [cached] if cached is not None and len(cached) else []
    for a, b in need:
        try:
            got = fetch_klines(symbol, interval, a, b)
            if len(got):
                parts.append(got)
        except RuntimeError as e:
            if not parts:
                raise
            print(f"WARNING: {e} Using cached {interval} data only.")
    if not parts:
        raise RuntimeError(f"No {interval} data available.")
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if need:
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        df.to_csv(path)
    df = df[df.index >= start_ts]
    if end_ts is not None:
        df = df[df.index < end_ts]
    return df


class IntradayBook:
    """Intraday bars grouped by UTC day for fast lookup. Days without intraday data fall back to
    the daily bar itself (one bar = worst-case intrabar ordering)."""

    def __init__(self, intraday: pd.DataFrame | None, bar_minutes: int):
        self.bar = pd.Timedelta(minutes=bar_minutes)
        self.days = {}
        if intraday is None or intraday.empty:
            return
        t = intraday.index.values
        day = intraday.index.normalize().values
        o, h, l, c = (intraday[k].to_numpy() for k in ("open", "high", "low", "close"))
        cuts = np.flatnonzero(day[1:] != day[:-1]) + 1
        starts = np.r_[0, cuts]
        ends = np.r_[cuts, len(day)]
        for s, e in zip(starts, ends):
            if e - s >= 200:                       # skip days with large gaps in the intraday data
                self.days[pd.Timestamp(day[s])] = (t[s:e], o[s:e], h[s:e], l[s:e], c[s:e])

    def coverage(self, days: pd.DatetimeIndex) -> float:
        return float(np.mean([d in self.days for d in days])) * 100 if len(days) else 0.0

    def get(self, day: pd.Timestamp, daily_row) -> tuple:
        """Returns (times, open, high, low, close, is_intraday) for one UTC day."""
        if day in self.days:
            return (*self.days[day], True)
        one = lambda v: np.array([v], dtype=float)
        return (np.array([np.datetime64(day)]), one(daily_row[0]), one(daily_row[1]),
                one(daily_row[2]), one(daily_row[3]), False)


def load_all(use_intraday: bool = True):
    """Daily bars (with warm-up history) and the intraday book used for execution."""
    start = pd.Timestamp(C.START_DATE)
    daily = load_data(C.SYMBOL, C.INTERVAL, start - pd.Timedelta(days=C.WARMUP_DAYS), C.END_DATE)
    print(f"{C.SYMBOL} {C.INTERVAL}: {len(daily):,} bars, {daily.index[0].date()} -> {daily.index[-1].date()}")
    intraday = None
    if use_intraday:
        try:
            intraday = load_data(C.SYMBOL, C.INTRADAY_INTERVAL, start, C.END_DATE)
            print(f"{C.SYMBOL} {C.INTRADAY_INTERVAL}: {len(intraday):,} bars, "
                  f"{intraday.index[0].date()} -> {intraday.index[-1].date()}")
        except RuntimeError as e:
            print(f"WARNING: no intraday data ({e}). Falling back to daily-bar fills.")
    book = IntradayBook(intraday, INTERVAL_MS[C.INTRADAY_INTERVAL] // 60_000)
    trade_days = daily.index[daily.index >= start]
    print(f"Intraday execution coverage: {book.coverage(trade_days):.1f}% of trading days")
    return daily, book
