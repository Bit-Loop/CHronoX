"""
Reinforcement Learning Agents Module

Components:
- TradingEnvironment: OpenAI Gym-compatible trading environment
- PositionSizer: Kelly Criterion + risk-adjusted position sizing
- LeverageManager: Dynamic leverage with regime awareness
- BiasManager: Long/short bias based on trend detection
"""

from .trading_env import (
    TradingEnvironment,
    TradingConfig,
    Position,
    VolatilityRegime
)
from .position_sizer import (
    PositionSizer,
    PositionSizingConfig
)
from .leverage_manager import (
    LeverageManager,
    LeverageConfig,
    MarketRegime
)
from .bias_manager import (
    BiasManager,
    BiasConfig,
    BiasState,
    TrendDirection
)

__all__ = [
    # Trading Environment
    'TradingEnvironment',
    'TradingConfig',
    'Position',
    'VolatilityRegime',
    
    # Position Sizing
    'PositionSizer',
    'PositionSizingConfig',
    
    # Leverage Management
    'LeverageManager',
    'LeverageConfig',
    'MarketRegime',
    
    # Bias Management
    'BiasManager',
    'BiasConfig',
    'BiasState',
    'TrendDirection'
]
