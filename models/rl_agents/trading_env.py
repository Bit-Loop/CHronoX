"""
Trading Environment for Reinforcement Learning

OpenAI Gym-compatible environment for training RL trading agents.
Supports multiple assets, margin trading, and realistic transaction costs.

State Space:
    - Current positions (long/short per asset)
    - Account balance & available margin
    - Market features (OHLCV, indicators)
    - Model predictions (supervised outputs)
    - Liquidity metrics (spreads, volume)
    - Leverage ratio
    - Volatility regime
    - Event signals

Action Space:
    - Buy/Sell/Short amounts
    - Leverage ratio
    - Stop-loss & take-profit distances

Reward Function:
    - Base: Realized profit from trades
    - Penalty: Transaction costs (0.1% per trade)
    - Penalty: Drawdown severity
    - Penalty: Slippage in illiquid markets
    - Bonus: Sharpe ratio improvement

Usage:
    env = TradingEnvironment(data, config)
    obs = env.reset()
    action = agent.get_action(obs)
    next_obs, reward, done, info = env.step(action)
"""

import numpy as np
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class VolatilityRegime(Enum):
    """Market volatility states."""
    CALM = 0
    NORMAL = 1
    VOLATILE = 2
    CRISIS = 3


@dataclass
class TradingConfig:
    """Configuration for trading environment."""
    
    # Asset parameters
    n_assets: int = 1
    initial_capital: float = 100000.0
    max_position_pct: float = 1.0  # Max 100% per asset
    max_total_leverage: float = 2.0
    
    # Trading costs
    transaction_cost_pct: float = 0.001  # 0.1% per trade
    slippage_pct: float = 0.0005  # 0.05% avg slippage
    funding_rate_annual: float = 0.05  # 5% annual for margin
    
    # Risk limits
    max_drawdown_pct: float = 0.15  # 15% max drawdown
    stop_loss_pct: float = 0.05  # 5% default stop loss
    take_profit_pct: float = 0.10  # 10% default take profit
    
    # Regime-based leverage limits
    leverage_limits: Dict[str, float] = None
    
    def __post_init__(self):
        if self.leverage_limits is None:
            self.leverage_limits = {
                'CALM': 2.0,
                'NORMAL': 1.5,
                'VOLATILE': 1.2,
                'CRISIS': 1.0
            }


@dataclass
class Position:
    """Position state for a single asset."""
    
    asset_id: int
    quantity: float  # Positive = long, negative = short
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    unrealized_pnl: float = 0.0
    
    def update_pnl(self, current_price: float) -> None:
        """Update unrealized P&L."""
        self.current_price = current_price
        if self.quantity > 0:  # Long position
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        elif self.quantity < 0:  # Short position
            self.unrealized_pnl = (self.entry_price - current_price) * abs(self.quantity)
    
    def is_stopped_out(self) -> bool:
        """Check if position hit stop loss."""
        if self.quantity > 0:  # Long
            return self.current_price <= self.stop_loss
        elif self.quantity < 0:  # Short
            return self.current_price >= self.stop_loss
        return False
    
    def is_target_hit(self) -> bool:
        """Check if position hit take profit."""
        if self.quantity > 0:  # Long
            return self.current_price >= self.take_profit
        elif self.quantity < 0:  # Short
            return self.current_price <= self.take_profit
        return False


class TradingEnvironment:
    """
    OpenAI Gym-compatible trading environment.
    
    Observation Space: [positions, balance, market_features, predictions, ...]
    Action Space: [buy_pct, sell_pct, short_pct, leverage, stop_loss, take_profit]
    
    Args:
        market_data: DataFrame with OHLCV and features
        predictions: DataFrame with model predictions
        config: Trading configuration
        seed: Random seed
    """
    
    def __init__(
        self,
        market_data: Any,  # DataFrame with OHLCV
        predictions: Optional[Any] = None,  # Model predictions
        config: Optional[TradingConfig] = None,
        seed: int = 42
    ):
        self.market_data = market_data
        self.predictions = predictions
        self.config = config or TradingConfig()
        
        np.random.seed(seed)
        
        # State variables
        self.current_step = 0
        self.balance = self.config.initial_capital
        self.equity = self.balance
        self.positions: Dict[int, Position] = {}
        
        # Performance tracking
        self.initial_equity = self.balance
        self.peak_equity = self.balance
        self.total_trades = 0
        self.winning_trades = 0
        self.total_fees = 0.0
        self.realized_pnl = 0.0
        
        # Episode history
        self.equity_history = []
        self.action_history = []
        self.reward_history = []
        
        # Define observation and action spaces
        self.observation_dim = self._get_observation_dim()
        self.action_dim = 6  # [buy, sell, short, leverage, stop_loss, take_profit]
        
        logger.info(f"TradingEnvironment initialized: {self.config.n_assets} assets")
        logger.info(f"Observation dim: {self.observation_dim}, Action dim: {self.action_dim}")
    
    def _get_observation_dim(self) -> int:
        """Calculate observation space dimension."""
        dim = 0
        dim += self.config.n_assets  # Position quantities
        dim += self.config.n_assets  # Unrealized P&L per asset
        dim += 3  # Balance, equity, available margin
        dim += 1  # Current leverage ratio
        dim += 1  # Drawdown percentage
        dim += 5  # Market features (open, high, low, close, volume)
        dim += 10  # Technical indicators
        dim += 3  # Model predictions (price, signal, confidence)
        dim += 2  # Liquidity metrics (spread, volume depth)
        dim += 4  # Volatility regime (one-hot)
        dim += 3  # Event signals (sentiment, impact, urgency)
        return dim
    
    def reset(self) -> np.ndarray:
        """Reset environment to initial state."""
        self.current_step = 0
        self.balance = self.config.initial_capital
        self.equity = self.balance
        self.positions = {}
        
        self.initial_equity = self.balance
        self.peak_equity = self.balance
        self.total_trades = 0
        self.winning_trades = 0
        self.total_fees = 0.0
        self.realized_pnl = 0.0
        
        self.equity_history = [self.equity]
        self.action_history = []
        self.reward_history = []
        
        return self._get_observation()
    
    def step(self, action: np.ndarray) -> Tuple[np.ndarray, float, bool, Dict]:
        """
        Execute one environment step.
        
        Args:
            action: [buy_pct, sell_pct, short_pct, leverage, stop_loss, take_profit]
        
        Returns:
            observation: Current state
            reward: Step reward
            done: Episode termination flag
            info: Additional information
        """
        # Parse action
        buy_pct = np.clip(action[0], 0, 1)
        sell_pct = np.clip(action[1], 0, 1)
        short_pct = np.clip(action[2], 0, 1)
        leverage = np.clip(action[3], 1.0, self.config.max_total_leverage)
        stop_loss_pct = np.clip(action[4], 0, 0.2)  # Max 20% stop loss
        take_profit_pct = np.clip(action[5], 0, 0.5)  # Max 50% take profit
        
        # Get current market state
        current_price = self._get_current_price()
        
        # Execute action
        executed_trades = self._execute_action(
            buy_pct, sell_pct, short_pct, leverage,
            stop_loss_pct, take_profit_pct, current_price
        )
        
        # Update positions
        self._update_positions(current_price)
        
        # Check stop-loss and take-profit
        self._check_exit_conditions(current_price)
        
        # Calculate reward
        reward = self._calculate_reward(executed_trades)
        
        # Update equity
        self.equity = self.balance + self._get_total_unrealized_pnl()
        self.peak_equity = max(self.peak_equity, self.equity)
        
        # Record history
        self.equity_history.append(self.equity)
        self.action_history.append(action)
        self.reward_history.append(reward)
        
        # Check termination
        done = self._is_done()
        
        # Advance time
        self.current_step += 1
        
        # Get next observation
        obs = self._get_observation()
        
        # Compile info
        info = {
            'equity': self.equity,
            'balance': self.balance,
            'total_trades': self.total_trades,
            'win_rate': self.winning_trades / max(1, self.total_trades),
            'total_fees': self.total_fees,
            'realized_pnl': self.realized_pnl,
            'drawdown': (self.peak_equity - self.equity) / self.peak_equity,
            'n_positions': len(self.positions)
        }
        
        return obs, reward, done, info
    
    def _get_observation(self) -> np.ndarray:
        """Construct observation vector."""
        obs = []
        
        # Position quantities
        for i in range(self.config.n_assets):
            pos = self.positions.get(i)
            obs.append(pos.quantity if pos else 0.0)
        
        # Unrealized P&L per asset
        for i in range(self.config.n_assets):
            pos = self.positions.get(i)
            obs.append(pos.unrealized_pnl if pos else 0.0)
        
        # Account state
        obs.extend([
            self.balance / self.config.initial_capital,  # Normalized balance
            self.equity / self.config.initial_capital,  # Normalized equity
            self._get_available_margin() / self.config.initial_capital
        ])
        
        # Current leverage
        obs.append(self._get_current_leverage())
        
        # Drawdown
        obs.append((self.peak_equity - self.equity) / self.peak_equity)
        
        # Market features (OHLCV)
        current_data = self._get_current_market_data()
        obs.extend([
            current_data.get('open', 0),
            current_data.get('high', 0),
            current_data.get('low', 0),
            current_data.get('close', 0),
            current_data.get('volume', 0)
        ])
        
        # Technical indicators (placeholder)
        obs.extend([0.0] * 10)
        
        # Model predictions (placeholder)
        obs.extend([0.0, 0.0, 0.0])
        
        # Liquidity metrics (placeholder)
        obs.extend([0.01, 1000.0])  # Spread, volume depth
        
        # Volatility regime (one-hot)
        regime = self._detect_regime()
        regime_onehot = [0, 0, 0, 0]
        regime_onehot[regime.value] = 1
        obs.extend(regime_onehot)
        
        # Event signals (placeholder)
        obs.extend([0.0, 0.0, 0.0])
        
        return np.array(obs, dtype=np.float32)
    
    def _execute_action(
        self,
        buy_pct: float,
        sell_pct: float,
        short_pct: float,
        leverage: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        current_price: float
    ) -> List[Dict]:
        """Execute trading action."""
        trades = []
        
        # Buy signal
        if buy_pct > 0.01:
            amount = self._get_available_margin() * buy_pct * leverage
            if amount > 0:
                quantity = amount / current_price
                cost = self._calculate_transaction_cost(amount)
                
                # Create/update position
                if 0 in self.positions:
                    # Add to existing position
                    pos = self.positions[0]
                    new_quantity = pos.quantity + quantity
                    pos.entry_price = ((pos.entry_price * pos.quantity) + 
                                     (current_price * quantity)) / new_quantity
                    pos.quantity = new_quantity
                else:
                    # New position
                    self.positions[0] = Position(
                        asset_id=0,
                        quantity=quantity,
                        entry_price=current_price,
                        current_price=current_price,
                        stop_loss=current_price * (1 - stop_loss_pct),
                        take_profit=current_price * (1 + take_profit_pct)
                    )
                
                self.balance -= (amount + cost)
                self.total_fees += cost
                self.total_trades += 1
                
                trades.append({'type': 'buy', 'quantity': quantity, 'price': current_price})
        
        # Sell signal
        if sell_pct > 0.01 and 0 in self.positions:
            pos = self.positions[0]
            if pos.quantity > 0:
                sell_quantity = pos.quantity * sell_pct
                amount = sell_quantity * current_price
                cost = self._calculate_transaction_cost(amount)
                
                # Realize P&L
                pnl = (current_price - pos.entry_price) * sell_quantity
                self.balance += (amount - cost)
                self.realized_pnl += pnl
                self.total_fees += cost
                
                if pnl > 0:
                    self.winning_trades += 1
                
                # Update position
                pos.quantity -= sell_quantity
                if pos.quantity < 1e-8:
                    del self.positions[0]
                
                trades.append({'type': 'sell', 'quantity': sell_quantity, 'price': current_price})
        
        # Short signal (simplified - not implemented fully)
        # Would require margin requirements and funding rates
        
        return trades
    
    def _update_positions(self, current_price: float) -> None:
        """Update all position P&L."""
        for pos in self.positions.values():
            pos.update_pnl(current_price)
    
    def _check_exit_conditions(self, current_price: float) -> None:
        """Check stop-loss and take-profit for all positions."""
        to_close = []
        
        for asset_id, pos in self.positions.items():
            if pos.is_stopped_out() or pos.is_target_hit():
                # Close position
                amount = abs(pos.quantity) * current_price
                cost = self._calculate_transaction_cost(amount)
                
                self.balance += (amount - cost if pos.quantity > 0 else -(amount + cost))
                self.realized_pnl += pos.unrealized_pnl
                self.total_fees += cost
                
                if pos.unrealized_pnl > 0:
                    self.winning_trades += 1
                
                to_close.append(asset_id)
        
        for asset_id in to_close:
            del self.positions[asset_id]
    
    def _calculate_reward(self, executed_trades: List[Dict]) -> float:
        """
        Calculate step reward.
        
        Components:
        - Realized P&L
        - Unrealized P&L change
        - Transaction cost penalty
        - Drawdown penalty
        - Sharpe ratio bonus
        """
        # Base reward: equity change
        if len(self.equity_history) > 1:
            equity_change = self.equity - self.equity_history[-1]
            base_reward = equity_change / self.config.initial_capital
        else:
            base_reward = 0.0
        
        # Transaction cost penalty
        n_trades = len(executed_trades)
        cost_penalty = -0.01 * n_trades  # Small penalty per trade
        
        # Drawdown penalty
        drawdown = (self.peak_equity - self.equity) / self.peak_equity
        drawdown_penalty = -10.0 * max(0, drawdown - 0.05)  # Penalize >5% drawdown
        
        # Sharpe bonus (if enough history)
        sharpe_bonus = 0.0
        if len(self.equity_history) > 20:
            returns = np.diff(self.equity_history[-20:]) / self.equity_history[-21:-1]
            sharpe = returns.mean() / (returns.std() + 1e-8)
            sharpe_bonus = 0.1 * np.clip(sharpe, -2, 2)
        
        total_reward = base_reward + cost_penalty + drawdown_penalty + sharpe_bonus
        
        return float(total_reward)
    
    def _is_done(self) -> bool:
        """Check episode termination conditions."""
        # Max drawdown exceeded
        drawdown = (self.peak_equity - self.equity) / self.peak_equity
        if drawdown >= self.config.max_drawdown_pct:
            logger.info(f"Episode terminated: max drawdown {drawdown:.2%}")
            return True
        
        # Bankruptcy
        if self.equity <= 0:
            logger.info("Episode terminated: bankruptcy")
            return True
        
        # End of data
        if self.current_step >= len(self.market_data) - 1:
            logger.info("Episode terminated: end of data")
            return True
        
        return False
    
    def _get_current_price(self) -> float:
        """Get current market price."""
        if hasattr(self.market_data, 'iloc'):
            return float(self.market_data.iloc[self.current_step]['close'])
        return 100.0  # Default
    
    def _get_current_market_data(self) -> Dict:
        """Get current market data."""
        if hasattr(self.market_data, 'iloc'):
            row = self.market_data.iloc[self.current_step]
            return {
                'open': float(row.get('open', 0)),
                'high': float(row.get('high', 0)),
                'low': float(row.get('low', 0)),
                'close': float(row.get('close', 0)),
                'volume': float(row.get('volume', 0))
            }
        return {'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1000}
    
    def _get_total_unrealized_pnl(self) -> float:
        """Calculate total unrealized P&L."""
        return sum(pos.unrealized_pnl for pos in self.positions.values())
    
    def _get_available_margin(self) -> float:
        """Calculate available margin for trading."""
        used_margin = sum(abs(pos.quantity * pos.current_price) 
                         for pos in self.positions.values())
        max_margin = self.equity * self.config.max_total_leverage
        return max(0, max_margin - used_margin)
    
    def _get_current_leverage(self) -> float:
        """Calculate current leverage ratio."""
        if self.equity <= 0:
            return 0.0
        position_value = sum(abs(pos.quantity * pos.current_price) 
                           for pos in self.positions.values())
        return position_value / self.equity
    
    def _calculate_transaction_cost(self, amount: float) -> float:
        """Calculate transaction cost."""
        base_cost = amount * self.config.transaction_cost_pct
        slippage = amount * self.config.slippage_pct
        return base_cost + slippage
    
    def _detect_regime(self) -> VolatilityRegime:
        """Detect current volatility regime."""
        # Simplified regime detection
        # In practice, use realized volatility calculation
        if hasattr(self.market_data, 'iloc'):
            if self.current_step < 20:
                return VolatilityRegime.NORMAL
            
            recent_data = self.market_data.iloc[max(0, self.current_step - 20):self.current_step]
            if 'close' in recent_data.columns:
                returns = recent_data['close'].pct_change().dropna()
                volatility = returns.std() * np.sqrt(252)  # Annualized
                
                if volatility < 0.15:
                    return VolatilityRegime.CALM
                elif volatility < 0.30:
                    return VolatilityRegime.NORMAL
                elif volatility < 0.50:
                    return VolatilityRegime.VOLATILE
                else:
                    return VolatilityRegime.CRISIS
        
        return VolatilityRegime.NORMAL
    
    def render(self, mode: str = 'human') -> None:
        """Render environment state."""
        if mode == 'human':
            print(f"\n=== Step {self.current_step} ===")
            print(f"Equity: ${self.equity:,.2f}")
            print(f"Balance: ${self.balance:,.2f}")
            print(f"Positions: {len(self.positions)}")
            print(f"Leverage: {self._get_current_leverage():.2f}x")
            print(f"Drawdown: {(self.peak_equity - self.equity) / self.peak_equity:.2%}")
            print(f"Total Trades: {self.total_trades}")
            print(f"Win Rate: {self.winning_trades / max(1, self.total_trades):.2%}")


if __name__ == '__main__':
    # Test trading environment
    import pandas as pd
    
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Trading Environment")
    print("="*60)
    
    # Create synthetic market data
    print("\n✓ Creating synthetic market data...")
    np.random.seed(42)
    n_steps = 1000
    
    # Random walk with drift
    returns = np.random.randn(n_steps) * 0.02 + 0.0002  # 2% daily vol, slight upward drift
    prices = 100 * (1 + returns).cumprod()
    
    market_data = pd.DataFrame({
        'open': prices * (1 + np.random.randn(n_steps) * 0.001),
        'high': prices * (1 + np.abs(np.random.randn(n_steps)) * 0.005),
        'low': prices * (1 - np.abs(np.random.randn(n_steps)) * 0.005),
        'close': prices,
        'volume': np.random.randint(1000, 10000, n_steps)
    })
    
    print(f"  Generated {n_steps} steps of market data")
    print(f"  Price range: ${market_data['close'].min():.2f} - ${market_data['close'].max():.2f}")
    
    # Initialize environment
    print("\n✓ Initializing environment...")
    config = TradingConfig(initial_capital=10000.0)
    env = TradingEnvironment(market_data, config=config)
    
    print(f"  Observation dim: {env.observation_dim}")
    print(f"  Action dim: {env.action_dim}")
    print(f"  Initial capital: ${config.initial_capital:,.2f}")
    
    # Test random agent
    print("\n✓ Running random agent for 100 steps...")
    obs = env.reset()
    
    for step in range(100):
        # Random action
        action = np.random.rand(env.action_dim)
        action[3] = np.random.uniform(1.0, 1.5)  # Leverage 1-1.5x
        
        obs, reward, done, info = env.step(action)
        
        if step % 20 == 0:
            print(f"\n  Step {step}:")
            print(f"    Equity: ${info['equity']:,.2f}")
            print(f"    Positions: {info['n_positions']}")
            print(f"    Reward: {reward:.4f}")
        
        if done:
            print(f"\n  Episode ended at step {step}")
            break
    
    # Final statistics
    print("\n✓ Episode summary:")
    print(f"  Final equity: ${env.equity:,.2f}")
    print(f"  Total return: {(env.equity / env.initial_equity - 1) * 100:.2f}%")
    print(f"  Total trades: {env.total_trades}")
    print(f"  Win rate: {env.winning_trades / max(1, env.total_trades):.2%}")
    print(f"  Total fees: ${env.total_fees:,.2f}")
    print(f"  Max drawdown: {(env.peak_equity - min(env.equity_history)) / env.peak_equity:.2%}")
    
    print("\n" + "="*60)
    print("Trading Environment Test Complete!")
    print("="*60)
