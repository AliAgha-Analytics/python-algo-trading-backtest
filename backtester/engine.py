"""Event-driven backtest engine.

Signals are computed on DAILY bars; orders are executed on INTRADAY (5-minute) bars:

* Entry:    limit order at the daily open (maker). If it is not traded through within
            entry_limit_wait_min minutes it is replaced by a market order (taker + slippage).
* TP1:      resting limit order (maker), filled only if price trades THROUGH the level.
* Stops:    initial stop, trailing stop and breakeven stop are stop-market orders (taker + slippage).
            If a 5-minute bar opens beyond the stop (a gap), the fill is that bar's open.
* Reverse:  decided on the daily close, executed as a market order at the next daily open.
* Interest: shorts borrow the full BTC quantity; longs borrow USDT only for the part of the
            position above account equity. Interest accrues per started hour (Binance convention).
Within a single 5-minute bar a stop is assumed to fill before a target (conservative). Days without
intraday data fall back to the daily bar (one bar) with the same conservative ordering.
"""
import math
import numpy as np
import pandas as pd

from . import config as C
from .config import StrategyParams, CostModel


def add_indicators(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    df = df.copy()
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / p.atr_period, adjust=False, min_periods=p.atr_period).mean()   # Wilder ATR
    df["kijun"] = (df["high"].rolling(p.kijun_period).max() + df["low"].rolling(p.kijun_period).min()) / 2.0
    df["vol_norm"] = df["volume"] / df["volume"].rolling(p.volnorm_period).mean() * 100.0
    df["vol_green"] = df["vol_norm"] > p.volnorm_threshold
    return df


def _round_up(x: float, step: float) -> float:
    return x if step <= 0 else round(float(np.ceil(round(x / step, 9)) * step), 10)


class Cycle:
    """One trade cycle = two equal legs opened together."""

    def __init__(self, d, entry, qty, t_open, i_open, atr_v, p: StrategyParams, borrow_frac, entry_type, exec_type):
        self.dir, self.entry, self.qty = d, entry, qty
        self.leg_open = {1: True, 2: True}
        self.sl = entry - d * p.sl_atr * atr_v
        self.tp1 = entry + d * p.tp1_atr * atr_v
        self.sl2 = self.sl
        self.t_open, self.i_open = t_open, i_open
        self.borrow_frac = borrow_frac          # longs: share of notional financed with borrowed USDT
        self.entry_type, self.exec_type = entry_type, exec_type
        self.fees = self.slippage = self.interest = self.gross = 0.0
        self.equity_at_open = None


class Result:
    def __init__(self, equity, events, cycles, df):
        self.equity, self.events, self.cycles, self.df = equity, events, cycles, df


def backtest(daily: pd.DataFrame, book, p: StrategyParams = StrategyParams(), cm: CostModel = CostModel(),
             start=None, start_cash: float = None, record_events: bool = True) -> Result:
    start = pd.Timestamp(start or C.START_DATE)
    start_cash = C.START_CASH if start_cash is None else start_cash
    df = add_indicators(daily, p)
    idx = df.index
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr, kij, vg_arr = df["atr"].to_numpy(), df["kijun"].to_numpy(), df["vol_green"].to_numpy()
    first = int(np.searchsorted(idx.values, np.datetime64(start)))

    cash = start_cash
    cyc = None
    prev_side, prev_vg = 0, False
    events, cycles = [], []
    equity = np.full(len(df), np.nan)

    # -------------------------------------------------------------- helpers
    def slip(price, bar_range, side_sign, intraday):
        """Adverse slippage for a market order. side_sign = +1 buy, -1 sell."""
        bps = cm.slip_base_bps / 1e4
        if intraday:
            bps += cm.slip_range_frac * bar_range / price
        return price * (1 + side_sign * bps)

    def mtm(price):
        if cyc is None:
            return cash
        q_open = sum(cyc.qty for leg in (1, 2) if cyc.leg_open[leg])
        return cash + cyc.dir * (price - cyc.entry) * q_open

    def close_leg(leg, fill, ideal, t, kind, maker):
        nonlocal cash
        q = cyc.qty
        pnl = cyc.dir * (fill - cyc.entry) * q
        fee = q * fill * (cm.maker_fee if maker else cm.taker_fee)
        hours = max(1, math.ceil((pd.Timestamp(t) - cyc.t_open) / pd.Timedelta(hours=1)))
        if cyc.dir == -1:                                         # borrowed BTC
            interest = q * (cyc.entry + fill) / 2 * cm.btc_borrow_daily * hours / 24
        else:                                                     # borrowed USDT (if any)
            interest = q * cyc.entry * cyc.borrow_frac * cm.usdt_borrow_daily * hours / 24
        cash += pnl - fee - interest
        cyc.gross += pnl
        cyc.fees += fee
        cyc.interest += interest
        cyc.slippage += abs(fill - ideal) * q
        cyc.leg_open[leg] = False
        if record_events:
            events.append(dict(date=pd.Timestamp(t), type=kind, leg=leg, price=fill, qty=q,
                               pnl=pnl - fee - interest))

    def finish(i, t, reason):
        nonlocal cyc
        net = cyc.gross - cyc.fees - cyc.interest
        cycles.append(dict(opened=cyc.t_open, closed=pd.Timestamp(t), side="LONG" if cyc.dir == 1 else "SHORT",
                           entry=cyc.entry, qty_each=cyc.qty, entry_type=cyc.entry_type, entry_exec=cyc.exec_type,
                           gross_pnl=cyc.gross + cyc.slippage, slippage=cyc.slippage, fees=cyc.fees,
                           interest=cyc.interest, pnl=net, pnl_pct=net / cyc.equity_at_open * 100,
                           days_held=(pd.Timestamp(t) - cyc.t_open) / pd.Timedelta(days=1), reason=reason))
        cyc = None

    def open_cycle(d, i, a, bars, reason):
        """Place the entry at the day's open; returns the intraday index to start scanning from."""
        nonlocal cyc, cash
        T, O, H, L, _, intraday = bars
        limit = O[0]
        fill, k, maker = None, 0, False
        if cm.entry_mode == "maker" and intraday:
            deadline = T[0] + np.timedelta64(cm.entry_limit_wait_min, "m")
            n_wait = int(np.searchsorted(T, deadline))
            through = (L[:n_wait] < limit) if d == 1 else (H[:n_wait] > limit)
            if through.any():
                k = int(np.argmax(through))
                fill, maker = limit, True
            elif n_wait < len(T):
                k = n_wait
                fill = slip(O[k], H[k] - L[k], d, True)
            else:
                return None
        if fill is None:
            fill = slip(O[0], H[0] - L[0], d, intraday)
        ideal = limit if maker else (O[k])
        eq = cash
        qty = _round_up(eq * p.risk_pct / 100 / (p.sl_atr * a) / 2, C.QTY_STEP) if a > 0 else 0
        max_qty = math.floor(cm.max_leverage * eq / fill / 2 / C.QTY_STEP) * C.QTY_STEP
        qty = min(qty, max_qty)
        if qty <= 0:
            return None
        notional = 2 * qty * fill
        borrow_frac = max(0.0, 1 - eq / notional) if d == 1 else 1.0
        fee = notional * (cm.maker_fee if maker else cm.taker_fee)
        cash -= fee
        cyc = Cycle(d, fill, qty, pd.Timestamp(T[k]), i, a, p, borrow_frac, reason, "maker" if maker else "taker")
        cyc.fees += fee
        cyc.slippage += abs(fill - ideal) * 2 * qty
        cyc.equity_at_open = eq
        if record_events:
            events.append(dict(date=pd.Timestamp(T[k]), type="ENTRY", side="LONG" if d == 1 else "SHORT",
                               price=fill, qty=2 * qty, sl=cyc.sl, tp1=cyc.tp1, reason=reason,
                               exec="maker" if maker else "taker"))
        # intraday: scan from the bar after the fill; daily fallback: the entry bar itself (conservative)
        return k + 1 if intraday else k

    def manage(i, bars, j):
        """Walk the intraday bars from index j: stops first, then TP1 (conservative within a bar)."""
        T, O, H, L, _, intraday = bars
        n = len(T)
        while cyc is not None and j < n:
            d = cyc.dir
            stop_hit = (L[j:] <= cyc.sl2) if d == 1 else (H[j:] >= cyc.sl2)
            ks = j + int(np.argmax(stop_hit)) if stop_hit.any() else n
            kt = n
            if cyc.leg_open[1]:
                tp_hit = (H[j:] > cyc.tp1) if d == 1 else (L[j:] < cyc.tp1)
                kt = j + int(np.argmax(tp_hit)) if tp_hit.any() else n
            if ks == n and kt == n:
                return
            if ks <= kt:                                          # stop (same bar -> stop first)
                level = cyc.sl2
                gapped = (O[ks] < level) if d == 1 else (O[ks] > level)
                ideal = O[ks] if gapped else level
                fill = slip(ideal, H[ks] - L[ks], -d, intraday)
                trailed = (cyc.sl2 != cyc.sl)
                close_leg(2, fill, ideal, T[ks], "TRAIL" if trailed else "SL2", False)
                if cyc.leg_open[1]:
                    close_leg(1, fill, ideal, T[ks], "FORCED_CLOSE" if trailed else "SL1", False)
                finish(i, T[ks], "trail/breakeven stop" if trailed else "initial stop")
                return
            gapped = (O[kt] > cyc.tp1) if d == 1 else (O[kt] < cyc.tp1)
            fill = O[kt] if gapped else cyc.tp1
            close_leg(1, fill, fill, T[kt], "TP1", True)
            cyc.sl2 = max(cyc.sl2, cyc.entry) if d == 1 else min(cyc.sl2, cyc.entry)
            j = kt + 1

    # ------------------------------------------------------------ main loop
    for i in range(max(first, 1), len(df)):
        a, k = atr[i - 1], kij[i - 1]
        bars = book.get(idx[i], (o[i], h[i], l[i], c[i]))
        if np.isnan(a) or np.isnan(k):
            equity[i] = mtm(c[i])
            continue
        prev_close = c[i - 1]
        side = 1 if prev_close > k else (-1 if prev_close < k else 0)
        crossed = prev_side != 0 and side != 0 and side != prev_side
        vg = bool(vg_arr[i - 1])
        turned_green = vg and not prev_vg
        bull, bear = c[i - 1] > o[i - 1], c[i - 1] < o[i - 1]
        primary = (side == 1 and bull and vg) or (side == -1 and bear and vg)
        j = 0

        if cyc is not None:
            d = cyc.dir
            if side == -d:                                        # reverse signal -> market exit at open
                T, O, H, L = bars[0], bars[1], bars[2], bars[3]
                fill = slip(O[0], H[0] - L[0], -d, bars[5])
                for leg in (2, 1):
                    if cyc.leg_open[leg]:
                        close_leg(leg, fill, O[0], T[0], "REVERSE", False)
                finish(i, T[0], "reverse signal")
                if primary:
                    j = open_cycle(side, i, a, bars, "reverse-reentry")
                    if j is None:
                        j = 0
            elif i > cyc.i_open and cyc.leg_open[2]:              # trail leg 2 once per new day
                cand = prev_close - d * p.trail_atr * a
                cyc.sl2 = max(cyc.sl2, cand) if d == 1 else min(cyc.sl2, cand)
        elif crossed and primary:
            j = open_cycle(side, i, a, bars, "cross") or 0
        elif not crossed and turned_green and side != 0:
            j = open_cycle(side, i, a, bars, "vol-refresh") or 0

        if cyc is not None:
            manage(i, bars, j)
        equity[i] = mtm(c[i])
        prev_side, prev_vg = side, vg

    if cyc is not None:                                           # still open: mark to market
        q_open = sum(cyc.qty for leg in (1, 2) if cyc.leg_open[leg])
        unreal = cyc.dir * (c[-1] - cyc.entry) * q_open
        net = cyc.gross + unreal - cyc.fees - cyc.interest
        cycles.append(dict(opened=cyc.t_open, closed=idx[-1], side="LONG" if cyc.dir == 1 else "SHORT",
                           entry=cyc.entry, qty_each=cyc.qty, entry_type=cyc.entry_type, entry_exec=cyc.exec_type,
                           gross_pnl=cyc.gross + unreal + cyc.slippage, slippage=cyc.slippage, fees=cyc.fees,
                           interest=cyc.interest, pnl=net, pnl_pct=net / cyc.equity_at_open * 100,
                           days_held=(idx[-1] - cyc.t_open) / pd.Timedelta(days=1), reason="OPEN (mark-to-market)"))

    eq = pd.Series(equity[first:], index=idx[first:], name="equity").ffill().fillna(start_cash)
    eq = pd.concat([pd.Series([start_cash], index=[idx[first] - pd.Timedelta(days=1)]), eq])
    return Result(eq, pd.DataFrame(events), pd.DataFrame(cycles), df.iloc[first:])
