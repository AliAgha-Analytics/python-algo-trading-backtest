"""Backtest the strategy with realistic execution costs, compare cost scenarios, and run the Monte
Carlo risk analysis.  Usage:  python run_backtest.py"""
import os
from dataclasses import replace

from backtester import config as C
from backtester.config import StrategyParams, CostModel, ZERO_COSTS
from backtester.data import load_all, IntradayBook
from backtester.engine import backtest
from backtester.metrics import compute_kpis, report
from backtester.monte_carlo import monte_carlo, report_mc
from backtester import plots


def main():
    os.makedirs(C.RESULTS_DIR, exist_ok=True)
    out = lambda name: os.path.join(C.RESULTS_DIR, name)
    daily, book = load_all()
    params, costs = StrategyParams(), CostModel()

    # ---- main run: realistic costs, intraday execution
    res = backtest(daily, book, params, costs)
    k = compute_kpis(res.equity, res.cycles, res.df["close"])
    report(k, f"{C.SYMBOL} | {params.label()}\nCosts: maker {costs.maker_fee*100:.3f}% / taker "
              f"{costs.taker_fee*100:.3f}% | slippage {costs.slip_base_bps}bp + {costs.slip_range_frac:.0%} "
              f"of bar range | borrow BTC {costs.btc_borrow_daily*100:.3f}%/day, USDT "
              f"{costs.usdt_borrow_daily*100:.3f}%/day")
    res.events.to_csv(out("events.csv"), index=False)
    res.cycles.to_csv(out("cycles.csv"), index=False)

    # ---- cost scenarios: how much each modelling choice matters
    no_intraday = IntradayBook(None, 5)
    scenarios = [
        ("No costs (daily bars)", ZERO_COSTS, no_intraday),
        ("Fees only (0.10%), daily bars", replace(ZERO_COSTS, maker_fee=0.001, taker_fee=0.001), no_intraday),
        ("Base case costs, daily bars only", replace(costs, entry_mode="taker"), no_intraday),
        ("VIP0 no BNB: 0.10% fees, all costs", replace(costs, maker_fee=0.001, taker_fee=0.001), book),
        ("Base case: 0.075% fees, all costs", costs, book),
        ("Base case, market-order entries", replace(costs, entry_mode="taker"), book),
        ("Base case, 2x slippage & 2x borrow", replace(costs, slip_base_bps=2 * costs.slip_base_bps,
                                                       slip_range_frac=2 * costs.slip_range_frac,
                                                       btc_borrow_daily=2 * costs.btc_borrow_daily,
                                                       usdt_borrow_daily=2 * costs.usdt_borrow_daily), book),
    ]
    print("\nCOST SCENARIOS")
    print(f"{'scenario':<38} {'return':>8} {'CAGR':>7} {'Sharpe':>7} {'max DD':>8} {'PF':>5} {'costs':>9}")
    for name, cm, bk in scenarios:
        r = backtest(daily, bk, params, cm, record_events=False)
        s = compute_kpis(r.equity, r.cycles, r.df["close"])
        total_cost = s["fees"] + s["slippage"] + s["interest"]
        print(f"{name:<38} {s['total_return']:>+7.1f}% {s['cagr']:>+6.1f}% {s['sharpe']:>7.2f} "
              f"{s['max_dd']:>7.1f}% {s['profit_factor']:>5.2f} {total_cost:>9,.0f}")

    # ---- Monte Carlo
    mc = monte_carlo(res.cycles, k["years"], params.risk_pct)
    report_mc(mc)

    # ---- charts
    plots.plot_results(res.equity, res.df, res.events, out("results.png"),
                       f"{C.SYMBOL} - Kijun-sen + Normalized Volume split-position strategy (after all costs)")
    if mc:
        plots.plot_monte_carlo(mc, out("monte_carlo.png"))
    plots.plot_interactive(res.equity, res.events, res.df, out("chart.html"))
    print(f"\nSaved charts and trade logs to ./{C.RESULTS_DIR}/")


if __name__ == "__main__":
    main()
