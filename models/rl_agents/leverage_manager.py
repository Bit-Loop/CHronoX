"""
Leverage Management Module

Dynamic leverage adjustment based on market conditions and risk state.
Implements regime-aware leverage caps and confidence-based scaling.

Leverage Rules:
- CALM regime: Up to 2.0x leverage
- NORMAL regime: Up to 1.5x leverage  
- VOLATILE regime: Up to 1.2x leverage
- CRISIS regime: No leverage (1.0x only)
- Drawdown cutback: Reduce leverage during losses
- Confidence scaling: Higher conviction = higher leverage

Usage:
    manager = LeverageManager(config)
    leverage = manager.calculate_leverage(
        regime='VOLATILE',
        confidence=0.8,
        current_drawdown=0.05,
        win_rate=0.65
    )
"""

import numpy as np
from typing import Dict, Optional
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """Market volatility regimes."""
    CALM = 0
    NORMAL = 1
    VOLATILE = 2
    CRISIS = 3


@dataclass
class LeverageConfig:
    """Configuration for leverage management."""
    
    # Regime-based leverage limits
    regime_max_leverage: Dict[str, float] = None
    
    # Confidence-based scaling
    min_confidence_for_leverage: float = 0.60  # Need 60% confidence for any leverage
    confidence_scaling_factor: float = 1.0  # Linear scaling with confidence
    
    # Drawdown-based cutbacks
    drawdown_threshold_1: float = 0.05  # 5% drawdown: reduce to 75% leverage
    drawdown_threshold_2: float = 0.10  # 10% drawdown: reduce to 50% leverage
    drawdown_threshold_3: float = 0.15  # 15% drawdown: reduce to 25% leverage
    
    # Win rate adjustment
    min_win_rate_for_leverage: float = 0.52  # Need >52% win rate
    win_rate_scaling: float = 0.5  # Moderate scaling with win rate
    
    # Hard limits
    absolute_max_leverage: float = 3.0  # Never exceed 3x
    default_base_leverage: float = 1.0
    
    # Asset class limits (override regime limits if stricter)
    asset_class_limits: Dict[str, float] = None
    
    def __post_init__(self):
        if self.regime_max_leverage is None:
            self.regime_max_leverage = {
                'CALM': 2.0,
                'NORMAL': 1.5,
                'VOLATILE': 1.2,
                'CRISIS': 1.0
            }
        
        if self.asset_class_limits is None:
            self.asset_class_limits = {
                'crypto': 1.5,      # Crypto max 1.5x
                'forex': 3.0,       # FX max 3.0x
                'stocks': 2.0,      # Stocks max 2.0x
                'commodities': 2.5  # Commodities max 2.5x
            }


class LeverageManager:
    """
    Dynamic leverage management with regime awareness.
    
    Calculates optimal leverage based on:
    - Market regime (volatility state)
    - Model confidence
    - Current drawdown
    - Win rate
    - Asset class
    
    Args:
        config: Leverage configuration
    """
    
    def __init__(self, config: Optional[LeverageConfig] = None):
        self.config = config or LeverageConfig()
        self.leverage_history = []
        self.regime_history = []
        
        logger.info("LeverageManager initialized")
        logger.info(f"Regime limits: {self.config.regime_max_leverage}")
    
    def calculate_leverage(
        self,
        regime: str,
        confidence: float,
        current_drawdown: float = 0.0,
        win_rate: Optional[float] = None,
        asset_class: Optional[str] = None,
        volatility: Optional[float] = None
    ) -> float:
        """
        Calculate optimal leverage for current conditions.
        
        Args:
            regime: Market regime (CALM/NORMAL/VOLATILE/CRISIS)
            confidence: Model confidence [0, 1]
            current_drawdown: Current drawdown percentage [0, 1]
            win_rate: Recent win rate [0, 1]
            asset_class: Asset class name
            volatility: Current volatility (annualized)
        
        Returns:
            Optimal leverage multiplier
        """
        # Start with regime-based base
        base_leverage = self._get_regime_leverage(regime)
        
        # Apply confidence scaling
        confidence_adjusted = self._apply_confidence_scaling(base_leverage, confidence)
        
        # Apply drawdown cutback
        drawdown_adjusted = self._apply_drawdown_cutback(confidence_adjusted, current_drawdown)
        
        # Apply win rate adjustment
        if win_rate is not None:
            drawdown_adjusted = self._apply_win_rate_adjustment(drawdown_adjusted, win_rate)
        
        # Apply asset class limit
        if asset_class is not None:
            drawdown_adjusted = self._apply_asset_class_limit(drawdown_adjusted, asset_class)
        
        # Apply volatility adjustment
        if volatility is not None:
            drawdown_adjusted = self._apply_volatility_adjustment(drawdown_adjusted, volatility)
        
        # Enforce absolute maximum
        final_leverage = min(drawdown_adjusted, self.config.absolute_max_leverage)
        
        # Ensure >= 1.0
        final_leverage = max(final_leverage, 1.0)
        
        # Record history
        self.leverage_history.append(final_leverage)
        self.regime_history.append(regime)
        
        logger.debug(f"Calculated leverage: {final_leverage:.2f}x (regime={regime}, conf={confidence:.2f})")
        
        return final_leverage
    
    def _get_regime_leverage(self, regime: str) -> float:
        """Get base leverage for regime."""
        regime_upper = regime.upper()
        
        if regime_upper not in self.config.regime_max_leverage:
            logger.warning(f"Unknown regime: {regime}, using NORMAL")
            regime_upper = 'NORMAL'
        
        leverage = self.config.regime_max_leverage[regime_upper]
        
        logger.debug(f"Regime leverage ({regime}): {leverage:.2f}x")
        
        return leverage
    
    def _apply_confidence_scaling(self, base_leverage: float, confidence: float) -> float:
        """
        Scale leverage based on model confidence.
        
        Low confidence = reduce leverage
        High confidence = allow full leverage
        """
        if confidence < self.config.min_confidence_for_leverage:
            logger.debug(f"Confidence too low ({confidence:.2f}), leverage = 1.0x")
            return 1.0
        
        # Normalize confidence to [0, 1] range above threshold
        normalized_conf = (confidence - self.config.min_confidence_for_leverage) / \
                         (1.0 - self.config.min_confidence_for_leverage)
        
        # Apply scaling
        scaling = normalized_conf ** self.config.confidence_scaling_factor
        
        adjusted_leverage = 1.0 + (base_leverage - 1.0) * scaling
        
        logger.debug(f"Confidence scaling: {base_leverage:.2f}x → {adjusted_leverage:.2f}x (conf={confidence:.2f})")
        
        return adjusted_leverage
    
    def _apply_drawdown_cutback(self, current_leverage: float, drawdown: float) -> float:
        """
        Reduce leverage during drawdowns.
        
        Progressive cutbacks at increasing drawdown levels.
        """
        if drawdown < self.config.drawdown_threshold_1:
            # No cutback
            return current_leverage
        
        elif drawdown < self.config.drawdown_threshold_2:
            # 5-10% drawdown: reduce to 75%
            cutback = 0.75
            
        elif drawdown < self.config.drawdown_threshold_3:
            # 10-15% drawdown: reduce to 50%
            cutback = 0.50
            
        else:
            # >15% drawdown: reduce to 25%
            cutback = 0.25
        
        adjusted_leverage = 1.0 + (current_leverage - 1.0) * cutback
        
        logger.debug(f"Drawdown cutback: {current_leverage:.2f}x → {adjusted_leverage:.2f}x (dd={drawdown:.1%})")
        
        return adjusted_leverage
    
    def _apply_win_rate_adjustment(self, current_leverage: float, win_rate: float) -> float:
        """
        Adjust leverage based on recent win rate.
        
        Poor win rate = reduce leverage
        Good win rate = maintain leverage
        """
        if win_rate < self.config.min_win_rate_for_leverage:
            logger.debug(f"Win rate too low ({win_rate:.1%}), leverage = 1.0x")
            return 1.0
        
        # Normalize win rate
        normalized_wr = (win_rate - self.config.min_win_rate_for_leverage) / \
                       (1.0 - self.config.min_win_rate_for_leverage)
        
        # Apply scaling
        scaling = normalized_wr ** self.config.win_rate_scaling
        
        adjusted_leverage = 1.0 + (current_leverage - 1.0) * scaling
        
        logger.debug(f"Win rate adjustment: {current_leverage:.2f}x → {adjusted_leverage:.2f}x (wr={win_rate:.1%})")
        
        return adjusted_leverage
    
    def _apply_asset_class_limit(self, current_leverage: float, asset_class: str) -> float:
        """
        Apply asset class-specific leverage limit.
        
        Some assets (e.g., crypto) may have stricter limits.
        """
        asset_lower = asset_class.lower()
        
        if asset_lower not in self.config.asset_class_limits:
            logger.debug(f"Unknown asset class: {asset_class}")
            return current_leverage
        
        limit = self.config.asset_class_limits[asset_lower]
        
        if current_leverage > limit:
            logger.debug(f"Asset class limit: {current_leverage:.2f}x → {limit:.2f}x ({asset_class})")
            return limit
        
        return current_leverage
    
    def _apply_volatility_adjustment(self, current_leverage: float, volatility: float) -> float:
        """
        Reduce leverage in high volatility environments.
        
        Volatility and leverage are inversely related.
        """
        # Base volatility: 20% annual
        base_vol = 0.20
        
        if volatility <= base_vol:
            return current_leverage
        
        # Scale down leverage as volatility increases
        vol_ratio = volatility / base_vol
        
        # Inverse square root relationship
        vol_factor = 1.0 / np.sqrt(vol_ratio)
        
        adjusted_leverage = 1.0 + (current_leverage - 1.0) * vol_factor
        
        logger.debug(f"Volatility adjustment: {current_leverage:.2f}x → {adjusted_leverage:.2f}x (vol={volatility:.1%})")
        
        return adjusted_leverage
    
    def get_max_leverage_for_regime(self, regime: str) -> float:
        """Get maximum allowed leverage for regime."""
        return self.config.regime_max_leverage.get(regime.upper(), 1.0)
    
    def is_leverage_safe(
        self,
        current_leverage: float,
        regime: str,
        drawdown: float
    ) -> bool:
        """
        Check if current leverage is safe.
        
        Returns True if leverage is within safe limits.
        """
        max_allowed = self.get_max_leverage_for_regime(regime)
        
        # Check regime limit
        if current_leverage > max_allowed:
            logger.warning(f"Leverage exceeds regime limit: {current_leverage:.2f}x > {max_allowed:.2f}x")
            return False
        
        # Check drawdown-adjusted limit
        if drawdown >= self.config.drawdown_threshold_3:
            # In severe drawdown, leverage should be minimal
            if current_leverage > 1.25:
                logger.warning(f"Leverage too high during drawdown: {current_leverage:.2f}x (dd={drawdown:.1%})")
                return False
        
        # Check absolute limit
        if current_leverage > self.config.absolute_max_leverage:
            logger.warning(f"Leverage exceeds absolute max: {current_leverage:.2f}x")
            return False
        
        return True
    
    def get_leverage_statistics(self) -> Dict[str, float]:
        """Get statistics on leverage usage."""
        if not self.leverage_history:
            return {}
        
        leverage_array = np.array(self.leverage_history)
        
        stats = {
            'mean_leverage': float(leverage_array.mean()),
            'max_leverage': float(leverage_array.max()),
            'min_leverage': float(leverage_array.min()),
            'std_leverage': float(leverage_array.std()),
            'median_leverage': float(np.median(leverage_array)),
            'pct_above_1x': float((leverage_array > 1.0).mean() * 100),
            'pct_above_1_5x': float((leverage_array > 1.5).mean() * 100),
            'pct_above_2x': float((leverage_array > 2.0).mean() * 100)
        }
        
        return stats


if __name__ == '__main__':
    # Test leverage management
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Leverage Management")
    print("="*60)
    
    # Initialize manager
    print("\n✓ Initializing LeverageManager...")
    config = LeverageConfig()
    manager = LeverageManager(config)
    
    print(f"  Regime limits: {config.regime_max_leverage}")
    print(f"  Min confidence: {config.min_confidence_for_leverage:.0%}")
    print(f"  Absolute max: {config.absolute_max_leverage:.1f}x")
    
    # Test regime-based leverage
    print("\n✓ Testing regime-based leverage...")
    for regime in ['CALM', 'NORMAL', 'VOLATILE', 'CRISIS']:
        leverage = manager.calculate_leverage(
            regime=regime,
            confidence=0.80,
            current_drawdown=0.0
        )
        print(f"  {regime:8s}: {leverage:.2f}x")
    
    # Test confidence scaling
    print("\n✓ Testing confidence scaling...")
    regime = 'NORMAL'
    for confidence in [0.50, 0.65, 0.80, 0.95]:
        leverage = manager.calculate_leverage(
            regime=regime,
            confidence=confidence,
            current_drawdown=0.0
        )
        print(f"  Confidence {confidence:.0%}: {leverage:.2f}x")
    
    # Test drawdown cutbacks
    print("\n✓ Testing drawdown cutbacks...")
    regime = 'NORMAL'
    confidence = 0.80
    for drawdown in [0.00, 0.05, 0.10, 0.15, 0.20]:
        leverage = manager.calculate_leverage(
            regime=regime,
            confidence=confidence,
            current_drawdown=drawdown
        )
        print(f"  Drawdown {drawdown:.0%}: {leverage:.2f}x")
    
    # Test win rate adjustment
    print("\n✓ Testing win rate adjustment...")
    regime = 'NORMAL'
    confidence = 0.80
    for win_rate in [0.45, 0.55, 0.65, 0.75]:
        leverage = manager.calculate_leverage(
            regime=regime,
            confidence=confidence,
            current_drawdown=0.0,
            win_rate=win_rate
        )
        print(f"  Win rate {win_rate:.0%}: {leverage:.2f}x")
    
    # Test asset class limits
    print("\n✓ Testing asset class limits...")
    regime = 'CALM'  # Max 2.0x normally
    confidence = 0.90
    for asset_class in ['stocks', 'crypto', 'forex', 'commodities']:
        leverage = manager.calculate_leverage(
            regime=regime,
            confidence=confidence,
            current_drawdown=0.0,
            asset_class=asset_class
        )
        print(f"  {asset_class:12s}: {leverage:.2f}x")
    
    # Test volatility adjustment
    print("\n✓ Testing volatility adjustment...")
    regime = 'NORMAL'
    confidence = 0.80
    for volatility in [0.15, 0.25, 0.40, 0.60]:
        leverage = manager.calculate_leverage(
            regime=regime,
            confidence=confidence,
            current_drawdown=0.0,
            volatility=volatility
        )
        print(f"  Volatility {volatility:.0%}: {leverage:.2f}x")
    
    # Test combined scenario
    print("\n✓ Testing combined scenarios...")
    scenarios = [
        {
            'name': 'Ideal conditions',
            'regime': 'CALM',
            'confidence': 0.90,
            'drawdown': 0.00,
            'win_rate': 0.70,
            'volatility': 0.15
        },
        {
            'name': 'Moderate risk',
            'regime': 'NORMAL',
            'confidence': 0.75,
            'drawdown': 0.03,
            'win_rate': 0.60,
            'volatility': 0.25
        },
        {
            'name': 'High risk',
            'regime': 'VOLATILE',
            'confidence': 0.60,
            'drawdown': 0.08,
            'win_rate': 0.55,
            'volatility': 0.45
        },
        {
            'name': 'Crisis mode',
            'regime': 'CRISIS',
            'confidence': 0.50,
            'drawdown': 0.12,
            'win_rate': 0.48,
            'volatility': 0.70
        }
    ]
    
    for scenario in scenarios:
        leverage = manager.calculate_leverage(
            regime=scenario['regime'],
            confidence=scenario['confidence'],
            current_drawdown=scenario['drawdown'],
            win_rate=scenario['win_rate'],
            volatility=scenario['volatility']
        )
        
        is_safe = manager.is_leverage_safe(
            leverage,
            scenario['regime'],
            scenario['drawdown']
        )
        
        print(f"\n  {scenario['name']}:")
        print(f"    Regime: {scenario['regime']}")
        print(f"    Confidence: {scenario['confidence']:.0%}")
        print(f"    Drawdown: {scenario['drawdown']:.0%}")
        print(f"    Win rate: {scenario['win_rate']:.0%}")
        print(f"    Volatility: {scenario['volatility']:.0%}")
        print(f"    → Leverage: {leverage:.2f}x {'✓' if is_safe else '✗ UNSAFE'}")
    
    # Get statistics
    print("\n✓ Leverage usage statistics:")
    stats = manager.get_leverage_statistics()
    for key, value in stats.items():
        print(f"  {key}: {value:.2f}")
    
    print("\n" + "="*60)
    print("Leverage Management Test Complete!")
    print("="*60)
