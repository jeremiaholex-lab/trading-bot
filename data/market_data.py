import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import pandas as pd
import yfinance as yf

from config import Config
from data.data_quality import (
    DataQualityConfig,
    DataQualityGate,
    QualityReport,
    QualityIssue,
    QualitySeverity,
    QualityCheck,
)

logger = logging.getLogger(__name__)


class MarketDataProvider:
    """Fetches real-time and historical bar data via Alpaca or yfinance fallback."""

    def __init__(self):
        self.alpaca_client = None
        self._quality_gate = DataQualityGate()
        self._quality_config = DataQualityConfig()
        self.latest_quality_reports: Dict[str, QualityReport] = {}

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

    def _check_data_quality(self, df: pd.DataFrame, symbol: str, timeframe: str) -> QualityReport:
        """Run data quality checks on the fetched dataframe."""
        if df.empty:
            report = QualityReport(symbol=symbol, timeframe=timeframe, total_rows=0)
            report.add_issue(QualityIssue(
                check=QualityCheck.GAP_DETECTION,
                severity=QualitySeverity.CRITICAL,
                message="Empty DataFrame returned"
            ))
            return report

        return self._quality_gate.run_all_checks(df, symbol, timeframe)

    def get_quality_report(self, symbol: str) -> Optional[QualityReport]:
        """Returns the most recent QualityReport for a symbol."""
        return self.latest_quality_reports.get(symbol)

    def _fetch_bars_raw(self, symbol: str, timeframe: str, days_back: int) -> pd.DataFrame:
        """Fetches raw bars without quality evaluation."""
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
                }
                if timeframe in unit_map:
                    amount, unit = unit_map[timeframe]
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
                logger.debug(f"Alpaca raw fetch failed for {symbol}: {e}. Falling back to yfinance.")

        return self._fetch_via_yfinance(symbol, timeframe, days_back)

    def _get_historical_bars_raw(self, symbol: str, timeframe: str, days_back: int) -> pd.DataFrame:
        """Alias for backward compatibility."""
        return self._fetch_bars_raw(symbol, timeframe, days_back)

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: str = "15Min",
        days_back: int = 60,
        quality_check: bool = True
    ) -> pd.DataFrame:
        """
        Fetch OHLCV DataFrame for a symbol.
        Standardizes columns to ['Open', 'High', 'Low', 'Close', 'Volume'].
        """
        df = self._fetch_bars_raw(symbol, timeframe, days_back)

        if quality_check and Config.DATA_QUALITY_ENABLED:
            report = self._check_data_quality(df, symbol, timeframe)
            self.latest_quality_reports[symbol] = report
            self._log_quality_report(report, symbol, timeframe)

        return df

    def _log_quality_report(self, report: QualityReport, symbol: str, timeframe: str):
        """Log quality report findings at appropriate levels."""
        if report.has_critical():
            logger.critical(
                f"Data quality CRITICAL for {symbol} ({timeframe}): score={report.quality_score:.2f} - "
                f"{len([i for i in report.issues if i.severity == QualitySeverity.CRITICAL])} critical issues"
            )
        elif report.has_warnings():
            logger.warning(
                f"Data quality WARNING for {symbol} ({timeframe}): score={report.quality_score:.2f} - "
                f"{len([i for i in report.issues if i.severity == QualitySeverity.WARNING])} warnings"
            )
        else:
            logger.debug(f"Data quality OK for {symbol} ({timeframe}): score={report.quality_score:.2f}")

    def _fetch_via_yfinance(self, symbol: str, timeframe: str, days_back: int) -> pd.DataFrame:
        """Fetch historical bars using Yahoo Finance."""
        yf_interval_map = {
            "1Min": "1m",
            "5Min": "5m",
            "15Min": "15m",
            "1Hour": "1h",
            "1Day": "1d",
        }
        if timeframe not in yf_interval_map:
            raise ValueError(
                f"Unsupported timeframe {timeframe!r}. "
                f"Supported: {sorted(yf_interval_map)}."
            )
        interval = yf_interval_map[timeframe]

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

            df = df.rename(columns={
                'Open': 'Open',
                'High': 'High',
                'Low': 'Low',
                'Close': 'Close',
                'Volume': 'Volume'
            })
            df.index = pd.to_datetime(df.index)
            return df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
        except Exception as e:
            logger.error(f"Error fetching yfinance data for {symbol}: {e}")
            return pd.DataFrame()

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Returns the most recent price for a symbol."""
        df = self.get_historical_bars(symbol, timeframe="15Min", days_back=2, quality_check=False)
        if not df.empty:
            return float(df['Close'].iloc[-1])
        return None
