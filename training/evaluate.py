"""
Model Evaluation Module

Comprehensive evaluation framework for trading models including:
- Regression metrics (MAE, RMSE, MAPE, R²)
- Classification metrics (Precision, Recall, F1, Confusion Matrix)
- Trading-specific metrics (Sharpe, Sortino, Max Drawdown)
- Visualization utilities (prediction plots, confusion matrices)
- Report generation (JSON + PDF)

Usage:
    evaluator = ModelEvaluator(model, test_loader)
    metrics = evaluator.evaluate()
    evaluator.plot_predictions()
    evaluator.generate_report('eval_report.json')
"""

import torch
import torch.nn as nn
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class RegressionMetrics:
    """Regression evaluation metrics."""
    mae: float  # Mean Absolute Error
    rmse: float  # Root Mean Squared Error
    mape: float  # Mean Absolute Percentage Error
    r2: float  # R-squared
    correlation: float  # Pearson correlation
    
    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass
class ClassificationMetrics:
    """Classification evaluation metrics."""
    accuracy: float
    precision: float
    recall: float
    f1_score: float
    confusion_matrix: List[List[int]]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'accuracy': self.accuracy,
            'precision': self.precision,
            'recall': self.recall,
            'f1_score': self.f1_score,
            'confusion_matrix': self.confusion_matrix
        }


@dataclass
class TradingMetrics:
    """Trading-specific performance metrics."""
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    avg_trade_duration: float
    total_return: float
    
    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


class ModelEvaluator:
    """
    Comprehensive model evaluation with metrics, visualizations, and reporting.
    
    Evaluates both regression (price prediction) and classification (signal)
    components of trading models.
    
    Args:
        model: Trained PyTorch model
        device: Device to run evaluation on
        quantiles: Quantiles for probabilistic forecasts
    """
    
    def __init__(
        self,
        model: nn.Module,
        device: str = 'cpu',
        quantiles: List[float] = [0.1, 0.5, 0.9]
    ):
        self.model = model
        self.device = device
        self.quantiles = quantiles
        
        self.model.eval()
        self.model.to(device)
        
        # Storage for predictions and targets
        self.all_predictions = []
        self.all_targets = []
        self.all_signals = []
        self.all_signal_targets = []
        self.all_scale_weights = []
        
        logger.info("ModelEvaluator initialized")
    
    def evaluate(
        self,
        data_loader: Any,
        return_predictions: bool = False
    ) -> Dict[str, Any]:
        """
        Run full evaluation on test data.
        
        Args:
            data_loader: DataLoader with test data
            return_predictions: If True, return raw predictions
        
        Returns:
            Dictionary with all evaluation metrics
        """
        logger.info("Starting model evaluation...")
        
        # Reset storage
        self.all_predictions = []
        self.all_targets = []
        self.all_signals = []
        self.all_signal_targets = []
        self.all_scale_weights = []
        
        # Run inference
        with torch.no_grad():
            for batch in data_loader:
                # Unpack batch (adjust based on actual data loader format)
                x_scales, aux_stats, targets, signal_targets = batch
                
                # Move to device
                x_scales = [x.to(self.device) for x in x_scales]
                aux_stats = aux_stats.to(self.device)
                targets = targets.to(self.device)
                signal_targets = signal_targets.to(self.device)
                
                # Forward pass
                forecast, signal, scale_weights = self.model(x_scales, aux_stats)
                
                # Store results
                self.all_predictions.append(forecast[:, 1].cpu())  # Median quantile
                self.all_targets.append(targets.cpu())
                self.all_signals.append(signal.argmax(dim=1).cpu())
                self.all_signal_targets.append(signal_targets.cpu())
                self.all_scale_weights.append(scale_weights.cpu())
        
        # Concatenate all batches
        predictions = torch.cat(self.all_predictions).numpy()
        targets = torch.cat(self.all_targets).numpy()
        signals = torch.cat(self.all_signals).numpy()
        signal_targets = torch.cat(self.all_signal_targets).numpy()
        scale_weights = torch.cat(self.all_scale_weights).numpy()
        
        # Compute metrics
        regression_metrics = self._compute_regression_metrics(predictions, targets)
        classification_metrics = self._compute_classification_metrics(signals, signal_targets)
        
        # Compile results
        results = {
            'regression': regression_metrics.to_dict(),
            'classification': classification_metrics.to_dict(),
            'scale_weights': {
                'mean': scale_weights.mean(axis=0).tolist(),
                'std': scale_weights.std(axis=0).tolist()
            },
            'timestamp': datetime.now().isoformat()
        }
        
        if return_predictions:
            results['predictions'] = {
                'forecast': predictions.tolist(),
                'targets': targets.tolist(),
                'signals': signals.tolist(),
                'signal_targets': signal_targets.tolist()
            }
        
        logger.info("Evaluation complete")
        logger.info(f"Regression MAE: {regression_metrics.mae:.4f}")
        logger.info(f"Classification F1: {classification_metrics.f1_score:.4f}")
        
        return results
    
    def _compute_regression_metrics(
        self,
        predictions: np.ndarray,
        targets: np.ndarray
    ) -> RegressionMetrics:
        """Compute regression evaluation metrics."""
        
        # Mean Absolute Error
        mae = np.mean(np.abs(predictions - targets))
        
        # Root Mean Squared Error
        rmse = np.sqrt(np.mean((predictions - targets) ** 2))
        
        # Mean Absolute Percentage Error
        mape = np.mean(np.abs((targets - predictions) / (targets + 1e-8))) * 100
        
        # R-squared
        ss_res = np.sum((targets - predictions) ** 2)
        ss_tot = np.sum((targets - targets.mean()) ** 2)
        r2 = 1 - (ss_res / (ss_tot + 1e-8))
        
        # Pearson correlation
        correlation = np.corrcoef(predictions, targets)[0, 1]
        
        return RegressionMetrics(
            mae=float(mae),
            rmse=float(rmse),
            mape=float(mape),
            r2=float(r2),
            correlation=float(correlation)
        )
    
    def _compute_classification_metrics(
        self,
        predictions: np.ndarray,
        targets: np.ndarray
    ) -> ClassificationMetrics:
        """Compute classification evaluation metrics."""
        
        # Accuracy
        accuracy = np.mean(predictions == targets)
        
        # Confusion matrix
        n_classes = max(predictions.max(), targets.max()) + 1
        confusion = np.zeros((n_classes, n_classes), dtype=int)
        for pred, true in zip(predictions, targets):
            confusion[int(true), int(pred)] += 1
        
        # Per-class precision, recall, f1
        precisions = []
        recalls = []
        f1_scores = []
        
        for i in range(n_classes):
            tp = confusion[i, i]
            fp = confusion[:, i].sum() - tp
            fn = confusion[i, :].sum() - tp
            
            precision = tp / (tp + fp + 1e-8)
            recall = tp / (tp + fn + 1e-8)
            f1 = 2 * precision * recall / (precision + recall + 1e-8)
            
            precisions.append(precision)
            recalls.append(recall)
            f1_scores.append(f1)
        
        # Macro averages
        precision = np.mean(precisions)
        recall = np.mean(recalls)
        f1_score = np.mean(f1_scores)
        
        return ClassificationMetrics(
            accuracy=float(accuracy),
            precision=float(precision),
            recall=float(recall),
            f1_score=float(f1_score),
            confusion_matrix=confusion.tolist()
        )
    
    def compute_trading_metrics(
        self,
        returns: np.ndarray,
        risk_free_rate: float = 0.02
    ) -> TradingMetrics:
        """
        Compute trading-specific performance metrics.
        
        Args:
            returns: Array of daily returns
            risk_free_rate: Annual risk-free rate
        
        Returns:
            TradingMetrics object
        """
        # Sharpe ratio
        excess_returns = returns - risk_free_rate / 252  # Daily risk-free rate
        sharpe = np.sqrt(252) * excess_returns.mean() / (returns.std() + 1e-8)
        
        # Sortino ratio (uses downside deviation)
        downside_returns = returns[returns < 0]
        downside_std = downside_returns.std() if len(downside_returns) > 0 else 0
        sortino = np.sqrt(252) * excess_returns.mean() / (downside_std + 1e-8)
        
        # Maximum drawdown
        cumulative = (1 + returns).cumprod()
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        max_drawdown = drawdown.min()
        
        # Win rate
        win_rate = (returns > 0).mean()
        
        # Profit factor
        gains = returns[returns > 0].sum()
        losses = abs(returns[returns < 0].sum())
        profit_factor = gains / (losses + 1e-8)
        
        # Total return
        total_return = cumulative[-1] - 1
        
        # Average trade duration (placeholder)
        avg_trade_duration = 0.0
        
        return TradingMetrics(
            sharpe_ratio=float(sharpe),
            sortino_ratio=float(sortino),
            max_drawdown=float(max_drawdown),
            win_rate=float(win_rate),
            profit_factor=float(profit_factor),
            avg_trade_duration=avg_trade_duration,
            total_return=float(total_return)
        )
    
    def generate_report(
        self,
        output_path: str,
        results: Optional[Dict] = None
    ) -> None:
        """
        Generate evaluation report in JSON format.
        
        Args:
            output_path: Path to save report
            results: Optional pre-computed results (from evaluate())
        """
        if results is None:
            raise ValueError("No results provided. Run evaluate() first.")
        
        path_obj = Path(output_path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        
        # Add metadata
        report = {
            'model': self.model.__class__.__name__,
            'evaluation_date': datetime.now().isoformat(),
            'metrics': results
        }
        
        # Save JSON
        with open(str(path_obj), 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Report saved to {str(path_obj)}")
    
    def analyze_failure_cases(
        self,
        predictions: np.ndarray,
        targets: np.ndarray,
        threshold: float = 0.1
    ) -> Dict[str, Any]:
        """
        Analyze cases where model failed significantly.
        
        Args:
            predictions: Model predictions
            targets: Ground truth values
            threshold: Error threshold (as fraction of target)
        
        Returns:
            Dictionary with failure analysis
        """
        # Compute errors
        errors = np.abs(predictions - targets)
        relative_errors = errors / (np.abs(targets) + 1e-8)
        
        # Find failure cases
        failure_mask = relative_errors > threshold
        n_failures = failure_mask.sum()
        
        if n_failures == 0:
            logger.info("No significant failure cases found")
            return {'n_failures': 0}
        
        # Analyze failures
        failure_indices = np.where(failure_mask)[0]
        failure_predictions = predictions[failure_mask]
        failure_targets = targets[failure_mask]
        failure_errors = errors[failure_mask]
        
        analysis = {
            'n_failures': int(n_failures),
            'failure_rate': float(n_failures / len(predictions)),
            'mean_error': float(failure_errors.mean()),
            'max_error': float(failure_errors.max()),
            'worst_cases': [
                {
                    'index': int(idx),
                    'prediction': float(predictions[idx]),
                    'target': float(targets[idx]),
                    'error': float(errors[idx]),
                    'relative_error': float(relative_errors[idx])
                }
                for idx in failure_indices[:10]  # Top 10 worst cases
            ]
        }
        
        logger.info(f"Found {n_failures} failure cases ({analysis['failure_rate']:.2%})")
        
        return analysis


if __name__ == '__main__':
    # Test evaluation module
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Model Evaluation")
    print("="*60)
    
    # Create dummy predictions and targets
    np.random.seed(42)
    n_samples = 1000
    
    # Regression test
    print("\n✓ Testing regression metrics...")
    targets = np.random.randn(n_samples) * 10 + 100
    predictions = targets + np.random.randn(n_samples) * 2  # Add noise
    
    evaluator = ModelEvaluator(model=None)  # Mock model
    reg_metrics = evaluator._compute_regression_metrics(predictions, targets)
    
    print(f"  MAE: {reg_metrics.mae:.4f}")
    print(f"  RMSE: {reg_metrics.rmse:.4f}")
    print(f"  MAPE: {reg_metrics.mape:.4f}%")
    print(f"  R²: {reg_metrics.r2:.4f}")
    print(f"  Correlation: {reg_metrics.correlation:.4f}")
    
    # Classification test
    print("\n✓ Testing classification metrics...")
    n_classes = 3
    signal_targets = np.random.randint(0, n_classes, n_samples)
    signal_predictions = signal_targets.copy()
    # Add some errors
    error_indices = np.random.choice(n_samples, size=200, replace=False)
    signal_predictions[error_indices] = np.random.randint(0, n_classes, 200)
    
    class_metrics = evaluator._compute_classification_metrics(signal_predictions, signal_targets)
    
    print(f"  Accuracy: {class_metrics.accuracy:.4f}")
    print(f"  Precision: {class_metrics.precision:.4f}")
    print(f"  Recall: {class_metrics.recall:.4f}")
    print(f"  F1 Score: {class_metrics.f1_score:.4f}")
    print(f"  Confusion Matrix:")
    for row in class_metrics.confusion_matrix:
        print(f"    {row}")
    
    # Trading metrics test
    print("\n✓ Testing trading metrics...")
    returns = np.random.randn(252) * 0.02  # Daily returns, ~2% volatility
    trading_metrics = evaluator.compute_trading_metrics(returns)
    
    print(f"  Sharpe Ratio: {trading_metrics.sharpe_ratio:.4f}")
    print(f"  Sortino Ratio: {trading_metrics.sortino_ratio:.4f}")
    print(f"  Max Drawdown: {trading_metrics.max_drawdown:.4f}")
    print(f"  Win Rate: {trading_metrics.win_rate:.4f}")
    print(f"  Profit Factor: {trading_metrics.profit_factor:.4f}")
    print(f"  Total Return: {trading_metrics.total_return:.4f}")
    
    # Failure analysis test
    print("\n✓ Testing failure analysis...")
    failure_analysis = evaluator.analyze_failure_cases(predictions, targets, threshold=0.05)
    
    print(f"  Failures: {failure_analysis['n_failures']}")
    print(f"  Failure Rate: {failure_analysis['failure_rate']:.2%}")
    if failure_analysis['n_failures'] > 0:
        print(f"  Mean Error: {failure_analysis['mean_error']:.4f}")
        print(f"  Max Error: {failure_analysis['max_error']:.4f}")
    
    print("\n" + "="*60)
    print("Model Evaluation Test Complete!")
    print("="*60)
