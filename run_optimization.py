"""Parameter sensitivity grid + walk-forward optimisation (takes a few minutes).
Usage:  python run_optimization.py"""
import os
import numpy as np
import pandas as pd

from backtester import config as C
from backtester.config import StrategyParams, CostModel, OptimisationGrid
from backtester.data import load_all
from backtester.metrics import series_kpis
from backtester.optimize import run_grid, walk_forward, fmt_params
from backtester import plots


def main():
    os.makedirs(C.RESULTS_DIR, exist_ok=True)
    out = lambda name: os.path.join(C.RESULTS_DIR, name)
    daily, book = load_all()
    base, costs, grid = StrategyParams(), CostModel(), OptimisationGrid()

    print(f"\nRunning {np.prod([len(getattr(grid, f)) for f in grid.__dataclass_fields__])} parameter sets "
          f"with realistic costs...")
    table, returns, opens, names = run_grid(daily, book, base, costs, grid)
    table.to_csv(out("parameter_grid.csv"), index=False)

    line = "=" * 74
    print(line)
    print("PARAMETER SENSITIVITY (full period 2020-2026, after costs)")
    print(line)
    n = len(table)
    print(f"Parameter sets tested        : {n}")
    print(f"Profitable (return > 0)      : {(table.total_return > 0).mean() * 100:.0f}%")
    print(f"Sharpe > 0.5                 : {(table.sharpe > 0.5).mean() * 100:.0f}%")
    print(f"Median Sharpe / return       : {table.sharpe.median():.2f} / {table.total_return.median():+.1f}%")
    base_row = table[(table.kijun_period == base.kijun_period) & np.isclose(table.sl_atr, base.sl_atr)
                     & np.isclose(table.tp1_atr, base.tp1_atr) & np.isclose(table.trail_atr, base.trail_atr)].iloc[0]
    rank = (table.sharpe > base_row.sharpe).sum() + 1
    print(f"Baseline parameters          : Sharpe {base_row.sharpe:.2f}, rank {rank} of {n}")
    print("Top 5 by full-period Sharpe (in-sample, i.e. optimistic):")
    print(table.sort_values("sharpe", ascending=False).head(5)[names + ["sharpe", "total_return", "max_dd", "trades"]]
          .to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    for col in names:
        print(f"  median Sharpe by {col:<13}: " + ", ".join(
            f"{v:g}={s:.2f}" for v, s in table.groupby(col).sharpe.median().items()))

    curves, windows = walk_forward(returns, opens, names, base, grid)
    close = daily["close"]
    oos_idx = curves["Fixed baseline parameters"].index
    bh = close[(close.index >= oos_idx[0])]
    curves["Buy & hold"] = bh / bh.iloc[0] * C.START_CASH

    print(line)
    print(f"WALK-FORWARD ({C.WF_IN_SAMPLE_MONTHS}m in-sample -> {C.WF_OUT_SAMPLE_MONTHS}m out-of-sample, "
          f"selection = in-sample Sharpe)")
    print(line)
    print(f"{'OOS window':<24} {'best single (IS->OOS Sharpe)':<36} {'plateau pick':<22} {'ens.':>6} {'base':>6}")
    for w in windows:
        print(f"{w['oos_start']:%Y-%m-%d} -> {w['oos_end']:%Y-%m-%d}  "
              f"{fmt_params(w['best'], names):<20} {w['best_is_sharpe']:>5.2f}->{w['best_oos_sharpe']:>5.2f}   "
              f"{fmt_params(w['plateau'], names):<22} {w['ensemble_oos_sharpe']:>6.2f} {w['baseline_oos_sharpe']:>6.2f}")
    pd.DataFrame([{**w, "best": fmt_params(w["best"], names), "plateau": fmt_params(w["plateau"], names),
                   "top": "; ".join(fmt_params(t, names) for t in w["top"])} for w in windows]
                 ).to_csv(out("walk_forward_windows.csv"), index=False)

    print()
    print(f"{'Out-of-sample result':<34} {'return':>8} {'CAGR':>7} {'Sharpe':>7} {'max DD':>8} {'Calmar':>7}")
    summary = []
    for name, eq in curves.items():
        s = series_kpis(eq)
        summary.append(dict(variant=name, **s))
        print(f"{name:<34} {s['total_return']:>+7.1f}% {s['cagr']:>+6.1f}% {s['sharpe']:>7.2f} "
              f"{s['max_dd']:>7.1f}% {s['calmar']:>7.2f}")
    is_mean = np.mean([w["best_is_sharpe"] for w in windows])
    oos_mean = np.mean([w["best_oos_sharpe"] for w in windows])
    print(f"\nBest-single overfitting check: mean in-sample Sharpe {is_mean:.2f} vs mean out-of-sample {oos_mean:.2f}")
    pd.DataFrame(summary).to_csv(out("walk_forward_summary.csv"), index=False)
    print(line)

    plots.plot_heatmaps(table, base, out("parameter_heatmap.png"))
    plots.plot_walk_forward(curves, windows, out("walk_forward.png"))
    print(f"Saved results to ./{C.RESULTS_DIR}/")


if __name__ == "__main__":
    main()
