"""
Bias Manager Module

Manages long/short bias based on market trend detection.
Dynamically adjusts portfolio bias in response to regime changes.

Bias Strategies:
- UPTREND: 70% long bias, 30% short allowed
- DOWNTREND: 30% long bias, 70% short bias
- SIDEWAYS: 50/50 neutral bias
- Adaptive: Bias strength scales with trend confidence

Usage:
    manager = BiasManager(config)
    bias = manager.detect_bias(market_data)
    long_pct, short_pct = manager.get_allocation_pcts(bias)
"""

import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class TrendDirection(Enum):
    """Market trend direction."""
    STRONG_UP = 3
    UP = 2
    SIDEWAYS = 1
    DOWN = 0
    STRONG_DOWN = -1


@dataclass
class BiasConfig:
    """Configuration for bias management."""
    
    # Trend detection parameters
    fast_ma_period: int = 20
    slow_ma_period: int = 50
    trend_strength_lookback: int = 100
    
    # Bias percentages by trend
    strong_uptrend_bias: float = 0.80  # 80% long
    uptrend_bias: float = 0.65         # 65% long
    sideways_bias: float = 0.50        # 50/50
    downtrend_bias: float = 0.35       # 35% long (65% short)
    strong_downtrend_bias: float = 0.20  # 20% long (80% short)
    
    # Trend strength thresholds
    strong_trend_threshold: float = 0.70  # 70% confidence
    weak_trend_threshold: float = 0.55    # 55% confidence
    
    # Regime transition smoothing
    bias_smoothing_factor: float = 0.3  # EMA factor for bias changes
    min_bias_change: float = 0.05       # Min 5% change to update
    
    # Risk limits
    max_net_exposure: float = 0.80  # Max 80% net long or short
    min_long_allocation: float = 0.10  # Always keep 10% long capacity
    min_short_allocation: float = 0.10  # Always keep 10% short capacity


@dataclass
class BiasState:
    """Current bias state."""
    
    trend_direction: TrendDirection
    trend_strength: float  # [0, 1]
    long_bias_pct: float   # [0, 1]
    short_bias_pct: float  # [0, 1]
    net_bias: float        # [-1, 1] where negative = short bias
    confidence: float      # [0, 1]


class BiasManager:
    """
    Long/Short bias manager with trend detection.
    
    Analyzes market trends and determines optimal long/short allocation.
    Smoothly transitions between bias regimes.
    
    Args:
        config: Bias configuration
    """
    
    def __init__(self, config: Optional[BiasConfig] = None):
        self.config = config or BiasConfig()
        
        # State tracking
        self.current_bias: Optional[BiasState] = None
        self.bias_history: List[BiasState] = []
        
        logger.info("BiasManager initialized")
        logger.info(f"Trend detection: MA({self.config.fast_ma_period}, {self.config.slow_ma_period})")
    
    def detect_bias(
        self,
        market_data: pd.DataFrame,
        price_col: str = 'close'
    ) -> BiasState:
        """
        Detect current market bias.
        
        Args:
            market_data: DataFrame with price data
            price_col: Column name for price
        
        Returns:
            BiasState object
        """
        # Detect trend direction and strength
        trend_direction, trend_strength = self._detect_trend(market_data, price_col)
        
        # Calculate bias percentages
        long_bias, short_bias = self._calculate_bias_pcts(trend_direction, trend_strength)
        
        # Calculate net bias
        net_bias = long_bias - short_bias
        
        # Calculate confidence
        confidence = self._calculate_confidence(trend_strength, market_data, price_col)
        
        # Create bias state
        new_bias = BiasState(
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            long_bias_pct=long_bias,
            short_bias_pct=short_bias,
            net_bias=net_bias,
            confidence=confidence
        )
        
        # Apply smoothing if previous bias exists
        if self.current_bias is not None:
            new_bias = self._smooth_bias_transition(self.current_bias, new_bias)
        
        # Update state
        self.current_bias = new_bias
        self.bias_history.append(new_bias)
        
        logger.debug(f"Bias detected: {trend_direction.name}, long={long_bias:.1%}, short={short_bias:.1%}")
        
        return new_bias
    
    def _detect_trend(
        self,
        data: pd.DataFrame,
        price_col: str
    ) -> Tuple[TrendDirection, float]:
        """
        Detect trend direction and strength.
        
        Uses moving average crossovers and momentum.
        
        Returns:
            (TrendDirection, strength [0, 1])
        """
        if len(data) < self.config.slow_ma_period:
            logger.warning("Insufficient data for trend detection")
            return TrendDirection.SIDEWAYS, 0.5
        
        prices = np.asarray(data[price_col].values)
        
        # Calculate moving averages
        fast_ma = self._moving_average(prices, self.config.fast_ma_period)
        slow_ma = self._moving_average(prices, self.config.slow_ma_period)
        
        # Current values
        current_price = prices[-1]
        current_fast_ma = fast_ma[-1]
        current_slow_ma = slow_ma[-1]
        
        # Trend direction
        if current_fast_ma > current_slow_ma:
            # Uptrend
            pct_above = (current_fast_ma - current_slow_ma) / current_slow_ma
            
            if pct_above > 0.05:  # >5% above
                direction = TrendDirection.STRONG_UP
                base_strength = 0.75
            else:
                direction = TrendDirection.UP
                base_strength = 0.60
        
        elif current_fast_ma < current_slow_ma:
            # Downtrend
            pct_below = (current_slow_ma - current_fast_ma) / current_slow_ma
            
            if pct_below > 0.05:  # >5% below
                direction = TrendDirection.STRONG_DOWN
                base_strength = 0.75
            else:
                direction = TrendDirection.DOWN
                base_strength = 0.60
        
        else:
            # Sideways
            direction = TrendDirection.SIDEWAYS
            base_strength = 0.50
        
        # Calculate trend strength using momentum
        lookback = min(len(prices), self.config.trend_strength_lookback)
        recent_prices = prices[-lookback:]
        
        # Linear regression slope
        x = np.arange(len(recent_prices))
        polyfit_result = np.polyfit(x, recent_prices, 1)  # type: ignore
        slope = polyfit_result[0]
        
        # Normalize slope
        avg_price = float(np.mean(recent_prices))
        normalized_slope = slope / avg_price
        
        # Combine with base strength
        momentum_strength = 0.5 + np.tanh(normalized_slope * 100) * 0.5
        final_strength = 0.7 * base_strength + 0.3 * momentum_strength
        
        return direction, float(final_strength)
    
    def _calculate_bias_pcts(
        self,
        direction: TrendDirection,
        strength: float
    ) -> Tuple[float, float]:
        """
        Calculate long and short bias percentages.
        
        Returns:
            (long_bias_pct, short_bias_pct)
        """
        # Base bias by direction
        if direction == TrendDirection.STRONG_UP:
            base_long = self.config.strong_uptrend_bias
        elif direction == TrendDirection.UP:
            base_long = self.config.uptrend_bias
        elif direction == TrendDirection.SIDEWAYS:
            base_long = self.config.sideways_bias
        elif direction == TrendDirection.DOWN:
            base_long = self.config.downtrend_bias
        else:  # STRONG_DOWN
            base_long = self.config.strong_downtrend_bias
        
        # Adjust based on strength
        # Stronger trend = more extreme bias
        neutral_bias = 0.50
        strength_adjusted_long = neutral_bias + (base_long - neutral_bias) * strength
        
        # Short bias is complement
        long_bias = strength_adjusted_long
        short_bias = 1.0 - long_bias
        
        # Apply limits
        long_bias = np.clip(
            long_bias,
            self.config.min_long_allocation,
            1.0 - self.config.min_short_allocation
        )
        short_bias = 1.0 - long_bias
        
        # Check net exposure limit
        net_bias = long_bias - short_bias
        if abs(net_bias) > self.config.max_net_exposure:
            # Scale back to limit
            if net_bias > 0:  # Long bias
                long_bias = (1.0 + self.config.max_net_exposure) / 2
                short_bias = 1.0 - long_bias
            else:  # Short bias
                short_bias = (1.0 + self.config.max_net_exposure) / 2
                long_bias = 1.0 - short_bias
        
        return float(long_bias), float(short_bias)
    
    def _calculate_confidence(
        self,
        trend_strength: float,
        data: pd.DataFrame,
        price_col: str
    ) -> float:
        """
        Calculate confidence in current bias.
        
        Higher confidence in strong, consistent trends.
        """
        # Base confidence from trend strength
        base_confidence = trend_strength
        
        # Check price consistency with trend
        if len(data) >= 20:
            recent_price_values = np.asarray(data[price_col].values[-20:])
            price_volatility = float(np.std(recent_price_values) / np.mean(recent_price_values))
            
            # Lower volatility = higher confidence
            vol_factor = 1.0 / (1.0 + 5.0 * price_volatility)
            
            confidence = 0.7 * base_confidence + 0.3 * vol_factor
        else:
            confidence = base_confidence
        
        return float(np.clip(confidence, 0.0, 1.0))
    
    def _smooth_bias_transition(
        self,
        old_bias: BiasState,
        new_bias: BiasState
    ) -> BiasState:
        """
        Smooth transition between bias states.
        
        Prevents rapid bias switching.
        """
        # Check if change is significant
        long_change = abs(new_bias.long_bias_pct - old_bias.long_bias_pct)
        
        if long_change < self.config.min_bias_change:
            # Change too small, keep old bias
            return old_bias
        
        # EMA smoothing
        alpha = self.config.bias_smoothing_factor
        
        smoothed_long = alpha * new_bias.long_bias_pct + (1 - alpha) * old_bias.long_bias_pct
        smoothed_short = 1.0 - smoothed_long
        smoothed_net = smoothed_long - smoothed_short
        
        smoothed_bias = BiasState(
            trend_direction=new_bias.trend_direction,
            trend_strength=new_bias.trend_strength,
            long_bias_pct=smoothed_long,
            short_bias_pct=smoothed_short,
            net_bias=smoothed_net,
            confidence=new_bias.confidence
        )
        
        return smoothed_bias
    
    def get_allocation_pcts(
        self,
        bias: Optional[BiasState] = None
    ) -> Tuple[float, float]:
        """
        Get current long and short allocation percentages.
        
        Args:
            bias: Optional BiasState (uses current if None)
        
        Returns:
            (long_pct, short_pct)
        """
        if bias is None:
            bias = self.current_bias
        
        if bias is None:
            logger.warning("No bias detected yet, using neutral")
            return 0.50, 0.50
        
        return bias.long_bias_pct, bias.short_bias_pct
    
    def should_take_long(self, bias: Optional[BiasState] = None) -> bool:
        """Check if should favor long positions."""
        if bias is None:
            bias = self.current_bias
        
        if bias is None:
            return True
        
        return bias.net_bias > 0
    
    def should_take_short(self, bias: Optional[BiasState] = None) -> bool:
        """Check if should favor short positions."""
        if bias is None:
            bias = self.current_bias
        
        if bias is None:
            return True
        
        return bias.net_bias < 0
    
    def get_bias_statistics(self) -> Dict[str, float]:
        """Get statistics on bias history."""
        if not self.bias_history:
            return {}
        
        long_biases = [b.long_bias_pct for b in self.bias_history]
        net_biases = [b.net_bias for b in self.bias_history]
        strengths = [b.trend_strength for b in self.bias_history]
        
        # Count trend directions
        direction_counts = {}
        for bias in self.bias_history:
            direction = bias.trend_direction.name
            direction_counts[direction] = direction_counts.get(direction, 0) + 1
        
        total = len(self.bias_history)
        
        stats = {
            'mean_long_bias': float(np.mean(long_biases)),
            'mean_net_bias': float(np.mean(net_biases)),
            'mean_trend_strength': float(np.mean(strengths)),
            'pct_long_bias': float((np.array(net_biases) > 0).mean() * 100),
            'pct_short_bias': float((np.array(net_biases) < 0).mean() * 100),
            'pct_neutral': float((np.abs(net_biases) < 0.1).mean() * 100)
        }
        
        # Add direction percentages
        for direction, count in direction_counts.items():
            stats[f'pct_{direction.lower()}'] = (count / total) * 100
        
        return stats
    
    @staticmethod
    def _moving_average(data: np.ndarray, period: int) -> np.ndarray:
        """Calculate simple moving average."""
        if len(data) < period:
            return np.array([data.mean()] * len(data))
        
        cumsum = np.cumsum(data)
        cumsum[period:] = cumsum[period:] - cumsum[:-period]
        ma = cumsum[period - 1:] / period
        
        # Pad beginning
        padded = np.concatenate([np.array([data[:period].mean()] * (period - 1)), ma])
        
        return padded


if __name__ == '__main__':
    # Test bias manager
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Bias Manager")
    print("="*60)
    
    # Initialize manager
    print("\n✓ Initializing BiasManager...")
    config = BiasConfig()
    manager = BiasManager(config)
    
    print(f"  Fast MA: {config.fast_ma_period}")
    print(f"  Slow MA: {config.slow_ma_period}")
    print(f"  Bias range: {config.strong_downtrend_bias:.0%} - {config.strong_uptrend_bias:.0%}")
    
    # Generate synthetic market data
    print("\n✓ Generating synthetic market scenarios...")
    np.random.seed(42)
    n_points = 200
    
    scenarios = {
        'Strong Uptrend': 100 * (1 + np.linspace(0, 0.30, n_points) + np.random.randn(n_points) * 0.01),
        'Weak Uptrend': 100 * (1 + np.linspace(0, 0.10, n_points) + np.random.randn(n_points) * 0.02),
        'Sideways': 100 * (1 + np.random.randn(n_points) * 0.02),
        'Weak Downtrend': 100 * (1 - np.linspace(0, 0.10, n_points) + np.random.randn(n_points) * 0.02),
        'Strong Downtrend': 100 * (1 - np.linspace(0, 0.30, n_points) + np.random.randn(n_points) * 0.01)
    }
    
    for scenario_name, prices in scenarios.items():
        df = pd.DataFrame({'close': prices})
        
        bias = manager.detect_bias(df)
        
        print(f"\n  {scenario_name}:")
        print(f"    Direction: {bias.trend_direction.name}")
        print(f"    Strength: {bias.trend_strength:.2f}")
        print(f"    Long bias: {bias.long_bias_pct:.1%}")
        print(f"    Short bias: {bias.short_bias_pct:.1%}")
        print(f"    Net bias: {bias.net_bias:+.1%}")
        print(f"    Confidence: {bias.confidence:.2f}")
    
    # Test bias smoothing
    print("\n✓ Testing bias transition smoothing...")
    
    # Create uptrend data
    uptrend_prices = 100 * (1 + np.linspace(0, 0.20, 100))
    df_up = pd.DataFrame({'close': uptrend_prices})
    
    bias1 = manager.detect_bias(df_up)
    print(f"  Initial (uptrend): long={bias1.long_bias_pct:.1%}")
    
    # Sudden shift to downtrend
    downtrend_prices = 100 * (1 - np.linspace(0, 0.20, 100))
    df_down = pd.DataFrame({'close': downtrend_prices})
    
    bias2 = manager.detect_bias(df_down)
    print(f"  After shift (downtrend): long={bias2.long_bias_pct:.1%}")
    print(f"  (Smoothing applied: {config.bias_smoothing_factor:.0%} EMA)")
    
    # Get statistics
    print("\n✓ Bias usage statistics:")
    stats = manager.get_bias_statistics()
    for key, value in stats.items():
        if 'pct_' in key:
            print(f"  {key}: {value:.1f}%")
        else:
            print(f"  {key}: {value:.3f}")
    
    print("\n" + "="*60)
    print("Bias Manager Test Complete!")
    print("="*60)
