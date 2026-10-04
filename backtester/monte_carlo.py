"""Monte Carlo resampling of the trade sequence: distribution of returns, drawdowns, losing
streaks and the risk of ruin, plus a risk-per-trade sensitivity table.

Method: each closed cycle's net return (% of equity at entry) is drawn at random WITH replacement
to build MC_SIMULATIONS synthetic trade sequences of the same length, compounding equity trade by
trade. Assumes trades are independent (ignores regime clustering). Drawdowns are measured on
closed-trade equity. Other risk levels are approximated by scaling returns by level / risk_pct.
"""
import numpy as np
import pandas as pd

from . import config as C


def _paths(draws_pct: np.ndarray) -> np.ndarray:
    return np.hstack([np.ones((draws_pct.shape[0], 1)), np.cumprod(1.0 + draws_pct / 100.0, axis=1)])


def _max_dd(paths: np.ndarray) -> np.ndarray:
    return (1.0 - paths / np.maximum.accumulate(paths, axis=1)).max(axis=1) * 100.0


def _max_losing_streak(draws: np.ndarray) -> np.ndarray:
    best = np.zeros(draws.shape[0], dtype=int)
    cur = np.zeros(draws.shape[0], dtype=int)
    for j in range(draws.shape[1]):
        cur = np.where(draws[:, j] <= 0, cur + 1, 0)
        best = np.maximum(best, cur)
    return best


def monte_carlo(cycles: pd.DataFrame, years: float, risk_pct: float) -> dict:
    r = cycles[~cycles.reason.str.startswith("OPEN")].pnl_pct.dropna().to_numpy()
    if len(r) < 10 or C.MC_SIMULATIONS <= 0:
        return {}
    n, sims = len(r), C.MC_SIMULATIONS
    hist_path = _paths(r[None, :])
    draws = np.random.default_rng(C.MC_SEED).choice(r, size=(sims, n), replace=True)
    paths = _paths(draws)
    final_ret = (paths[:, -1] - 1) * 100
    cagr = (np.clip(paths[:, -1], 1e-12, None) ** (1 / years) - 1) * 100
    dd = _max_dd(paths)
    streak = _max_losing_streak(draws)
    hist_dd = _max_dd(hist_path)[0]

    sens = []
    for lvl in C.MC_RISK_LEVELS:
        d2 = np.random.default_rng(C.MC_SEED).choice(r * lvl / risk_pct, size=(sims, n), replace=True)
        p2 = _paths(d2)
        dd2 = _max_dd(p2)
        sens.append(dict(risk=lvl, median_ret=np.median((p2[:, -1] - 1) * 100), median_dd=np.median(dd2),
                         dd95=np.percentile(dd2, 95), p_loss=(p2[:, -1] < 1).mean() * 100,
                         ruin=(dd2 >= C.MC_RUIN_DD_PCT).mean() * 100))

    q = lambda a, x: float(np.percentile(a, x))
    return dict(
        n_trades=n, n_sims=sims, risk_pct=risk_pct, hist_dd=hist_dd, hist_ret=(hist_path[0, -1] - 1) * 100,
        ret_p5=q(final_ret, 5), ret_p50=q(final_ret, 50), ret_p95=q(final_ret, 95),
        cagr_p5=q(cagr, 5), cagr_p50=q(cagr, 50), cagr_p95=q(cagr, 95),
        p_loss=(final_ret < 0).mean() * 100,
        dd_p50=q(dd, 50), dd_p95=q(dd, 95), dd_p99=q(dd, 99), dd_worst=float(dd.max()),
        dd_hist_percentile=(dd <= hist_dd).mean() * 100,
        dd_probs={t: (dd >= t).mean() * 100 for t in C.MC_DD_THRESHOLDS},
        ruin=(dd >= C.MC_RUIN_DD_PCT).mean() * 100,
        streak_p50=q(streak, 50), streak_p95=q(streak, 95),
        sensitivity=sens, bands=np.percentile(paths, [5, 25, 50, 75, 95], axis=0),
        sample_paths=paths[:100], max_dd=dd, hist_path=hist_path[0],
    )


def report_mc(m: dict):
    if not m:
        print("Monte Carlo skipped (too few trades).")
        return
    line = "=" * 74
    print(line)
    print(f"MONTE CARLO | {m['n_sims']:,} bootstrapped sequences of {m['n_trades']} trades | "
          f"risk {m['risk_pct']}% per cycle")
    print(line)
    print(f"Final return 5/50/95%  : {m['ret_p5']:+.1f}% / {m['ret_p50']:+.1f}% / {m['ret_p95']:+.1f}%"
          f"  (historical {m['hist_ret']:+.1f}%)")
    print(f"CAGR 5/50/95%          : {m['cagr_p5']:+.1f}% / {m['cagr_p50']:+.1f}% / {m['cagr_p95']:+.1f}%")
    print(f"P(ending at a loss)    : {m['p_loss']:.1f}%")
    print(f"Max DD 50/95/99%       : {m['dd_p50']:.1f}% / {m['dd_p95']:.1f}% / {m['dd_p99']:.1f}%"
          f"  (worst {m['dd_worst']:.1f}%)")
    print(f"Historical max DD      : {m['hist_dd']:.1f}%  (worse than {m['dd_hist_percentile']:.0f}% of simulations)")
    print("P(max DD >= X%)        : " + " | ".join(f"{t}%: {p:.1f}%" for t, p in m["dd_probs"].items()))
    print(f"Risk of ruin (DD>={C.MC_RUIN_DD_PCT:.0f}%): {m['ruin']:.2f}%")
    print(f"Losing streak 50/95%   : {m['streak_p50']:.0f} / {m['streak_p95']:.0f}")
    print("Risk-per-trade sensitivity:")
    print(f"  {'risk':>5} | {'median ret':>10} | {'median DD':>9} | {'95% DD':>7} | {'P(loss)':>7} | {'ruin':>6}")
    for s in m["sensitivity"]:
        print(f"  {s['risk']:>4.1f}% | {s['median_ret']:>+9.1f}% | {s['median_dd']:>8.1f}% | {s['dd95']:>6.1f}% | "
              f"{s['p_loss']:>6.1f}% | {s['ruin']:>5.2f}%")
    print(line)
