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
    # Alpaca API Credentials
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "").strip()
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "").strip()
    ALPACA_LIVE: bool = os.getenv("ALPACA_LIVE", "False").lower() in ("true", "1", "yes")

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

    # Risk Management
    MAX_PORTFOLIO_RISK_PER_TRADE: float = float(os.getenv("MAX_PORTFOLIO_RISK_PER_TRADE", "0.015"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
    DAILY_DRAWDOWN_LIMIT: float = float(os.getenv("DAILY_DRAWDOWN_LIMIT", "0.03"))
    PROFIT_TARGET_RR: float = float(os.getenv("PROFIT_TARGET_RR", "2.0")) # 2:1 Reward to Risk ratio
    MIN_CASH_BUFFER_PCT: float = 0.10 # Keep at least 10% in cash

    # Self-Improvement & Parameter Tuning
    ENABLE_SELF_IMPROVEMENT: bool = os.getenv("ENABLE_SELF_IMPROVEMENT", "True").lower() in ("true", "1", "yes")
    OPTIMIZATION_LOOKBACK_DAYS: int = int(os.getenv("OPTIMIZATION_LOOKBACK_DAYS", "60"))
    OPTIMIZATION_INTERVAL_HOURS: int = int(os.getenv("OPTIMIZATION_INTERVAL_HOURS", "24"))

    # State Storage
    STRATEGY_STATE_FILE: Path = BASE_DIR / "strategy_state.json"
    LOGS_DIR: Path = BASE_DIR / "logs"

    @classmethod
    def has_alpaca_credentials(cls) -> bool:
        return bool(
            cls.ALPACA_API_KEY
            and cls.ALPACA_SECRET_KEY
            and cls.ALPACA_API_KEY != "your_alpaca_api_key_here"
        )

# Ensure logs directory exists
Config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
