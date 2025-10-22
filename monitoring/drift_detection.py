"""
Model Drift Detection

Detects distribution shifts and model degradation using statistical tests
and performance monitoring.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import logging

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.metrics import mutual_info_score
import torch

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)


@dataclass
class DriftMetrics:
    """Container for drift detection metrics."""
    drift_score: float
    drift_detected: bool
    psi_score: float  # Population Stability Index
    kl_divergence: float
    js_divergence: float
    ks_statistic: float
    ks_pvalue: float
    prediction_drift: float
    feature_drift: Dict[str, float]
    timestamp: datetime


class DriftDetector:
    """
    Detects model drift using multiple statistical methods.
    
    Methods:
        - Population Stability Index (PSI)
        - Kullback-Leibler (KL) divergence
        - Jensen-Shannon (JS) divergence
        - Kolmogorov-Smirnov (KS) test
        - Prediction drift monitoring
        - Feature distribution shift detection
    """
    
    def __init__(
        self,
        timescale_url: str = "postgresql://chronox:chronox_db_pass_2024@timescaledb:5432/chronox_timeseries",
        postgres_url: str = "postgresql://chronox:chronox_db_pass_2024@postgres:5432/chronox_metadata",
        drift_threshold: float = 0.3,
        psi_threshold: float = 0.2,
        kl_threshold: float = 0.5,
        ks_threshold: float = 0.05,
    ):
        """
        Initialize drift detector.
        
        Args:
            timescale_url: TimescaleDB connection URL (time-series data)
            postgres_url: PostgreSQL connection URL (metadata)
            drift_threshold: Overall drift threshold (0-1)
            psi_threshold: PSI threshold (typically 0.1-0.25)
            kl_threshold: KL divergence threshold
            ks_threshold: KS test p-value threshold
        """
        self.drift_threshold = drift_threshold
        self.psi_threshold = psi_threshold
        self.kl_threshold = kl_threshold
        self.ks_threshold = ks_threshold
        
        # Database connections
        self.timescale_engine = create_engine(timescale_url)
        self.postgres_engine = create_engine(postgres_url)
    
    def detect_drift(
        self,
        model_name: str,
        lookback_days: int = 7,
        reference_days: int = 30,
        drift_threshold: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Detect drift for a specific model.
        
        Args:
            model_name: Name of the model to check
            lookback_days: Number of recent days to analyze
            reference_days: Number of days for reference distribution
            drift_threshold: Override default drift threshold
        
        Returns:
            Dictionary with drift detection results
        """
        logger.info(f"Detecting drift for {model_name}")
        
        threshold = drift_threshold or self.drift_threshold
        
        # Get reference and current data
        reference_data = self._get_reference_data(model_name, reference_days)
        current_data = self._get_current_data(model_name, lookback_days)
        
        if reference_data is None or current_data is None:
            logger.warning(f"Insufficient data for drift detection on {model_name}")
            return {
                'model_name': model_name,
                'drift_detected': False,
                'drift_score': 0.0,
                'reason': 'insufficient_data'
            }
        
        # Calculate drift metrics
        metrics = self._calculate_drift_metrics(
            reference_data=reference_data,
            current_data=current_data,
            model_name=model_name
        )
        
        # Determine if drift detected
        drift_detected = (
            metrics.drift_score > threshold or
            metrics.psi_score > self.psi_threshold or
            metrics.kl_divergence > self.kl_threshold or
            metrics.ks_pvalue < self.ks_threshold
        )
        
        # Log results
        self._log_drift_results(model_name, metrics, drift_detected)
        
        return {
            'model_name': model_name,
            'drift_detected': drift_detected,
            'drift_score': metrics.drift_score,
            'psi_score': metrics.psi_score,
            'kl_divergence': metrics.kl_divergence,
            'js_divergence': metrics.js_divergence,
            'ks_statistic': metrics.ks_statistic,
            'ks_pvalue': metrics.ks_pvalue,
            'prediction_drift': metrics.prediction_drift,
            'feature_drift': metrics.feature_drift,
            'timestamp': metrics.timestamp.isoformat()
        }
    
    def _get_reference_data(
        self,
        model_name: str,
        days: int
    ) -> Optional[pd.DataFrame]:
        """
        Get reference data distribution (training data distribution).
        
        Args:
            model_name: Model name
            days: Number of days of historical data
        
        Returns:
            DataFrame with reference features and predictions
        """
        end_date = datetime.utcnow() - timedelta(days=days)
        start_date = end_date - timedelta(days=30)  # 30 days of reference
        
        query = f'''
        SELECT 
            prediction,
            feature_data
        FROM model_predictions
        WHERE model_name = :model_name
            AND timestamp >= :start_date
            AND timestamp < :end_date
        ORDER BY timestamp
        '''
        
        with self.engine.connect() as conn:
            result = conn.execute(
                text(query),
                {
                    'model_name': model_name,
                    'start_date': start_date,
                    'end_date': end_date
                }
            )
            
            data = result.fetchall()
            
            if len(data) == 0:
                return None
            
            df = pd.DataFrame(data, columns=['prediction', 'feature_data'])
            return df
    
    def _get_current_data(
        self,
        model_name: str,
        days: int
    ) -> Optional[pd.DataFrame]:
        """
        Get current data distribution.
        
        Args:
            model_name: Model name
            days: Number of recent days
        
        Returns:
            DataFrame with current features and predictions
        """
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        query = f'''
        SELECT 
            prediction,
            feature_data
        FROM model_predictions
        WHERE model_name = :model_name
            AND timestamp >= :start_date
            AND timestamp < :end_date
        ORDER BY timestamp
        '''
        
        with self.engine.connect() as conn:
            result = conn.execute(
                text(query),
                {
                    'model_name': model_name,
                    'start_date': start_date,
                    'end_date': end_date
                }
            )
            
            data = result.fetchall()
            
            if len(data) == 0:
                return None
            
            df = pd.DataFrame(data, columns=['prediction', 'feature_data'])
            return df
    
    def _calculate_drift_metrics(
        self,
        reference_data: pd.DataFrame,
        current_data: pd.DataFrame,
        model_name: str
    ) -> DriftMetrics:
        """
        Calculate comprehensive drift metrics.
        
        Args:
            reference_data: Reference distribution
            current_data: Current distribution
            model_name: Model name
        
        Returns:
            DriftMetrics object with all metrics
        """
        # Extract predictions
        ref_predictions = reference_data['prediction'].values
        curr_predictions = current_data['prediction'].values
        
        # Calculate PSI for predictions
        psi_score = self._calculate_psi(ref_predictions, curr_predictions)
        
        # Calculate KL divergence
        kl_div = self._calculate_kl_divergence(ref_predictions, curr_predictions)
        
        # Calculate JS divergence
        js_div = self._calculate_js_divergence(ref_predictions, curr_predictions)
        
        # Kolmogorov-Smirnov test
        ks_stat, ks_pval = stats.ks_2samp(ref_predictions, curr_predictions)
        
        # Prediction drift (change in mean prediction)
        pred_drift = abs(np.mean(curr_predictions) - np.mean(ref_predictions))
        
        # Feature drift (if feature data available)
        feature_drift = self._calculate_feature_drift(
            reference_data,
            current_data
        )
        
        # Overall drift score (weighted combination)
        drift_score = self._compute_overall_drift_score(
            psi=psi_score,
            kl=kl_div,
            js=js_div,
            ks_pval=ks_pval,
            pred_drift=pred_drift
        )
        
        return DriftMetrics(
            drift_score=drift_score,
            drift_detected=drift_score > self.drift_threshold,
            psi_score=psi_score,
            kl_divergence=kl_div,
            js_divergence=js_div,
            ks_statistic=ks_stat,
            ks_pvalue=ks_pval,
            prediction_drift=pred_drift,
            feature_drift=feature_drift,
            timestamp=datetime.utcnow()
        )
    
    def _calculate_psi(
        self,
        reference: np.ndarray,
        current: np.ndarray,
        bins: int = 10
    ) -> float:
        """
        Calculate Population Stability Index (PSI).
        
        PSI < 0.1: No significant change
        0.1 <= PSI < 0.25: Small change
        PSI >= 0.25: Significant change
        
        Args:
            reference: Reference distribution
            current: Current distribution
            bins: Number of bins for discretization
        
        Returns:
            PSI score
        """
        # Create bins based on reference distribution
        breakpoints = np.percentile(reference, np.linspace(0, 100, bins + 1))
        breakpoints = np.unique(breakpoints)  # Remove duplicates
        
        # Calculate distributions
        ref_counts, _ = np.histogram(reference, bins=breakpoints)
        curr_counts, _ = np.histogram(current, bins=breakpoints)
        
        # Convert to percentages (add small epsilon to avoid log(0))
        epsilon = 1e-10
        ref_pct = ref_counts / len(reference) + epsilon
        curr_pct = curr_counts / len(current) + epsilon
        
        # Calculate PSI
        psi = np.sum((curr_pct - ref_pct) * np.log(curr_pct / ref_pct))
        
        return float(psi)
    
    def _calculate_kl_divergence(
        self,
        reference: np.ndarray,
        current: np.ndarray,
        bins: int = 50
    ) -> float:
        """
        Calculate Kullback-Leibler divergence.
        
        Args:
            reference: Reference distribution
            current: Current distribution
            bins: Number of bins
        
        Returns:
            KL divergence
        """
        # Create histograms
        min_val = min(reference.min(), current.min())
        max_val = max(reference.max(), current.max())
        
        ref_hist, _ = np.histogram(reference, bins=bins, range=(min_val, max_val))
        curr_hist, _ = np.histogram(current, bins=bins, range=(min_val, max_val))
        
        # Normalize to probabilities
        epsilon = 1e-10
        ref_prob = ref_hist / ref_hist.sum() + epsilon
        curr_prob = curr_hist / curr_hist.sum() + epsilon
        
        # Calculate KL divergence
        kl_div = np.sum(curr_prob * np.log(curr_prob / ref_prob))
        
        return float(kl_div)
    
    def _calculate_js_divergence(
        self,
        reference: np.ndarray,
        current: np.ndarray,
        bins: int = 50
    ) -> float:
        """
        Calculate Jensen-Shannon divergence (symmetric version of KL).
        
        Args:
            reference: Reference distribution
            current: Current distribution
            bins: Number of bins
        
        Returns:
            JS divergence
        """
        # Create histograms
        min_val = min(reference.min(), current.min())
        max_val = max(reference.max(), current.max())
        
        ref_hist, _ = np.histogram(reference, bins=bins, range=(min_val, max_val))
        curr_hist, _ = np.histogram(current, bins=bins, range=(min_val, max_val))
        
        # Normalize
        epsilon = 1e-10
        p = ref_hist / ref_hist.sum() + epsilon
        q = curr_hist / curr_hist.sum() + epsilon
        
        # Calculate JS divergence
        m = 0.5 * (p + q)
        js_div = 0.5 * (
            np.sum(p * np.log(p / m)) + 
            np.sum(q * np.log(q / m))
        )
        
        return float(js_div)
    
    def _calculate_feature_drift(
        self,
        reference_data: pd.DataFrame,
        current_data: pd.DataFrame
    ) -> Dict[str, float]:
        """
        Calculate drift for individual features.
        
        Args:
            reference_data: Reference feature data
            current_data: Current feature data
        
        Returns:
            Dictionary of feature names to drift scores
        """
        # Simplified - assumes feature_data is JSON
        # In production, extract and analyze each feature
        
        feature_drift = {}
        
        # Example: if features were stored as columns
        # for feature in feature_columns:
        #     ref_vals = reference_data[feature].values
        #     curr_vals = current_data[feature].values
        #     feature_drift[feature] = self._calculate_psi(ref_vals, curr_vals)
        
        return feature_drift
    
    def _compute_overall_drift_score(
        self,
        psi: float,
        kl: float,
        js: float,
        ks_pval: float,
        pred_drift: float
    ) -> float:
        """
        Compute weighted overall drift score.
        
        Args:
            psi: PSI score
            kl: KL divergence
            js: JS divergence
            ks_pval: KS test p-value
            pred_drift: Prediction drift
        
        Returns:
            Overall drift score (0-1)
        """
        # Normalize metrics to 0-1 scale
        psi_norm = min(psi / 0.25, 1.0)  # PSI > 0.25 is significant
        kl_norm = min(kl / 1.0, 1.0)      # KL > 1.0 is significant
        js_norm = min(js / 0.5, 1.0)      # JS > 0.5 is significant
        ks_norm = 1.0 - ks_pval           # Lower p-value = more drift
        pred_norm = min(pred_drift / 0.1, 1.0)  # 10% change is significant
        
        # Weighted average
        weights = {
            'psi': 0.30,
            'kl': 0.25,
            'js': 0.20,
            'ks': 0.15,
            'pred': 0.10
        }
        
        drift_score = (
            weights['psi'] * psi_norm +
            weights['kl'] * kl_norm +
            weights['js'] * js_norm +
            weights['ks'] * ks_norm +
            weights['pred'] * pred_norm
        )
        
        return drift_score
    
    def _log_drift_results(
        self,
        model_name: str,
        metrics: DriftMetrics,
        drift_detected: bool
    ) -> None:
        """
        Log drift detection results to database.
        
        Args:
            model_name: Model name
            metrics: Drift metrics
            drift_detected: Whether drift was detected
        """
        query = '''
        INSERT INTO model_drift_log (
            model_name,
            drift_detected,
            drift_score,
            psi_score,
            kl_divergence,
            js_divergence,
            ks_statistic,
            ks_pvalue,
            prediction_drift,
            timestamp
        ) VALUES (
            :model_name,
            :drift_detected,
            :drift_score,
            :psi_score,
            :kl_divergence,
            :js_divergence,
            :ks_statistic,
            :ks_pvalue,
            :prediction_drift,
            :timestamp
        )
        '''
        
        with self.postgres_engine.connect() as conn:
            conn.execute(
                text(query),
                {
                    'model_name': model_name,
                    'drift_detected': drift_detected,
                    'drift_score': metrics.drift_score,
                    'psi_score': metrics.psi_score,
                    'kl_divergence': metrics.kl_divergence,
                    'js_divergence': metrics.js_divergence,
                    'ks_statistic': metrics.ks_statistic,
                    'ks_pvalue': metrics.ks_pvalue,
                    'prediction_drift': metrics.prediction_drift,
                    'timestamp': metrics.timestamp
                }
            )
            conn.commit()
        
        logger.info(
            f"Logged drift results for {model_name}: "
            f"detected={drift_detected}, score={metrics.drift_score:.3f}"
        )
    
    def __del__(self):
        """Cleanup database connections."""
        if hasattr(self, 'timescale_engine'):
            self.timescale_engine.dispose()
        if hasattr(self, 'postgres_engine'):
            self.postgres_engine.dispose()
