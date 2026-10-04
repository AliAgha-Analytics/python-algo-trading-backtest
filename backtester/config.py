"""All configurable settings: market/data, strategy parameters, execution costs, Monte Carlo and
optimisation. Edit this file (or pass modified copies of the dataclasses) to change a run."""
from dataclasses import dataclass

# ------------------------------- MARKET / DATA -------------------------------
SYMBOL = "BTCUSDT"
INTERVAL = "1d"                 # signal timeframe (the strategy trades daily bars)
INTRADAY_INTERVAL = "5m"        # execution timeframe used for fills, stops, targets and slippage
START_DATE = "2020-01-01"       # first day the strategy may trade
END_DATE = "2026-10-03"         # fixed so README results are reproducible; None = latest bar
WARMUP_DAYS = 120               # extra daily history loaded before START_DATE for indicator warm-up
CACHE_DIR = "data_cache"
RESULTS_DIR = "results"

START_CASH = 100_000.0
QTY_STEP = 0.0001               # BTC quantity step per leg (Binance BTCUSDT lot step is 0.00001)


# ------------------------------ STRATEGY PARAMETERS --------------------------
@dataclass(frozen=True)
class StrategyParams:
    kijun_period: int = 14
    volnorm_period: int = 14
    volnorm_threshold: float = 100.0   # volume is "green" when Normalized Volume > this
    atr_period: int = 14
    sl_atr: float = 1.5                # initial stop, both legs (x ATR)
    tp1_atr: float = 1.0               # leg 1 take-profit (x ATR)
    trail_atr: float = 1.5             # leg 2 trailing distance (x ATR)
    risk_pct: float = 2.0              # % of equity risked per cycle at the initial stop (see README section 5)

    def label(self) -> str:
        return (f"Kijun {self.kijun_period} | SL {self.sl_atr}xATR | TP1 {self.tp1_atr}xATR | "
                f"Trail {self.trail_atr}xATR | risk {self.risk_pct}%")


# -------------------------------- EXECUTION COSTS ----------------------------
@dataclass(frozen=True)
class CostModel:
    """Binance spot MARGIN account, regular user (VIP 0) paying fees in BNB.

    Fees: VIP 0 is 0.100% maker / 0.100% taker; paying with BNB gives 25% off -> 0.075% / 0.075%.
    Borrow interest (charged hourly on Binance, rates are dynamic): ~0.012%/day for BTC and
    ~0.03%/day for USDT are typical published levels (~4.4% and ~11% a year).
    """
    maker_fee: float = 0.00075         # limit orders that rest in the book (entries filled as maker, TP1)
    taker_fee: float = 0.00075         # market / stop orders (stop-loss, trailing stop, reverse exits)
    entry_mode: str = "maker"          # "maker": limit at the daily open, market fallback; "taker": market
    entry_limit_wait_min: int = 60     # how long the entry limit order rests before switching to market
    slip_base_bps: float = 1.0         # slippage on every market/stop fill, in basis points
    slip_range_frac: float = 0.10      # + this fraction of the fill bar's high-low range (volatility)
    btc_borrow_daily: float = 0.00012  # interest on borrowed BTC (shorts), per day
    usdt_borrow_daily: float = 0.00030 # interest on borrowed USDT (longs larger than equity), per day
    max_leverage: float = 3.0          # Binance cross-margin limit: position notional <= 3x equity
    use_intraday: bool = True          # False = daily bars only (worst-case intrabar ordering)


ZERO_COSTS = CostModel(maker_fee=0.0, taker_fee=0.0, slip_base_bps=0.0, slip_range_frac=0.0,
                       btc_borrow_daily=0.0, usdt_borrow_daily=0.0, entry_mode="taker")


# ---------------------------------- MONTE CARLO ------------------------------
MC_SIMULATIONS = 10_000
MC_SEED = 42
MC_RUIN_DD_PCT = 50.0
MC_DD_THRESHOLDS = (20, 30, 40, 50)
MC_RISK_LEVELS = (1.0, 2.0, 3.0, 4.0, 5.0)


# ---------------------------------- OPTIMISATION -----------------------------
@dataclass(frozen=True)
class OptimisationGrid:
    kijun_period: tuple = (10, 14, 20, 26, 34, 50)
    sl_atr: tuple = (1.0, 1.5, 2.0, 2.5)
    tp1_atr: tuple = (0.75, 1.0, 1.5, 2.0)
    trail_atr: tuple = (1.0, 1.5, 2.0, 3.0)


WF_IN_SAMPLE_MONTHS = 24        # optimise on 2 years...
WF_OUT_SAMPLE_MONTHS = 6        # ...then trade the next 6 months with the chosen parameters
WF_MIN_TRADES = 20              # ignore parameter sets with too few in-sample trades
WF_TOP_K = 5                    # size of the walk-forward parameter ensemble
