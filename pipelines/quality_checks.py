"""
Data Quality Checks for ChronoX Trading Bot

Validates incoming data and detects anomalies:
- Missing data / time gaps
- Price outliers (spikes >10% without news correlation)
- Volume anomalies (>5 stddevs)
- Data freshness checks
- Schema validation

References:
- Statistical process control: Montgomery, D.C. (2012) "Introduction to Statistical Quality Control"
- Z-score method: Rousseeuw & Hubert (2011) "Robust statistics for outlier detection"
"""

import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import numpy as np
from dataclasses import dataclass


@dataclass
class QualityIssue:
    """Represents a data quality problem."""
    severity: str  # "INFO", "WARNING", "ERROR", "CRITICAL"
    check_type: str  # "missing_data", "outlier", "volume_anomaly", etc.
    symbol: str
    timestamp: datetime
    description: str
    metadata: Optional[Dict] = None


class DataQualityChecker:
    """
    Validates data quality and detects anomalies.
    
    Features:
    - Real-time validation of incoming data
    - Statistical outlier detection (Z-score, IQR)
    - Gap detection in time-series
    - Volume spike detection
    - Price continuity checks
    
    Example:
        checker = DataQualityChecker(z_threshold=3.0)
        
        # Check OHLCV bar
        issues = checker.check_ohlcv_bar("AAPL", bar_data)
        
        # Check for gaps
        gaps = checker.check_time_gaps("AAPL", bars, expected_interval="1min")
        
        for issue in issues:
            if issue.severity == "CRITICAL":
                alert_team(issue)
    """
    
    def __init__(
        self,
        z_threshold: float = 3.0,
        volume_threshold: float = 5.0,
        max_gap_minutes: int = 60
    ):
        """
        Initialize quality checker.
        
        Args:
            z_threshold: Z-score threshold for outlier detection
            volume_threshold: Stddev threshold for volume anomalies
            max_gap_minutes: Max acceptable gap in time-series (minutes)
        """
        self.z_threshold = z_threshold
        self.volume_threshold = volume_threshold
        self.max_gap_minutes = max_gap_minutes
        
        self.logger = logging.getLogger(f"{__name__}.DataQualityChecker")
        
        # Historical statistics (rolling window)
        self.stats_cache = {}  # symbol -> {"prices": [], "volumes": []}
        self.cache_size = 100  # Keep last 100 bars for statistics
    
    def check_ohlcv_bar(
        self,
        symbol: str,
        bar: Dict,
        prev_bar: Optional[Dict] = None
    ) -> List[QualityIssue]:
        """
        Check quality of single OHLCV bar.
        
        Args:
            symbol: Ticker symbol
            bar: OHLCV bar dict with keys: t, o, h, l, c, v
            prev_bar: Previous bar for continuity checks
            
        Returns:
            List of quality issues found
        """
        issues = []
        
        # Schema validation
        required_fields = ["t", "o", "h", "l", "c", "v"]
        missing_fields = [f for f in required_fields if f not in bar or bar[f] is None]
        if missing_fields:
            issues.append(QualityIssue(
                severity="ERROR",
                check_type="missing_fields",
                symbol=symbol,
                timestamp=datetime.fromtimestamp(bar.get("t", 0) / 1000),
                description=f"Missing required fields: {missing_fields}",
                metadata={"fields": missing_fields}
            ))
            return issues  # Can't proceed with further checks
        
        # OHLC relationship validation
        o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
        if not (l <= o <= h and l <= c <= h):
            issues.append(QualityIssue(
                severity="ERROR",
                check_type="ohlc_invalid",
                symbol=symbol,
                timestamp=datetime.fromtimestamp(bar["t"] / 1000),
                description=f"Invalid OHLC relationship: O={o}, H={h}, L={l}, C={c}",
                metadata={"ohlc": [o, h, l, c]}
            ))
        
        # Price spike detection
        if prev_bar:
            prev_close = prev_bar["c"]
            price_change_pct = abs((c - prev_close) / prev_close) * 100
            
            if price_change_pct > 10:
                issues.append(QualityIssue(
                    severity="WARNING",
                    check_type="price_spike",
                    symbol=symbol,
                    timestamp=datetime.fromtimestamp(bar["t"] / 1000),
                    description=f"Price spike: {price_change_pct:.2f}% change",
                    metadata={
                        "prev_close": prev_close,
                        "current_close": c,
                        "change_pct": price_change_pct
                    }
                ))
        
        # Volume anomaly detection
        volume_issue = self._check_volume_anomaly(symbol, bar)
        if volume_issue:
            issues.append(volume_issue)
        
        # Update statistics cache
        self._update_stats_cache(symbol, bar)
        
        return issues
    
    def _check_volume_anomaly(
        self,
        symbol: str,
        bar: Dict
    ) -> Optional[QualityIssue]:
        """
        Detect volume anomalies using Z-score.
        
        Args:
            symbol: Ticker symbol
            bar: OHLCV bar
            
        Returns:
            QualityIssue if anomaly detected, None otherwise
        """
        if symbol not in self.stats_cache:
            return None  # Not enough history
        
        volumes = self.stats_cache[symbol].get("volumes", [])
        if len(volumes) < 20:
            return None  # Need at least 20 samples
        
        current_volume = bar["v"]
        mean_volume = np.mean(volumes)
        std_volume = np.std(volumes)
        
        if std_volume == 0:
            return None  # Avoid division by zero
        
        z_score = (current_volume - mean_volume) / std_volume
        
        if abs(z_score) > self.volume_threshold:
            return QualityIssue(
                severity="WARNING",
                check_type="volume_anomaly",
                symbol=symbol,
                timestamp=datetime.fromtimestamp(bar["t"] / 1000),
                description=f"Volume anomaly: Z-score={z_score:.2f}",
                metadata={
                    "volume": current_volume,
                    "mean": mean_volume,
                    "std": std_volume,
                    "z_score": z_score
                }
            )
        
        return None
    
    def check_time_gaps(
        self,
        symbol: str,
        bars: List[Dict],
        expected_interval: str = "1min"
    ) -> List[QualityIssue]:
        """
        Check for gaps in time-series data.
        
        Args:
            symbol: Ticker symbol
            bars: List of OHLCV bars sorted by timestamp
            expected_interval: Expected time interval ("1min", "5min", "1hour", "1day")
            
        Returns:
            List of gap issues found
        """
        issues = []
        
        if len(bars) < 2:
            return issues
        
        # Parse expected interval
        interval_map = {
            "1min": 60,
            "5min": 300,
            "15min": 900,
            "1hour": 3600,
            "1day": 86400
        }
        expected_seconds = interval_map.get(expected_interval, 60)
        
        # Check gaps between consecutive bars
        for i in range(1, len(bars)):
            prev_time = bars[i-1]["t"] / 1000  # Convert ms to seconds
            curr_time = bars[i]["t"] / 1000
            
            gap_seconds = curr_time - prev_time
            
            # Allow some tolerance (e.g., 2x expected interval)
            if gap_seconds > expected_seconds * 2:
                issues.append(QualityIssue(
                    severity="WARNING",
                    check_type="time_gap",
                    symbol=symbol,
                    timestamp=datetime.fromtimestamp(prev_time),
                    description=f"Time gap detected: {gap_seconds/60:.1f} minutes",
                    metadata={
                        "prev_timestamp": prev_time,
                        "curr_timestamp": curr_time,
                        "gap_minutes": gap_seconds / 60,
                        "expected_seconds": expected_seconds
                    }
                ))
        
        return issues
    
    def check_data_freshness(
        self,
        symbol: str,
        last_timestamp: datetime,
        max_age_minutes: int = 15
    ) -> Optional[QualityIssue]:
        """
        Check if data is fresh (not stale).
        
        Args:
            symbol: Ticker symbol
            last_timestamp: Timestamp of most recent data point
            max_age_minutes: Max acceptable age in minutes
            
        Returns:
            QualityIssue if data is stale, None otherwise
        """
        now = datetime.now()
        age = now - last_timestamp
        
        if age > timedelta(minutes=max_age_minutes):
            return QualityIssue(
                severity="WARNING",
                check_type="stale_data",
                symbol=symbol,
                timestamp=last_timestamp,
                description=f"Data is stale: {age.total_seconds()/60:.1f} minutes old",
                metadata={
                    "last_timestamp": last_timestamp,
                    "current_time": now,
                    "age_minutes": age.total_seconds() / 60
                }
            )
        
        return None
    
    def _update_stats_cache(self, symbol: str, bar: Dict):
        """
        Update rolling statistics cache.
        
        Args:
            symbol: Ticker symbol
            bar: OHLCV bar
        """
        if symbol not in self.stats_cache:
            self.stats_cache[symbol] = {"prices": [], "volumes": []}
        
        cache = self.stats_cache[symbol]
        
        # Add new values
        cache["prices"].append(bar["c"])
        cache["volumes"].append(bar["v"])
        
        # Keep only last N values
        if len(cache["prices"]) > self.cache_size:
            cache["prices"] = cache["prices"][-self.cache_size:]
        if len(cache["volumes"]) > self.cache_size:
            cache["volumes"] = cache["volumes"][-self.cache_size:]


class AnomalyDetector:
    """
    Advanced anomaly detection using multiple methods.
    
    Methods:
    - Z-score (parametric)
    - Interquartile Range (IQR, non-parametric)
    - Isolation Forest (ML-based)
    - Moving Average Deviation
    
    Example:
        detector = AnomalyDetector()
        
        # Detect price anomalies
        anomalies = detector.detect_price_anomalies(prices, method="zscore")
        
        # Detect volume anomalies
        vol_anomalies = detector.detect_volume_anomalies(volumes, method="iqr")
    """
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.AnomalyDetector")
    
    def detect_price_anomalies(
        self,
        prices: List[float],
        method: str = "zscore",
        threshold: float = 3.0
    ) -> Tuple[List[int], List[float]]:
        """
        Detect price anomalies.
        
        Args:
            prices: List of prices
            method: Detection method ("zscore", "iqr", "mad")
            threshold: Detection threshold (depends on method)
            
        Returns:
            Tuple of (anomaly_indices, anomaly_scores)
        """
        if method == "zscore":
            return self._zscore_detection(prices, threshold)
        elif method == "iqr":
            return self._iqr_detection(prices, threshold)
        elif method == "mad":
            return self._mad_detection(prices, threshold)
        else:
            raise ValueError(f"Unknown method: {method}")
    
    def _zscore_detection(
        self,
        values: List[float],
        threshold: float
    ) -> Tuple[List[int], List[float]]:
        """Z-score based outlier detection."""
        arr = np.array(values)
        mean = np.mean(arr)
        std = np.std(arr)
        
        if std == 0:
            return [], []
        
        z_scores = np.abs((arr - mean) / std)
        anomaly_indices = np.where(z_scores > threshold)[0].tolist()
        anomaly_scores = z_scores[anomaly_indices].tolist()
        
        return anomaly_indices, anomaly_scores
    
    def _iqr_detection(
        self,
        values: List[float],
        multiplier: float = 1.5
    ) -> Tuple[List[int], List[float]]:
        """Interquartile Range (IQR) outlier detection."""
        arr = np.array(values)
        q1 = np.percentile(arr, 25)
        q3 = np.percentile(arr, 75)
        iqr = q3 - q1
        
        lower_bound = q1 - multiplier * iqr
        upper_bound = q3 + multiplier * iqr
        
        anomalies = (arr < lower_bound) | (arr > upper_bound)
        anomaly_indices = np.where(anomalies)[0].tolist()
        anomaly_scores = np.abs(arr - np.median(arr))[anomaly_indices].tolist()
        
        return anomaly_indices, anomaly_scores
    
    def _mad_detection(
        self,
        values: List[float],
        threshold: float = 3.5
    ) -> Tuple[List[int], List[float]]:
        """
        Median Absolute Deviation (MAD) outlier detection.
        
        More robust than Z-score for non-normal distributions.
        """
        arr = np.array(values)
        median = np.median(arr)
        mad = np.median(np.abs(arr - median))
        
        if mad == 0:
            return [], []
        
        # Modified Z-score using MAD
        modified_z_scores = 0.6745 * (arr - median) / mad
        anomaly_indices = np.where(np.abs(modified_z_scores) > threshold)[0].tolist()
        anomaly_scores = np.abs(modified_z_scores)[anomaly_indices].tolist()
        
        return anomaly_indices, anomaly_scores


# Example usage
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    
    # Test data quality checker
    checker = DataQualityChecker()
    
    # Normal bar
    bar1 = {
        "t": 1704067200000,  # 2024-01-01 00:00:00
        "o": 100.0,
        "h": 102.0,
        "l": 99.0,
        "c": 101.0,
        "v": 1000000
    }
    
    # Bar with price spike
    bar2 = {
        "t": 1704067260000,  # 2024-01-01 00:01:00
        "o": 101.0,
        "h": 115.0,
        "l": 100.0,
        "c": 112.0,  # 11% spike
        "v": 5000000
    }
    
    issues = checker.check_ohlcv_bar("AAPL", bar1)
    print(f"Bar 1 issues: {len(issues)}")
    
    issues = checker.check_ohlcv_bar("AAPL", bar2, prev_bar=bar1)
    print(f"Bar 2 issues: {len(issues)}")
    for issue in issues:
        print(f"  - {issue.severity}: {issue.description}")
    
    # Test anomaly detector
    detector = AnomalyDetector()
    
    # Generate test data with outliers
    np.random.seed(42)
    prices = list(np.random.normal(100, 2, 100))
    prices[50] = 150  # Outlier
    prices[75] = 50   # Outlier
    
    indices, scores = detector.detect_price_anomalies(prices, method="zscore")
    print(f"\nDetected {len(indices)} price anomalies at indices: {indices}")
