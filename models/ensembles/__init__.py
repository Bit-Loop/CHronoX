"""
Ensembles Module

Components:
- SignalEnsemble: Multi-model signal aggregation with regime-aware weighting
- MarketRegime: Market regime classification
- SignalType: Trading signal enumeration
- SignalSource: Individual signal source representation
- EnsembleConfig: Ensemble configuration
"""

from .signal_ensemble import (
    SignalEnsemble,
    MarketRegime,
    SignalType,
    SignalSource,
    EnsembleConfig
)

__all__ = [
    'SignalEnsemble',
    'MarketRegime',
    'SignalType',
    'SignalSource',
    'EnsembleConfig'
]
