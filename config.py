import os
from pathlib import Path
from dotenv import load_dotenv

# Base Directory
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables
env_file = BASE_DIR / ".env"
if env_file.exists():
    load_dotenv(env_file)
else:
    # Also attempt loading from default environment
    load_dotenv()

class Config:
    """Central configuration for Trading Bot."""
    BASE_DIR: Path = BASE_DIR

    # Alpaca API Credentials - must come from the environment (.env), never hardcode them here.
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "").strip()
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "").strip()
    ALPACA_LIVE: bool = os.getenv("ALPACA_LIVE", "False").lower() in ("true", "1", "yes")

    # When credentials are configured but Alpaca rejects them, the bot refuses to
    # start rather than silently downgrading to the local simulator. A silent
    # downgrade looks exactly like "trading" while placing no orders at all.
    # Set to True only if you deliberately want simulator mode with bad keys.
    ALLOW_SIMULATOR_FALLBACK: bool = os.getenv("ALLOW_SIMULATOR_FALLBACK", "False").lower() in ("true", "1", "yes")

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

    # State Storage
    STRATEGY_STATE_FILE: Path = BASE_DIR / "strategy_state.json"
    LOGS_DIR: Path = BASE_DIR / "logs"

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

# Ensure logs directory exists
Config.LOGS_DIR.mkdir(parents=True, exist_ok=True)

