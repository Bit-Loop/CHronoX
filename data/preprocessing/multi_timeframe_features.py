"""
Multi-Timeframe Feature Integration

Implements feature fusion across multiple timeframes for improved trading signals.

Combines technical indicators, price action, and volume data from different
timeframes (5-min, 15-min, 1-hour, 12-hour, daily) into unified feature vectors
for machine learning models.

Key Concepts:
- **Feature Alignment**: Synchronize data across different timeframes
- **Hierarchical Features**: Create multi-scale representations
- **Fibonacci Levels**: Calculate retracement/extension levels per timeframe
- **Cross-Timeframe Validation**: Confirm signals across multiple scales

Architecture:
    Raw OHLCV Data (Multiple Timeframes)
            ↓
    Technical Indicators (RSI, MACD, BB, etc.) per timeframe
            ↓
    Fibonacci Levels per timeframe
            ↓
    Feature Alignment & Synchronization
            ↓
    Hierarchical Feature Vector
            ↓
    ML Model Input

References:
- Multi-Scale Analysis: https://en.wikipedia.org/wiki/Multiscale_modeling
- Technical Analysis: Murphy (1999) "Technical Analysis of the Financial Markets"
- Fibonacci Trading: https://en.wikipedia.org/wiki/Fibonacci_retracement
"""

import logging
from typing import List, Dict, Optional, Tuple, Any, Union, TypedDict
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class FibonacciLevelsResult(TypedDict):
    """Type-safe structure for Fibonacci levels."""
    swing_high: float
    swing_low: float
    retracements: Dict[float, float]
    extensions: Dict[float, float]


class TechnicalIndicators:
    """
    Calculate common technical indicators for financial data.
    
    Indicators include:
    - Moving Averages (SMA, EMA)
    - RSI (Relative Strength Index)
    - MACD (Moving Average Convergence Divergence)
    - Bollinger Bands
    - ATR (Average True Range)
    - Stochastic Oscillator
    - Volume indicators
    """
    
    @staticmethod
    def sma(series: pd.Series, period: int) -> pd.Series:
        """Simple Moving Average."""
        return series.rolling(window=period).mean()
    
    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        """Exponential Moving Average."""
        return series.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        """
        Relative Strength Index (RSI).
        
        RSI = 100 - (100 / (1 + RS))
        where RS = Average Gain / Average Loss
        
        Args:
            series: Price series (typically close)
            period: Lookback period (default: 14)
        
        Returns:
            RSI values (0-100)
        """
        delta = series.diff()
        # Type ignore for pandas comparison operators
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()  # type: ignore
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()  # type: ignore
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def macd(
        series: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        MACD (Moving Average Convergence Divergence).
        
        MACD Line = EMA(fast) - EMA(slow)
        Signal Line = EMA(MACD, signal)
        Histogram = MACD - Signal
        
        Args:
            series: Price series
            fast: Fast EMA period (default: 12)
            slow: Slow EMA period (default: 26)
            signal: Signal line period (default: 9)
        
        Returns:
            macd_line, signal_line, histogram
        """
        ema_fast = TechnicalIndicators.ema(series, fast)
        ema_slow = TechnicalIndicators.ema(series, slow)
        
        macd_line = ema_fast - ema_slow
        signal_line = TechnicalIndicators.ema(macd_line, signal)
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram
    
    @staticmethod
    def bollinger_bands(
        series: pd.Series,
        period: int = 20,
        std_dev: float = 2.0
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Bollinger Bands.
        
        Middle Band = SMA(period)
        Upper Band = Middle Band + (std_dev × std)
        Lower Band = Middle Band - (std_dev × std)
        
        Args:
            series: Price series
            period: SMA period (default: 20)
            std_dev: Number of standard deviations (default: 2.0)
        
        Returns:
            upper_band, middle_band, lower_band
        """
        middle = TechnicalIndicators.sma(series, period)
        std = series.rolling(window=period).std()
        
        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)
        
        return upper, middle, lower
    
    @staticmethod
    def atr(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:
        """
        Average True Range (ATR).
        
        True Range = max(high - low, |high - prev_close|, |low - prev_close|)
        ATR = EMA(True Range, period)
        
        Args:
            high: High prices
            low: Low prices
            close: Close prices
            period: ATR period (default: 14)
        
        Returns:
            ATR values
        """
        prev_close = close.shift(1)
        
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = true_range.ewm(span=period, adjust=False).mean()
        
        return atr
    
    @staticmethod
    def stochastic(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14,
        smooth_k: int = 3,
        smooth_d: int = 3
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Stochastic Oscillator.
        
        %K = 100 × (Close - Lowest Low) / (Highest High - Lowest Low)
        %D = SMA(%K, smooth_d)
        
        Args:
            high: High prices
            low: Low prices
            close: Close prices
            period: Lookback period (default: 14)
            smooth_k: %K smoothing (default: 3)
            smooth_d: %D smoothing (default: 3)
        
        Returns:
            k_line, d_line
        """
        lowest_low = low.rolling(window=period).min()
        highest_high = high.rolling(window=period).max()
        
        k_raw = 100 * (close - lowest_low) / (highest_high - lowest_low)
        k_line = k_raw.rolling(window=smooth_k).mean()
        d_line = k_line.rolling(window=smooth_d).mean()
        
        return k_line, d_line


class FibonacciLevels:
    """
    Calculate Fibonacci retracement and extension levels.
    
    Fibonacci ratios are derived from the Fibonacci sequence and are widely
    used in technical analysis to identify potential support/resistance levels.
    
    Key Ratios:
    - Retracement: 0.236, 0.382, 0.500, 0.618, 0.786
    - Extension: 1.272, 1.414, 1.618, 2.000, 2.618
    
    Reference: https://en.wikipedia.org/wiki/Fibonacci_retracement
    """
    
    # Standard Fibonacci ratios
    RETRACEMENT_LEVELS = [0.0, 0.236, 0.382, 0.500, 0.618, 0.786, 1.0]
    EXTENSION_LEVELS = [1.272, 1.414, 1.618, 2.000, 2.618]
    
    @staticmethod
    def calculate_retracements(
        swing_high: float,
        swing_low: float
    ) -> Dict[float, float]:
        """
        Calculate Fibonacci retracement levels.
        
        Args:
            swing_high: Recent swing high price
            swing_low: Recent swing low price
        
        Returns:
            Dictionary mapping ratio -> price level
        
        Example:
            levels = FibonacciLevels.calculate_retracements(150, 100)
            # {0.0: 150, 0.236: 138.2, 0.382: 130.9, ..., 1.0: 100}
        """
        diff = swing_high - swing_low
        
        levels = {}
        for ratio in FibonacciLevels.RETRACEMENT_LEVELS:
            levels[ratio] = swing_high - (diff * ratio)
        
        return levels
    
    @staticmethod
    def calculate_extensions(
        swing_high: float,
        swing_low: float
    ) -> Dict[float, float]:
        """
        Calculate Fibonacci extension levels.
        
        Args:
            swing_high: Recent swing high price
            swing_low: Recent swing low price
        
        Returns:
            Dictionary mapping ratio -> price level
        """
        diff = swing_high - swing_low
        
        levels = {}
        for ratio in FibonacciLevels.EXTENSION_LEVELS:
            levels[ratio] = swing_high + (diff * (ratio - 1.0))
        
        return levels
    
    @staticmethod
    def find_swing_points(
        high: pd.Series,
        low: pd.Series,
        lookback: int = 20
    ) -> Tuple[float, float]:
        """
        Find recent swing high and swing low.
        
        Args:
            high: High price series
            low: Low price series
            lookback: Number of periods to look back
        
        Returns:
            (swing_high, swing_low)
        """
        recent_high = high.iloc[-lookback:].max()
        recent_low = low.iloc[-lookback:].min()
        
        return recent_high, recent_low
    
    @staticmethod
    def calculate_levels_from_series(
        high: pd.Series,
        low: pd.Series,
        lookback: int = 20
    ) -> FibonacciLevelsResult:
        """
        Calculate both retracement and extension levels from price series.
        
        Args:
            high: High price series
            low: Low price series
            lookback: Lookback period for swing points
        
        Returns:
            FibonacciLevelsResult with swing points, retracements, and extensions
        """
        swing_high, swing_low = FibonacciLevels.find_swing_points(high, low, lookback)
        
        return FibonacciLevelsResult(
            swing_high=swing_high,
            swing_low=swing_low,
            retracements=FibonacciLevels.calculate_retracements(swing_high, swing_low),
            extensions=FibonacciLevels.calculate_extensions(swing_high, swing_low)
        )


class MultiTimeframeFeatureGenerator:
    """
    Generate unified feature vectors from multiple timeframes.
    
    Combines technical indicators, price action, and Fibonacci levels across
    different timeframes into a hierarchical feature representation for ML models.
    
    Features per timeframe:
    - Price features: open, high, low, close, volume, VWAP
    - Moving averages: SMA(5,10,20,50,200), EMA(12,26)
    - Momentum: RSI(14), MACD, Stochastic
    - Volatility: ATR(14), Bollinger Bands width
    - Volume: Volume SMA ratio, volume trend
    - Fibonacci: Distance to nearest levels
    
    Example:
        generator = MultiTimeframeFeatureGenerator(
            timeframes=['5min', '15min', '1hour', '12hour']
        )
        
        # Data dict with DataFrames for each timeframe
        data = {
            '5min': df_5min,
            '15min': df_15min,
            '1hour': df_1hour,
            '12hour': df_12hour
        }
        
        features = generator.generate_features(data, timestamp='2024-01-15 10:00:00')
        # Returns aligned feature vector across all timeframes
    """
    
    def __init__(
        self,
        timeframes: Optional[List[str]] = None,
        include_fibonacci: bool = True,
        fibonacci_lookback: int = 20
    ):
        """
        Initialize multi-timeframe feature generator.
        
        Args:
            timeframes: List of timeframes to use (default: ['5min', '15min', '1hour', '12hour'])
            include_fibonacci: Whether to include Fibonacci levels (default: True)
            fibonacci_lookback: Lookback period for Fibonacci calculations (default: 20)
        """
        self.timeframes = timeframes or ['5min', '15min', '1hour', '12hour']
        self.include_fibonacci = include_fibonacci
        self.fibonacci_lookback = fibonacci_lookback
        
        self.indicators = TechnicalIndicators()
        self.fibonacci = FibonacciLevels()
        
        logger.info(f"Initialized MultiTimeframeFeatureGenerator for timeframes: {self.timeframes}")
    
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate all technical indicators for a single timeframe.
        
        Args:
            df: DataFrame with columns: open, high, low, close, volume
        
        Returns:
            DataFrame with additional indicator columns
        """
        df = df.copy()
        
        # Moving averages
        for period in [5, 10, 20, 50, 200]:
            df[f'sma_{period}'] = self.indicators.sma(df['close'], period)
            df[f'ema_{period}'] = self.indicators.ema(df['close'], period)
        
        # RSI
        df['rsi_14'] = self.indicators.rsi(df['close'], 14)
        
        # MACD
        macd_line, signal_line, histogram = self.indicators.macd(df['close'])
        df['macd'] = macd_line
        df['macd_signal'] = signal_line
        df['macd_histogram'] = histogram
        
        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = self.indicators.bollinger_bands(df['close'])
        df['bb_upper'] = bb_upper
        df['bb_middle'] = bb_middle
        df['bb_lower'] = bb_lower
        df['bb_width'] = (bb_upper - bb_lower) / bb_middle  # Normalized width
        df['bb_position'] = (df['close'] - bb_lower) / (bb_upper - bb_lower)  # Price position [0,1]
        
        # ATR
        df['atr_14'] = self.indicators.atr(df['high'], df['low'], df['close'], 14)
        
        # Stochastic
        stoch_k, stoch_d = self.indicators.stochastic(df['high'], df['low'], df['close'])
        df['stoch_k'] = stoch_k
        df['stoch_d'] = stoch_d
        
        # Volume indicators
        df['volume_sma_20'] = self.indicators.sma(df['volume'], 20)
        df['volume_ratio'] = df['volume'] / df['volume_sma_20']
        
        # Price momentum
        df['returns_1'] = df['close'].pct_change(1)
        df['returns_5'] = df['close'].pct_change(5)
        df['returns_20'] = df['close'].pct_change(20)
        
        return df
    
    def calculate_fibonacci_features(
        self,
        df: pd.DataFrame,
        current_price: float
    ) -> Dict[str, float]:
        """
        Calculate Fibonacci-based features.
        
        Args:
            df: DataFrame with price data
            current_price: Current price for distance calculation
        
        Returns:
            Dictionary of Fibonacci features
        """
        fib_levels = self.fibonacci.calculate_levels_from_series(
            df['high'],
            df['low'],
            lookback=self.fibonacci_lookback
        )
        
        # Extract values with proper typing
        swing_high = float(fib_levels['swing_high'])
        swing_low = float(fib_levels['swing_low'])
        retracements = fib_levels['retracements']
        
        # Ensure retracements is a dict
        if not isinstance(retracements, dict):
            retracements = {}
        
        # Find nearest retracement level
        distances = {ratio: abs(current_price - price) for ratio, price in retracements.items()}
        
        if distances:
            nearest_ratio = min(distances.keys(), key=lambda k: distances[k])
            nearest_level = retracements[nearest_ratio]
        else:
            nearest_ratio = 0.5
            nearest_level = current_price
        
        features = {
            'fib_swing_high': swing_high,
            'fib_swing_low': swing_low,
            'fib_range': swing_high - swing_low,
            'fib_nearest_ratio': nearest_ratio,
            'fib_nearest_level': nearest_level,
            'fib_distance_to_nearest': (current_price - nearest_level) / current_price if current_price != 0 else 0.0,
            'fib_position_in_range': (
                (current_price - swing_low) / (swing_high - swing_low)
            ) if swing_high != swing_low else 0.5
        }
        
        return features
    
    def align_timeframes(
        self,
        data: Dict[str, pd.DataFrame],
        reference_timestamp: str
    ) -> Dict[str, pd.Series]:
        """
        Align data from multiple timeframes to a reference timestamp.
        
        Args:
            data: Dict mapping timeframe -> DataFrame
            reference_timestamp: Timestamp to align to (ISO format)
        
        Returns:
            Dict mapping timeframe -> aligned row
        """
        aligned = {}
        ref_time = pd.to_datetime(reference_timestamp)
        
        for timeframe, df in data.items():
            # Ensure time column is datetime
            if 'time' in df.columns:
                df['time'] = pd.to_datetime(df['time'])
                df = df.set_index('time')
            
            # Find closest timestamp <= reference_timestamp
            valid_times = df.index[df.index <= ref_time]
            
            if len(valid_times) > 0:
                closest_time = valid_times[-1]
                aligned[timeframe] = df.loc[closest_time]
            else:
                logger.warning(f"No data found for {timeframe} before {reference_timestamp}")
                aligned[timeframe] = None
        
        return aligned
    
    def generate_features(
        self,
        data: Dict[str, pd.DataFrame],
        timestamp: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate unified feature vector from multiple timeframes.
        
        Args:
            data: Dict mapping timeframe -> DataFrame with OHLCV data
            timestamp: Reference timestamp (uses latest if None)
        
        Returns:
            Dictionary with features from all timeframes
        """
        # Calculate indicators for each timeframe
        data_with_indicators = {}
        for timeframe, df in data.items():
            if df is not None and len(df) > 0:
                data_with_indicators[timeframe] = self.calculate_indicators(df)
            else:
                data_with_indicators[timeframe] = None
        
        # Determine reference timestamp
        if timestamp is None:
            # Use latest timestamp from shortest timeframe
            shortest_tf = self.timeframes[0]
            if shortest_tf in data_with_indicators and data_with_indicators[shortest_tf] is not None:
                timestamp = str(data_with_indicators[shortest_tf].index[-1])
            else:
                raise ValueError("No valid data to determine timestamp")
        
        # Align all timeframes
        aligned = self.align_timeframes(data_with_indicators, timestamp)
        
        # Extract features per timeframe
        features = {
            'timestamp': timestamp,
            'features': {}
        }
        
        for timeframe in self.timeframes:
            if aligned.get(timeframe) is None:
                logger.warning(f"Skipping {timeframe} - no aligned data")
                continue
            
            row = aligned[timeframe]
            tf_features = {}
            
            # Basic OHLCV
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in row:
                    tf_features[col] = float(row[col])
            
            # Indicators
            indicator_cols = [
                'sma_5', 'sma_10', 'sma_20', 'sma_50', 'sma_200',
                'ema_5', 'ema_10', 'ema_20',
                'rsi_14', 'macd', 'macd_signal', 'macd_histogram',
                'bb_upper', 'bb_middle', 'bb_lower', 'bb_width', 'bb_position',
                'atr_14', 'stoch_k', 'stoch_d',
                'volume_ratio', 'returns_1', 'returns_5', 'returns_20'
            ]
            
            for col in indicator_cols:
                if col in row and not pd.isna(row[col]):
                    tf_features[col] = float(row[col])
            
            # Fibonacci features (if enabled)
            if self.include_fibonacci:
                df_for_fib = data_with_indicators[timeframe]
                current_price = float(row['close'])
                fib_features = self.calculate_fibonacci_features(df_for_fib, current_price)
                tf_features.update(fib_features)
            
            features['features'][timeframe] = tf_features
        
        return features
    
    def get_feature_vector(
        self,
        features: Dict[str, Any],
        flatten: bool = True
    ) -> np.ndarray:
        """
        Convert feature dictionary to numpy array.
        
        Args:
            features: Output from generate_features()
            flatten: If True, flatten to 1D array; if False, keep hierarchical structure
        
        Returns:
            Numpy array of features
        """
        if flatten:
            # Flatten all features into single vector
            all_values = []
            
            for timeframe in self.timeframes:
                if timeframe in features['features']:
                    tf_features = features['features'][timeframe]
                    # Sort keys for consistent ordering
                    for key in sorted(tf_features.keys()):
                        all_values.append(tf_features[key])
            
            return np.array(all_values)
        
        else:
            # Keep hierarchical structure (list of arrays per timeframe)
            hierarchical = []
            
            for timeframe in self.timeframes:
                if timeframe in features['features']:
                    tf_features = features['features'][timeframe]
                    values = [tf_features[key] for key in sorted(tf_features.keys())]
                    hierarchical.append(np.array(values))
                else:
                    hierarchical.append(np.array([]))
            
            return np.array(hierarchical, dtype=object)


if __name__ == '__main__':
    # Test multi-timeframe feature generation
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 70)
    print("Testing Multi-Timeframe Feature Integration")
    print("=" * 70)
    
    # Generate sample data
    np.random.seed(42)
    dates = pd.date_range('2024-01-01', periods=500, freq='5min')
    
    sample_data = {
        '5min': pd.DataFrame({
            'time': dates,
            'open': 100 + np.cumsum(np.random.randn(500) * 0.5),
            'high': 101 + np.cumsum(np.random.randn(500) * 0.5),
            'low': 99 + np.cumsum(np.random.randn(500) * 0.5),
            'close': 100 + np.cumsum(np.random.randn(500) * 0.5),
            'volume': np.random.randint(1000, 10000, 500)
        })
    }
    
    # Add derived data
    sample_data['5min']['high'] = sample_data['5min'][['open', 'close']].max(axis=1) + np.random.rand(500)
    sample_data['5min']['low'] = sample_data['5min'][['open', 'close']].min(axis=1) - np.random.rand(500)
    
    # Create 15min data (aggregate from 5min)
    df_15min = sample_data['5min'].set_index('time').resample('15min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).reset_index()
    sample_data['15min'] = df_15min
    
    print("\nSample Data Shapes:")
    for tf, df in sample_data.items():
        print(f"  {tf}: {df.shape}")
    
    # Initialize generator
    generator = MultiTimeframeFeatureGenerator(
        timeframes=['5min', '15min'],
        include_fibonacci=True
    )
    
    # Generate features
    print("\nGenerating features...")
    features = generator.generate_features(sample_data)
    
    print(f"\nTimestamp: {features['timestamp']}")
    print("\nFeatures by timeframe:")
    for tf, tf_features in features['features'].items():
        print(f"\n{tf.upper()}:")
        print(f"  Number of features: {len(tf_features)}")
        print(f"  Sample features:")
        for key, value in list(tf_features.items())[:5]:
            print(f"    {key}: {value:.4f}")
    
    # Get flattened feature vector
    feature_vector = generator.get_feature_vector(features, flatten=True)
    print(f"\nFlattened feature vector shape: {feature_vector.shape}")
    print(f"Sample values: {feature_vector[:10]}")
    
    print("\n" + "=" * 70)
    print("✓ Multi-Timeframe Feature Integration test completed!")
    print("=" * 70)
