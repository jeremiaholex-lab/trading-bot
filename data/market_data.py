import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd
import yfinance as yf

from config import Config

logger = logging.getLogger(__name__)

class MarketDataProvider:
    """Fetches real-time and historical bar data via Alpaca or yfinance fallback."""

    def __init__(self):
        self.alpaca_client = None
        if Config.has_alpaca_credentials():
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                self.alpaca_client = StockHistoricalDataClient(
                    api_key=Config.ALPACA_API_KEY,
                    secret_key=Config.ALPACA_SECRET_KEY
                )
                logger.info("MarketDataProvider initialized with Alpaca Market Data Client.")
            except Exception as e:
                logger.warning(f"Could not initialize Alpaca Market Data Client: {e}. Falling back to yfinance.")

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str = "15m",
        days_back: int = 60
    ) -> pd.DataFrame:
        """
        Fetch OHLCV DataFrame for a symbol.
        Standardizes columns to ['Open', 'High', 'Low', 'Close', 'Volume'].
        """
        # Try Alpaca if configured
        if self.alpaca_client is not None:
            try:
                from alpaca.data.requests import StockBarsRequest
                from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

                unit_map = {
                    "1Min": (1, TimeFrameUnit.Minute),
                    "5Min": (5, TimeFrameUnit.Minute),
                    "15Min": (15, TimeFrameUnit.Minute),
                    "1Hour": (1, TimeFrameUnit.Hour),
                    "1Day": (1, TimeFrameUnit.Day),
                    "15m": (15, TimeFrameUnit.Minute),
                    "1d": (1, TimeFrameUnit.Day)
                }
                amount, unit = unit_map.get(timeframe, (15, TimeFrameUnit.Minute))
                tf = TimeFrame(amount, unit)

                end_dt = datetime.now(timezone.utc)
                start_dt = end_dt - timedelta(days=days_back)

                req = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=tf,
                    start=start_dt,
                    end=end_dt
                )
                bars = self.alpaca_client.get_stock_bars(req)
                if symbol in bars.data and len(bars.data[symbol]) > 0:
                    df = bars.df
                    if isinstance(df.index, pd.MultiIndex):
                        df = df.xs(symbol, level='symbol')
                    df = df.rename(columns={
                        'open': 'Open',
                        'high': 'High',
                        'low': 'Low',
                        'close': 'Close',
                        'volume': 'Volume'
                    })
                    return df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
            except Exception as e:
                logger.debug(f"Alpaca data fetch failed for {symbol}: {e}. Trying yfinance...")

        # Fallback to Yahoo Finance
        return self._fetch_via_yfinance(symbol, timeframe, days_back)

    def _fetch_via_yfinance(self, symbol: str, timeframe: str, days_back: int) -> pd.DataFrame:
        """Fetch historical bars using Yahoo Finance."""
        # Map common timeframes to yfinance intervals
        yf_interval_map = {
            "1Min": "1m",
            "5Min": "5m",
            "15Min": "15m",
            "15m": "15m",
            "1Hour": "1h",
            "1h": "1h",
            "1Day": "1d",
            "1d": "1d"
        }
        interval = yf_interval_map.get(timeframe, "15m")

        # Yahoo finance allows max 60 days for 15m/5m intervals
        if interval in ["1m", "5m", "15m"] and days_back > 59:
            period = "59d"
        else:
            period = f"{min(days_back, 720)}d"

        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval, auto_adjust=False)
            if df.empty:
                logger.warning(f"No yfinance data found for {symbol}")
                return pd.DataFrame()

            # Clean dataframe
            df = df.rename(columns={
                'Open': 'Open',
                'High': 'High',
                'Low': 'Low',
                'Close': 'Close',
                'Volume': 'Volume'
            })
            # Ensure index has no timezone or clean tz
            df.index = pd.to_datetime(df.index)
            return df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
        except Exception as e:
            logger.error(f"Error fetching yfinance data for {symbol}: {e}")
            return pd.DataFrame()

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Returns the most recent price for a symbol."""
        df = self.get_historical_bars(symbol, timeframe="15Min", days_back=2)
        if not df.empty:
            return float(df['Close'].iloc[-1])
        return None
