"""
Performance Analysis Module

Comprehensive performance analysis and reporting for backtesting results.

Features:
- Equity curve visualization
- Drawdown analysis
- Trade distribution analysis
- Risk-adjusted metrics (Sharpe, Sortino, Calmar)
- Monthly/yearly breakdowns
- Benchmark comparisons
- HTML/PDF report generation

Usage:
    analyzer = PerformanceAnalyzer(trade_history, equity_curve)
    metrics = analyzer.calculate_metrics()
    analyzer.generate_report('backtest_report.html')
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """Container for performance metrics."""
    
    # Return metrics
    total_return: float
    annual_return: float
    monthly_return: float
    
    # Risk metrics
    volatility: float
    max_drawdown: float
    max_drawdown_duration: int
    
    # Risk-adjusted metrics
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    
    # Trade metrics
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    
    # P&L metrics
    total_pnl: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy: float
    
    # Trade statistics
    avg_trade_duration: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'total_return_pct': self.total_return * 100,
            'annual_return_pct': self.annual_return * 100,
            'monthly_return_pct': self.monthly_return * 100,
            'volatility_pct': self.volatility * 100,
            'max_drawdown_pct': self.max_drawdown * 100,
            'max_drawdown_duration_days': self.max_drawdown_duration,
            'sharpe_ratio': self.sharpe_ratio,
            'sortino_ratio': self.sortino_ratio,
            'calmar_ratio': self.calmar_ratio,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate_pct': self.win_rate * 100,
            'total_pnl': self.total_pnl,
            'avg_win': self.avg_win,
            'avg_loss': self.avg_loss,
            'profit_factor': self.profit_factor,
            'expectancy': self.expectancy,
            'avg_trade_duration_hours': self.avg_trade_duration,
            'max_consecutive_wins': self.max_consecutive_wins,
            'max_consecutive_losses': self.max_consecutive_losses
        }


class PerformanceAnalyzer:
    """
    Comprehensive performance analysis for trading strategies.
    
    Args:
        trades: List of trade dictionaries
        equity_curve: DataFrame with timestamp and equity columns
        initial_capital: Starting capital
        risk_free_rate: Annual risk-free rate for Sharpe ratio
    """
    
    def __init__(
        self,
        trades: List[Dict],
        equity_curve: pd.DataFrame,
        initial_capital: float = 100000.0,
        risk_free_rate: float = 0.02
    ):
        self.trades = trades
        self.equity_curve = equity_curve
        self.initial_capital = initial_capital
        self.risk_free_rate = risk_free_rate
        
        # Ensure equity curve has proper columns
        if 'equity' not in equity_curve.columns:
            raise ValueError("Equity curve must have 'equity' column")
        
        logger.info(f"PerformanceAnalyzer initialized with {len(trades)} trades")
    
    def calculate_metrics(self) -> PerformanceMetrics:
        """Calculate comprehensive performance metrics."""
        
        # Return metrics
        total_return = self._calculate_total_return()
        annual_return = self._calculate_annual_return()
        monthly_return = annual_return / 12
        
        # Risk metrics
        volatility = self._calculate_volatility()
        max_dd, max_dd_duration = self._calculate_max_drawdown()
        
        # Risk-adjusted metrics
        sharpe = self._calculate_sharpe_ratio(annual_return, volatility)
        sortino = self._calculate_sortino_ratio(annual_return)
        calmar = self._calculate_calmar_ratio(annual_return, max_dd)
        
        # Trade metrics
        total_trades = len(self.trades)
        winning_trades = len([t for t in self.trades if t.get('pnl', 0) > 0])
        losing_trades = len([t for t in self.trades if t.get('pnl', 0) <= 0])
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        
        # P&L metrics
        total_pnl = sum(t.get('pnl', 0) for t in self.trades)
        wins = [t['pnl'] for t in self.trades if t.get('pnl', 0) > 0]
        losses = [t['pnl'] for t in self.trades if t.get('pnl', 0) <= 0]
        
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = np.mean(losses) if losses else 0.0
        
        total_wins = sum(wins) if wins else 0.0
        total_losses = abs(sum(losses)) if losses else 0.0
        profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
        
        expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
        
        # Trade statistics
        avg_duration = self._calculate_avg_trade_duration()
        max_cons_wins = self._calculate_max_consecutive(wins=True)
        max_cons_losses = self._calculate_max_consecutive(wins=False)
        
        return PerformanceMetrics(
            total_return=total_return,
            annual_return=annual_return,
            monthly_return=monthly_return,
            volatility=volatility,
            max_drawdown=max_dd,
            max_drawdown_duration=max_dd_duration,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            total_pnl=total_pnl,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            expectancy=expectancy,
            avg_trade_duration=avg_duration,
            max_consecutive_wins=max_cons_wins,
            max_consecutive_losses=max_cons_losses
        )
    
    def _calculate_total_return(self) -> float:
        """Calculate total return."""
        if len(self.equity_curve) == 0:
            return 0.0
        
        final_equity = self.equity_curve['equity'].iloc[-1]
        return (final_equity - self.initial_capital) / self.initial_capital
    
    def _calculate_annual_return(self) -> float:
        """Calculate annualized return."""
        if len(self.equity_curve) < 2:
            return 0.0
        
        total_return = self._calculate_total_return()
        
        # Calculate time period in years
        if 'timestamp' in self.equity_curve.columns:
            start = pd.to_datetime(self.equity_curve['timestamp'].iloc[0])
            end = pd.to_datetime(self.equity_curve['timestamp'].iloc[-1])
            years = (end - start).days / 365.25
        else:
            # Assume daily data
            years = len(self.equity_curve) / 252  # Trading days
        
        if years <= 0:
            return 0.0
        
        # Annualize
        annual_return = (1 + total_return) ** (1 / years) - 1
        
        return annual_return
    
    def _calculate_volatility(self) -> float:
        """Calculate annualized volatility."""
        if len(self.equity_curve) < 2:
            return 0.0
        
        # Calculate returns
        returns = self.equity_curve['equity'].pct_change().dropna()
        
        # Annualize (assume daily data)
        volatility = returns.std() * np.sqrt(252)
        
        return volatility
    
    def _calculate_max_drawdown(self) -> Tuple[float, int]:
        """
        Calculate maximum drawdown and duration.
        
        Returns:
            (max_drawdown, duration_in_days)
        """
        if len(self.equity_curve) == 0:
            return 0.0, 0
        
        equity = self.equity_curve['equity'].values
        
        # Calculate running maximum
        running_max = np.maximum.accumulate(equity)
        
        # Calculate drawdown
        drawdown = (equity - running_max) / running_max
        
        # Max drawdown
        max_dd = abs(drawdown.min())
        
        # Find duration
        in_drawdown = False
        current_duration = 0
        max_duration = 0
        
        for dd in drawdown:
            if dd < 0:
                in_drawdown = True
                current_duration += 1
            else:
                if in_drawdown:
                    max_duration = max(max_duration, current_duration)
                    current_duration = 0
                    in_drawdown = False
        
        # Check final drawdown
        if in_drawdown:
            max_duration = max(max_duration, current_duration)
        
        return max_dd, max_duration
    
    def _calculate_sharpe_ratio(self, annual_return: float, volatility: float) -> float:
        """Calculate Sharpe ratio."""
        if volatility == 0:
            return 0.0
        
        excess_return = annual_return - self.risk_free_rate
        sharpe = excess_return / volatility
        
        return sharpe
    
    def _calculate_sortino_ratio(self, annual_return: float) -> float:
        """Calculate Sortino ratio (uses downside deviation)."""
        if len(self.equity_curve) < 2:
            return 0.0
        
        returns = self.equity_curve['equity'].pct_change().dropna()
        
        # Downside deviation (only negative returns)
        downside_returns = returns[returns < 0]
        
        if len(downside_returns) == 0:
            return float('inf')
        
        downside_std = downside_returns.std() * np.sqrt(252)
        
        if downside_std == 0:
            return 0.0
        
        excess_return = annual_return - self.risk_free_rate
        sortino = excess_return / downside_std
        
        return sortino
    
    def _calculate_calmar_ratio(self, annual_return: float, max_drawdown: float) -> float:
        """Calculate Calmar ratio."""
        if max_drawdown == 0:
            return float('inf')
        
        calmar = annual_return / max_drawdown
        
        return calmar
    
    def _calculate_avg_trade_duration(self) -> float:
        """Calculate average trade duration in hours."""
        if not self.trades:
            return 0.0
        
        durations = []
        for trade in self.trades:
            if 'entry_time' in trade and 'exit_time' in trade:
                try:
                    entry = pd.to_datetime(trade['entry_time'])
                    exit_time = pd.to_datetime(trade['exit_time'])
                    duration = (exit_time - entry).total_seconds() / 3600  # Hours
                    durations.append(duration)
                except:
                    pass
        
        if not durations:
            return 0.0
        
        return np.mean(durations)
    
    def _calculate_max_consecutive(self, wins: bool = True) -> int:
        """Calculate maximum consecutive wins or losses."""
        if not self.trades:
            return 0
        
        max_consecutive = 0
        current_consecutive = 0
        
        for trade in self.trades:
            pnl = trade.get('pnl', 0)
            
            if wins:
                # Counting wins
                if pnl > 0:
                    current_consecutive += 1
                    max_consecutive = max(max_consecutive, current_consecutive)
                else:
                    current_consecutive = 0
            else:
                # Counting losses
                if pnl <= 0:
                    current_consecutive += 1
                    max_consecutive = max(max_consecutive, current_consecutive)
                else:
                    current_consecutive = 0
        
        return max_consecutive
    
    def get_monthly_returns(self) -> pd.DataFrame:
        """Calculate monthly returns."""
        if 'timestamp' not in self.equity_curve.columns:
            return pd.DataFrame()
        
        df = self.equity_curve.copy()
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)
        
        # Resample to monthly
        monthly = df['equity'].resample('M').last()
        monthly_returns = monthly.pct_change().dropna()
        
        return pd.DataFrame({
            'month': monthly_returns.index,
            'return': monthly_returns.values * 100  # Percentage
        })
    
    def compare_to_benchmark(
        self,
        benchmark_returns: pd.Series,
        benchmark_name: str = 'Benchmark'
    ) -> Dict[str, float]:
        """
        Compare strategy to benchmark.
        
        Args:
            benchmark_returns: Series of benchmark returns
            benchmark_name: Name of benchmark
        
        Returns:
            Dictionary with comparison metrics
        """
        strategy_returns = self.equity_curve['equity'].pct_change().dropna()
        
        # Align returns
        min_len = min(len(strategy_returns), len(benchmark_returns))
        strat_ret = strategy_returns.values[-min_len:]
        bench_ret = benchmark_returns.values[-min_len:]
        
        # Calculate metrics
        strat_total = (1 + strat_ret).prod() - 1
        bench_total = (1 + bench_ret).prod() - 1
        
        alpha = strat_total - bench_total
        
        # Beta (regression slope)
        if bench_ret.std() > 0:
            beta = np.cov(strat_ret, bench_ret)[0, 1] / np.var(bench_ret)
        else:
            beta = 0.0
        
        # Correlation
        correlation = np.corrcoef(strat_ret, bench_ret)[0, 1] if len(strat_ret) > 1 else 0.0
        
        return {
            f'{benchmark_name}_return': bench_total * 100,
            'alpha': alpha * 100,
            'beta': beta,
            'correlation': correlation,
            'outperformance': (strat_total - bench_total) * 100
        }
    
    def generate_report(self, output_path: str) -> None:
        """
        Generate HTML performance report.
        
        Args:
            output_path: Path to save HTML report
        """
        path_obj = Path(output_path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        
        # Calculate metrics
        metrics = self.calculate_metrics()
        metrics_dict = metrics.to_dict()
        
        # Generate HTML
        html = self._generate_html_report(metrics_dict)
        
        # Save
        with open(str(path_obj), 'w') as f:
            f.write(html)
        
        logger.info(f"Report saved to {str(path_obj)}")
    
    def _generate_html_report(self, metrics: Dict) -> str:
        """Generate HTML report content."""
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Backtest Performance Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        h1 {{ color: #333; }}
        h2 {{ color: #666; margin-top: 30px; }}
        table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
        tr:nth-child(even) {{ background-color: #f2f2f2; }}
        .metric {{ font-weight: bold; }}
        .positive {{ color: green; }}
        .negative {{ color: red; }}
    </style>
</head>
<body>
    <h1>Backtest Performance Report</h1>
    <p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    
    <h2>Return Metrics</h2>
    <table>
        <tr><td class="metric">Total Return</td><td>{metrics['total_return_pct']:.2f}%</td></tr>
        <tr><td class="metric">Annual Return</td><td>{metrics['annual_return_pct']:.2f}%</td></tr>
        <tr><td class="metric">Monthly Return</td><td>{metrics['monthly_return_pct']:.2f}%</td></tr>
    </table>
    
    <h2>Risk Metrics</h2>
    <table>
        <tr><td class="metric">Volatility</td><td>{metrics['volatility_pct']:.2f}%</td></tr>
        <tr><td class="metric">Max Drawdown</td><td class="negative">{metrics['max_drawdown_pct']:.2f}%</td></tr>
        <tr><td class="metric">Max DD Duration</td><td>{metrics['max_drawdown_duration_days']} days</td></tr>
    </table>
    
    <h2>Risk-Adjusted Metrics</h2>
    <table>
        <tr><td class="metric">Sharpe Ratio</td><td>{metrics['sharpe_ratio']:.2f}</td></tr>
        <tr><td class="metric">Sortino Ratio</td><td>{metrics['sortino_ratio']:.2f}</td></tr>
        <tr><td class="metric">Calmar Ratio</td><td>{metrics['calmar_ratio']:.2f}</td></tr>
    </table>
    
    <h2>Trade Statistics</h2>
    <table>
        <tr><td class="metric">Total Trades</td><td>{metrics['total_trades']}</td></tr>
        <tr><td class="metric">Winning Trades</td><td class="positive">{metrics['winning_trades']}</td></tr>
        <tr><td class="metric">Losing Trades</td><td class="negative">{metrics['losing_trades']}</td></tr>
        <tr><td class="metric">Win Rate</td><td>{metrics['win_rate_pct']:.2f}%</td></tr>
        <tr><td class="metric">Profit Factor</td><td>{metrics['profit_factor']:.2f}</td></tr>
        <tr><td class="metric">Expectancy</td><td>${metrics['expectancy']:.2f}</td></tr>
    </table>
    
</body>
</html>
"""
        return html


if __name__ == '__main__':
    # Test performance analyzer
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Performance Analyzer")
    print("="*60)
    
    # Generate synthetic equity curve
    print("\n✓ Generating synthetic equity curve...")
    np.random.seed(42)
    n_days = 252  # 1 year
    
    daily_returns = np.random.randn(n_days) * 0.015 + 0.0005  # 1.5% daily vol, slight drift
    equity = 100000 * (1 + daily_returns).cumprod()
    
    timestamps = pd.date_range('2023-01-01', periods=n_days, freq='D')
    equity_curve = pd.DataFrame({
        'timestamp': timestamps,
        'equity': equity
    })
    
    print(f"  Generated {n_days} days of data")
    print(f"  Starting equity: ${equity[0]:,.2f}")
    print(f"  Ending equity: ${equity[-1]:,.2f}")
    
    # Generate synthetic trades
    print("\n✓ Generating synthetic trades...")
    n_trades = 50
    trades = []
    
    for i in range(n_trades):
        win = np.random.rand() > 0.45  # 55% win rate
        pnl = np.random.uniform(500, 2000) if win else -np.random.uniform(300, 1500)
        
        entry_idx = np.random.randint(0, n_days - 5)
        exit_idx = entry_idx + np.random.randint(1, 10)
        
        trades.append({
            'symbol': 'BTC',
            'pnl': pnl,
            'entry_time': timestamps[entry_idx],
            'exit_time': timestamps[min(exit_idx, n_days - 1)]
        })
    
    print(f"  Generated {n_trades} trades")
    
    # Initialize analyzer
    print("\n✓ Initializing PerformanceAnalyzer...")
    analyzer = PerformanceAnalyzer(trades, equity_curve, initial_capital=100000.0)
    
    # Calculate metrics
    print("\n✓ Calculating performance metrics...")
    metrics = analyzer.calculate_metrics()
    metrics_dict = metrics.to_dict()
    
    print("\n  Return Metrics:")
    print(f"    Total Return: {metrics_dict['total_return_pct']:.2f}%")
    print(f"    Annual Return: {metrics_dict['annual_return_pct']:.2f}%")
    print(f"    Volatility: {metrics_dict['volatility_pct']:.2f}%")
    
    print("\n  Risk Metrics:")
    print(f"    Max Drawdown: {metrics_dict['max_drawdown_pct']:.2f}%")
    print(f"    Sharpe Ratio: {metrics_dict['sharpe_ratio']:.2f}")
    print(f"    Sortino Ratio: {metrics_dict['sortino_ratio']:.2f}")
    
    print("\n  Trade Statistics:")
    print(f"    Total Trades: {metrics_dict['total_trades']}")
    print(f"    Win Rate: {metrics_dict['win_rate_pct']:.2f}%")
    print(f"    Profit Factor: {metrics_dict['profit_factor']:.2f}")
    print(f"    Avg Trade Duration: {metrics_dict['avg_trade_duration_hours']:.1f} hours")
    
    print("\n" + "="*60)
    print("Performance Analyzer Test Complete!")
    print("="*60)
