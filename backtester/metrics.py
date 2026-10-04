"""Performance KPIs and the console report."""
import numpy as np
import pandas as pd


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.resample("1D").last().ffill().pct_change().dropna()


def sharpe(ret: pd.Series) -> float:
    return float(ret.mean() / ret.std() * np.sqrt(365)) if len(ret) > 2 and ret.std() > 0 else np.nan


def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min() * 100)


def series_kpis(equity: pd.Series) -> dict:
    """KPIs that only need an equity curve (used by the optimiser and walk-forward too)."""
    ret = daily_returns(equity)
    years = (equity.index[-1] - equity.index[0]) / pd.Timedelta(days=365.25)
    growth = equity.iloc[-1] / equity.iloc[0]
    downside = ret[ret < 0]
    mdd = max_drawdown(equity)
    cagr = (growth ** (1 / years) - 1) * 100 if years > 0 and growth > 0 else np.nan
    return dict(total_return=(growth - 1) * 100, cagr=cagr, max_dd=mdd, sharpe=sharpe(ret),
                sortino=float(ret.mean() / downside.std() * np.sqrt(365)) if len(downside) > 2 else np.nan,
                calmar=cagr / abs(mdd) if mdd < 0 else np.nan, years=years)


def compute_kpis(equity: pd.Series, cycles: pd.DataFrame, close: pd.Series) -> dict:
    k = series_kpis(equity)
    start_val, end_val = equity.iloc[0], equity.iloc[-1]
    max_dd_usd = float((equity.cummax() - equity).max())
    k.update(start=start_val, end=end_val, max_dd_usd=max_dd_usd,
             recovery=(end_val - start_val) / max_dd_usd if max_dd_usd > 0 else np.nan)
    bh = close / close.iloc[0] * start_val
    b = series_kpis(bh)
    k.update(bh_return=b["total_return"], bh_cagr=b["cagr"], bh_max_dd=b["max_dd"], bh_sharpe=b["sharpe"])
    if len(cycles) == 0:
        return k

    wins, losses = cycles[cycles.pnl > 0], cycles[cycles.pnl <= 0]
    avg_loss = losses.pnl.mean() if len(losses) else 0.0

    def streak(flags):
        best = cur = 0
        for f in flags:
            cur = cur + 1 if f else 0
            best = max(best, cur)
        return best

    k.update(
        n=len(cycles), n_win=len(wins), n_loss=len(losses),
        win_rate=len(wins) / len(cycles) * 100,
        profit_factor=wins.pnl.sum() / -losses.pnl.sum() if losses.pnl.sum() < 0 else np.inf,
        avg_win=wins.pnl.mean() if len(wins) else 0.0, avg_loss=avg_loss,
        payoff=wins.pnl.mean() / -avg_loss if avg_loss < 0 else np.inf,
        expectancy=cycles.pnl.mean(), expectancy_pct=cycles.pnl_pct.mean(),
        best=cycles.pnl.max(), worst=cycles.pnl.min(),
        win_streak=streak(cycles.pnl > 0), loss_streak=streak(cycles.pnl <= 0),
        avg_days=cycles.days_held.mean(),
        exposure=min(cycles.days_held.sum() / (k["years"] * 365.25) * 100, 100.0),
        gross=cycles.gross_pnl.sum(), fees=cycles.fees.sum(), slippage=cycles.slippage.sum(),
        interest=cycles.interest.sum(), net=cycles.pnl.sum(),
        maker_entries=(cycles.entry_exec == "maker").mean() * 100,
        reasons=cycles.reason.value_counts().to_dict(),
        by_side={s: (len(g), g.pnl.sum(), (g.pnl > 0).mean() * 100) for s, g in cycles.groupby("side")},
        by_entry={s: (len(g), g.pnl.sum(), (g.pnl > 0).mean() * 100) for s, g in cycles.groupby("entry_type")},
    )
    return k


def report(k: dict, title: str):
    line = "=" * 74
    print(line)
    print(title)
    print(line)
    print(f"Start / final equity : {k['start']:,.2f} -> {k['end']:,.2f}  ({k['total_return']:+.2f}%)")
    print(f"CAGR                 : {k['cagr']:+.2f}%   ({k['years']:.2f} years)")
    print(f"Max drawdown         : {k['max_dd']:.2f}%  ({k['max_dd_usd']:,.2f})")
    print(f"Sharpe / Sortino     : {k['sharpe']:.2f} / {k['sortino']:.2f}  (annualised, rf = 0)")
    print(f"Calmar / Recovery    : {k['calmar']:.2f} / {k['recovery']:.2f}")
    print(f"Buy & hold           : {k['bh_return']:+.2f}% | CAGR {k['bh_cagr']:+.2f}% | "
          f"max DD {k['bh_max_dd']:.2f}% | Sharpe {k['bh_sharpe']:.2f}")
    if "n" not in k:
        print("No cycles were taken.")
        print(line)
        return
    print()
    print(f"Cycles               : {k['n']}  ({k['n_win']} win / {k['n_loss']} loss), win rate {k['win_rate']:.1f}%")
    print(f"Profit factor        : {k['profit_factor']:.2f}   | Payoff ratio {k['payoff']:.2f}")
    print(f"Expectancy           : {k['expectancy']:,.2f} per cycle ({k['expectancy_pct']:+.3f}% of equity)")
    print(f"Avg win / avg loss   : {k['avg_win']:,.2f} / {k['avg_loss']:,.2f}")
    print(f"Largest win / loss   : {k['best']:,.2f} / {k['worst']:,.2f}")
    print(f"Max win / loss streak: {k['win_streak']} / {k['loss_streak']}")
    print(f"Avg duration         : {k['avg_days']:.1f} days | time in market {k['exposure']:.1f}%")
    print(f"Entries filled maker : {k['maker_entries']:.1f}%")
    print()
    print("Cost breakdown (all cycles):")
    print(f"  Gross P&L before costs : {k['gross']:>12,.2f}")
    print(f"  - Exchange fees        : {k['fees']:>12,.2f}")
    print(f"  - Slippage             : {k['slippage']:>12,.2f}")
    print(f"  - Margin interest      : {k['interest']:>12,.2f}")
    print(f"  = Net P&L              : {k['net']:>12,.2f}")
    print()
    print("Exit reasons         :", ", ".join(f"{r} {n}" for r, n in k["reasons"].items()))
    for name, grp in (("Side", k["by_side"]), ("Entry", k["by_entry"])):
        for s, (n, pnl, wr) in grp.items():
            print(f"  {name:<5} {s:<16}: {n:>4} cycles | net P&L {pnl:>12,.2f} | win rate {wr:.0f}%")
    print(line)
