"""Parameter robustness: full grid sweep (sensitivity) and walk-forward optimisation.

Why: one hand-picked parameter set can be lucky. Picking the best set over the whole history is
worse - that is curve-fitting. Instead:

1. Sensitivity grid: run every parameter combination and look at the SHAPE of the results. A broad
   plateau of decent Sharpe ratios means the edge is robust; an isolated spike means it is fragile.
2. Walk-forward: optimise on 24 months, trade the next 6 months with the chosen parameters on data
   the optimiser never saw, roll forward 6 months, repeat. Only the out-of-sample (OOS) months are
   stitched together, giving an honest estimate of live performance. Three selection rules:
     - best single:     highest in-sample Sharpe
     - robust plateau:  highest AVERAGE in-sample Sharpe of the set and its grid neighbours
     - top-K ensemble:  capital split equally across the K best sets (multiple parameters at once)

Implementation note: every combination is backtested once over the full period, and each window
is evaluated on the slice of that run's daily returns. A cycle that straddles a window boundary is
therefore attributed to the window in which each day's P&L occurred.
"""
import itertools
import numpy as np
import pandas as pd

from . import config as C
from .config import StrategyParams, CostModel, OptimisationGrid
from .engine import backtest
from .metrics import daily_returns, sharpe, series_kpis


def run_grid(daily, book, base: StrategyParams, cm: CostModel, grid: OptimisationGrid = OptimisationGrid()):
    names = ["kijun_period", "sl_atr", "tp1_atr", "trail_atr"]
    combos = list(itertools.product(*(getattr(grid, n) for n in names)))
    rows, returns, opens = [], {}, {}
    for n, values in enumerate(combos, 1):
        p = StrategyParams(**{**base.__dict__, **dict(zip(names, values))})
        r = backtest(daily, book, p, cm, record_events=False)
        k = series_kpis(r.equity)
        cyc = r.cycles
        wins, losses = cyc.pnl[cyc.pnl > 0].sum(), -cyc.pnl[cyc.pnl <= 0].sum()
        rows.append(dict(zip(names, values), **k, trades=len(cyc),
                         profit_factor=wins / losses if losses > 0 else np.nan))
        returns[values] = daily_returns(r.equity)
        opens[values] = pd.to_datetime(cyc.opened).dt.normalize() if len(cyc) else pd.Series(dtype="datetime64[ns]")
        if n % 50 == 0 or n == len(combos):
            print(f"  grid: {n}/{len(combos)} parameter sets done")
    return pd.DataFrame(rows), returns, opens, names


def _neighbour_mean(scores: pd.Series, grid: OptimisationGrid, names) -> pd.Series:
    """Average score of each combo and its immediate neighbours on the grid (+/- one step per axis)."""
    axes = [list(getattr(grid, n)) for n in names]
    out = {}
    for key in scores.index:
        pos = [axes[d].index(key[d]) for d in range(len(names))]
        vals = []
        for delta in itertools.product((-1, 0, 1), repeat=len(names)):
            q = [pos[d] + delta[d] for d in range(len(names))]
            if all(0 <= q[d] < len(axes[d]) for d in range(len(names))):
                nk = tuple(axes[d][q[d]] for d in range(len(names)))
                if nk in scores.index and not np.isnan(scores[nk]):
                    vals.append(scores[nk])
        out[key] = np.mean(vals) if vals else np.nan
    return pd.Series(out)


def walk_forward(returns: dict, opens: dict, names, base: StrategyParams,
                 grid: OptimisationGrid = OptimisationGrid()):
    keys = list(returns)
    R = pd.DataFrame({k: returns[k] for k in keys}).fillna(0.0)
    R.columns = pd.MultiIndex.from_tuples(keys, names=names)
    base_key = tuple(getattr(base, n) for n in names)
    start, end = R.index[0], R.index[-1]
    windows, oos = [], {"best": [], "plateau": [], "ensemble": [], "baseline": []}

    oos_start = start + pd.DateOffset(months=C.WF_IN_SAMPLE_MONTHS)
    while oos_start <= end:
        is_start = oos_start - pd.DateOffset(months=C.WF_IN_SAMPLE_MONTHS)
        oos_end = oos_start + pd.DateOffset(months=C.WF_OUT_SAMPLE_MONTHS)
        ins = R[(R.index >= is_start) & (R.index < oos_start)]
        out = R[(R.index >= oos_start) & (R.index < oos_end)]
        n_trades = pd.Series({k: ((opens[k] >= is_start) & (opens[k] < oos_start)).sum() for k in keys})
        scores = pd.Series({k: sharpe(ins[k]) for k in keys})
        scores[n_trades[scores.index] < C.WF_MIN_TRADES] = np.nan
        best = scores.idxmax()
        plateau_scores = _neighbour_mean(scores, grid, names)
        plateau = plateau_scores.idxmax()
        top = list(scores.sort_values(ascending=False).dropna().index[:C.WF_TOP_K])

        oos["best"].append(out[best])
        oos["plateau"].append(out[plateau])
        oos["ensemble"].append(out[top].mean(axis=1))      # equal capital per parameter set, daily rebalance
        oos["baseline"].append(out[base_key])
        windows.append(dict(
            oos_start=oos_start, oos_end=min(oos_end, end + pd.Timedelta(days=1)) - pd.Timedelta(days=1),
            best=best, best_is_sharpe=scores[best], best_oos_sharpe=sharpe(out[best]),
            plateau=plateau, plateau_is_sharpe=plateau_scores[plateau], plateau_oos_sharpe=sharpe(out[plateau]),
            ensemble_oos_sharpe=sharpe(out[top].mean(axis=1)), baseline_oos_sharpe=sharpe(out[base_key]),
            best_oos_ret=((1 + out[best]).prod() - 1) * 100,
            ensemble_oos_ret=((1 + out[top].mean(axis=1)).prod() - 1) * 100,
            baseline_oos_ret=((1 + out[base_key]).prod() - 1) * 100, top=top))
        oos_start = oos_end

    curves = {}
    first_day = windows[0]["oos_start"] - pd.Timedelta(days=1)
    labels = {"baseline": "Fixed baseline parameters", "best": "Walk-forward: best single",
              "plateau": "Walk-forward: robust plateau", "ensemble": f"Walk-forward: top-{C.WF_TOP_K} ensemble"}
    for key, label in labels.items():
        r = pd.concat(oos[key])
        eq = pd.concat([pd.Series([C.START_CASH], index=[first_day]), C.START_CASH * (1 + r).cumprod()])
        curves[label] = eq
    return curves, windows


def fmt_params(key, names) -> str:
    short = {"kijun_period": "K", "sl_atr": "SL", "tp1_atr": "TP", "trail_atr": "TR"}
    return " ".join(f"{short[n]}{v:g}" for n, v in zip(names, key))
