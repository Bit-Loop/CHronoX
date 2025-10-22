"""
Backtesting Module

Components:
- BaseMLStrategy: Base strategy class with common functionality
- PerformanceAnalyzer: Comprehensive performance analysis and reporting
- Signal: Trading signal enum
- StrategyConfig: Strategy configuration
- PerformanceMetrics: Performance metrics container
"""

from .strategies import (
    BaseMLStrategy,
    Signal,
    StrategyConfig
)
from .analysis import (
    PerformanceAnalyzer,
    PerformanceMetrics
)

__all__ = [
    # Strategies
    'BaseMLStrategy',
    'Signal',
    'StrategyConfig',
    
    # Analysis
    'PerformanceAnalyzer',
    'PerformanceMetrics'
]
