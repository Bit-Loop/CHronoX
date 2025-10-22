"""
Technical Indicators for ChronoX Trading Bot

Implements common technical analysis indicators:
- Moving Averages (SMA, EMA, WMA)
- Momentum indicators (RSI, MACD, Stochastic)
- Volatility indicators (Bollinger Bands, ATR, Standard Deviation)
- Volume indicators (OBV, Volume MA)
- Trend indicators (ADX, Aroon)

All functions operate on pandas DataFrames with OHLCV data.

References:
- Wilder, J.W. (1978) "New Concepts in Technical Trading Systems"
- Murphy, J.J. (1999) "Technical Analysis of the Financial Markets"
- Bollinger, J. (2002) "Bollinger on Bollinger Bands"
"""

import pandas as pd
import numpy as np
from typing import Optional, Tuple, Union
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# Moving Averages
# ============================================================================

def sma(series: pd.Series, period: int) -> pd.Series:
    """
    Simple Moving Average.
    
    Args:
        series: Price series (typically 'close')
        period: Number of periods
        
    Returns:
        SMA series
    """
    return series.rolling(window=period).mean()


def ema(series: pd.Series, period: int, adjust: bool = False) -> pd.Series:
    """
    Exponential Moving Average.
    
    Uses standard EMA formula: EMA = Price * k + EMA_prev * (1 - k)
    where k = 2 / (period + 1)
    
    Args:
        series: Price series (typically 'close')
        period: Number of periods
        adjust: If True, use adjusted exponential calculation
        
    Returns:
        EMA series
    """
    return series.ewm(span=period, adjust=adjust).mean()


def wma(series: pd.Series, period: int) -> pd.Series:
    """
    Weighted Moving Average.
    
    Gives more weight to recent prices.
    Weights: 1, 2, 3, ..., period
    
    Args:
        series: Price series
        period: Number of periods
        
    Returns:
        WMA series
    """
    weights = np.arange(1, period + 1)
    
    def weighted_avg(values):
        if len(values) < period:
            return np.nan
        return np.dot(values[-period:], weights) / weights.sum()
    
    return series.rolling(window=period).apply(weighted_avg, raw=True)


# ============================================================================
# Momentum Indicators
# ============================================================================

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (RSI).
    
    Measures momentum on scale of 0-100.
    - Above 70: Overbought
    - Below 30: Oversold
    
    Formula:
        RSI = 100 - (100 / (1 + RS))
        RS = Average Gain / Average Loss
    
    Args:
        series: Price series (typically 'close')
        period: Lookback period (default 14)
        
    Returns:
        RSI series (0-100)
        
    Reference:
        Wilder, J.W. (1978) "New Concepts in Technical Trading Systems"
    """
    # Calculate price changes
    delta = series.diff()
    
    # Separate gains and losses
    gains = delta.where(delta > 0, 0)
    losses = -delta.where(delta < 0, 0)
    
    # Calculate average gains and losses using Wilder's smoothing
    avg_gains = gains.ewm(alpha=1/period, adjust=False).mean()
    avg_losses = losses.ewm(alpha=1/period, adjust=False).mean()
    
    # Calculate RS and RSI
    rs = avg_gains / avg_losses
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def macd(
    series: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Moving Average Convergence Divergence (MACD).
    
    Trend-following momentum indicator.
    - MACD Line: Fast EMA - Slow EMA
    - Signal Line: EMA of MACD Line
    - Histogram: MACD Line - Signal Line
    
    Signals:
    - MACD crosses above signal: Bullish
    - MACD crosses below signal: Bearish
    
    Args:
        series: Price series (typically 'close')
        fast_period: Fast EMA period (default 12)
        slow_period: Slow EMA period (default 26)
        signal_period: Signal EMA period (default 9)
        
    Returns:
        Tuple of (macd_line, signal_line, histogram)
    """
    fast_ema = ema(series, fast_period)
    slow_ema = ema(series, slow_period)
    
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    
    return macd_line, signal_line, histogram


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int = 14,
    d_period: int = 3
) -> Tuple[pd.Series, pd.Series]:
    """
    Stochastic Oscillator (%K and %D).
    
    Compares closing price to price range over period.
    Range: 0-100
    - Above 80: Overbought
    - Below 20: Oversold
    
    Formula:
        %K = 100 * (Close - Low_n) / (High_n - Low_n)
        %D = SMA(%K, d_period)
    
    Args:
        high: High prices
        low: Low prices
        close: Close prices
        k_period: Lookback period for %K
        d_period: Smoothing period for %D
        
    Returns:
        Tuple of (%K, %D)
    """
    # Calculate %K
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()
    
    k = 100 * (close - lowest_low) / (highest_high - lowest_low)
    
    # Calculate %D (smoothed %K)
    d = k.rolling(window=d_period).mean()
    
    return k, d


# ============================================================================
# Volatility Indicators
# ============================================================================

def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands.
    
    Volatility bands placed above and below moving average.
    - Middle Band: SMA
    - Upper Band: SMA + (std * num_std)
    - Lower Band: SMA - (std * num_std)
    
    Price touching bands suggests overbought/oversold conditions.
    Band squeeze suggests low volatility (potential breakout).
    
    Args:
        series: Price series (typically 'close')
        period: Moving average period (default 20)
        num_std: Number of standard deviations (default 2.0)
        
    Returns:
        Tuple of (upper_band, middle_band, lower_band)
        
    Reference:
        Bollinger, J. (2002) "Bollinger on Bollinger Bands"
    """
    middle_band = sma(series, period)
    std_dev = series.rolling(window=period).std()
    
    upper_band = middle_band + (std_dev * num_std)
    lower_band = middle_band - (std_dev * num_std)
    
    return upper_band, middle_band, lower_band


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14
) -> pd.Series:
    """
    Average True Range (ATR).
    
    Measures market volatility using true range.
    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    
    Higher ATR = Higher volatility
    
    Args:
        high: High prices
        low: Low prices
        close: Close prices
        period: Smoothing period (default 14)
        
    Returns:
        ATR series
        
    Reference:
        Wilder, J.W. (1978) "New Concepts in Technical Trading Systems"
    """
    # Calculate True Range
    prev_close = close.shift(1)
    
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Calculate ATR using Wilder's smoothing
    atr = true_range.ewm(alpha=1/period, adjust=False).mean()
    
    return atr


def standard_deviation(series: pd.Series, period: int = 20) -> pd.Series:
    """
    Rolling standard deviation of price.
    
    Measures price volatility.
    
    Args:
        series: Price series
        period: Lookback period
        
    Returns:
        Standard deviation series
    """
    return series.rolling(window=period).std()


# ============================================================================
# Volume Indicators
# ============================================================================

def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    On-Balance Volume (OBV).
    
    Cumulative volume indicator:
    - If close > prev_close: OBV += volume
    - If close < prev_close: OBV -= volume
    - If close == prev_close: OBV unchanged
    
    Measures buying/selling pressure.
    
    Args:
        close: Close prices
        volume: Volume
        
    Returns:
        OBV series
    """
    price_change = close.diff()
    
    # Determine volume direction
    volume_direction = np.sign(price_change)
    volume_direction[volume_direction == 0] = 1  # Neutral = positive
    
    # Calculate OBV
    obv = (volume * volume_direction).cumsum()
    
    return obv


def volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    """
    Volume moving average.
    
    Used to identify unusual volume spikes.
    
    Args:
        volume: Volume series
        period: Averaging period
        
    Returns:
        Volume SMA
    """
    return sma(volume, period)


# ============================================================================
# Trend Indicators
# ============================================================================

def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Average Directional Index (ADX).
    
    Measures trend strength (not direction).
    - ADX > 25: Strong trend
    - ADX < 20: Weak/no trend
    
    Also returns +DI and -DI for direction:
    - +DI > -DI: Uptrend
    - -DI > +DI: Downtrend
    
    Args:
        high: High prices
        low: Low prices
        close: Close prices
        period: Smoothing period (default 14)
        
    Returns:
        Tuple of (adx, plus_di, minus_di)
        
    Reference:
        Wilder, J.W. (1978) "New Concepts in Technical Trading Systems"
    """
    # Calculate True Range
    atr_value = atr(high, low, close, period)
    
    # Calculate Directional Movement
    high_diff = high.diff()
    low_diff = -low.diff()
    
    plus_dm = high_diff.where((high_diff > low_diff) & (high_diff > 0), 0)
    minus_dm = low_diff.where((low_diff > high_diff) & (low_diff > 0), 0)
    
    # Smooth DM values
    plus_dm_smooth = plus_dm.ewm(alpha=1/period, adjust=False).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=1/period, adjust=False).mean()
    
    # Calculate Directional Indicators
    plus_di = 100 * plus_dm_smooth / atr_value
    minus_di = 100 * minus_dm_smooth / atr_value
    
    # Calculate DX and ADX
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    
    return adx, plus_di, minus_di


# ============================================================================
# Utility Functions
# ============================================================================

def add_all_indicators(
    df: pd.DataFrame,
    include_volume: bool = True
) -> pd.DataFrame:
    """
    Add all technical indicators to OHLCV DataFrame.
    
    Args:
        df: DataFrame with columns: open, high, low, close, volume
        include_volume: Include volume-based indicators
        
    Returns:
        DataFrame with additional indicator columns
    """
    df = df.copy()
    
    # Moving Averages
    for period in [7, 20, 50, 200]:
        df[f'sma_{period}'] = sma(df['close'], period)
        df[f'ema_{period}'] = ema(df['close'], period)
    
    # RSI
    df['rsi_14'] = rsi(df['close'], 14)
    
    # MACD
    macd_line, signal_line, histogram = macd(df['close'])
    df['macd'] = macd_line
    df['macd_signal'] = signal_line
    df['macd_hist'] = histogram
    
    # Bollinger Bands
    upper, middle, lower = bollinger_bands(df['close'])
    df['bb_upper'] = upper
    df['bb_middle'] = middle
    df['bb_lower'] = lower
    df['bb_width'] = (upper - lower) / middle  # Normalized width
    
    # ATR
    df['atr_14'] = atr(df['high'], df['low'], df['close'], 14)
    
    # Stochastic
    k, d = stochastic(df['high'], df['low'], df['close'])
    df['stoch_k'] = k
    df['stoch_d'] = d
    
    # ADX
    adx_val, plus_di, minus_di = adx(df['high'], df['low'], df['close'])
    df['adx'] = adx_val
    df['plus_di'] = plus_di
    df['minus_di'] = minus_di
    
    # Volume indicators
    if include_volume and 'volume' in df.columns:
        df['obv'] = obv(df['close'], df['volume'])
        df['volume_sma_20'] = volume_sma(df['volume'], 20)
        df['volume_ratio'] = df['volume'] / df['volume_sma_20']
    
    logger.info(f"Added {len(df.columns) - 6} technical indicators")
    
    return df


# Example usage
if __name__ == "__main__":
    # Create sample data
    np.random.seed(42)
    dates = pd.date_range('2024-01-01', periods=100, freq='D')
    
    df = pd.DataFrame({
        'timestamp': dates,
        'open': 100 + np.cumsum(np.random.randn(100)),
        'high': 102 + np.cumsum(np.random.randn(100)),
        'low': 98 + np.cumsum(np.random.randn(100)),
        'close': 100 + np.cumsum(np.random.randn(100)),
        'volume': np.random.randint(1000000, 5000000, 100)
    })
    
    # Ensure high >= low
    df['high'] = df[['open', 'high', 'close']].max(axis=1)
    df['low'] = df[['open', 'low', 'close']].min(axis=1)
    
    # Add all indicators
    df_with_indicators = add_all_indicators(df)
    
    print("Sample OHLCV with indicators:")
    print(df_with_indicators.tail())
    print(f"\nTotal columns: {len(df_with_indicators.columns)}")
    print(f"Indicator columns: {len(df_with_indicators.columns) - 6}")
