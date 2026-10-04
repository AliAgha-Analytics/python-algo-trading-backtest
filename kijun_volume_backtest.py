"""
Kijun-sen + Normalized Volume split-position strategy - crypto backtest
=======================================================================

Python port of a MetaTrader 5 Expert Advisor (SplitTP_Kijun_NormVol_EA.mq5), backtested on
real Binance spot daily OHLCV data.

STRATEGY RULES
--------------
* Baseline: Kijun-sen(KIJUN_PERIOD) = (highest high + lowest low) / 2 over the last N closed bars.
* Confirmation: Normalized Volume = volume / SMA(volume, VOLNORM_PERIOD) * 100.
  Volume is "green" when Normalized Volume > VOLNORM_THRESHOLD (above its own recent average).
* PRIMARY entry: price CROSSES Kijun-sen on the last closed bar (previous close on the other side),
  the candle closes in the direction of the cross (bullish for longs, bearish for shorts) AND volume
  is green on that bar.
* SECONDARY entry ("vol-refresh"): no fresh cross, price is still on one side of Kijun-sen and
  Normalized Volume makes a fresh red -> green transition. Enters in the direction of that side.
* Every entry opens TWO equal legs (risk-based sizing, split evenly, each leg rounded up to QTY_STEP).
  - Leg 1: SL = SL_ATR_MULT x ATR, TP = TP1_ATR_MULT x ATR.
  - Leg 2: SL = SL_ATR_MULT x ATR, no TP. Trails at TRAIL_ATR_MULT x ATR from the previous close,
    updated once per new bar (starting the bar after entry) and only ever moved in the trade's favour.
* When leg 1 hits its TP, leg 2's stop moves to breakeven (entry price) if that is an improvement.
* If leg 2 is stopped out while leg 1 is still open, leg 1 is force-closed at the same price, so a
  cycle always resolves as one event.
* Reverse signal: if price closes on the opposite side of Kijun-sen while a cycle is open, both legs
  are closed at the next open. On that same bar the primary entry rule is re-checked and, if it
  qualifies, a new cycle opens immediately in the opposite direction.

CRYPTO-SPECIFIC DIFFERENCES VS THE MT5 EA
-----------------------------------------
* Position sizes are in units of the base asset (e.g. BTC), so CONTRACT_SIZE = 1.
* Exchange fees (FEE_PCT) are charged on entry AND exit of every leg.
* No hedging/netting account restrictions: the two legs are tracked independently.
* No execution delay parameter: with daily bars there is no intraday data to fill a delayed order.

FILL MODEL / ASSUMPTIONS
------------------------
* Signals are evaluated on the LAST CLOSED bar and executed at the NEXT bar's open (no look-ahead).
* Within a bar: entries first, then stop-loss checks (adverse extreme), then take-profit checks
  (favourable extreme). This assumes the adverse extreme is reached first - a conservative choice.
* SL/TP/trailing fills happen at the level itself, or at the bar's open if price gapped through it.

USAGE
-----
    pip install -r requirements.txt
    python kijun_volume_backtest.py

Outputs: KPI report in the console, cycles_*.csv / events_*.csv trade logs, an interactive
chart_*.html, and a static results_*.png used in the README.
"""

import os
import time
import numpy as np
import pandas as pd
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ------------------------------ CONFIG ---------------------------------
SYMBOL = "BTCUSDT"             # any Binance spot symbol, e.g. "ETHUSDT", "SOLUSDT"
INTERVAL = "1d"                # 1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M
START_DATE = "1 Jan 2020"
END_DATE = "3 Oct 2026"        # fixed so the README results are reproducible; None = up to the latest bar
PLOT_MAX_BARS = 30_000
USE_CACHE = True
CACHE_DIR = "data_cache"

START_CASH = 100_000.0
QUOTE_IS_ACCOUNT_CCY = True    # True for USDT/USDC-quoted pairs. False needs a conversion series (usd_rate)
CONTRACT_SIZE = 1.0            # crypto: 1 unit = 1 unit of the base asset
QTY_STEP = 0.0001              # rounding step for each leg, in units of the base asset
FEE_PCT = 0.001                # 0.10% Binance spot taker fee, charged per leg on entry and on exit

ATR_PERIOD = 14
KIJUN_PERIOD = 14
VOLNORM_PERIOD = 14
VOLNORM_THRESHOLD = 100.0      # volume is "green" when Normalized Volume > this

RISK_PCT = 3.0                 # % of equity risked per cycle (both legs combined) at the initial SL
SL_ATR_MULT = 1.5
TP1_ATR_MULT = 1.0
TRAIL_ATR_MULT = 1.5

SAVE_PNG = True                # static chart for the README (needs matplotlib)
OPEN_BROWSER = True            # open the interactive HTML chart when done


# ------------------------------- DATA ----------------------------------
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
               "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
               "1M": 2_592_000_000}
BASE_URLS = ["https://api.binance.com", "https://data-api.binance.vision"]


def _to_ms(ts: pd.Timestamp) -> int:
    return int((ts - pd.Timestamp("1970-01-01")) / pd.Timedelta(milliseconds=1))


def _get_json(path: str, params: dict, tries: int = 6):
    last = None
    for attempt in range(tries):
        base = BASE_URLS[attempt % len(BASE_URLS)]
        try:
            r = requests.get(base + path, params=params, timeout=15)
            if r.status_code in (418, 429):          # rate limited
                time.sleep(int(r.headers.get("Retry-After", 10)))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"Cannot reach Binance market data ({last}).")


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Download OHLCV candles from Binance's public REST API, 1,000 bars per request."""
    rows, cursor = [], start_ms
    while cursor < end_ms:
        batch = _get_json("/api/v3/klines", {"symbol": symbol, "interval": interval,
                                             "startTime": cursor, "endTime": end_ms, "limit": 1000})
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + 1
        print(f"  {len(rows):>9,} bars | reached {pd.to_datetime(batch[-1][0], unit='ms')}")
        if len(batch) < 1000:
            break
        time.sleep(0.12)
    now_ms = int(time.time() * 1000)
    rows = [r for r in rows if r[6] < now_ms]        # drop the still-forming candle
    df = pd.DataFrame([r[:6] for r in rows], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.set_index("timestamp").astype(float)


def load_data(symbol=None, interval=None, start=None, end=None) -> pd.DataFrame:
    """Load candles from the local CSV cache, downloading only the missing range from Binance.
    If Binance is unreachable but a cache exists, the cached data is used with a warning."""
    symbol, interval = symbol or SYMBOL, interval or INTERVAL
    if interval not in INTERVAL_MS:
        raise ValueError(f"INTERVAL '{interval}' not supported. Use one of {list(INTERVAL_MS)}")
    start_ts = pd.Timestamp(start or START_DATE)
    end = end or END_DATE
    start_ms, now_ms, step = _to_ms(start_ts), int(time.time() * 1000), INTERVAL_MS[interval]

    cache_path = os.path.join(CACHE_DIR, f"{symbol}_{interval}.csv")
    cached = None
    if USE_CACHE and os.path.exists(cache_path):
        cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        if len(cached):
            print(f"Cache: {len(cached):,} bars ({cached.index[0].date()} -> {cached.index[-1].date()})")

    need = []
    if cached is None or cached.empty:
        need.append((start_ms, now_ms))
    else:
        first_ms, last_ms = _to_ms(cached.index[0]), _to_ms(cached.index[-1])
        if start_ms < first_ms - step:
            need.append((start_ms, first_ms - 1))
        if end is None:
            need.append((last_ms + 1, now_ms))

    parts = [cached] if cached is not None and len(cached) else []
    for a_ms, b_ms in need:
        try:
            got = fetch_klines(symbol, interval, a_ms, b_ms)
            if len(got):
                parts.append(got)
        except RuntimeError as e:
            if not parts:
                raise
            print(f"WARNING: {e} Using cached data only.")
    if not parts:
        raise RuntimeError("No data returned - check the symbol / interval / start date.")

    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if USE_CACHE:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(cache_path)
    df = df[df.index >= start_ts]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    if df.empty:
        raise RuntimeError("No candles in the selected date range.")
    print(f"{symbol} {interval}: {len(df):,} bars, {df.index[0].date()} -> {df.index[-1].date()}")
    return df


def usd_rate(at_date) -> float:
    """Value of 1 unit of the quote asset in account currency. 1.0 for stablecoin-quoted pairs."""
    if QUOTE_IS_ACCOUNT_CCY:
        return 1.0
    raise NotImplementedError("Cross pairs (e.g. ETHBTC) need a conversion series plugged in here.")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()   # Wilder's ATR
    df["kijun"] = (df["high"].rolling(KIJUN_PERIOD).max() + df["low"].rolling(KIJUN_PERIOD).min()) / 2.0
    vol_ma = df["volume"].rolling(VOLNORM_PERIOD).mean()
    df["vol_norm"] = df["volume"] / vol_ma * 100.0
    df["vol_green"] = df["vol_norm"] > VOLNORM_THRESHOLD
    return df


# ----------------------------- BACKTEST --------------------------------
def round_up(x: float, step: float) -> float:
    return x if step <= 0 else round(float(np.ceil(round(x / step, 9)) * step), 10)


class Cycle:
    """One trade 'cycle' = two equal legs opened together at the same price."""

    def __init__(self, direction, entry_price, qty_leg, opened_date, opened_i, atr_v):
        self.dir = direction                                      # +1 long, -1 short
        self.entry = entry_price
        self.qty1 = self.qty2 = qty_leg
        self.leg1_open = self.leg2_open = True
        self.sl = entry_price - direction * SL_ATR_MULT * atr_v   # leg 1 stop
        self.tp1 = entry_price + direction * TP1_ATR_MULT * atr_v # leg 1 target
        self.sl2 = self.sl                                        # leg 2 (trailing) stop
        self.breakeven = False
        self.opened_date = opened_date
        self.opened_i = opened_i
        self.trail_armed_i = None
        self.fees = 0.0
        self.gross_pnl = 0.0
        self.equity_at_open = None


def backtest(df: pd.DataFrame):
    df = add_indicators(df)
    idx = df.index
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr, kijun, vol_green = df["atr"].to_numpy(), df["kijun"].to_numpy(), df["vol_green"].to_numpy()

    state = {"cash": float(START_CASH)}
    cyc = None                   # the open Cycle, or None when flat
    prev_side = 0                # +1 above Kijun, -1 below, 0 unknown
    prev_vol_green = False
    block_i = -1                 # a cycle closed on bar i cannot re-open on that same bar (except reversals)

    events, closed = [], []
    equity = np.full(len(df), np.nan)
    equity[0] = state["cash"]

    def mark_to_market(price):
        if cyc is None:
            return state["cash"]
        pnl = 0.0
        if cyc.leg1_open:
            pnl += cyc.dir * (price - cyc.entry) * cyc.qty1
        if cyc.leg2_open:
            pnl += cyc.dir * (price - cyc.entry) * cyc.qty2
        return state["cash"] + pnl

    def leg_qty(equity_now, atr_v):
        if atr_v <= 0:
            return 0.0
        risk_budget = equity_now * RISK_PCT / 100.0
        sl_dist = SL_ATR_MULT * atr_v
        total = risk_budget / (sl_dist * CONTRACT_SIZE * usd_rate(None))
        return round_up(total / 2.0, QTY_STEP)

    def open_cycle(direction, price, atr_v, i, reason):
        nonlocal cyc
        eq = mark_to_market(price)
        qty = leg_qty(eq, atr_v)
        if qty <= 0:
            events.append(dict(date=idx[i], type="SKIP", reason="leg size rounds to 0"))
            return
        fee = qty * 2 * price * FEE_PCT
        state["cash"] -= fee
        cyc = Cycle(direction, price, qty, idx[i], i, atr_v)
        cyc.equity_at_open = eq
        cyc.fees += fee
        events.append(dict(date=idx[i], type="ENTRY", side=("LONG" if direction == 1 else "SHORT"),
                           price=price, qty=qty * 2, sl=cyc.sl, tp1=cyc.tp1, reason=reason))

    def close_leg(which, price, i, reason):
        qty = cyc.qty1 if which == 1 else cyc.qty2
        pnl = cyc.dir * (price - cyc.entry) * qty
        fee = qty * price * FEE_PCT
        state["cash"] += pnl - fee
        cyc.fees += fee
        cyc.gross_pnl += pnl
        if which == 1:
            cyc.leg1_open = False
        else:
            cyc.leg2_open = False
        events.append(dict(date=idx[i], type=reason, leg=which, price=price, qty=qty, pnl=pnl - fee))

    def cycle_record(end_i, net_pnl, reason):
        return dict(opened=cyc.opened_date, closed=idx[end_i], side=("LONG" if cyc.dir == 1 else "SHORT"),
                    entry=cyc.entry, qty_each=cyc.qty1, fees=cyc.fees, pnl=net_pnl,
                    pnl_pct=(net_pnl / cyc.equity_at_open * 100 if cyc.equity_at_open else np.nan),
                    bars_held=end_i - cyc.opened_i,
                    days_held=(idx[end_i] - cyc.opened_date).total_seconds() / 86400, reason=reason)

    def finish_cycle(i, reason):
        nonlocal cyc, block_i
        closed.append(cycle_record(i, cyc.gross_pnl - cyc.fees, reason))
        cyc = None
        block_i = i

    for i in range(1, len(df)):
        a, k = atr[i - 1], kijun[i - 1]
        if np.isnan(a) or np.isnan(k):
            equity[i] = mark_to_market(c[i])
            continue
        prev_close = c[i - 1]
        cur_side = 1 if prev_close > k else (-1 if prev_close < k else 0)
        crossed = prev_side != 0 and cur_side != 0 and cur_side != prev_side
        vg = bool(vol_green[i - 1])
        vol_just_turned_green = (not prev_vol_green) and vg
        bull = c[i - 1] > o[i - 1]        # shape of the closed signal candle
        bear = c[i - 1] < o[i - 1]

        # ---- entries (only while flat) ----
        if cyc is None and i != block_i:
            fired_dir, reason = 0, ""
            if crossed:
                if cur_side == 1 and bull and vg:
                    fired_dir, reason = 1, "cross"
                elif cur_side == -1 and bear and vg:
                    fired_dir, reason = -1, "cross"
            elif vol_just_turned_green and cur_side != 0:
                fired_dir, reason = cur_side, "vol-refresh"
            if fired_dir != 0:
                open_cycle(fired_dir, o[i], a, i, reason)     # decided on the close, filled at next open

        # ---- manage an open cycle ----
        if cyc is not None:
            d = cyc.dir

            # 1) reverse signal: price closed on the wrong side of Kijun -> close both legs at the open
            if cur_side != 0 and cur_side == -d:
                fill = o[i]
                if cyc.leg2_open:
                    close_leg(2, fill, i, "REVERSE")
                if cyc.leg1_open:
                    close_leg(1, fill, i, "REVERSE")
                finish_cycle(i, "reverse")
                new_dir = 0
                if cur_side == 1 and bull and vg:
                    new_dir = 1
                elif cur_side == -1 and bear and vg:
                    new_dir = -1
                if new_dir != 0:
                    open_cycle(new_dir, fill, a, i, "reverse-reentry")
                equity[i] = mark_to_market(c[i])
                prev_side, prev_vol_green = cur_side, vg
                continue

            # 2) stop-loss checks (adverse extreme first)
            worst = l[i] if d == 1 else h[i]
            if cyc.leg1_open:
                if (worst <= cyc.sl) if d == 1 else (worst >= cyc.sl):
                    gapped = (o[i] <= cyc.sl) if d == 1 else (o[i] >= cyc.sl)
                    close_leg(1, o[i] if gapped else cyc.sl, i, "SL1")
            if cyc.leg2_open:
                if (worst <= cyc.sl2) if d == 1 else (worst >= cyc.sl2):
                    gapped = (o[i] <= cyc.sl2) if d == 1 else (o[i] >= cyc.sl2)
                    fill = o[i] if gapped else cyc.sl2
                    close_leg(2, fill, i, "SL2")
                    if cyc.leg1_open:                     # never leave leg 1 dangling
                        close_leg(1, fill, i, "FORCED_CLOSE")
                    finish_cycle(i, "sl")
                    equity[i] = mark_to_market(c[i])
                    prev_side, prev_vol_green = cur_side, vg
                    continue

            # 3) take-profit for leg 1 -> move leg 2 to breakeven
            if cyc.leg1_open:
                best = h[i] if d == 1 else l[i]
                if (best >= cyc.tp1) if d == 1 else (best <= cyc.tp1):
                    gapped = (o[i] >= cyc.tp1) if d == 1 else (o[i] <= cyc.tp1)
                    close_leg(1, o[i] if gapped else cyc.tp1, i, "TP1")
                    cyc.sl2 = max(cyc.sl2, cyc.entry) if d == 1 else min(cyc.sl2, cyc.entry)
                    cyc.breakeven = True

            # 4) both legs closed
            if not cyc.leg1_open and not cyc.leg2_open:
                finish_cycle(i, "both-closed")

            # 5) trail leg 2 once per new bar, starting the bar after entry
            if cyc is not None and cyc.leg2_open:
                if cyc.trail_armed_i is None:
                    cyc.trail_armed_i = cyc.opened_i
                elif i != cyc.trail_armed_i:
                    cyc.trail_armed_i = i
                    candidate = prev_close - d * TRAIL_ATR_MULT * a
                    cyc.sl2 = max(cyc.sl2, candidate) if d == 1 else min(cyc.sl2, candidate)

        equity[i] = mark_to_market(c[i])
        prev_side, prev_vol_green = cur_side, vg

    if cyc is not None:      # still open at the end of the data -> mark to market at the last close
        unrealized = 0.0
        if cyc.leg1_open:
            unrealized += cyc.dir * (c[-1] - cyc.entry) * cyc.qty1
        if cyc.leg2_open:
            unrealized += cyc.dir * (c[-1] - cyc.entry) * cyc.qty2
        closed.append(cycle_record(len(df) - 1, cyc.gross_pnl + unrealized - cyc.fees, "OPEN (mark-to-market)"))

    eq = pd.Series(equity, index=idx, name="equity").ffill()
    return eq, pd.DataFrame(events), pd.DataFrame(closed), df


# ------------------------------ REPORT ---------------------------------
def compute_kpis(equity: pd.Series, cycles: pd.DataFrame, df: pd.DataFrame) -> dict:
    start_val, end_val = equity.iloc[0], equity.iloc[-1]
    years = (equity.index[-1] - equity.index[0]).total_seconds() / 86400 / 365.25
    dd = (equity / equity.cummax() - 1) * 100
    daily_ret = equity.resample("1D").last().ffill().pct_change().dropna()
    downside = daily_ret[daily_ret < 0]
    max_dd_usd = (equity.cummax() - equity).max()

    bh = df["close"] / df["close"].iloc[0] * start_val          # buy & hold benchmark
    bh_ret = bh.resample("1D").last().ffill().pct_change().dropna()

    k = dict(
        start=start_val, end=end_val, years=years,
        total_return=(end_val / start_val - 1) * 100,
        cagr=((end_val / start_val) ** (1 / years) - 1) * 100 if years > 0 and end_val > 0 else np.nan,
        max_dd=dd.min(), max_dd_usd=max_dd_usd,
        sharpe=daily_ret.mean() / daily_ret.std() * np.sqrt(365) if daily_ret.std() > 0 else np.nan,
        sortino=daily_ret.mean() / downside.std() * np.sqrt(365) if len(downside) > 1 else np.nan,
        recovery=(end_val - start_val) / max_dd_usd if max_dd_usd > 0 else np.nan,
        bh_return=(bh.iloc[-1] / start_val - 1) * 100,
        bh_max_dd=((bh / bh.cummax() - 1) * 100).min(),
        bh_sharpe=bh_ret.mean() / bh_ret.std() * np.sqrt(365),
    )
    k["calmar"] = k["cagr"] / abs(k["max_dd"]) if k["max_dd"] < 0 else np.nan
    k["bh_cagr"] = ((bh.iloc[-1] / start_val) ** (1 / years) - 1) * 100

    if len(cycles):
        wins, losses = cycles[cycles.pnl > 0], cycles[cycles.pnl <= 0]
        avg_loss = losses.pnl.mean() if len(losses) else 0.0

        def streak(flags):
            best = cur = 0
            for f in flags:
                cur = cur + 1 if f else 0
                best = max(best, cur)
            return best

        in_market_days = cycles.days_held.sum()
        k.update(
            n=len(cycles), n_win=len(wins), n_loss=len(losses),
            win_rate=len(wins) / len(cycles) * 100,
            profit_factor=wins.pnl.sum() / -losses.pnl.sum() if losses.pnl.sum() < 0 else np.inf,
            avg_win=wins.pnl.mean() if len(wins) else 0.0, avg_loss=avg_loss,
            payoff=(wins.pnl.mean() / -avg_loss) if avg_loss < 0 else np.inf,
            expectancy=cycles.pnl.mean(), expectancy_pct=cycles.pnl_pct.mean(),
            best=cycles.pnl.max(), worst=cycles.pnl.min(),
            win_streak=streak(cycles.pnl > 0), loss_streak=streak(cycles.pnl <= 0),
            avg_days=cycles.days_held.mean(),
            exposure=min(in_market_days / (years * 365.25) * 100, 100.0),
            fees=cycles.fees.sum(),
            reasons=cycles.reason.value_counts().to_dict(),
            by_side={s: (len(g), g.pnl.sum(), (g.pnl > 0).mean() * 100)
                     for s, g in cycles.groupby("side")},
        )
    return k


def report(k: dict):
    line = "=" * 70
    print(line)
    print(f"{SYMBOL} {INTERVAL} | {k['years']:.2f} years | risk {RISK_PCT}% per cycle | fee {FEE_PCT*100:.2f}%/side")
    print(line)
    print(f"Start equity         : {k['start']:,.2f}")
    print(f"Final equity         : {k['end']:,.2f}  ({k['total_return']:+.2f}%)")
    print(f"CAGR                 : {k['cagr']:+.2f}%")
    print(f"Max drawdown         : {k['max_dd']:.2f}%  ({k['max_dd_usd']:,.2f})")
    print(f"Sharpe / Sortino     : {k['sharpe']:.2f} / {k['sortino']:.2f}  (annualised, rf = 0)")
    print(f"Calmar ratio         : {k['calmar']:.2f}  (CAGR / |max drawdown|)")
    print(f"Recovery factor      : {k['recovery']:.2f}  (net profit / max drawdown $)")
    print(f"Buy & hold benchmark : {k['bh_return']:+.2f}% | CAGR {k['bh_cagr']:+.2f}% | "
          f"max DD {k['bh_max_dd']:.2f}% | Sharpe {k['bh_sharpe']:.2f}")
    if "n" not in k:
        print("No cycles were taken.")
        print(line)
        return
    print()
    print(f"Total cycles         : {k['n']}  ({k['n_win']} win, {k['n_loss']} loss)")
    print(f"Win rate             : {k['win_rate']:.1f}%")
    print(f"Profit factor        : {k['profit_factor']:.2f}")
    print(f"Payoff ratio         : {k['payoff']:.2f}  (avg win / avg loss)")
    print(f"Expectancy           : {k['expectancy']:,.2f} per cycle ({k['expectancy_pct']:+.3f}% of equity)")
    print(f"Average win / loss   : {k['avg_win']:,.2f} / {k['avg_loss']:,.2f}")
    print(f"Largest win / loss   : {k['best']:,.2f} / {k['worst']:,.2f}")
    print(f"Max win / loss streak: {k['win_streak']} / {k['loss_streak']} cycles")
    print(f"Avg cycle duration   : {k['avg_days']:.1f} days")
    print(f"Time in market       : {k['exposure']:.1f}%")
    print(f"Total fees paid      : {k['fees']:,.2f}  ({k['fees'] / k['start'] * 100:.2f}% of starting equity)")
    print("Exit reasons         :", ", ".join(f"{r} {n}" for r, n in k["reasons"].items()))
    for side, (n, pnl, wr) in k["by_side"].items():
        print(f"  {side:<5}: {n:>4} cycles | PnL {pnl:>12,.2f} | win rate {wr:.0f}%")
    print(line)


# ------------------------------- CHARTS --------------------------------
def plot_png(equity: pd.Series, df: pd.DataFrame, events: pd.DataFrame, path: str):
    """Static summary chart for the README: price + Kijun + entries, equity vs buy & hold, drawdown."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    bh = df["close"] / df["close"].iloc[0] * equity.iloc[0]
    dd = (equity / equity.cummax() - 1) * 100
    fig, ax = plt.subplots(3, 1, figsize=(12, 9), sharex=True, gridspec_kw={"height_ratios": [3, 2, 1]})

    ax[0].plot(df.index, df["close"], color="#555", lw=0.9, label=f"{SYMBOL} close")
    ax[0].plot(df.index, df["kijun"], color="#e69500", lw=0.9, label=f"Kijun-sen({KIJUN_PERIOD})")
    if len(events):
        ent = events[events.type == "ENTRY"]
        for side, col, mk in (("LONG", "#1a9850", "^"), ("SHORT", "#d73027", "v")):
            e = ent[ent.side == side]
            ax[0].scatter(e.date, e.price, marker=mk, color=col, s=22, zorder=3, label=f"{side.title()} entry")
    ax[0].set_yscale("log")
    ax[0].set_title(f"{SYMBOL} {INTERVAL} - Kijun-sen + Normalized Volume split-position strategy")
    ax[0].legend(loc="upper left", fontsize=8)

    ax[1].plot(equity.index, equity, color="#2166ac", lw=1.3, label="Strategy equity")
    ax[1].plot(bh.index, bh, color="#999", lw=1.0, ls="--", label="Buy & hold")
    ax[1].set_yscale("log")
    ax[1].set_ylabel("Equity (log)")
    ax[1].legend(loc="upper left", fontsize=8)

    ax[2].fill_between(dd.index, dd, 0, color="#d73027", alpha=0.35)
    ax[2].set_ylabel("Drawdown %")
    for a in ax:
        a.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"Static chart saved to {path}")


CROSSHAIR_JS = """
(function () {
  var gd = document.getElementById('{plot_id}');
  function mk() {
    var d = document.createElement('div');
    d.style.cssText = 'position:fixed;pointer-events:none;z-index:9999;padding:2px 6px;' +
      'font:12px sans-serif;background:#222;color:#fff;border-radius:3px;display:none;white-space:nowrap;';
    document.body.appendChild(d);
    return d;
  }
  var lx = mk(), ly = mk();
  function hide() { lx.style.display = 'none'; ly.style.display = 'none'; }
  gd.addEventListener('mouseleave', hide);
  gd.addEventListener('mousemove', function (e) {
    try {
      var fl = gd._fullLayout; if (!fl) return;
      var r = gd.getBoundingClientRect();
      var xa = fl.xaxis, y1 = fl.yaxis, y2 = fl.yaxis2, y3 = fl.yaxis3;
      var px = e.clientX - r.left - xa._offset;
      var py = e.clientY - r.top;
      var ya = null;
      [y1, y2, y3].forEach(function (a) {
        if (!a) return;
        var p = py - a._offset;
        if (p >= 0 && p <= a._length) ya = a;
      });
      if (px < 0 || px > xa._length || !ya) { hide(); return; }
      var ms = xa.p2l(px);
      if (typeof ms === 'string') ms = Date.parse(ms);
      var v = ya.p2l(py - ya._offset);
      if (ya.type === 'log') v = Math.pow(10, v);
      lx.textContent = new Date(ms).toISOString().slice(0, 10);
      ly.textContent = v.toLocaleString(undefined, {maximumFractionDigits: 4});
      lx.style.display = ly.style.display = 'block';
      lx.style.left = e.clientX + 'px';
      lx.style.top = (r.top + y3._offset + y3._length + 4) + 'px';
      lx.style.transform = 'translateX(-50%)';
      ly.style.left = (r.left + xa._offset + xa._length + 4) + 'px';
      ly.style.top = e.clientY + 'px';
      ly.style.transform = 'translateY(-50%)';
    } catch (err) { hide(); }
  });
})();
"""


def plot(equity: pd.Series, events: pd.DataFrame, df: pd.DataFrame, html_path: str = None,
         open_browser: bool = True):
    """Interactive chart: candles + Kijun-sen + entry/exit markers, Normalized Volume, equity curve."""
    html_path = html_path or f"chart_{SYMBOL}_{INTERVAL}.html"
    if len(df) > PLOT_MAX_BARS:
        first = df.index[-PLOT_MAX_BARS]
        df, equity = df[df.index >= first], equity[equity.index >= first]
        events = events[events.date >= first] if len(events) else events

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        row_heights=[0.55, 0.15, 0.30])
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name=SYMBOL, increasing_line_color="#26a69a", decreasing_line_color="#ef5350"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["kijun"], mode="lines", name=f"Kijun-sen({KIJUN_PERIOD})",
                             line=dict(color="#ff9800", width=1.5), hoverinfo="skip"), row=1, col=1)

    if len(events):
        entries = events[events.type == "ENTRY"]
        for side, color, symbol in (("LONG", "#00c853", "triangle-up"), ("SHORT", "#d50000", "triangle-down")):
            e = entries[entries.side == side]
            if len(e):
                fig.add_trace(go.Scatter(
                    x=e.date, y=e.price, mode="markers", name=f"{side.title()} entry",
                    marker=dict(symbol=symbol, size=10, color=color, line=dict(width=1, color="black")),
                    customdata=np.c_[e.qty, e.sl, e.tp1, e.reason],
                    hovertemplate=side + " ENTRY (%{customdata[3]})<br>price %{y:,.4f}<br>"
                                  "qty %{customdata[0]:,.4f} (both legs)<br>SL %{customdata[1]:,.4f}<br>"
                                  "TP1 %{customdata[2]:,.4f}<extra></extra>"), row=1, col=1)

        exit_styles = {"TP1": ("Take profit (leg 1)", "#ffd600", "star"),
                       "SL1": ("Stop loss (leg 1)", "#d50000", "x"),
                       "SL2": ("Stop loss / trail (leg 2)", "#d50000", "x"),
                       "FORCED_CLOSE": ("Forced close (leg 1)", "#757575", "x"),
                       "REVERSE": ("Reverse-signal close", "#9e9e9e", "circle")}
        for etype, (name, color, symbol) in exit_styles.items():
            x = events[events.type == etype]
            if len(x):
                fig.add_trace(go.Scatter(
                    x=x.date, y=x.price, mode="markers", name=name,
                    marker=dict(symbol=symbol, size=9, color=color, line=dict(width=1, color="black")),
                    customdata=np.c_[x.leg, x.qty, x.pnl],
                    hovertemplate=name + " (leg %{customdata[0]:.0f})<br>price %{y:,.4f}<br>"
                                  "qty %{customdata[1]:,.4f}<br>pnl %{customdata[2]:,.2f}<extra></extra>"),
                    row=1, col=1)

    vol_colors = np.where(df["vol_green"], "#26a69a", "#ef5350")
    fig.add_trace(go.Bar(x=df.index, y=df["vol_norm"], marker_color=vol_colors, name="Normalized Volume",
                         hovertemplate="VolNorm %{y:.1f}%%<extra></extra>"), row=2, col=1)
    fig.add_hline(y=VOLNORM_THRESHOLD, line_dash="dot", line_color="#888", row=2, col=1)
    fig.add_trace(go.Scatter(x=equity.index, y=equity.values, mode="lines", name="Equity",
                             line=dict(color="#1976d2", width=1.5)), row=3, col=1)

    spikes = dict(showspikes=True, spikemode="across", spikesnap="cursor", spikedash="dot",
                  spikethickness=1, spikecolor="#888")
    fig.update_xaxes(rangeslider_visible=False, **spikes)
    fig.update_yaxes(**spikes)
    fig.update_yaxes(type="log", title_text=f"{SYMBOL} (log)", row=1, col=1)
    fig.update_yaxes(title_text="VolNorm %", row=2, col=1)
    fig.update_yaxes(title_text="Equity", row=3, col=1)
    fig.update_layout(
        title=f"{SYMBOL} {INTERVAL} - Kijun-sen({KIJUN_PERIOD}) + Normalized Volume({VOLNORM_PERIOD}) split-TP strategy",
        height=950, hovermode="x", dragmode="pan", margin=dict(l=60, r=80, t=60, b=50),
        legend=dict(orientation="h", y=1.03, x=0), template="plotly_white")
    fig.write_html(html_path, include_plotlyjs=True, auto_open=open_browser,
                   config={"scrollZoom": True, "displaylogo": False}, post_script=CROSSHAIR_JS)
    print(f"Interactive chart saved to {html_path}")


if __name__ == "__main__":
    data = load_data()
    equity, events, cycles, data = backtest(data)
    kpis = compute_kpis(equity, cycles, data)
    report(kpis)
    events.to_csv(f"events_{SYMBOL}_{INTERVAL}.csv", index=False)
    cycles.to_csv(f"cycles_{SYMBOL}_{INTERVAL}.csv", index=False)
    if SAVE_PNG:
        plot_png(equity, data, events, f"results_{SYMBOL}_{INTERVAL}.png")
    plot(equity, events, data, open_browser=OPEN_BROWSER)
