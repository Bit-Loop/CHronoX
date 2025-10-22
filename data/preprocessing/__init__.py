"""
Data Preprocessing Package

Provides feature engineering and data transformation for ChronoX trading bot:
- Technical indicators (MA, RSI, MACD, BB, ATR, etc.)
- Multi-resolution aggregation (1min → daily)
- Cross-scale features
- Volatility gating
- Multi-timeframe feature integration
"""

from .indicators import (
    sma, ema, wma,
    rsi, macd, stochastic,
    bollinger_bands, atr, standard_deviation,
    obv, volume_sma,
    adx,
    add_all_indicators
)

from .multi_resolution_pipeline import (
    MultiResolutionPipeline,
    TimeScale,
    TIMESCALES
)

from .regime_detection import (
    MarketRegimeDetector,
    TrendRegime, VolatilityRegime, VolumeRegime,
    RegimeState
)

from .multi_timeframe_features import (
    TechnicalIndicators,
    FibonacciLevels,
    MultiTimeframeFeatureGenerator,
    FibonacciLevelsResult
)

from .multi_scale_aggregator import (
    MultiScaleAggregator,
    TimeScale,
    VolatilityRegime,
    ScaleStatistics,
    CrossScaleMetrics
)

__all__ = [
    # Indicators
    'sma', 'ema', 'wma',
    'rsi', 'macd', 'stochastic',
    'bollinger_bands', 'atr', 'standard_deviation',
    'obv', 'volume_sma',
    'adx',
    'add_all_indicators',
    
    # Multi-resolution
    'MultiResolutionPipeline',
    'TimeScale',
    'TIMESCALES',
    
    # Regime detection
    'MarketRegimeDetector',
    'TrendRegime', 'VolatilityRegime', 'VolumeRegime',
    'RegimeState',
    
    # Multi-timeframe features
    'TechnicalIndicators',
    'FibonacciLevels',
    'MultiTimeframeFeatureGenerator',
    'FibonacciLevelsResult',
    
    # Multi-scale aggregation
    'MultiScaleAggregator',
    'TimeScale',
    'VolatilityRegime',
    'ScaleStatistics',
    'CrossScaleMetrics',
]
