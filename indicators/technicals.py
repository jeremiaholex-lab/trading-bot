import numpy as np
import pandas as pd

class TechnicalIndicators:
    """Computes technical indicators, VWAP, multi-timeframe trends, and market regime analytics on OHLCV data."""

    @staticmethod
    def add_all_indicators(
        df: pd.DataFrame,
        ema_fast: int = 9,
        ema_slow: int = 21,
        rsi_period: int = 14,
        atr_period: int = 14,
        bb_period: int = 20,
        bb_std: float = 2.0
    ) -> pd.DataFrame:
        """
        Enriches a DataFrame containing ['Open', 'High', 'Low', 'Close', 'Volume']
        with all necessary mathematical indicators, VWAP, and multi-timeframe alignment.
        """
        df = df.copy()

        # Ensure column names are standardized
        rename_map = {col: col.capitalize() for col in df.columns}
        df.rename(columns=rename_map, inplace=True)

        close = df['Close']
        high = df['High']
        low = df['Low']
        volume = df['Volume']

        # 1. Exponential Moving Averages
        df[f'EMA_{ema_fast}'] = close.ewm(span=ema_fast, adjust=False).mean()
        df[f'EMA_{ema_slow}'] = close.ewm(span=ema_slow, adjust=False).mean()
        df['EMA_50'] = close.ewm(span=50, adjust=False).mean()
        df['EMA_200'] = close.ewm(span=200, adjust=False).mean()

        # 2. RSI (Relative Strength Index)
        delta = close.diff()
        gain = (delta.where(delta > 0, 0.0)).rolling(window=rsi_period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=rsi_period).mean()
        rs = gain / (loss + 1e-9)
        df['RSI'] = 100 - (100 / (1 + rs))

        # 3. ATR (Average True Range) for volatility & stop loss sizing
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['ATR'] = tr.rolling(window=atr_period).mean()
        df['ATR_Pct'] = df['ATR'] / close

        # 4. Bollinger Bands
        bb_mid = close.rolling(window=bb_period).mean()
        bb_std_val = close.rolling(window=bb_period).std()
        df['BB_Mid'] = bb_mid
        df['BB_Upper'] = bb_mid + (bb_std * bb_std_val)
        df['BB_Lower'] = bb_mid - (bb_std * bb_std_val)
        df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / (bb_mid + 1e-9)

        # 5. MACD (12, 26, 9)
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        df['MACD'] = ema_12 - ema_26
        df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']

        # 6. Volume Moving Average & VWAP (Volume Weighted Average Price)
        df['Volume_SMA20'] = volume.rolling(window=20).mean()
        df['Volume_Ratio'] = volume / (df['Volume_SMA20'] + 1e-9)

        typical_price = (high + low + close) / 3.0
        pv = typical_price * volume
        df['VWAP'] = pv.cumsum() / (volume.cumsum() + 1e-9)

        # 7. Multi-Timeframe Trend Confirmation (Macro trend + Higher timeframe proxy)
        df['Macro_Trend'] = np.where(close > df['EMA_200'], 1, -1)
        df['MTF_Bullish'] = (close > df['EMA_50']) & (df['EMA_50'] > df['EMA_200'])

        # 8. Market Regime Identification
        df['Regime'] = TechnicalIndicators.detect_regime(df, ema_fast, ema_slow)

        return df

    @staticmethod
    def detect_regime(df: pd.DataFrame, ema_fast: int, ema_slow: int) -> pd.Series:
        """
        Classifies the prevailing market condition for each bar.
        - TRENDING_BULL: Fast EMA > Slow EMA, Price > 200 EMA (or 50 EMA), MACD > 0, MTF Bullish
        - TRENDING_BEAR: Fast EMA < Slow EMA, Price < 200 EMA (or 50 EMA), MACD < 0
        - HIGH_VOLATILITY: ATR % in top 90th percentile or BB Width spiking
        - RANGING: EMAs flat/intertwined, moderate ATR
        """
        fast = df[f'EMA_{ema_fast}']
        slow = df[f'EMA_{ema_slow}']
        close = df['Close']
        ema_50 = df['EMA_50']
        mtf_bull = df.get('MTF_Bullish', pd.Series(True, index=df.index))

        # Volatility spike threshold (rolling 90th percentile of ATR_Pct)
        atr_pct = df['ATR_Pct']
        rolling_atr_high = atr_pct.rolling(window=50, min_periods=10).quantile(0.90)
        is_high_vol = atr_pct > (rolling_atr_high * 1.25)

        is_bull = (fast > slow) & (close > ema_50) & (df['MACD'] > 0) & mtf_bull
        is_bear = (fast < slow) & (close < ema_50) & (df['MACD'] < 0)

        regime = pd.Series('RANGING', index=df.index)
        regime[is_bull] = 'TRENDING_BULL'
        regime[is_bear] = 'TRENDING_BEAR'
        regime[is_high_vol] = 'HIGH_VOLATILITY'

        return regime
