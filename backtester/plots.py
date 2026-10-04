"""Charts: static PNGs for the README and an interactive Plotly HTML chart."""
import numpy as np
import pandas as pd

from . import config as C


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def plot_results(equity: pd.Series, df: pd.DataFrame, events: pd.DataFrame, path: str, title: str):
    """Price + Kijun + entries, equity vs buy & hold, drawdown."""
    plt = _plt()
    bh = df["close"] / df["close"].iloc[0] * equity.iloc[0]
    dd = (equity / equity.cummax() - 1) * 100
    fig, ax = plt.subplots(3, 1, figsize=(12, 9), sharex=True, gridspec_kw={"height_ratios": [3, 2, 1]})
    ax[0].plot(df.index, df["close"], color="#555", lw=0.9, label=f"{C.SYMBOL} close")
    ax[0].plot(df.index, df["kijun"], color="#e69500", lw=0.9, label="Kijun-sen")
    if len(events):
        ent = events[events.type == "ENTRY"]
        for side, col, mk in (("LONG", "#1a9850", "^"), ("SHORT", "#d73027", "v")):
            e = ent[ent.side == side]
            ax[0].scatter(e.date.dt.normalize(), e.price, marker=mk, color=col, s=20, zorder=3,
                          label=f"{side.title()} entry")
    ax[0].set_yscale("log")
    ax[0].set_title(title)
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


def plot_monte_carlo(m: dict, path: str):
    plt = _plt()
    fig, ax = plt.subplots(1, 2, figsize=(13, 5), gridspec_kw={"width_ratios": [3, 2]})
    x = np.arange(m["bands"].shape[1])
    s = C.START_CASH
    for p in m["sample_paths"]:
        ax[0].plot(x, p * s, color="#9ecae1", lw=0.4, alpha=0.35)
    ax[0].fill_between(x, m["bands"][0] * s, m["bands"][4] * s, color="#2166ac", alpha=0.12, label="5th-95th percentile")
    ax[0].fill_between(x, m["bands"][1] * s, m["bands"][3] * s, color="#2166ac", alpha=0.22, label="25th-75th percentile")
    ax[0].plot(x, m["bands"][2] * s, color="#2166ac", lw=1.6, label="Median path")
    ax[0].plot(x, m["hist_path"] * s, color="#d73027", lw=1.4, label="Actual backtest sequence")
    ax[0].axhline(s, color="#555", lw=0.8, ls=":")
    ax[0].set_yscale("log")
    ax[0].set_xlabel("Trade number")
    ax[0].set_ylabel("Equity (log)")
    ax[0].set_title(f"Monte Carlo equity paths ({m['n_sims']:,} simulations, {m['risk_pct']}% risk)")
    ax[0].legend(loc="upper left", fontsize=8)
    ax[1].hist(m["max_dd"], bins=60, color="#2166ac", alpha=0.75)
    for val, lab, col in ((m["dd_p50"], "median", "#333"), (m["dd_p95"], "95th pct", "#e69500"),
                          (m["hist_dd"], "actual", "#d73027")):
        ax[1].axvline(val, color=col, lw=1.4, ls="--", label=f"{lab}: {val:.1f}%")
    ax[1].set_xlabel("Maximum drawdown (%)")
    ax[1].set_ylabel("Number of simulations")
    ax[1].set_title("Distribution of maximum drawdown")
    ax[1].legend(fontsize=8)
    for a in ax:
        a.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_heatmaps(grid: pd.DataFrame, base, path: str):
    """Sharpe ratio across parameter pairs, other parameters held at the base values."""
    plt = _plt()
    pairs = [("kijun_period", "sl_atr", dict(tp1_atr=base.tp1_atr, trail_atr=base.trail_atr)),
             ("tp1_atr", "trail_atr", dict(kijun_period=base.kijun_period, sl_atr=base.sl_atr))]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    vmax = np.nanmax(np.abs(grid["sharpe"]))
    for ax, (rows, cols, fixed) in zip(axes, pairs):
        sub = grid
        for kname, v in fixed.items():
            sub = sub[np.isclose(sub[kname], v)]
        piv = sub.pivot_table(index=rows, columns=cols, values="sharpe")
        im = ax.imshow(piv.values, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto", origin="lower")
        ax.set_xticks(range(len(piv.columns)), [f"{c:g}" for c in piv.columns])
        ax.set_yticks(range(len(piv.index)), [f"{r:g}" for r in piv.index])
        ax.set_xlabel(cols.replace("_atr", " (x ATR)").replace("_", " "))
        ax.set_ylabel(rows.replace("_atr", " (x ATR)").replace("_", " "))
        for (r, cidx), v in np.ndenumerate(piv.values):
            ax.text(cidx, r, f"{v:.2f}", ha="center", va="center", fontsize=8)
        fixed_txt = ", ".join(f"{k.replace('_', ' ')}={v:g}" for k, v in fixed.items())
        ax.set_title(f"Sharpe ratio ({fixed_txt})", fontsize=10)
        fig.colorbar(im, ax=ax, fraction=0.046)
    fig.suptitle("Parameter sensitivity, 2020-2026, after all costs", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_walk_forward(curves: dict, windows: list, path: str):
    plt = _plt()
    fig, ax = plt.subplots(figsize=(12, 5.5))
    styles = {"Fixed baseline parameters": ("#555", "--"), "Walk-forward: best single": ("#d73027", "-"),
              "Walk-forward: robust plateau": ("#1a9850", "-"), "Walk-forward: top-5 ensemble": ("#2166ac", "-"),
              "Buy & hold": ("#bbb", ":")}
    for name, eq in curves.items():
        col, ls = styles.get(name, ("#000", "-"))
        ax.plot(eq.index, eq, color=col, ls=ls, lw=1.5 if "ensemble" in name else 1.2, label=name)
    for w in windows:
        ax.axvline(w["oos_start"], color="#ddd", lw=0.7, zorder=0)
    ax.set_yscale("log")
    ax.set_ylabel("Equity (log)")
    ax.set_title("Walk-forward out-of-sample equity (24-month optimisation, 6-month trading windows)")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_interactive(equity: pd.Series, events: pd.DataFrame, df: pd.DataFrame, path: str, open_browser=False):
    """Candles + Kijun-sen + entry/exit markers, Normalized Volume, equity curve (Plotly HTML)."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.55, 0.15, 0.30])
    fig.add_trace(go.Candlestick(x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                                 name=C.SYMBOL, increasing_line_color="#26a69a", decreasing_line_color="#ef5350"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["kijun"], mode="lines", name="Kijun-sen",
                             line=dict(color="#ff9800", width=1.5), hoverinfo="skip"), row=1, col=1)
    if len(events):
        ent = events[events.type == "ENTRY"]
        for side, color, sym in (("LONG", "#00c853", "triangle-up"), ("SHORT", "#d50000", "triangle-down")):
            e = ent[ent.side == side]
            if len(e):
                fig.add_trace(go.Scatter(
                    x=e.date, y=e.price, mode="markers", name=f"{side.title()} entry",
                    marker=dict(symbol=sym, size=10, color=color, line=dict(width=1, color="black")),
                    customdata=np.c_[e.qty, e.sl, e.tp1, e.reason, e.exec],
                    hovertemplate=side + " (%{customdata[3]}, %{customdata[4]})<br>price %{y:,.2f}<br>qty "
                                  "%{customdata[0]:,.4f}<br>SL %{customdata[1]:,.2f}<br>TP1 %{customdata[2]:,.2f}"
                                  "<extra></extra>"), row=1, col=1)
        styles = {"TP1": ("Take profit (leg 1)", "#ffd600", "star"), "SL1": ("Initial stop", "#d50000", "x"),
                  "SL2": ("Initial stop", "#d50000", "x"), "TRAIL": ("Trailing / breakeven stop", "#ff6d00", "x"),
                  "FORCED_CLOSE": ("Forced close (leg 1)", "#757575", "x"),
                  "REVERSE": ("Reverse-signal close", "#9e9e9e", "circle")}
        for etype, (name, color, sym) in styles.items():
            x = events[events.type == etype]
            if len(x):
                fig.add_trace(go.Scatter(
                    x=x.date, y=x.price, mode="markers", name=name, legendgroup=name,
                    marker=dict(symbol=sym, size=9, color=color, line=dict(width=1, color="black")),
                    customdata=np.c_[x.leg, x.pnl],
                    hovertemplate=name + " (leg %{customdata[0]:.0f})<br>price %{y:,.2f}<br>net pnl "
                                         "%{customdata[1]:,.2f}<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Bar(x=df.index, y=df["vol_norm"], marker_color=np.where(df["vol_green"], "#26a69a", "#ef5350"),
                         name="Normalized Volume"), row=2, col=1)
    fig.add_trace(go.Scatter(x=equity.index, y=equity.values, mode="lines", name="Equity",
                             line=dict(color="#1976d2", width=1.5)), row=3, col=1)
    fig.update_xaxes(rangeslider_visible=False)
    fig.update_yaxes(type="log", row=1, col=1)
    fig.update_layout(height=950, hovermode="x", dragmode="pan", template="plotly_white",
                      title=f"{C.SYMBOL} - Kijun-sen + Normalized Volume split-position strategy",
                      legend=dict(orientation="h", y=1.03, x=0))
    fig.write_html(path, include_plotlyjs=True, auto_open=open_browser, config={"scrollZoom": True})
