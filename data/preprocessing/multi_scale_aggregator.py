"""
Multi-Scale Data Aggregation Pipeline

Implements synchronized time-series aggregation across multiple resolutions
(1min, 15min, 1hour, 12hour, 1day) with continuous cross-scale statistics.

This module provides the foundation for adaptive multi-timescale indicator
evaluation as specified in Phase 3.5 of the FLOORPLAN_CHECKLIST.

Key Features:
- OHLCV aggregation from 1-minute bars
- Per-scale technical indicator calculation
- Rolling volatility and regime detection
- Cross-scale correlation and lag estimation
- Trend divergence detection

Architecture:
    1-Minute Bars (Raw Data)
            ↓
    ┌───────────────────────────────────┐
    │   Multi-Scale Aggregator          │
    │   - 1min (raw)                    │
    │   - 15min (aggregate)             │
    │   - 1hour (aggregate)             │
    │   - 12hour (aggregate)            │
    │   - 1day (aggregate)              │
    └───────────────────────────────────┘
            ↓
    ┌───────────────────────────────────┐
    │   Per-Scale Processing            │
    │   - Technical Indicators          │
    │   - Realized Volatility           │
    │   - Regime Detection              │
    └───────────────────────────────────┘
            ↓
    ┌───────────────────────────────────┐
    │   Cross-Scale Analysis            │
    │   - Trend Divergence Flags        │
    │   - Cross-Correlation Matrix      │
    │   - Optimal Lag Estimation        │
    └───────────────────────────────────┘
            ↓
    Multi-Scale Features → ML Model

References:
- Temporal Fusion Transformer: https://arxiv.org/abs/1912.09363
- Multi-scale CNNs: https://arxiv.org/abs/2107.09441
- Cross-correlation: https://en.wikipedia.org/wiki/Cross-correlation
- Volatility modeling: Andersen & Bollerslev (1998)
"""

# type: ignore  # Suppress pandas-heavy type checking issues

import logging
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class TimeScale(Enum):
    """Supported timescales for multi-resolution analysis."""
    MIN_1 = '1min'
    MIN_15 = '15min'
    HOUR_1 = '1hour'
    HOUR_12 = '12hour'
    DAY_1 = '1day'


class VolatilityRegime(Enum):
    """Volatility regime classification."""
    CALM = 'calm'          # vol < 0.5 * historical_avg
    NORMAL = 'normal'      # 0.5 * avg <= vol <= 1.5 * avg
    VOLATILE = 'volatile'  # 1.5 * avg < vol <= 2.5 * avg
    CRISIS = 'crisis'      # vol > 2.5 * avg


@dataclass
class ScaleStatistics:
    """Statistics for a single timescale."""
    scale: TimeScale
    volatility: float
    regime: VolatilityRegime
    atr: float
    trend_strength: float  # Based on MA alignment
    volume_ma_ratio: float  # Current volume / MA volume


@dataclass
class CrossScaleMetrics:
    """Cross-scale correlation and divergence metrics."""
    scale_a: TimeScale
    scale_b: TimeScale
    correlation: float
    optimal_lag: int
    divergence_flag: bool


class MultiScaleAggregator:
    """
    Maintains synchronized OHLCV and indicators across multiple timescales.
    
    This class handles:
    1. Aggregation from 1-minute bars to coarser resolutions
    2. Per-scale technical indicator calculation
    3. Volatility regime detection
    4. Cross-scale correlation and divergence analysis
    
    Usage:
        aggregator = MultiScaleAggregator(db_client)
        
        # Load and aggregate data
        aggregator.load_data('BTC/USD', start_date, end_date)
        
        # Compute indicators across all scales
        aggregator.compute_all_indicators()
        
        # Analyze cross-scale relationships
        corr_matrix = aggregator.build_correlation_matrix()
        divergences = aggregator.detect_all_divergences()
        
        # Get features for ML model
        features = aggregator.get_unified_features(timestamp)
    """
    
    TIMESCALES = [TimeScale.MIN_1, TimeScale.MIN_15, TimeScale.HOUR_1, 
                  TimeScale.HOUR_12, TimeScale.DAY_1]
    
    # Scale-appropriate indicator windows
    INDICATOR_WINDOWS = {
        TimeScale.MIN_1: {
            'ma_short': 5, 'ma_medium': 10, 'ma_long': 20,
            'rsi': 14, 'macd': (12, 26, 9), 'bb': (20, 2)
        },
        TimeScale.MIN_15: {
            'ma_short': 10, 'ma_medium': 20, 'ma_long': 50,
            'rsi': 14, 'macd': (12, 26, 9), 'bb': (20, 2)
        },
        TimeScale.HOUR_1: {
            'ma_short': 20, 'ma_medium': 50, 'ma_long': 100,
            'rsi': 14, 'macd': (12, 26, 9), 'bb': (20, 2)
        },
        TimeScale.HOUR_12: {
            'ma_short': 7, 'ma_medium': 14, 'ma_long': 30,
            'rsi': 14, 'macd': (12, 26, 9), 'bb': (20, 2)
        },
        TimeScale.DAY_1: {
            'ma_short': 20, 'ma_medium': 50, 'ma_long': 200,
            'rsi': 14, 'macd': (12, 26, 9), 'bb': (20, 2)
        }
    }
    
    def __init__(self, db_client: Any = None):
        """
        Initialize multi-scale aggregator.
        
        Args:
            db_client: Database client for fetching market data
        """
        self.db = db_client
        self.scale_data: Dict[TimeScale, Optional[pd.DataFrame]] = {
            scale: None for scale in self.TIMESCALES
        }
        self.indicators: Dict[TimeScale, Optional[pd.DataFrame]] = {
            scale: None for scale in self.TIMESCALES
        }
        self.volatility: Dict[TimeScale, Optional[pd.Series]] = {
            scale: None for scale in self.TIMESCALES
        }
        self.scale_stats: Dict[TimeScale, Optional[ScaleStatistics]] = {
            scale: None for scale in self.TIMESCALES
        }
        self.divergence_flags: Dict[Tuple[TimeScale, TimeScale], Optional[pd.Series]] = {}
        self.corr_matrix: Optional[pd.DataFrame] = None
        self.lag_matrix: Optional[pd.DataFrame] = None
        
        logger.info("MultiScaleAggregator initialized with %d timescales", len(self.TIMESCALES))
    
    def aggregate_from_minute_bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        minute_data: Optional[pd.DataFrame] = None
    ) -> Dict[TimeScale, pd.DataFrame]:
        """
        Aggregate 1-minute bars into all timescales.
        
        Uses OHLCV aggregation rules:
        - Open: FIRST(open)
        - High: MAX(high)
        - Low: MIN(low)
        - Close: LAST(close)
        - Volume: SUM(volume)
        
        Args:
            ticker: Trading pair symbol
            start: Start datetime
            end: End datetime
            minute_data: Optional pre-loaded 1-minute data
        
        Returns:
            Dictionary mapping each timescale to its aggregated DataFrame
        """
        logger.info("Aggregating data for %s from %s to %s", ticker, start, end)
        
        # Load 1-minute data if not provided
        if minute_data is None:
            if self.db is None:
                raise ValueError("Database client required if minute_data not provided")
            minute_data = self._fetch_minute_data(ticker, start, end)
        
        # Ensure datetime index
        if not isinstance(minute_data.index, pd.DatetimeIndex):
            minute_data['time'] = pd.to_datetime(minute_data['time'])
            minute_data = minute_data.set_index('time')
        
        # Store 1-minute data
        self.scale_data[TimeScale.MIN_1] = minute_data
        
        # Aggregate to coarser resolutions
        aggregation_rules = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }
        
        # 15-minute aggregation
        self.scale_data[TimeScale.MIN_15] = minute_data.resample('15T').agg(aggregation_rules)  # type: ignore
        
        # 1-hour aggregation
        self.scale_data[TimeScale.HOUR_1] = minute_data.resample('1H').agg(aggregation_rules)  # type: ignore
        
        # 12-hour aggregation
        self.scale_data[TimeScale.HOUR_12] = minute_data.resample('12H').agg(aggregation_rules)  # type: ignore
        
        # 1-day aggregation
        self.scale_data[TimeScale.DAY_1] = minute_data.resample('1D').agg(aggregation_rules)  # type: ignore
        
        # Remove any rows with NaN values
        for scale in self.TIMESCALES:
            df = self.scale_data[scale]
            if df is not None:
                self.scale_data[scale] = df.dropna()
                logger.debug("Scale %s: %d bars", scale.value, len(df))
        
        return self.scale_data  # type: ignore
    
    def compute_indicators_per_scale(self, scale: TimeScale) -> pd.DataFrame:
        """
        Calculate technical indicators for a specific timescale.
        
        Uses scale-appropriate windows defined in INDICATOR_WINDOWS.
        
        Args:
            scale: Timescale to compute indicators for
        
        Returns:
            DataFrame with all technical indicators
        """
        if self.scale_data[scale] is None:
            raise ValueError(f"No data available for scale {scale.value}")
        
        df = self.scale_data[scale].copy()
        windows = self.INDICATOR_WINDOWS[scale]
        
        logger.debug("Computing indicators for %s", scale.value)
        
        # Moving Averages
        df['ma_short'] = df['close'].rolling(window=windows['ma_short']).mean()
        df['ma_medium'] = df['close'].rolling(window=windows['ma_medium']).mean()
        df['ma_long'] = df['close'].rolling(window=windows['ma_long']).mean()
        
        # RSI
        df['rsi'] = self._calculate_rsi(df['close'], windows['rsi'])
        
        # MACD
        fast, slow, signal = windows['macd']
        ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
        ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
        df['macd'] = ema_fast - ema_slow
        df['macd_signal'] = df['macd'].ewm(span=signal, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        
        # Bollinger Bands
        bb_period, bb_std = windows['bb']
        bb_ma = df['close'].rolling(window=bb_period).mean()
        bb_stddev = df['close'].rolling(window=bb_period).std()
        df['bb_upper'] = bb_ma + (bb_std * bb_stddev)
        df['bb_middle'] = bb_ma
        df['bb_lower'] = bb_ma - (bb_std * bb_stddev)
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_middle']
        
        # ATR (Average True Range)
        df['atr'] = self._calculate_atr(df, period=14)
        
        # Volume indicators
        df['volume_ma'] = df['volume'].rolling(window=windows['ma_short']).mean()
        df['volume_ratio'] = df['volume'] / df['volume_ma']
        
        # Trend strength (based on MA alignment)
        df['trend_strength'] = self._calculate_trend_strength(
            df['ma_short'], df['ma_medium'], df['ma_long']
        )
        
        self.indicators[scale] = df
        return df
    
    def compute_all_indicators(self) -> None:
        """Compute technical indicators for all timescales."""
        logger.info("Computing indicators for all timescales")
        for scale in self.TIMESCALES:
            if self.scale_data[scale] is not None:
                self.compute_indicators_per_scale(scale)
    
    def compute_realized_volatility(
        self,
        scale: TimeScale,
        window: int = 20
    ) -> pd.Series:
        """
        Calculate rolling realized volatility for a timescale.
        
        Uses log returns for numerical stability:
        vol = sqrt(sum(log_returns^2) / window)
        
        Args:
            scale: Timescale to compute volatility for
            window: Rolling window size
        
        Returns:
            Series of realized volatility values
        """
        if self.scale_data[scale] is None:
            raise ValueError(f"No data available for scale {scale.value}")
        
        df = self.scale_data[scale]
        
        # Calculate log returns
        log_returns = np.log(df['close'] / df['close'].shift(1))
        
        # Realized volatility: sqrt of rolling sum of squared returns
        realized_vol = np.sqrt((log_returns ** 2).rolling(window=window).sum() / window)
        
        # Annualize based on timescale
        annualization_factor = self._get_annualization_factor(scale)
        realized_vol = realized_vol * np.sqrt(annualization_factor)
        
        self.volatility[scale] = realized_vol
        
        logger.debug("Computed volatility for %s (mean: %.4f)", 
                    scale.value, realized_vol.mean())
        
        return realized_vol
    
    def detect_volatility_regime(
        self,
        scale: TimeScale,
        lookback: int = 100
    ) -> VolatilityRegime:
        """
        Detect current volatility regime for a timescale.
        
        Regimes:
        - CALM: vol < 0.5 * historical_avg
        - NORMAL: 0.5 * avg <= vol <= 1.5 * avg
        - VOLATILE: 1.5 * avg < vol <= 2.5 * avg
        - CRISIS: vol > 2.5 * avg
        
        Args:
            scale: Timescale to analyze
            lookback: Historical window for average calculation
        
        Returns:
            Current volatility regime
        """
        if self.volatility[scale] is None:
            self.compute_realized_volatility(scale)
        
        vol_series = self.volatility[scale]
        current_vol = vol_series.iloc[-1]
        historical_avg = vol_series.iloc[-lookback:].mean()
        
        ratio = current_vol / historical_avg if historical_avg > 0 else 1.0
        
        if ratio < 0.5:
            regime = VolatilityRegime.CALM
        elif ratio <= 1.5:
            regime = VolatilityRegime.NORMAL
        elif ratio <= 2.5:
            regime = VolatilityRegime.VOLATILE
        else:
            regime = VolatilityRegime.CRISIS
        
        logger.info("Volatility regime for %s: %s (ratio: %.2f)", 
                   scale.value, regime.value, ratio)
        
        return regime
    
    def detect_trend_divergence(
        self,
        fast_scale: TimeScale = TimeScale.MIN_1,
        slow_scale: TimeScale = TimeScale.HOUR_1
    ) -> pd.Series:
        """
        Detect trend divergence between fast and slow timescales.
        
        Divergence occurs when:
        sign(MA_fast - price) != sign(MA_slow - price)
        
        This indicates potential trend reversal or weakening.
        
        Args:
            fast_scale: Fast timescale (e.g., 1min)
            slow_scale: Slow timescale (e.g., 1hour)
        
        Returns:
            Boolean series indicating divergence at each timestamp
        """
        if self.indicators[fast_scale] is None or self.indicators[slow_scale] is None:
            raise ValueError("Indicators must be computed first")
        
        # Get aligned data
        fast_df = self.indicators[fast_scale]
        slow_df = self.indicators[slow_scale]
        
        # Resample fast to slow frequency for comparison
        fast_resampled = fast_df.resample(self._get_resample_rule(slow_scale)).last()
        
        # Align indices
        aligned_fast = fast_resampled.reindex(slow_df.index, method='ffill')
        
        # Calculate divergence
        fast_signal = np.sign(aligned_fast['ma_short'] - aligned_fast['close'])
        slow_signal = np.sign(slow_df['ma_medium'] - slow_df['close'])
        
        divergence = (fast_signal != slow_signal) & (fast_signal != 0) & (slow_signal != 0)
        
        self.divergence_flags[(fast_scale, slow_scale)] = divergence
        
        logger.debug("Detected %d divergence points between %s and %s",
                    divergence.sum(), fast_scale.value, slow_scale.value)
        
        return divergence
    
    def compute_cross_correlation(
        self,
        scale_a: TimeScale,
        scale_b: TimeScale,
        max_lag: int = 10,
        window: int = 50
    ) -> Tuple[float, int]:
        """
        Compute rolling cross-correlation between two timescales.
        
        Finds the optimal lag and correlation strength between returns
        of two different timescales.
        
        Algorithm:
        1. Extract aligned returns for both scales
        2. Normalize to zero mean per window
        3. For each lag in [-max_lag, +max_lag]:
            corr[lag] = pearson_corr(returns_a, shifted(returns_b, lag))
        4. Return max |corr| and its lag
        
        Args:
            scale_a: First timescale
            scale_b: Second timescale
            max_lag: Maximum lag to test (in bars of coarser scale)
            window: Rolling window size
        
        Returns:
            Tuple of (correlation_strength, optimal_lag)
        """
        if self.scale_data[scale_a] is None or self.scale_data[scale_b] is None:
            raise ValueError("Data must be loaded for both scales")
        
        # Get returns for both scales
        returns_a = self.scale_data[scale_a]['close'].pct_change()
        returns_b = self.scale_data[scale_b]['close'].pct_change()
        
        # Resample to coarser scale
        coarser_scale = max(scale_a, scale_b, key=lambda s: self.TIMESCALES.index(s))
        resample_rule = self._get_resample_rule(coarser_scale)
        
        returns_a_resampled = returns_a.resample(resample_rule).sum()
        returns_b_resampled = returns_b.resample(resample_rule).sum()
        
        # Align indices
        common_index = returns_a_resampled.index.intersection(returns_b_resampled.index)
        returns_a_aligned = returns_a_resampled.loc[common_index]
        returns_b_aligned = returns_b_resampled.loc[common_index]
        
        # Compute cross-correlation at different lags
        correlations = {}
        for lag in range(-max_lag, max_lag + 1):
            if lag == 0:
                corr = returns_a_aligned.corr(returns_b_aligned)
            elif lag > 0:
                corr = returns_a_aligned.iloc[lag:].corr(returns_b_aligned.iloc[:-lag])
            else:
                corr = returns_a_aligned.iloc[:lag].corr(returns_b_aligned.iloc[-lag:])
            
            correlations[lag] = corr if not np.isnan(corr) else 0.0
        
        # Find optimal lag (max absolute correlation)
        optimal_lag = max(correlations.keys(), key=lambda k: abs(correlations[k]))
        corr_strength = correlations[optimal_lag]
        
        logger.debug("Cross-correlation %s vs %s: %.3f at lag %d",
                    scale_a.value, scale_b.value, corr_strength, optimal_lag)
        
        return corr_strength, optimal_lag
    
    def build_correlation_matrix(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Build full 5x5 correlation and lag matrices for all scale pairs.
        
        Returns:
            Tuple of (correlation_matrix, lag_matrix)
        """
        logger.info("Building correlation matrix for all scale pairs")
        
        n_scales = len(self.TIMESCALES)
        corr_matrix = np.zeros((n_scales, n_scales))
        lag_matrix = np.zeros((n_scales, n_scales), dtype=int)
        
        scale_names = [s.value for s in self.TIMESCALES]
        
        for i, scale_a in enumerate(self.TIMESCALES):
            for j, scale_b in enumerate(self.TIMESCALES):
                if i == j:
                    corr_matrix[i, j] = 1.0
                    lag_matrix[i, j] = 0
                elif i < j:  # Compute only upper triangle
                    try:
                        corr, lag = self.compute_cross_correlation(scale_a, scale_b)
                        corr_matrix[i, j] = corr
                        lag_matrix[i, j] = lag
                        # Mirror to lower triangle
                        corr_matrix[j, i] = corr
                        lag_matrix[j, i] = -lag
                    except Exception as e:
                        logger.warning("Failed to compute correlation for %s vs %s: %s",
                                     scale_a.value, scale_b.value, str(e))
                        corr_matrix[i, j] = 0.0
                        corr_matrix[j, i] = 0.0
        
        self.corr_matrix = pd.DataFrame(corr_matrix, index=scale_names, columns=scale_names)
        self.lag_matrix = pd.DataFrame(lag_matrix, index=scale_names, columns=scale_names)
        
        logger.info("Correlation matrix built:\n%s", self.corr_matrix)
        
        return self.corr_matrix, self.lag_matrix
    
    def detect_all_divergences(self) -> Dict[Tuple[TimeScale, TimeScale], pd.Series]:
        """
        Detect divergences for all meaningful scale pairs.
        
        Returns:
            Dictionary mapping scale pairs to divergence flags
        """
        logger.info("Detecting divergences for all scale pairs")
        
        # Define meaningful pairs (adjacent and skip-one scales)
        pairs = [
            (TimeScale.MIN_1, TimeScale.MIN_15),
            (TimeScale.MIN_1, TimeScale.HOUR_1),
            (TimeScale.MIN_15, TimeScale.HOUR_1),
            (TimeScale.MIN_15, TimeScale.HOUR_12),
            (TimeScale.HOUR_1, TimeScale.DAY_1),
        ]
        
        for fast, slow in pairs:
            try:
                self.detect_trend_divergence(fast, slow)
            except Exception as e:
                logger.warning("Failed to detect divergence for %s vs %s: %s",
                             fast.value, slow.value, str(e))
        
        return self.divergence_flags
    
    def get_scale_statistics(self, scale: TimeScale) -> ScaleStatistics:
        """
        Get comprehensive statistics for a timescale.
        
        Args:
            scale: Timescale to analyze
        
        Returns:
            ScaleStatistics object with all metrics
        """
        if self.indicators[scale] is None:
            self.compute_indicators_per_scale(scale)
        
        df = self.indicators[scale]
        
        # Get volatility and regime
        if self.volatility[scale] is None:
            self.compute_realized_volatility(scale)
        
        current_vol = self.volatility[scale].iloc[-1]
        regime = self.detect_volatility_regime(scale)
        
        # Get ATR
        atr = df['atr'].iloc[-1]
        
        # Get trend strength
        trend_strength = df['trend_strength'].iloc[-1]
        
        # Get volume ratio
        volume_ratio = df['volume_ratio'].iloc[-1]
        
        stats = ScaleStatistics(
            scale=scale,
            volatility=current_vol,
            regime=regime,
            atr=atr,
            trend_strength=trend_strength,
            volume_ma_ratio=volume_ratio
        )
        
        self.scale_stats[scale] = stats
        
        return stats
    
    def get_unified_features(
        self,
        timestamp: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Get unified feature vector for ML model input.
        
        Args:
            timestamp: Specific timestamp to get features for (default: latest)
        
        Returns:
            Dictionary with features from all scales plus cross-scale metrics
        """
        features = {}
        
        # Per-scale features
        for scale in self.TIMESCALES:
            if self.indicators[scale] is None:
                continue
            
            df = self.indicators[scale]
            
            if timestamp is not None:
                # Get closest timestamp
                idx = df.index.get_indexer([timestamp], method='nearest')[0]
                row = df.iloc[idx]
            else:
                row = df.iloc[-1]
            
            scale_prefix = scale.value.replace('hour', 'h').replace('min', 'm').replace('day', 'd')
            
            features.update({
                f'{scale_prefix}_ma_short': row['ma_short'],
                f'{scale_prefix}_ma_medium': row['ma_medium'],
                f'{scale_prefix}_ma_long': row['ma_long'],
                f'{scale_prefix}_rsi': row['rsi'],
                f'{scale_prefix}_macd': row['macd'],
                f'{scale_prefix}_macd_signal': row['macd_signal'],
                f'{scale_prefix}_bb_width': row['bb_width'],
                f'{scale_prefix}_atr': row['atr'],
                f'{scale_prefix}_volume_ratio': row['volume_ratio'],
                f'{scale_prefix}_trend_strength': row['trend_strength'],
            })
        
        # Cross-scale features
        if self.corr_matrix is not None:
            # Flatten correlation matrix (upper triangle)
            for i, scale_a in enumerate(self.TIMESCALES):
                for j, scale_b in enumerate(self.TIMESCALES[i+1:], start=i+1):
                    key = f'corr_{scale_a.value}_{scale_b.value}'
                    features[key] = self.corr_matrix.iloc[i, j]
        
        # Divergence flags
        for (fast, slow), div_series in self.divergence_flags.items():
            key = f'div_{fast.value}_{slow.value}'
            if timestamp is not None:
                idx = div_series.index.get_indexer([timestamp], method='nearest')[0]
                features[key] = int(div_series.iloc[idx])
            else:
                features[key] = int(div_series.iloc[-1])
        
        return features
    
    # Helper methods
    
    def _fetch_minute_data(
        self,
        ticker: str,
        start: datetime,
        end: datetime
    ) -> pd.DataFrame:
        """Fetch 1-minute data from database."""
        # Placeholder - implement with actual DB query
        raise NotImplementedError("Database integration required")
    
    @staticmethod
    def _calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        """Calculate RSI indicator."""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()  # type: ignore
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()  # type: ignore
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    @staticmethod
    def _calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range."""
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = true_range.rolling(window=period).mean()
        return atr
    
    @staticmethod
    def _calculate_trend_strength(
        ma_short: pd.Series,
        ma_medium: pd.Series,
        ma_long: pd.Series
    ) -> pd.Series:
        """
        Calculate trend strength based on MA alignment.
        
        Returns value in [-1, 1]:
        - +1: Strong uptrend (short > medium > long)
        - -1: Strong downtrend (short < medium < long)
        - 0: No clear trend
        """
        # Count aligned MAs
        uptrend = (ma_short > ma_medium) & (ma_medium > ma_long)
        downtrend = (ma_short < ma_medium) & (ma_medium < ma_long)
        
        strength = pd.Series(0.0, index=ma_short.index)
        strength[uptrend] = 1.0
        strength[downtrend] = -1.0
        
        # Partial trends (2 out of 3 aligned)
        partial_up = ((ma_short > ma_medium) & ~uptrend) | ((ma_medium > ma_long) & ~uptrend)
        partial_down = ((ma_short < ma_medium) & ~downtrend) | ((ma_medium < ma_long) & ~downtrend)
        
        strength[partial_up & ~downtrend] = 0.5
        strength[partial_down & ~uptrend] = -0.5
        
        return strength
    
    @staticmethod
    def _get_resample_rule(scale: TimeScale) -> str:
        """Get pandas resample rule for a timescale."""
        rules = {
            TimeScale.MIN_1: '1T',
            TimeScale.MIN_15: '15T',
            TimeScale.HOUR_1: '1H',
            TimeScale.HOUR_12: '12H',
            TimeScale.DAY_1: '1D'
        }
        return rules[scale]
    
    @staticmethod
    def _get_annualization_factor(scale: TimeScale) -> float:
        """Get annualization factor for volatility scaling."""
        # Approximate number of periods per year
        factors = {
            TimeScale.MIN_1: 252 * 390,     # ~252 trading days * 390 minutes
            TimeScale.MIN_15: 252 * 26,     # ~252 days * 26 15-min periods
            TimeScale.HOUR_1: 252 * 6.5,    # ~252 days * 6.5 hours
            TimeScale.HOUR_12: 252 * 2,     # ~252 days * 2 12-hour periods
            TimeScale.DAY_1: 252            # 252 trading days
        }
        return factors[scale]


if __name__ == '__main__':
    # Test multi-scale aggregation
    logging.basicConfig(level=logging.INFO)
    
    # Generate sample 1-minute data
    dates = pd.date_range('2024-01-01', periods=1000, freq='1min')
    np.random.seed(42)
    
    # Simulate price with trend and noise
    trend = np.linspace(100, 110, 1000)
    noise = np.random.randn(1000) * 0.5
    close = trend + noise
    
    data = pd.DataFrame({
        'time': dates,
        'open': close + np.random.randn(1000) * 0.1,
        'high': close + np.abs(np.random.randn(1000) * 0.3),
        'low': close - np.abs(np.random.randn(1000) * 0.3),
        'close': close,
        'volume': np.random.randint(1000, 10000, 1000)
    })
    
    # Test aggregation
    aggregator = MultiScaleAggregator()
    
    print("\n" + "="*60)
    print("Testing Multi-Scale Aggregation")
    print("="*60)
    
    # Aggregate data
    aggregator.aggregate_from_minute_bars(
        'BTC/USD',
        dates[0],
        dates[-1],
        minute_data=data
    )
    
    print("\n✓ Data aggregated across all timescales")
    for scale, df in aggregator.scale_data.items():
        if df is not None:
            print(f"  {scale.value}: {len(df)} bars")
    
    # Compute indicators
    aggregator.compute_all_indicators()
    print("\n✓ Indicators computed for all timescales")
    
    # Compute volatility
    for scale in aggregator.TIMESCALES:
        aggregator.compute_realized_volatility(scale)
    print("\n✓ Realized volatility computed")
    
    # Detect regimes
    print("\n✓ Volatility regimes:")
    for scale in aggregator.TIMESCALES:
        regime = aggregator.detect_volatility_regime(scale)
        print(f"  {scale.value}: {regime.value}")
    
    # Build correlation matrix
    corr_matrix, lag_matrix = aggregator.build_correlation_matrix()
    print("\n✓ Cross-scale correlation matrix:")
    print(corr_matrix.round(3))
    
    # Detect divergences
    aggregator.detect_all_divergences()
    print("\n✓ Divergences detected:")
    for (fast, slow), div in aggregator.divergence_flags.items():
        print(f"  {fast.value} vs {slow.value}: {div.sum()} divergence points")
    
    # Get unified features
    features = aggregator.get_unified_features()
    print(f"\n✓ Unified feature vector: {len(features)} features")
    print("\nSample features:")
    for i, (key, value) in enumerate(list(features.items())[:10]):
        print(f"  {key}: {value:.4f}")
    
    print("\n" + "="*60)
    print("Multi-Scale Aggregation Test Complete!")
    print("="*60)
