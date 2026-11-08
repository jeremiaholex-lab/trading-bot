import os
from pathlib import Path
from dotenv import load_dotenv

# Base Directory
BASE_DIR = Path(__file__).resolve().parent
BOT_PROFILE = os.getenv("BOT_PROFILE", "primary").strip().lower()
if BOT_PROFILE not in {"primary", "secondary"}:
    raise ValueError(f"Unknown BOT_PROFILE: {BOT_PROFILE!r}")

if BOT_PROFILE == "secondary":
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_LIVE"):
        os.environ.pop(key, None)

env_file = Path(os.getenv(
    "BOT_ENV_FILE",
    BASE_DIR / (".env" if BOT_PROFILE == "primary" else ".env.secondary"),
))
if env_file.exists():
    load_dotenv(env_file)
elif BOT_PROFILE == "primary":
    load_dotenv()

class Config:
    """Central configuration for Trading Bot."""
    BASE_DIR: Path = BASE_DIR
    BOT_PROFILE: str = BOT_PROFILE
    STATE_DIR: Path = Path(os.getenv(
        "BOT_STATE_DIR",
        BASE_DIR if BOT_PROFILE == "primary" else BASE_DIR / "runtime" / BOT_PROFILE,
    ))

    # Alpaca API Credentials - must come from the environment (.env), never hardcode them here.
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "").strip()
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "").strip()
    ALPACA_LIVE: bool = os.getenv("ALPACA_LIVE", "False").lower() in ("true", "1", "yes")

    # When credentials are configured but Alpaca rejects them, the bot refuses to
    # start rather than silently downgrading to the local simulator. A silent
    # downgrade looks exactly like "trading" while placing no orders at all.
    # Set to True only if you deliberately want simulator mode with bad keys.
    ALLOW_SIMULATOR_FALLBACK: bool = (
        BOT_PROFILE == "primary"
        and os.getenv("ALLOW_SIMULATOR_FALLBACK", "False").lower() in ("true", "1", "yes")
    )

    # Endpoints
    PAPER_BASE_URL: str = "https://paper-api.alpaca.markets"
    LIVE_BASE_URL: str = "https://api.alpaca.markets"
    BASE_URL: str = LIVE_BASE_URL if ALPACA_LIVE else PAPER_BASE_URL

    # Watchlist
    WATCHLIST: list[str] = [
        s.strip().upper() for s in os.getenv("WATCHLIST", "SPY,QQQ,AAPL,NVDA,MSFT,TSLA,AMD").split(",") if s.strip()
    ]

    # Bar timeframe: '1Min', '5Min', '15Min', '1Hour', '1Day'
    BAR_TIMEFRAME: str = os.getenv("BAR_TIMEFRAME", "15Min")

    # The canonical set of supported bar timeframes. Both the Alpaca unit
    # map and the yfinance interval map in data/market_data.py must handle
    # every one of these -- otherwise the timeframe is silently downgraded
    # to 15 minutes at runtime while the operator believes otherwise.
    VALID_TIMEFRAMES: set = {"1Min", "5Min", "15Min", "1Hour", "1Day"}

    # Risk Management
    MAX_PORTFOLIO_RISK_PER_TRADE: float = float(os.getenv("MAX_PORTFOLIO_RISK_PER_TRADE", "0.015"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
    DAILY_DRAWDOWN_LIMIT: float = float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.03"))
    PROFIT_TARGET_RR: float = float(os.getenv("PROFIT_TARGET_RR", "2.0")) # 2:1 Reward to Risk ratio
    MIN_CASH_BUFFER_PCT: float = 0.10 # Keep at least 10% in cash

    # Dynamic Universe Screening (In-play & High-Alpha Selection)
    ENABLE_DYNAMIC_UNIVERSE_SCREENER: bool = os.getenv("ENABLE_DYNAMIC_UNIVERSE_SCREENER", "True").lower() in ("true", "1", "yes")
    SCREENER_MIN_RVOL: float = float(os.getenv("SCREENER_MIN_RVOL", "1.1"))
    SCREENER_MIN_ATR_PCT: float = float(os.getenv("SCREENER_MIN_ATR_PCT", "0.012"))

    # Sector & Correlation Exposure Control
    MAX_POSITIONS_PER_SECTOR: int = int(os.getenv("MAX_POSITIONS_PER_SECTOR", "2"))
    MAX_PORTFOLIO_CORRELATION: float = float(os.getenv("MAX_PORTFOLIO_CORRELATION", "0.75"))
    SECTOR_MAP: dict = {
        "SPY": "INDEX",
        "QQQ": "TECH_INDEX",
        "AAPL": "TECH",
        "MSFT": "TECH",
        "NVDA": "SEMIS",
        "AMD": "SEMIS",
        "TSLA": "AUTO_TECH",
        "SH": "INVERSE_INDEX",
        "PSQ": "INVERSE_INDEX",
    }

    # Bear Market Monetization & Inverse ETFs
    ENABLE_INVERSE_ETFS: bool = os.getenv("ENABLE_INVERSE_ETFS", "True").lower() in ("true", "1", "yes")
    INVERSE_ETF_MAP: dict = {"SPY": "SH", "QQQ": "PSQ"}
    INVERSE_ETFS: list = ["SH", "PSQ"]

    # Event Risk & Earnings / Macro Blackout
    ENABLE_EARNINGS_FILTER: bool = os.getenv("ENABLE_EARNINGS_FILTER", "True").lower() in ("true", "1", "yes")
    EARNINGS_BLACKOUT_DAYS: int = int(os.getenv("EARNINGS_BLACKOUT_DAYS", "2"))
    ENABLE_MACRO_CALENDAR_FILTER: bool = os.getenv("ENABLE_MACRO_CALENDAR_FILTER", "True").lower() in ("true", "1", "yes")
    MACRO_BLACKOUT_MINUTES_BEFORE: int = int(os.getenv("MACRO_BLACKOUT_MINUTES_BEFORE", "30"))
    MACRO_BLACKOUT_MINUTES_AFTER: int = int(os.getenv("MACRO_BLACKOUT_MINUTES_AFTER", "30"))
    MAX_SPREAD_PCT: float = float(os.getenv("MAX_SPREAD_PCT", "0.0015")) # 0.15% max relative spread

    # Breaking-News / Event-Risk Gate
    #
    # Blocks entries when a symbol has material event risk: an important SEC
    # 8-K filing (bankruptcy, earnings results, delisting, auditor change...)
    # or a risk-keyword headline within the lookback window. The data comes
    # from free feeds (Yahoo headline RSS, SEC EDGAR). A feed outage logs a
    # warning and allows the trade rather than halting trading on a false
    # positive. Set NEWS_IMPORTANT_ONLY=False to block on any headline.
    ENABLE_NEWS_FILTER: bool = os.getenv("ENABLE_NEWS_FILTER", "True").lower() in ("true", "1", "yes")
    NEWS_BLACKOUT_HOURS: float = float(os.getenv("NEWS_BLACKOUT_HOURS", "24"))
    NEWS_IMPORTANT_ONLY: bool = os.getenv("NEWS_IMPORTANT_ONLY", "True").lower() in ("true", "1", "yes")
    # SEC EDGAR requires a User-Agent naming you ("Your Name you@example.com").
    # Without one the news gate reads headlines only and skips SEC filings.
    SEC_USER_AGENT: str = os.getenv("SEC_USER_AGENT", "").strip()

    # Congressional STOCK Act disclosure watch (advisory, off by default).
    # Logs fresh congressional trades in watchlist tickers. Needs pdftotext
    # (poppler) to map a filing to its tickers; never blocks a trade.
    ENABLE_CONGRESS_WATCH: bool = os.getenv("ENABLE_CONGRESS_WATCH", "False").lower() in ("true", "1", "yes")
    CONGRESS_SINCE_DAYS: int = int(os.getenv("CONGRESS_SINCE_DAYS", "45"))

    # Macro Market Trend Confluence Filter
    ENABLE_MACRO_TREND_FILTER: bool = os.getenv("ENABLE_MACRO_TREND_FILTER", "True").lower() in ("true", "1", "yes")
    MACRO_BENCHMARK_SYMBOLS: list = ["SPY", "QQQ"]

    # Partial Profit-Taking (Staged Exits / Scale-Outs)
    ENABLE_PARTIAL_PROFIT_TAKING: bool = os.getenv("ENABLE_PARTIAL_PROFIT_TAKING", "True").lower() in ("true", "1", "yes")
    SCALE_OUT_RR: float = float(os.getenv("SCALE_OUT_RR", "1.2"))
    SCALE_OUT_PERCENT: float = float(os.getenv("SCALE_OUT_PERCENT", "0.50"))

    # Self-Improvement & Parameter Tuning
    ENABLE_SELF_IMPROVEMENT: bool = os.getenv("ENABLE_SELF_IMPROVEMENT", "True").lower() in ("true", "1", "yes")
    OPTIMIZATION_INTERVAL_HOURS: int = int(os.getenv("OPTIMIZATION_INTERVAL_HOURS", "24"))

    # End-of-session reporting. The report is sent on the market-close
    # transition, so it fires once per trading session rather than once
    # per scan.
    ENABLE_SESSION_REPORT: bool = os.getenv("ENABLE_SESSION_REPORT", "True").lower() in ("true", "1", "yes")

    # The timeframe and window used to TUNE the strategy.
    #
    # These are deliberately separate from BAR_TIMEFRAME. Alpaca caps
    # intraday history hard (15-minute bars stop at ~1534 bars, about 83
    # days), and this strategy only trades a few times a month. Optimising
    # on a 60-day 15-minute window produced 4 trades in the held-out slice,
    # which is not enough to tell a good parameter set from a bad one --
    # the optimiser simply could not work.
    #
    # At 1-hour bars Alpaca serves ~2 years, which yields 20+ trades in the
    # training slice and 8+ in the held-out slice. The parameters are all
    # timeframe-agnostic ratios (EMA spans, RSI levels, ATR multiples), so
    # they transfer to the live bar size, and the ATR stop rescales itself.
    OPTIMIZATION_TIMEFRAME: str = os.getenv("OPTIMIZATION_TIMEFRAME", "1Hour")
    OPTIMIZATION_LOOKBACK_DAYS: int = int(os.getenv("OPTIMIZATION_LOOKBACK_DAYS", "400"))

    # Optimizer adoption gates.
    #
    # These were calibrated empirically: a rolling-origin walk-forward over
    # 311 (symbol, origin) pairs across seven liquid symbols, with a third
    # untouched forward slice measuring what actually happened after each
    # adoption.
    #
    # The finding was that a candidate's ABSOLUTE fitness does not reliably
    # predict whether it goes on to work. What did predict it was the SIZE
    # OF THE IMPROVEMENT over the parameters already in use. So adoption
    # needs three things, not one:
    #
    #   1. the candidate must be profitable out of sample (fitness >= 0),
    #   2. it must beat the baseline by a material margin, and
    #   3. it must be supported by enough out-of-sample trades to be more
    #      than noise.
    #
    # A high absolute fitness on its own is not enough: a candidate scoring
    # 9.0 loses to one scoring 5.0 if the baseline is already at 12.0.
    MIN_FITNESS_TO_ADOPT: float = float(os.getenv("MIN_FITNESS_TO_ADOPT", "0.0"))
    MIN_FITNESS_IMPROVEMENT: float = float(os.getenv("MIN_FITNESS_IMPROVEMENT", "0.25"))
    MIN_TRADES_TO_ADOPT: int = int(os.getenv("MIN_TRADES_TO_ADOPT", "3"))

    # Seed for the parameter search. Without it, every calibration run
    # explored a different random subset and the bot's parameters drifted
    # non-reproducibly.
    OPTIMIZER_SEED: int = int(os.getenv("OPTIMIZER_SEED", "42"))

    # The benchmark whose returns the optimizer scores alpha against, so the
    # search cannot mistake riding a bull market for edge.
    BENCHMARK_SYMBOL: str = os.getenv("BENCHMARK_SYMBOL", "SPY").strip().upper()

    # ===== SAFETY INFRASTRUCTURE (Vibe-Trading inspired) =====

    # Mandate Gate Configuration
    MANDATE_MAX_NOTIONAL_PER_ORDER: float = float(os.getenv("MANDATE_MAX_NOTIONAL_PER_ORDER", "50000.0"))
    MANDATE_MAX_NOTIONAL_PER_SYMBOL: float = float(os.getenv("MANDATE_MAX_NOTIONAL_PER_SYMBOL", "100000.0"))
    MANDATE_MAX_DAILY_NOTIONAL_USD: float = float(os.getenv("MANDATE_MAX_DAILY_NOTIONAL_USD", "500000.0"))
    MANDATE_MAX_LEVERAGE: float = float(os.getenv("MANDATE_MAX_LEVERAGE", "2.0"))
    MANDATE_MAX_DAILY_ORDERS: int = int(os.getenv("MANDATE_MAX_DAILY_ORDERS", "100"))
    MANDATE_MAX_DAILY_TRADES: int = int(os.getenv("MANDATE_MAX_DAILY_TRADES", "50"))
    MANDATE_MAX_POSITION_CONCENTRATION: float = float(os.getenv("MANDATE_MAX_POSITION_CONCENTRATION", "0.25"))
    MANDATE_MAX_SECTOR_CONCENTRATION: float = float(os.getenv("MANDATE_MAX_SECTOR_CONCENTRATION", "0.40"))
    MANDATE_MIN_ACCOUNT_EQUITY: float = float(os.getenv("MANDATE_MIN_ACCOUNT_EQUITY", "1000.0"))
    MANDATE_EXPECTED_ACCOUNT_MODE: str = os.getenv(
        "MANDATE_EXPECTED_ACCOUNT_MODE", "live" if ALPACA_LIVE else "paper"
    ).strip().lower()
    MANDATE_EXTRA_ALLOWED_SYMBOLS: str = os.getenv("MANDATE_EXTRA_ALLOWED_SYMBOLS", "").strip()
    MANDATE_DENIED_SYMBOLS: str = os.getenv("MANDATE_DENIED_SYMBOLS", "").strip()

    # Kill Switch Configuration
    KILL_SWITCH_SENTINEL_PATH: Path = BASE_DIR / "kill_switch.sentinel"
    KILL_SWITCH_STATE_PATH: Path = BASE_DIR / "kill_switch_state.json"

    # Advisory Configuration (opt-in)
    ADVISORY_ENABLED: bool = os.getenv("ADVISORY_ENABLED", "False").lower() in ("true", "1", "yes")
    ADVISORY_TIMEOUT_MS: float = float(os.getenv("ADVISORY_TIMEOUT_MS", "150.0"))

    # Paper/Live Boundary
    ENABLE_PAPER_LIVE_BOUNDARY: bool = os.getenv("ENABLE_PAPER_LIVE_BOUNDARY", "True").lower() in ("true", "1", "yes")

    # Simulator slippage and latency configuration (paper-trading realism)
    SIM_SLIPPAGE_ENABLED: bool = os.getenv("SIM_SLIPPAGE_ENABLED", "True").lower() in ("true", "1", "yes")
    SIM_EXIT_SLIPPAGE: bool = os.getenv("SIM_EXIT_SLIPPAGE", "False").lower() in ("true", "1", "yes")
    SIM_SLIPPAGE_BPS: float = float(os.getenv("SIM_SLIPPAGE_BPS", "5.0"))
    SIM_IMPACT_FACTOR: float = float(os.getenv("SIM_IMPACT_FACTOR", "0.1"))
    SIM_MIN_SLIPPAGE_BPS: float = float(os.getenv("SIM_MIN_SLIPPAGE_BPS", "0.5"))
    SIM_MAX_SLIPPAGE_BPS: float = float(os.getenv("SIM_MAX_SLIPPAGE_BPS", "150.0"))
    SIM_LATENCY_ENABLED: bool = os.getenv("SIM_LATENCY_ENABLED", "True").lower() in ("true", "1", "yes")
    SIM_LATENCY_MEAN_MS: float = float(os.getenv("SIM_LATENCY_MEAN_MS", "50.0"))
    SIM_LATENCY_SIGMA_MS: float = float(os.getenv("SIM_LATENCY_SIGMA_MS", "20.0"))
    SIM_LATENCY_FILL_MS: float = float(os.getenv("SIM_LATENCY_FILL_MS", "30.0"))

    # Trading costs charged on every simulated backtest / optimizer fill.
    # A backtest with no costs flatters a strategy whose edge per trade is a
    # few tenths of a percent. Slippage defaults to the paper simulator's base
    # figure so a backtest and a paper run are charged comparable costs.
    # Commission is per side. Alpaca charges $0 commission, but the sell side
    # carries SEC/FINRA regulatory fees (well under 1 bp) and other brokers
    # charge more, so 1 bp is a deliberately conservative default. Set the
    # commission values to 0 to model Alpaca alone.
    BACKTEST_SLIPPAGE_BPS: float = float(os.getenv("BACKTEST_SLIPPAGE_BPS", "5.0"))
    BACKTEST_COMMISSION_BPS: float = float(os.getenv("BACKTEST_COMMISSION_BPS", "1.0"))
    BACKTEST_COMMISSION_PER_SHARE: float = float(os.getenv("BACKTEST_COMMISSION_PER_SHARE", "0.0"))
    # Stop-market and signal exits pay slippage; take-profit limit fills do not.
    BACKTEST_EXIT_SLIPPAGE: bool = os.getenv("BACKTEST_EXIT_SLIPPAGE", "True").lower() in ("true", "1", "yes")

    # Order Execution Variety
    DEFAULT_ORDER_TYPE: str = os.getenv("DEFAULT_ORDER_TYPE", "market").strip().lower()
    TWAP_SLICES: int = int(os.getenv("TWAP_SLICES", "5"))
    ICEBERG_DISPLAY_PCT: float = float(os.getenv("ICEBERG_DISPLAY_PCT", "0.20"))

    # Data Quality Configuration
    DATA_QUALITY_ENABLED: bool = os.getenv("DATA_QUALITY_ENABLED", "True").lower() in ("true", "1", "yes")
    DATA_QUALITY_MAX_GAP_MULTIPLIER: float = float(os.getenv("DATA_QUALITY_MAX_GAP_MULTIPLIER", "3.0"))
    DATA_QUALITY_OUTLIER_ZSCORE_THRESHOLD: float = float(os.getenv("DATA_QUALITY_OUTLIER_ZSCORE_THRESHOLD", "4.0"))
    DATA_QUALITY_OUTLIER_LOOKBACK_WINDOW: int = int(os.getenv("DATA_QUALITY_OUTLIER_LOOKBACK_WINDOW", "50"))
    DATA_QUALITY_MAX_STALE_BARS: int = int(os.getenv("DATA_QUALITY_MAX_STALE_BARS", "3"))
    DATA_QUALITY_MAX_STALENESS_SECONDS: int = int(os.getenv("DATA_QUALITY_MAX_STALENESS_SECONDS", "3600"))
    DATA_QUALITY_VOLUME_SPIKE_MULTIPLIER: float = float(os.getenv("DATA_QUALITY_VOLUME_SPIKE_MULTIPLIER", "10.0"))
    DATA_QUALITY_VOLUME_LOOKBACK: int = int(os.getenv("DATA_QUALITY_VOLUME_LOOKBACK", "20"))
    DATA_QUALITY_MIN_BARS_FOR_GAP_DETECTION: int = int(os.getenv("DATA_QUALITY_MIN_BARS_FOR_GAP_DETECTION", "50"))
    DATA_QUALITY_MIN_BARS_FOR_OUTLIER_DETECTION: int = int(os.getenv("DATA_QUALITY_MIN_BARS_FOR_OUTLIER_DETECTION", "50"))

    # State Storage
    STRATEGY_STATE_FILE: Path = STATE_DIR / "strategy_state.json"
    TRADES_DB_PATH: Path = STATE_DIR / "trades.db"
    SIMULATOR_STATE_FILE: Path = STATE_DIR / "simulator_state.json"
    HEARTBEAT_PATH: Path = STATE_DIR / "bot_heartbeat.json"
    LOGS_DIR: Path = STATE_DIR / "logs"

    @classmethod
    def has_alpaca_credentials(cls) -> bool:
        placeholders = {"", "your_alpaca_api_key_here", "your_alpaca_secret_key_here"}
        return (
            cls.ALPACA_API_KEY not in placeholders
            and cls.ALPACA_SECRET_KEY not in placeholders
        )

    @staticmethod
    def _is_valid_ticker(symbol: str) -> bool:
        """Ticker validity check that tolerates class shares.

        Dots are legal and common: BRK.B, BF.B, RDS.A. The previous check
        used str.isalnum(), which silently rejected every class share.
        """
        if not isinstance(symbol, str) or not symbol:
            return False
        if not symbol[0].isalpha():
            return False
        return all(ch.isalnum() or ch == "." for ch in symbol)

    @classmethod
    def validate(cls) -> list[str]:
        """Validates configuration parameters and returns a list of error messages (if any)."""
        errors = []

        # Check credentials for live trading
        if cls.ALPACA_LIVE and (not cls.has_alpaca_credentials()):
            errors.append("Valid ALPACA_API_KEY and ALPACA_SECRET_KEY are required when ALPACA_LIVE is True")
        if cls.BOT_PROFILE == "secondary" and cls.ALPACA_LIVE:
            errors.append("secondary profile must use Alpaca paper mode")
        if cls.BOT_PROFILE == "secondary" and not cls.has_alpaca_credentials():
            errors.append("secondary profile requires Alpaca credentials")

        # Validate risk percentage (0 < risk <= 5%)
        if not (0 < cls.MAX_PORTFOLIO_RISK_PER_TRADE <= 0.05):
            errors.append(f"MAX_PORTFOLIO_RISK_PER_TRADE must be between 0 and 0.05 (got {cls.MAX_PORTFOLIO_RISK_PER_TRADE})")

        # Validate open positions (1 to 20)
        if not (1 <= cls.MAX_OPEN_POSITIONS <= 20):
            errors.append(f"MAX_OPEN_POSITIONS must be between 1 and 20 (got {cls.MAX_OPEN_POSITIONS})")

        # Validate drawdown limit (0 < limit <= 10%)
        if not (0 < cls.DAILY_DRAWDOWN_LIMIT <= 0.10):
            errors.append(f"DAILY_DRAWDOWN_LIMIT must be between 0 and 0.10 (got {cls.DAILY_DRAWDOWN_LIMIT})")

        # Validate profit target RR
        if cls.PROFIT_TARGET_RR < 1.0:
            errors.append(f"PROFIT_TARGET_RR must be >= 1.0 (got {cls.PROFIT_TARGET_RR})")

        # Validate cash buffer pct
        if not (0.0 <= cls.MIN_CASH_BUFFER_PCT < 1.0):
            errors.append(f"MIN_CASH_BUFFER_PCT must be between 0.0 and 1.0 (got {cls.MIN_CASH_BUFFER_PCT})")

        # Validate sector cap
        if cls.MAX_POSITIONS_PER_SECTOR < 1:
            errors.append(f"MAX_POSITIONS_PER_SECTOR must be >= 1 (got {cls.MAX_POSITIONS_PER_SECTOR})")

        # Validate earnings blackout days
        if cls.EARNINGS_BLACKOUT_DAYS < 0:
            errors.append(f"EARNINGS_BLACKOUT_DAYS must be >= 0 (got {cls.EARNINGS_BLACKOUT_DAYS})")

        # Validate news gate
        if cls.NEWS_BLACKOUT_HOURS < 0:
            errors.append(f"NEWS_BLACKOUT_HOURS must be >= 0 (got {cls.NEWS_BLACKOUT_HOURS})")

        if cls.CONGRESS_SINCE_DAYS < 1:
            errors.append(f"CONGRESS_SINCE_DAYS must be >= 1 (got {cls.CONGRESS_SINCE_DAYS})")

        # Validate partial profit taking
        if not (0.0 < cls.SCALE_OUT_PERCENT < 1.0):
            errors.append(f"SCALE_OUT_PERCENT must be between 0 and 1 (got {cls.SCALE_OUT_PERCENT})")

        if cls.SCALE_OUT_RR < 0.5:
            errors.append(f"SCALE_OUT_RR must be >= 0.5 (got {cls.SCALE_OUT_RR})")

        # Validate watchlist
        if not cls.WATCHLIST or not all(cls._is_valid_ticker(s) for s in cls.WATCHLIST):
            errors.append("WATCHLIST must contain at least one valid ticker symbol")

        # Validate timeframe
        if cls.BAR_TIMEFRAME not in cls.VALID_TIMEFRAMES:
            errors.append(
                f"BAR_TIMEFRAME '{cls.BAR_TIMEFRAME}' is invalid. Must be one of {sorted(cls.VALID_TIMEFRAMES)}"
            )

        # Validate optimizer settings
        if cls.OPTIMIZATION_LOOKBACK_DAYS < 5:
            errors.append(f"OPTIMIZATION_LOOKBACK_DAYS must be >= 5 (got {cls.OPTIMIZATION_LOOKBACK_DAYS})")

        if cls.OPTIMIZATION_INTERVAL_HOURS < 1:
            errors.append(f"OPTIMIZATION_INTERVAL_HOURS must be >= 1 (got {cls.OPTIMIZATION_INTERVAL_HOURS})")

        # Validate data quality settings
        if cls.DATA_QUALITY_MAX_GAP_MULTIPLIER < 1:
            errors.append(f"DATA_QUALITY_MAX_GAP_MULTIPLIER must be >= 1 (got {cls.DATA_QUALITY_MAX_GAP_MULTIPLIER})")
        if cls.DATA_QUALITY_OUTLIER_ZSCORE_THRESHOLD < 1:
            errors.append(f"DATA_QUALITY_OUTLIER_ZSCORE_THRESHOLD must be >= 1 (got {cls.DATA_QUALITY_OUTLIER_ZSCORE_THRESHOLD})")
        if cls.DATA_QUALITY_MAX_STALE_BARS < 1:
            errors.append(f"DATA_QUALITY_MAX_STALE_BARS must be >= 1 (got {cls.DATA_QUALITY_MAX_STALE_BARS})")
        if cls.DATA_QUALITY_MAX_STALENESS_SECONDS < 60:
            errors.append(f"DATA_QUALITY_MAX_STALENESS_SECONDS must be >= 60 (got {cls.DATA_QUALITY_MAX_STALENESS_SECONDS})")
        if cls.DATA_QUALITY_VOLUME_SPIKE_MULTIPLIER < 1:
            errors.append(f"DATA_QUALITY_VOLUME_SPIKE_MULTIPLIER must be >= 1 (got {cls.DATA_QUALITY_VOLUME_SPIKE_MULTIPLIER})")

        # Validate order type settings
        valid_order_types = {"market", "limit", "stop_limit", "twap", "vwap", "iceberg"}
        if cls.DEFAULT_ORDER_TYPE not in valid_order_types:
            errors.append(f"DEFAULT_ORDER_TYPE must be one of {sorted(valid_order_types)} (got {cls.DEFAULT_ORDER_TYPE!r})")
        if cls.TWAP_SLICES < 1:
            errors.append(f"TWAP_SLICES must be >= 1 (got {cls.TWAP_SLICES})")
        if not (0.0 < cls.ICEBERG_DISPLAY_PCT <= 1.0):
            errors.append(f"ICEBERG_DISPLAY_PCT must be between 0.0 and 1.0 (got {cls.ICEBERG_DISPLAY_PCT})")

        # Validate simulator realism settings
        if cls.SIM_MIN_SLIPPAGE_BPS < 0:
            errors.append(f"SIM_MIN_SLIPPAGE_BPS must be >= 0 (got {cls.SIM_MIN_SLIPPAGE_BPS})")
        if cls.SIM_MAX_SLIPPAGE_BPS < cls.SIM_MIN_SLIPPAGE_BPS:
            errors.append(f"SIM_MAX_SLIPPAGE_BPS must be >= SIM_MIN_SLIPPAGE_BPS (got {cls.SIM_MAX_SLIPPAGE_BPS} < {cls.SIM_MIN_SLIPPAGE_BPS})")

        # Backtest trading costs cannot be negative (a negative cost would
        # reward trading and silently inflate every result).
        for name in ("BACKTEST_SLIPPAGE_BPS", "BACKTEST_COMMISSION_BPS", "BACKTEST_COMMISSION_PER_SHARE"):
            if getattr(cls, name) < 0:
                errors.append(f"{name} must be >= 0 (got {getattr(cls, name)})")

        # The optimisation timeframe must be one the data layer can serve.
        if cls.OPTIMIZATION_TIMEFRAME not in cls.VALID_TIMEFRAMES:
            errors.append(
                f"OPTIMIZATION_TIMEFRAME '{cls.OPTIMIZATION_TIMEFRAME}' is invalid. "
                f"Must be one of {sorted(cls.VALID_TIMEFRAMES)}"
            )

        # The adoption gates must be self-consistent.
        if cls.MIN_TRADES_TO_ADOPT < 1:
            errors.append(f"MIN_TRADES_TO_ADOPT must be >= 1 (got {cls.MIN_TRADES_TO_ADOPT})")

        if cls.MIN_FITNESS_IMPROVEMENT < 0:
            errors.append(
                f"MIN_FITNESS_IMPROVEMENT must be >= 0 (got {cls.MIN_FITNESS_IMPROVEMENT})"
            )

        return errors

    @classmethod
    def assert_valid(cls):
        """Raises ValueError if any configuration parameter fails validation."""
        errors = cls.validate()
        if errors:
            raise ValueError(f"Configuration validation failed:\n - " + "\n - ".join(errors))

# Ensure state and logs directories exist
Config.STATE_DIR.mkdir(parents=True, exist_ok=True)
Config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
