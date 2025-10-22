"""
Multi-Resolution Pipeline for ChronoX Trading Bot

Aggregates time-series data across multiple timeframes:
1min → 15min → 1hour → 12hour → daily

Features:
- Temporal aggregation (OHLCV resampling)
- Cross-scale correlation features
- Volatility gating (scale selection based on market conditions)
- Scale-aware feature engineering

Architecture:
    Raw 1min bars → [15min, 1hr, 12hr, daily] aggregates
                  → Technical indicators per scale
                  → Cross-scale features
                  → Volatility regime classification

References:
- arXiv:2302.11939 - "Multi-Scale Temporal Fusion Transformers"
- arXiv:1703.07015 - "Attention Is All You Need"
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import logging
from dataclasses import dataclass

from .indicators import (
    sma, ema, rsi, macd, bollinger_bands, atr,
    stochastic, adx, obv
)

logger = logging.getLogger(__name__)


@dataclass
class TimeScale:
    """Represents a temporal resolution."""
    name: str
    freq: str  # Pandas frequency string
    minutes: int  # Duration in minutes
    
    def __repr__(self):
        return f"TimeScale({self.name}, {self.freq})"


# Define standard timescales used in ChronoX
TIMESCALES = [
    TimeScale("1min", "1T", 1),
    TimeScale("15min", "15T", 15),
    TimeScale("1hour", "1H", 60),
    TimeScale("12hour", "12H", 720),
    TimeScale("daily", "1D", 1440)
]


class MultiResolutionPipeline:
    """
    Aggregates and processes data across multiple time resolutions.
    
    Features:
    - OHLCV resampling for all scales
    - Per-scale technical indicators
    - Cross-scale correlation features
    - Volatility-based scale weighting
    
    Example:
        pipeline = MultiResolutionPipeline(
            timescales=['1min', '15min', '1hour', 'daily']
        )
        
        # Process 1-minute data
        multi_scale_features = pipeline.transform(df_1min)
        
        # Features will have columns like:
        # - close_15min, close_1hour, close_daily
        # - rsi_15min, rsi_1hour, rsi_daily
        # - volatility_ratio_15min_to_1hour
    """
    
    def __init__(
        self,
        timescales: Optional[List[str]] = None,
        base_scale: str = "1min"
    ):
        """
        Initialize multi-resolution pipeline.
        
        Args:
            timescales: List of scale names to use (default: all)
            base_scale: Base resolution of input data
        """
        if timescales is None:
            timescales = [ts.name for ts in TIMESCALES]
        
        self.timescales = [ts for ts in TIMESCALES if ts.name in timescales]
        self.base_scale = next(ts for ts in TIMESCALES if ts.name == base_scale)
        
        logger.info(f"Initialized pipeline with {len(self.timescales)} timescales: {[ts.name for ts in self.timescales]}")
    
    def resample_ohlcv(
        self,
        df: pd.DataFrame,
        target_scale: TimeScale
    ) -> pd.DataFrame:
        """
        Resample OHLCV data to target timescale.
        
        Args:
            df: DataFrame with columns: timestamp, open, high, low, close, volume
            target_scale: Target timescale
            
        Returns:
            Resampled DataFrame
        """
        if 'timestamp' not in df.columns:
            raise ValueError("DataFrame must have 'timestamp' column")
        
        # Set timestamp as index
        df = df.set_index('timestamp')
        
        # Define resampling rules
        agg_rules = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }
        
        # Resample
        resampled = df.resample(target_scale.freq).agg(agg_rules)
        
        # Drop rows with NaN (incomplete periods)
        resampled = resampled.dropna()
        
        # Reset index
        resampled = resampled.reset_index()
        
        return resampled
    
    def add_scale_indicators(
        self,
        df: pd.DataFrame,
        scale_name: str,
        include_volume: bool = True
    ) -> pd.DataFrame:
        """
        Add technical indicators with scale suffix.
        
        Args:
            df: OHLCV DataFrame
            scale_name: Scale name for column suffix
            include_volume: Include volume indicators
            
        Returns:
            DataFrame with indicators
        """
        df = df.copy()
        
        # Moving Averages
        df[f'sma_20_{scale_name}'] = sma(df['close'], 20)
        df[f'ema_20_{scale_name}'] = ema(df['close'], 20)
        
        # RSI
        df[f'rsi_{scale_name}'] = rsi(df['close'], 14)
        
        # MACD
        macd_line, signal_line, histogram = macd(df['close'])
        df[f'macd_{scale_name}'] = macd_line
        df[f'macd_signal_{scale_name}'] = signal_line
        df[f'macd_hist_{scale_name}'] = histogram
        
        # Bollinger Bands
        upper, middle, lower = bollinger_bands(df['close'])
        df[f'bb_upper_{scale_name}'] = upper
        df[f'bb_lower_{scale_name}'] = lower
        df[f'bb_width_{scale_name}'] = (upper - lower) / middle
        
        # ATR (volatility)
        df[f'atr_{scale_name}'] = atr(df['high'], df['low'], df['close'], 14)
        df[f'volatility_{scale_name}'] = df['close'].rolling(20).std() / df['close'].rolling(20).mean()
        
        # Stochastic
        k, d = stochastic(df['high'], df['low'], df['close'])
        df[f'stoch_k_{scale_name}'] = k
        df[f'stoch_d_{scale_name}'] = d
        
        # ADX (trend strength)
        adx_val, plus_di, minus_di = adx(df['high'], df['low'], df['close'])
        df[f'adx_{scale_name}'] = adx_val
        
        # Volume indicators
        if include_volume and 'volume' in df.columns:
            df[f'obv_{scale_name}'] = obv(df['close'], df['volume'])
            df[f'volume_sma_{scale_name}'] = df['volume'].rolling(20).mean()
        
        return df
    
    def compute_cross_scale_features(
        self,
        scale_data: Dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        """
        Compute features that capture relationships between scales.
        
        Features:
        - Price ratio between scales
        - Volatility ratio between scales
        - Correlation between scales
        - Divergence signals
        
        Args:
            scale_data: Dict mapping scale name to DataFrame
            
        Returns:
            DataFrame with cross-scale features (aligned to finest scale)
        """
        # Use base scale as reference
        base_df = scale_data[self.base_scale.name].copy()
        
        # Add features comparing adjacent scales
        for i in range(len(self.timescales) - 1):
            scale1 = self.timescales[i]
            scale2 = self.timescales[i + 1]
            
            if scale1.name not in scale_data or scale2.name not in scale_data:
                continue
            
            df1 = scale_data[scale1.name]
            df2 = scale_data[scale2.name]
            
            # Merge on timestamp (forward fill coarser scale)
            merged = pd.merge_asof(
                df1[['timestamp', 'close', f'volatility_{scale1.name}']].sort_values('timestamp'),
                df2[['timestamp', 'close', f'volatility_{scale2.name}']].sort_values('timestamp'),
                on='timestamp',
                direction='backward',
                suffixes=('_fine', '_coarse')
            )
            
            # Price momentum ratio
            base_df[f'price_ratio_{scale1.name}_to_{scale2.name}'] = \
                merged['close_fine'] / merged['close_coarse']
            
            # Volatility ratio (important for scale selection)
            base_df[f'volatility_ratio_{scale1.name}_to_{scale2.name}'] = \
                merged[f'volatility_{scale1.name}'] / merged[f'volatility_{scale2.name}']
        
        return base_df
    
    def compute_volatility_gates(
        self,
        df: pd.DataFrame,
        scale_volatilities: Dict[str, pd.Series]
    ) -> pd.DataFrame:
        """
        Compute volatility-based gating weights for scale selection.
        
        In high volatility → prefer shorter timescales
        In low volatility → prefer longer timescales
        
        Args:
            df: Base DataFrame
            scale_volatilities: Dict mapping scale to volatility series
            
        Returns:
            DataFrame with gate weights per scale
        """
        df = df.copy()
        
        # Stack volatilities
        vol_matrix = pd.DataFrame(scale_volatilities)
        
        # Compute softmax weights (inverted - high vol → low weight for long scales)
        for scale in scale_volatilities.keys():
            # Inverse volatility weighting
            inv_vol = 1.0 / (vol_matrix[scale] + 1e-6)
            
            # Softmax normalization
            exp_inv_vol = np.exp(inv_vol - inv_vol.max())
            df[f'gate_weight_{scale}'] = exp_inv_vol / exp_inv_vol.sum()
        
        return df
    
    def transform(
        self,
        df: pd.DataFrame,
        include_cross_scale: bool = True,
        include_gates: bool = True
    ) -> Dict[str, pd.DataFrame]:
        """
        Transform base-scale data to multi-resolution features.
        
        Args:
            df: Base-scale OHLCV DataFrame (e.g., 1-minute bars)
            include_cross_scale: Add cross-scale features
            include_gates: Add volatility gating weights
            
        Returns:
            Dict mapping scale names to DataFrames with features
        """
        logger.info(f"Transforming {len(df)} rows of {self.base_scale.name} data")
        
        scale_data = {}
        scale_volatilities = {}
        
        # Process each timescale
        for scale in self.timescales:
            if scale.name == self.base_scale.name:
                # Base scale: use as-is
                df_scale = df.copy()
            else:
                # Resample to target scale
                df_scale = self.resample_ohlcv(df, scale)
            
            # Add indicators
            df_scale = self.add_scale_indicators(df_scale, scale.name)
            
            # Store
            scale_data[scale.name] = df_scale
            scale_volatilities[scale.name] = df_scale[f'volatility_{scale.name}']
            
            logger.info(f"  {scale.name}: {len(df_scale)} rows, {len(df_scale.columns)} features")
        
        # Add cross-scale features
        if include_cross_scale and len(self.timescales) > 1:
            logger.info("Computing cross-scale features...")
            base_with_cross = self.compute_cross_scale_features(scale_data)
            scale_data[self.base_scale.name] = base_with_cross
        
        # Add volatility gates
        if include_gates and len(self.timescales) > 1:
            logger.info("Computing volatility gates...")
            base_with_gates = self.compute_volatility_gates(
                scale_data[self.base_scale.name],
                scale_volatilities
            )
            scale_data[self.base_scale.name] = base_with_gates
        
        logger.info(f"Transformation complete: {sum(len(df.columns) for df in scale_data.values())} total features")
        
        return scale_data
    
    def get_feature_names(self, scale: str) -> List[str]:
        """
        Get list of feature names for a given scale.
        
        Args:
            scale: Scale name
            
        Returns:
            List of feature column names
        """
        # Base OHLCV
        features = ['open', 'high', 'low', 'close', 'volume']
        
        # Indicators with scale suffix
        indicators = [
            f'sma_20_{scale}', f'ema_20_{scale}',
            f'rsi_{scale}',
            f'macd_{scale}', f'macd_signal_{scale}', f'macd_hist_{scale}',
            f'bb_upper_{scale}', f'bb_lower_{scale}', f'bb_width_{scale}',
            f'atr_{scale}', f'volatility_{scale}',
            f'stoch_k_{scale}', f'stoch_d_{scale}',
            f'adx_{scale}',
            f'obv_{scale}', f'volume_sma_{scale}'
        ]
        
        return features + indicators


# Example usage
if __name__ == "__main__":
    from data.storage.timescale_writer import TimescaleWriter
    import logging
    
    logging.basicConfig(level=logging.INFO)
    
    # Initialize pipeline
    pipeline = MultiResolutionPipeline(
        timescales=['1min', '15min', '1hour', 'daily']
    )
    
    # Connect to database
    try:
        db = TimescaleWriter()
        logger.info("✓ Connected to TimescaleDB")
        
        # Fetch 1-minute data for AAPL
        query = """
            SELECT time as timestamp, open, high, low, close, volume
            FROM market_data
            WHERE symbol = 'AAPL' AND timeframe = '1min'
            ORDER BY time
            LIMIT 10000
        """
        
        conn = db.get_connection()
        df = pd.read_sql(query, conn)
        db.return_connection(conn)
        
        logger.info(f"Fetched {len(df)} rows of 1-minute data")
        
        # Transform to multi-resolution
        scale_data = pipeline.transform(df)
        
        # Show results
        for scale_name, df_scale in scale_data.items():
            print(f"\n{scale_name}:")
            print(f"  Shape: {df_scale.shape}")
            print(f"  Features: {list(df_scale.columns)[:10]}...")
            print(df_scale.tail(3))
        
        db.close()
    
    except Exception as e:
        logger.error(f"Error: {e}")
