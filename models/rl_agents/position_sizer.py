"""
Position Sizing Module

Implements sophisticated position sizing strategies including:
- Kelly Criterion for optimal bet sizing
- Volatility adjustment (reduce size in high volatility)
- Liquidity adjustment (reduce size for illiquid assets)
- Leverage scaling (reduce size when leveraged)
- Hard limits and safety caps

Usage:
    sizer = PositionSizer(config)
    position_size = sizer.calculate_size(
        capital=100000,
        win_probability=0.65,
        win_loss_ratio=1.5,
        volatility=0.25,
        liquidity_score=0.8,
        current_leverage=1.2
    )
"""

import numpy as np
from typing import Dict, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class PositionSizingConfig:
    """Configuration for position sizing."""
    
    # Kelly parameters
    kelly_fraction: float = 0.25  # Conservative Kelly (1/4 Kelly)
    max_kelly_position: float = 0.20  # Max 20% per position
    min_position_size: float = 0.01  # Min 1% per position
    
    # Volatility adjustment
    base_volatility: float = 0.20  # 20% annual volatility baseline
    vol_scaling_factor: float = 0.5  # Reduce size by 50% if vol doubles
    
    # Liquidity adjustment
    min_liquidity_score: float = 0.3  # Min 30% liquidity
    liquidity_scaling: float = 1.0  # Linear scaling with liquidity
    
    # Leverage adjustment
    leverage_penalty: float = 0.5  # Reduce size by 50% at 2x leverage
    max_leverage_for_sizing: float = 3.0  # Don't size if leverage > 3x
    
    # Absolute limits
    max_position_pct: float = 0.25  # Never exceed 25% per position
    max_total_exposure: float = 1.0  # Never exceed 100% total
    
    # Risk management
    max_loss_per_trade: float = 0.02  # Max 2% loss per trade
    max_correlated_exposure: float = 0.40  # Max 40% in correlated assets


class PositionSizer:
    """
    Sophisticated position sizing engine.
    
    Combines Kelly Criterion with risk adjustments for volatility,
    liquidity, leverage, and hard limits.
    
    Args:
        config: Position sizing configuration
    """
    
    def __init__(self, config: Optional[PositionSizingConfig] = None):
        self.config = config or PositionSizingConfig()
        logger.info("PositionSizer initialized")
    
    def calculate_size(
        self,
        capital: float,
        win_probability: float,
        win_loss_ratio: float,
        volatility: Optional[float] = None,
        liquidity_score: Optional[float] = None,
        current_leverage: Optional[float] = None,
        confidence: Optional[float] = None
    ) -> float:
        """
        Calculate optimal position size.
        
        Args:
            capital: Available trading capital
            win_probability: Estimated probability of winning trade
            win_loss_ratio: Ratio of average win to average loss
            volatility: Asset volatility (annualized)
            liquidity_score: Liquidity score [0, 1]
            current_leverage: Current portfolio leverage
            confidence: Model confidence [0, 1]
        
        Returns:
            Position size in dollar amount
        """
        # Base Kelly size
        kelly_size = self._kelly_criterion(win_probability, win_loss_ratio)
        
        # Apply Kelly fraction (conservative scaling)
        kelly_size *= self.config.kelly_fraction
        
        # Cap at max Kelly position
        kelly_size = min(kelly_size, self.config.max_kelly_position)
        
        # Adjust for volatility
        if volatility is not None:
            vol_adjustment = self._volatility_adjustment(volatility)
            kelly_size *= vol_adjustment
        
        # Adjust for liquidity
        if liquidity_score is not None:
            liq_adjustment = self._liquidity_adjustment(liquidity_score)
            kelly_size *= liq_adjustment
        
        # Adjust for leverage
        if current_leverage is not None:
            lev_adjustment = self._leverage_adjustment(current_leverage)
            kelly_size *= lev_adjustment
        
        # Adjust for confidence
        if confidence is not None:
            conf_adjustment = self._confidence_adjustment(confidence)
            kelly_size *= conf_adjustment
        
        # Apply absolute limits
        kelly_size = np.clip(
            kelly_size,
            self.config.min_position_size,
            self.config.max_position_pct
        )
        
        # Convert to dollar amount
        position_size = capital * kelly_size
        
        logger.debug(f"Position size: {kelly_size:.2%} of capital (${position_size:,.2f})")
        
        return position_size
    
    def _kelly_criterion(self, win_prob: float, win_loss_ratio: float) -> float:
        """
        Calculate Kelly Criterion optimal bet size.
        
        Formula: f* = (p*b - q) / b
        where:
            p = probability of winning
            q = probability of losing (1-p)
            b = win/loss ratio
        
        Args:
            win_prob: Probability of winning trade
            win_loss_ratio: Average win / average loss
        
        Returns:
            Optimal fraction of capital to bet
        """
        if win_prob <= 0 or win_prob >= 1:
            logger.warning(f"Invalid win probability: {win_prob}")
            return 0.0
        
        if win_loss_ratio <= 0:
            logger.warning(f"Invalid win/loss ratio: {win_loss_ratio}")
            return 0.0
        
        p = win_prob
        q = 1 - win_prob
        b = win_loss_ratio
        
        kelly = (p * b - q) / b
        
        # Kelly can be negative if edge is negative
        if kelly < 0:
            logger.debug(f"Negative Kelly: {kelly:.4f} (no edge)")
            return 0.0
        
        return kelly
    
    def _volatility_adjustment(self, volatility: float) -> float:
        """
        Adjust position size based on volatility.
        
        Higher volatility = smaller position size
        
        Args:
            volatility: Asset volatility (annualized)
        
        Returns:
            Adjustment multiplier [0, 1]
        """
        if volatility <= 0:
            return 1.0
        
        # Scale based on ratio to base volatility
        vol_ratio = volatility / self.config.base_volatility
        
        # Inverse relationship: higher vol = smaller size
        adjustment = 1.0 / (1.0 + self.config.vol_scaling_factor * (vol_ratio - 1.0))
        
        # Clip to reasonable range
        adjustment = np.clip(adjustment, 0.2, 1.5)
        
        logger.debug(f"Volatility adjustment: {adjustment:.3f} (vol={volatility:.2%})")
        
        return adjustment
    
    def _liquidity_adjustment(self, liquidity_score: float) -> float:
        """
        Adjust position size based on liquidity.
        
        Lower liquidity = smaller position size
        
        Args:
            liquidity_score: Liquidity score [0, 1]
        
        Returns:
            Adjustment multiplier [0, 1]
        """
        if liquidity_score < self.config.min_liquidity_score:
            logger.warning(f"Liquidity too low: {liquidity_score:.2f}")
            return 0.0
        
        # Linear scaling with liquidity
        adjustment = liquidity_score ** self.config.liquidity_scaling
        
        logger.debug(f"Liquidity adjustment: {adjustment:.3f} (score={liquidity_score:.2f})")
        
        return adjustment
    
    def _leverage_adjustment(self, current_leverage: float) -> float:
        """
        Adjust position size based on current leverage.
        
        Higher leverage = smaller new positions
        
        Args:
            current_leverage: Current portfolio leverage
        
        Returns:
            Adjustment multiplier [0, 1]
        """
        if current_leverage >= self.config.max_leverage_for_sizing:
            logger.warning(f"Leverage too high: {current_leverage:.2f}x")
            return 0.0
        
        if current_leverage <= 1.0:
            return 1.0  # No penalty for unleveraged
        
        # Exponential decay with leverage
        excess_leverage = current_leverage - 1.0
        adjustment = np.exp(-self.config.leverage_penalty * excess_leverage)
        
        logger.debug(f"Leverage adjustment: {adjustment:.3f} (leverage={current_leverage:.2f}x)")
        
        return adjustment
    
    def _confidence_adjustment(self, confidence: float) -> float:
        """
        Adjust position size based on model confidence.
        
        Lower confidence = smaller position size
        
        Args:
            confidence: Model confidence [0, 1]
        
        Returns:
            Adjustment multiplier [0, 1]
        """
        # Quadratic scaling: require high confidence for full size
        adjustment = confidence ** 2
        
        logger.debug(f"Confidence adjustment: {adjustment:.3f} (conf={confidence:.2f})")
        
        return adjustment
    
    def calculate_stop_loss_size(
        self,
        capital: float,
        entry_price: float,
        stop_loss_price: float,
        max_loss_pct: Optional[float] = None
    ) -> float:
        """
        Calculate position size based on stop-loss.
        
        Ensures maximum loss does not exceed max_loss_pct of capital.
        
        Args:
            capital: Available capital
            entry_price: Entry price
            stop_loss_price: Stop-loss price
            max_loss_pct: Maximum loss percentage (default: from config)
        
        Returns:
            Maximum position size (shares/units)
        """
        max_loss_pct = max_loss_pct or self.config.max_loss_per_trade
        
        # Calculate per-share loss
        loss_per_share = abs(entry_price - stop_loss_price)
        
        if loss_per_share == 0:
            logger.warning("Stop loss equals entry price")
            return 0.0
        
        # Maximum capital to risk
        max_risk = capital * max_loss_pct
        
        # Maximum shares
        max_shares = max_risk / loss_per_share
        
        logger.debug(f"Stop-loss sizing: {max_shares:.2f} shares (risk=${max_risk:,.2f})")
        
        return max_shares
    
    def validate_portfolio_limits(
        self,
        new_position_size: float,
        current_positions: Dict[str, float],
        correlations: Optional[Dict[str, float]] = None
    ) -> bool:
        """
        Validate that new position respects portfolio limits.
        
        Args:
            new_position_size: Proposed new position size
            current_positions: Dictionary of current positions {asset: size}
            correlations: Dictionary of correlations with existing positions
        
        Returns:
            True if position is valid, False otherwise
        """
        # Check max position size
        if new_position_size > self.config.max_position_pct:
            logger.warning(f"Position exceeds max: {new_position_size:.2%}")
            return False
        
        # Check total exposure
        total_exposure = new_position_size + sum(current_positions.values())
        if total_exposure > self.config.max_total_exposure:
            logger.warning(f"Total exposure exceeds max: {total_exposure:.2%}")
            return False
        
        # Check correlated exposure
        if correlations:
            correlated_exposure = new_position_size
            for asset, size in current_positions.items():
                if asset in correlations:
                    # Weight by correlation
                    correlated_exposure += size * abs(correlations[asset])
            
            if correlated_exposure > self.config.max_correlated_exposure:
                logger.warning(f"Correlated exposure too high: {correlated_exposure:.2%}")
                return False
        
        return True
    
    def adjust_for_regime(
        self,
        base_size: float,
        regime: str
    ) -> float:
        """
        Adjust position size based on market regime.
        
        Args:
            base_size: Base position size
            regime: Market regime (CALM/NORMAL/VOLATILE/CRISIS)
        
        Returns:
            Adjusted position size
        """
        regime_multipliers = {
            'CALM': 1.2,      # Increase size in calm markets
            'NORMAL': 1.0,    # Normal size
            'VOLATILE': 0.6,  # Reduce size in volatile markets
            'CRISIS': 0.3     # Minimal size in crisis
        }
        
        multiplier = regime_multipliers.get(regime, 1.0)
        adjusted_size = base_size * multiplier
        
        logger.debug(f"Regime adjustment: {multiplier:.2f}x ({regime})")
        
        return adjusted_size


if __name__ == '__main__':
    # Test position sizing
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Position Sizing")
    print("="*60)
    
    # Initialize sizer
    print("\n✓ Initializing PositionSizer...")
    config = PositionSizingConfig()
    sizer = PositionSizer(config)
    
    print(f"  Kelly fraction: {config.kelly_fraction}")
    print(f"  Max position: {config.max_position_pct:.0%}")
    print(f"  Max loss per trade: {config.max_loss_per_trade:.0%}")
    
    # Test Kelly Criterion
    print("\n✓ Testing Kelly Criterion...")
    test_cases = [
        (0.60, 1.5, "Moderate edge"),
        (0.70, 2.0, "Strong edge"),
        (0.55, 1.2, "Weak edge"),
        (0.50, 1.0, "No edge"),
    ]
    
    for win_prob, win_loss, description in test_cases:
        kelly = sizer._kelly_criterion(win_prob, win_loss)
        print(f"  {description}: p={win_prob:.0%}, R={win_loss:.1f} → Kelly={kelly:.2%}")
    
    # Test full position sizing
    print("\n✓ Testing position sizing with adjustments...")
    capital = 100000.0
    
    scenarios = [
        {
            'name': 'Ideal conditions',
            'win_prob': 0.65,
            'win_loss': 1.5,
            'volatility': 0.20,
            'liquidity': 0.9,
            'leverage': 1.0,
            'confidence': 0.85
        },
        {
            'name': 'High volatility',
            'win_prob': 0.65,
            'win_loss': 1.5,
            'volatility': 0.50,
            'liquidity': 0.9,
            'leverage': 1.0,
            'confidence': 0.85
        },
        {
            'name': 'Low liquidity',
            'win_prob': 0.65,
            'win_loss': 1.5,
            'volatility': 0.20,
            'liquidity': 0.4,
            'leverage': 1.0,
            'confidence': 0.85
        },
        {
            'name': 'High leverage',
            'win_prob': 0.65,
            'win_loss': 1.5,
            'volatility': 0.20,
            'liquidity': 0.9,
            'leverage': 2.5,
            'confidence': 0.85
        },
        {
            'name': 'Low confidence',
            'win_prob': 0.65,
            'win_loss': 1.5,
            'volatility': 0.20,
            'liquidity': 0.9,
            'leverage': 1.0,
            'confidence': 0.50
        }
    ]
    
    for scenario in scenarios:
        size = sizer.calculate_size(
            capital=capital,
            win_probability=scenario['win_prob'],
            win_loss_ratio=scenario['win_loss'],
            volatility=scenario['volatility'],
            liquidity_score=scenario['liquidity'],
            current_leverage=scenario['leverage'],
            confidence=scenario['confidence']
        )
        
        size_pct = (size / capital) * 100
        print(f"\n  {scenario['name']}:")
        print(f"    Position size: ${size:,.2f} ({size_pct:.1f}%)")
    
    # Test stop-loss sizing
    print("\n✓ Testing stop-loss position sizing...")
    entry_price = 100.0
    stop_loss_price = 95.0  # 5% stop loss
    
    max_shares = sizer.calculate_stop_loss_size(
        capital=capital,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price
    )
    
    position_value = max_shares * entry_price
    max_loss = max_shares * (entry_price - stop_loss_price)
    
    print(f"  Entry: ${entry_price:.2f}")
    print(f"  Stop loss: ${stop_loss_price:.2f}")
    print(f"  Max shares: {max_shares:.0f}")
    print(f"  Position value: ${position_value:,.2f}")
    print(f"  Max loss: ${max_loss:,.2f} ({max_loss/capital:.1%})")
    
    # Test portfolio validation
    print("\n✓ Testing portfolio limit validation...")
    current_positions = {
        'AAPL': 0.15,  # 15% in AAPL
        'MSFT': 0.10   # 10% in MSFT
    }
    
    correlations = {
        'AAPL': 0.7,  # 70% correlated with new position
        'MSFT': 0.6   # 60% correlated
    }
    
    test_sizes = [0.05, 0.15, 0.30]
    for test_size in test_sizes:
        is_valid = sizer.validate_portfolio_limits(
            new_position_size=test_size,
            current_positions=current_positions,
            correlations=correlations
        )
        print(f"  New position {test_size:.0%}: {'✓ Valid' if is_valid else '✗ Invalid'}")
    
    # Test regime adjustment
    print("\n✓ Testing regime-based adjustment...")
    base_size = 0.10  # 10% base position
    
    for regime in ['CALM', 'NORMAL', 'VOLATILE', 'CRISIS']:
        adjusted = sizer.adjust_for_regime(base_size, regime)
        print(f"  {regime}: {base_size:.0%} → {adjusted:.0%}")
    
    print("\n" + "="*60)
    print("Position Sizing Test Complete!")
    print("="*60)
