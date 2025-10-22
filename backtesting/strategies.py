"""
Backtesting Strategies

Base strategy and implementations for backtesting ML trading models.
Compatible with Backtrader framework.

Strategies:
- BaseMLStrategy: Base class with common functionality
- MLPredictionStrategy: Strategy using ML model predictions
- MultiTimeframeStrategy: Strategy across multiple timeframes

Usage:
    strategy = MLPredictionStrategy(model, config)
    cerebro.addstrategy(strategy)
    cerebro.run()
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class Signal(Enum):
    """Trading signal types."""
    STRONG_BUY = 2
    BUY = 1
    NEUTRAL = 0
    SELL = -1
    STRONG_SELL = -2


@dataclass
class StrategyConfig:
    """Configuration for trading strategy."""
    
    # Position sizing
    position_size_pct: float = 0.10  # 10% per position
    max_positions: int = 5
    use_kelly_sizing: bool = True
    
    # Risk management
    stop_loss_pct: float = 0.05  # 5% stop loss
    take_profit_pct: float = 0.10  # 10% take profit
    trailing_stop_pct: float = 0.03  # 3% trailing stop
    
    # Signal thresholds
    buy_threshold: float = 0.65  # Need 65% confidence to buy
    sell_threshold: float = 0.65  # Need 65% confidence to sell
    strong_signal_threshold: float = 0.80  # 80% for strong signals
    
    # Leverage
    max_leverage: float = 1.5
    use_leverage: bool = False
    
    # Costs
    commission_pct: float = 0.001  # 0.1% commission
    slippage_pct: float = 0.0005  # 0.05% slippage
    
    # Timeframe
    timeframe_minutes: int = 60  # 1 hour default


class BaseMLStrategy:
    """
    Base strategy class for ML-based trading.
    
    Provides common functionality for:
    - Signal generation
    - Position management
    - Risk controls
    - Performance tracking
    
    Args:
        config: Strategy configuration
    """
    
    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        
        # State tracking
        self.positions: Dict[str, Dict] = {}
        self.trade_history: List[Dict] = []
        self.signal_history: List[Dict] = []
        
        # Performance metrics
        self.total_trades = 0
        self.winning_trades = 0
        self.total_pnl = 0.0
        self.peak_equity = 0.0
        self.current_equity = 0.0
        
        logger.info("BaseMLStrategy initialized")
    
    def initialize(self, initial_capital: float) -> None:
        """Initialize strategy with starting capital."""
        self.current_equity = initial_capital
        self.peak_equity = initial_capital
        logger.info(f"Strategy initialized with ${initial_capital:,.2f}")
    
    def generate_signal(
        self,
        prediction: float,
        confidence: float,
        current_price: float,
        **kwargs
    ) -> Signal:
        """
        Generate trading signal from model prediction.
        
        Args:
            prediction: Model price prediction or direction
            confidence: Model confidence [0, 1]
            current_price: Current market price
            **kwargs: Additional features
        
        Returns:
            Signal enum
        """
        # Determine direction
        if prediction > current_price:
            # Bullish prediction
            if confidence >= self.config.strong_signal_threshold:
                return Signal.STRONG_BUY
            elif confidence >= self.config.buy_threshold:
                return Signal.BUY
            else:
                return Signal.NEUTRAL
        
        elif prediction < current_price:
            # Bearish prediction
            if confidence >= self.config.strong_signal_threshold:
                return Signal.STRONG_SELL
            elif confidence >= self.config.sell_threshold:
                return Signal.SELL
            else:
                return Signal.NEUTRAL
        
        else:
            return Signal.NEUTRAL
    
    def calculate_position_size(
        self,
        signal: Signal,
        current_price: float,
        confidence: float,
        available_capital: float
    ) -> float:
        """
        Calculate position size.
        
        Args:
            signal: Trading signal
            current_price: Current price
            confidence: Signal confidence
            available_capital: Available capital
        
        Returns:
            Position size in dollar amount
        """
        # Base size
        base_size = available_capital * self.config.position_size_pct
        
        # Adjust for signal strength
        if signal in [Signal.STRONG_BUY, Signal.STRONG_SELL]:
            size_multiplier = 1.5  # Increase size for strong signals
        else:
            size_multiplier = 1.0
        
        # Adjust for confidence
        confidence_multiplier = confidence
        
        # Calculate final size
        position_size = base_size * size_multiplier * confidence_multiplier
        
        # Apply leverage if enabled
        if self.config.use_leverage:
            position_size *= self.config.max_leverage
        
        # Ensure position size doesn't exceed available capital
        max_size = available_capital * (
            self.config.max_leverage if self.config.use_leverage else 1.0
        )
        position_size = min(position_size, max_size)
        
        logger.debug(f"Position size: ${position_size:,.2f} (conf={confidence:.2f})")
        
        return position_size
    
    def calculate_stop_loss(
        self,
        entry_price: float,
        signal: Signal
    ) -> float:
        """Calculate stop-loss price."""
        if signal in [Signal.BUY, Signal.STRONG_BUY]:
            # Long position: stop below entry
            stop_price = entry_price * (1 - self.config.stop_loss_pct)
        else:
            # Short position: stop above entry
            stop_price = entry_price * (1 + self.config.stop_loss_pct)
        
        return stop_price
    
    def calculate_take_profit(
        self,
        entry_price: float,
        signal: Signal
    ) -> float:
        """Calculate take-profit price."""
        if signal in [Signal.BUY, Signal.STRONG_BUY]:
            # Long position: profit above entry
            target_price = entry_price * (1 + self.config.take_profit_pct)
        else:
            # Short position: profit below entry
            target_price = entry_price * (1 - self.config.take_profit_pct)
        
        return target_price
    
    def open_position(
        self,
        symbol: str,
        signal: Signal,
        entry_price: float,
        position_size: float,
        timestamp: Any
    ) -> None:
        """Open new position."""
        stop_loss = self.calculate_stop_loss(entry_price, signal)
        take_profit = self.calculate_take_profit(entry_price, signal)
        
        position = {
            'symbol': symbol,
            'signal': signal,
            'entry_price': entry_price,
            'size': position_size,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'entry_time': timestamp,
            'highest_price': entry_price,
            'lowest_price': entry_price
        }
        
        self.positions[symbol] = position
        
        logger.info(f"Opened {signal.name} position: {symbol} @ ${entry_price:.2f}")
        logger.info(f"  Size: ${position_size:,.2f}")
        logger.info(f"  Stop loss: ${stop_loss:.2f}")
        logger.info(f"  Take profit: ${take_profit:.2f}")
    
    def close_position(
        self,
        symbol: str,
        exit_price: float,
        timestamp: Any,
        reason: str = 'signal'
    ) -> Dict:
        """Close existing position."""
        if symbol not in self.positions:
            logger.warning(f"No position to close: {symbol}")
            return {}
        
        position = self.positions[symbol]
        
        # Calculate P&L
        if position['signal'] in [Signal.BUY, Signal.STRONG_BUY]:
            # Long position
            pnl = (exit_price - position['entry_price']) * (position['size'] / position['entry_price'])
        else:
            # Short position
            pnl = (position['entry_price'] - exit_price) * (position['size'] / position['entry_price'])
        
        # Apply costs
        entry_cost = position['size'] * self.config.commission_pct
        exit_cost = position['size'] * self.config.commission_pct
        slippage = position['size'] * self.config.slippage_pct * 2  # Entry + exit
        
        net_pnl = pnl - entry_cost - exit_cost - slippage
        
        # Record trade
        trade = {
            'symbol': symbol,
            'signal': position['signal'].name,
            'entry_price': position['entry_price'],
            'exit_price': exit_price,
            'size': position['size'],
            'pnl': net_pnl,
            'pnl_pct': (net_pnl / position['size']) * 100,
            'entry_time': position['entry_time'],
            'exit_time': timestamp,
            'reason': reason
        }
        
        self.trade_history.append(trade)
        
        # Update metrics
        self.total_trades += 1
        if net_pnl > 0:
            self.winning_trades += 1
        self.total_pnl += net_pnl
        self.current_equity += net_pnl
        self.peak_equity = max(self.peak_equity, self.current_equity)
        
        # Remove position
        del self.positions[symbol]
        
        logger.info(f"Closed position: {symbol} @ ${exit_price:.2f}")
        logger.info(f"  P&L: ${net_pnl:,.2f} ({trade['pnl_pct']:.2f}%)")
        logger.info(f"  Reason: {reason}")
        
        return trade
    
    def update_trailing_stops(self, symbol: str, current_price: float) -> None:
        """Update trailing stop-loss."""
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        
        # Update highest/lowest
        position['highest_price'] = max(position['highest_price'], current_price)
        position['lowest_price'] = min(position['lowest_price'], current_price)
        
        # Update trailing stop
        if position['signal'] in [Signal.BUY, Signal.STRONG_BUY]:
            # Long position: trail stop upward
            new_stop = position['highest_price'] * (1 - self.config.trailing_stop_pct)
            position['stop_loss'] = max(position['stop_loss'], new_stop)
        else:
            # Short position: trail stop downward
            new_stop = position['lowest_price'] * (1 + self.config.trailing_stop_pct)
            position['stop_loss'] = min(position['stop_loss'], new_stop)
    
    def check_exit_conditions(
        self,
        symbol: str,
        current_price: float
    ) -> Optional[str]:
        """
        Check if position should be closed.
        
        Returns:
            Exit reason if should close, None otherwise
        """
        if symbol not in self.positions:
            return None
        
        position = self.positions[symbol]
        
        # Check stop-loss
        if position['signal'] in [Signal.BUY, Signal.STRONG_BUY]:
            if current_price <= position['stop_loss']:
                return 'stop_loss'
        else:
            if current_price >= position['stop_loss']:
                return 'stop_loss'
        
        # Check take-profit
        if position['signal'] in [Signal.BUY, Signal.STRONG_BUY]:
            if current_price >= position['take_profit']:
                return 'take_profit'
        else:
            if current_price <= position['take_profit']:
                return 'take_profit'
        
        return None
    
    def get_performance_metrics(self) -> Dict[str, float]:
        """Calculate performance metrics."""
        if self.total_trades == 0:
            return {}
        
        wins = [t for t in self.trade_history if t['pnl'] > 0]
        losses = [t for t in self.trade_history if t['pnl'] <= 0]
        
        total_wins = sum(t['pnl'] for t in wins)
        total_losses = abs(sum(t['pnl'] for t in losses))
        
        metrics = {
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'win_rate': (self.winning_trades / self.total_trades) * 100,
            'total_pnl': self.total_pnl,
            'avg_win': total_wins / len(wins) if wins else 0,
            'avg_loss': total_losses / len(losses) if losses else 0,
            'profit_factor': total_wins / total_losses if total_losses > 0 else float('inf'),
            'max_drawdown': ((self.peak_equity - self.current_equity) / self.peak_equity) * 100,
            'return_pct': (self.total_pnl / (self.current_equity - self.total_pnl)) * 100
        }
        
        return metrics


if __name__ == '__main__':
    # Test strategy
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Base ML Strategy")
    print("="*60)
    
    # Initialize strategy
    print("\n✓ Initializing strategy...")
    config = StrategyConfig()
    strategy = BaseMLStrategy(config)
    strategy.initialize(100000.0)
    
    print(f"  Position size: {config.position_size_pct:.0%}")
    print(f"  Stop loss: {config.stop_loss_pct:.0%}")
    print(f"  Take profit: {config.take_profit_pct:.0%}")
    
    # Test signal generation
    print("\n✓ Testing signal generation...")
    test_cases = [
        (110.0, 0.85, 100.0, "Strong bullish"),
        (105.0, 0.70, 100.0, "Moderate bullish"),
        (100.0, 0.60, 100.0, "Neutral"),
        (95.0, 0.70, 100.0, "Moderate bearish"),
        (90.0, 0.85, 100.0, "Strong bearish")
    ]
    
    for pred, conf, price, desc in test_cases:
        signal = strategy.generate_signal(pred, conf, price)
        print(f"  {desc}: {signal.name}")
    
    # Test position management
    print("\n✓ Testing position management...")
    
    # Open long position
    signal = Signal.BUY
    entry_price = 100.0
    size = strategy.calculate_position_size(signal, entry_price, 0.75, 100000.0)
    strategy.open_position('BTC', signal, entry_price, size, '2024-01-01')
    
    print(f"  Opened position: ${size:,.2f}")
    
    # Update trailing stop
    strategy.update_trailing_stops('BTC', 110.0)
    print(f"  Updated trailing stop: ${strategy.positions['BTC']['stop_loss']:.2f}")
    
    # Close position
    exit_price = 110.0
    trade = strategy.close_position('BTC', exit_price, '2024-01-02', 'take_profit')
    print(f"  Closed position: P&L ${trade['pnl']:,.2f}")
    
    # Get metrics
    print("\n✓ Performance metrics:")
    metrics = strategy.get_performance_metrics()
    for key, value in metrics.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.2f}")
        else:
            print(f"  {key}: {value}")
    
    print("\n" + "="*60)
    print("Base ML Strategy Test Complete!")
    print("="*60)
