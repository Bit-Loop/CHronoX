"""
Training Module

Components:
- TimeSeriesDataLoader: Dataset and data loading utilities
- MultiScaleLoss: Multi-task loss function with coherence regularization
- RegimeAdaptiveTrainer: Volatility regime-aware training orchestrator
- ModelEvaluator: Comprehensive evaluation framework
"""

from .data_loader import TimeSeriesDataLoader
from .multi_scale_training import (
    MultiScaleLoss,
    RegimeAdaptiveTrainer,
    VolatilityRegime,
    RegimeParameters,
    REGIME_PARAMS,
    compute_auxiliary_stats
)
from .evaluate import (
    ModelEvaluator,
    RegressionMetrics,
    ClassificationMetrics,
    TradingMetrics
)

__all__ = [
    'TimeSeriesDataLoader',
    'MultiScaleLoss',
    'RegimeAdaptiveTrainer',
    'VolatilityRegime',
    'RegimeParameters',
    'REGIME_PARAMS',
    'compute_auxiliary_stats',
    'ModelEvaluator',
    'RegressionMetrics',
    'ClassificationMetrics',
    'TradingMetrics'
]

from .data_loader import TimeSeriesDataLoader
from .multi_scale_training import (
    MultiScaleLoss,
    RegimeAdaptiveTrainer,
    VolatilityRegime,
    RegimeParameters,
    REGIME_PARAMS,
    compute_auxiliary_stats
)

__all__ = [
    'TimeSeriesDataLoader',
    'MultiScaleLoss',
    'RegimeAdaptiveTrainer',
    'VolatilityRegime',
    'RegimeParameters',
    'REGIME_PARAMS',
    'compute_auxiliary_stats',
]
