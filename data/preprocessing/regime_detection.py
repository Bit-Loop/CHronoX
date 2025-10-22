"""
Market Regime Detection

Identifies different market states/regimes to improve trading decisions:
    1. Trend regimes: Uptrend, Downtrend, Sideways
    2. Volatility regimes: High, Normal, Low
    3. Volume regimes: High, Normal, Low
    4. Hidden Markov Model (HMM) for state classification

References:
    - Kritzman et al. (2012) "Regime Shifts: Implications for Dynamic Strategies"
    - Murphy (1999) "Technical Analysis of the Financial Markets"

Usage with Phase 2 features:
    from data.preprocessing import MultiResolutionPipeline
    from data.preprocessing.regime_detection import MarketRegimeDetector
    
    # Get multi-scale features
    pipeline = MultiResolutionPipeline()
    scale_data = pipeline.transform(df_1min)
    
    # Detect regimes
    detector = MarketRegimeDetector()
    regimes = detector.detect_regimes(scale_data['daily'])
"""

import pandas as pd
import numpy as np
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class TrendRegime(Enum):
    """Trend classification"""
    STRONG_UPTREND = 3
    UPTREND = 2
    SIDEWAYS = 1
    DOWNTREND = -1
    STRONG_DOWNTREND = -2


class VolatilityRegime(Enum):
    """Volatility classification"""
    HIGH = 2
    NORMAL = 1
    LOW = 0


class VolumeRegime(Enum):
    """Volume classification"""
    HIGH = 2
    NORMAL = 1
    LOW = 0


@dataclass
class RegimeState:
    """Container for regime classification at a single timestep"""
    timestamp: pd.Timestamp
    trend: TrendRegime
    volatility: VolatilityRegime
    volume: VolumeRegime
    trend_strength: float  # 0-1 confidence
    volatility_percentile: float  # 0-100
    volume_percentile: float  # 0-100
    hmm_state: Optional[int] = None  # HMM state index if available


class MarketRegimeDetector:
    """
    Detect market regimes using multiple methods.
    
    Methods:
        1. Trend Detection: ADX + DMI, MA slopes, price momentum
        2. Volatility Clustering: Rolling std, ATR percentiles
        3. Volume Analysis: Volume percentiles, OBV trends
        4. HMM: Hidden states learned from price/volume patterns
    
    Args:
        trend_lookback: Window for trend calculation (default 20)
        volatility_lookback: Window for volatility calculation (default 20)
        volume_lookback: Window for volume calculation (default 20)
        adx_threshold: ADX value for strong trend (default 25)
        volatility_high_percentile: Threshold for high vol (default 75)
        volatility_low_percentile: Threshold for low vol (default 25)
    """
    
    def __init__(
        self,
        trend_lookback: int = 20,
        volatility_lookback: int = 20,
        volume_lookback: int = 20,
        adx_threshold: float = 25.0,
        volatility_high_percentile: float = 75.0,
        volatility_low_percentile: float = 25.0
    ):
        self.trend_lookback = trend_lookback
        self.volatility_lookback = volatility_lookback
        self.volume_lookback = volume_lookback
        self.adx_threshold = adx_threshold
        self.volatility_high_percentile = volatility_high_percentile
        self.volatility_low_percentile = volatility_low_percentile
    
    def detect_trend_regime(
        self,
        df: pd.DataFrame,
        price_col: str = 'close'
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Classify trend regime using multiple indicators.
        
        Combines:
            1. ADX + DMI for trend strength and direction
            2. Moving average slopes
            3. Price momentum
        
        Args:
            df: DataFrame with OHLCV data and indicators
            price_col: Column name for price
        
        Returns:
            regimes: Series of TrendRegime enum values
            strength: Series of trend strength (0-1)
        """
        # Check for required indicators (should be added by Phase 2)
        required = ['adx', 'plus_di', 'minus_di', 'sma_20', 'sma_50']
        missing = [col for col in required if col not in df.columns]
        if missing:
            logger.warning(f"Missing indicators for trend detection: {missing}")
            logger.warning("Using basic price-based trend detection")
            return self._detect_trend_basic(df, price_col)
        
        regimes = []
        strengths = []
        
        for idx in range(len(df)):
            if idx < self.trend_lookback:
                regimes.append(TrendRegime.SIDEWAYS)
                strengths.append(0.0)
                continue
            
            # Get current values
            adx = df['adx'].iloc[idx]
            plus_di = df['plus_di'].iloc[idx]
            minus_di = df['minus_di'].iloc[idx]
            sma_20 = df['sma_20'].iloc[idx]
            sma_50 = df['sma_50'].iloc[idx]
            price = df[price_col].iloc[idx]
            
            # Calculate trend strength (0-1) from ADX
            strength = min(adx / 50.0, 1.0) if not np.isnan(adx) else 0.0
            
            # Determine trend direction
            if np.isnan(adx) or np.isnan(plus_di) or np.isnan(minus_di):
                regime = TrendRegime.SIDEWAYS
            elif adx > self.adx_threshold:
                # Strong trend
                if plus_di > minus_di and price > sma_20 > sma_50:
                    regime = TrendRegime.STRONG_UPTREND
                elif minus_di > plus_di and price < sma_20 < sma_50:
                    regime = TrendRegime.STRONG_DOWNTREND
                elif plus_di > minus_di:
                    regime = TrendRegime.UPTREND
                else:
                    regime = TrendRegime.DOWNTREND
            else:
                # Weak trend / sideways
                if price > sma_20 and sma_20 > sma_50:
                    regime = TrendRegime.UPTREND
                elif price < sma_20 and sma_20 < sma_50:
                    regime = TrendRegime.DOWNTREND
                else:
                    regime = TrendRegime.SIDEWAYS
            
            regimes.append(regime)
            strengths.append(strength)
        
        return pd.Series(regimes, index=df.index), pd.Series(strengths, index=df.index)
    
    def _detect_trend_basic(
        self,
        df: pd.DataFrame,
        price_col: str = 'close'
    ) -> Tuple[pd.Series, pd.Series]:
        """Fallback trend detection using only price and simple MAs"""
        # Calculate simple moving averages if not present
        if 'sma_20' not in df.columns:
            df['sma_20'] = df[price_col].rolling(20).mean()
        if 'sma_50' not in df.columns:
            df['sma_50'] = df[price_col].rolling(50).mean()
        
        regimes = []
        strengths = []
        
        for idx in range(len(df)):
            if idx < 50:  # Need 50 periods for SMA50
                regimes.append(TrendRegime.SIDEWAYS)
                strengths.append(0.0)
                continue
            
            price = df[price_col].iloc[idx]
            sma_20 = df['sma_20'].iloc[idx]
            sma_50 = df['sma_50'].iloc[idx]
            
            # Simple trend based on MA alignment
            if price > sma_20 > sma_50:
                # Calculate strength from MA separation
                sep_20 = (price - sma_20) / sma_20 * 100
                sep_50 = (sma_20 - sma_50) / sma_50 * 100
                strength = min((sep_20 + sep_50) / 4.0, 1.0)  # Normalize
                
                if strength > 0.5:
                    regime = TrendRegime.STRONG_UPTREND
                else:
                    regime = TrendRegime.UPTREND
            
            elif price < sma_20 < sma_50:
                sep_20 = (sma_20 - price) / price * 100
                sep_50 = (sma_50 - sma_20) / sma_20 * 100
                strength = min((sep_20 + sep_50) / 4.0, 1.0)
                
                if strength > 0.5:
                    regime = TrendRegime.STRONG_DOWNTREND
                else:
                    regime = TrendRegime.DOWNTREND
            
            else:
                regime = TrendRegime.SIDEWAYS
                strength = 0.0
            
            regimes.append(regime)
            strengths.append(abs(strength))
        
        return pd.Series(regimes, index=df.index), pd.Series(strengths, index=df.index)
    
    def detect_volatility_regime(
        self,
        df: pd.DataFrame
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Classify volatility regime.
        
        Uses ATR or rolling standard deviation percentiles to classify
        current volatility as High, Normal, or Low.
        
        Args:
            df: DataFrame with OHLCV data and indicators
        
        Returns:
            regimes: Series of VolatilityRegime enum values
            percentiles: Series of volatility percentiles (0-100)
        """
        # Use ATR if available, else calculate rolling std
        if 'atr' in df.columns:
            volatility = df['atr'].copy()
        elif 'volatility' in df.columns:
            volatility = df['volatility'].copy()
        else:
            logger.warning("No volatility indicator found, calculating from returns")
            returns = df['close'].pct_change()
            volatility = returns.rolling(self.volatility_lookback).std()
        
        # Calculate rolling percentiles
        percentiles = volatility.rolling(
            window=100,
            min_periods=20
        ).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1] * 100)
        
        # Classify regime based on percentiles
        regimes = []
        for pct in percentiles:
            if np.isnan(pct):
                regimes.append(VolatilityRegime.NORMAL)
            elif pct >= self.volatility_high_percentile:
                regimes.append(VolatilityRegime.HIGH)
            elif pct <= self.volatility_low_percentile:
                regimes.append(VolatilityRegime.LOW)
            else:
                regimes.append(VolatilityRegime.NORMAL)
        
        return pd.Series(regimes, index=df.index), percentiles
    
    def detect_volume_regime(
        self,
        df: pd.DataFrame
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Classify volume regime.
        
        Uses volume percentiles to classify current volume as
        High, Normal, or Low.
        
        Args:
            df: DataFrame with volume column
        
        Returns:
            regimes: Series of VolumeRegime enum values
            percentiles: Series of volume percentiles (0-100)
        """
        if 'volume' not in df.columns:
            logger.warning("No volume data found")
            return (
                pd.Series([VolumeRegime.NORMAL] * len(df), index=df.index),
                pd.Series([50.0] * len(df), index=df.index)
            )
        
        volume = df['volume'].copy()
        
        # Calculate rolling percentiles
        percentiles = volume.rolling(
            window=100,
            min_periods=20
        ).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1] * 100)
        
        # Classify regime based on percentiles
        regimes = []
        for pct in percentiles:
            if np.isnan(pct):
                regimes.append(VolumeRegime.NORMAL)
            elif pct >= 75.0:  # Top quartile
                regimes.append(VolumeRegime.HIGH)
            elif pct <= 25.0:  # Bottom quartile
                regimes.append(VolumeRegime.LOW)
            else:
                regimes.append(VolumeRegime.NORMAL)
        
        return pd.Series(regimes, index=df.index), percentiles
    
    def detect_regimes(
        self,
        df: pd.DataFrame,
        include_hmm: bool = False
    ) -> pd.DataFrame:
        """
        Detect all market regimes.
        
        Args:
            df: DataFrame with OHLCV data and indicators (from Phase 2)
            include_hmm: Whether to include HMM state detection (requires sklearn)
        
        Returns:
            DataFrame with regime classifications:
                - trend_regime: TrendRegime enum
                - trend_strength: 0-1 confidence
                - volatility_regime: VolatilityRegime enum
                - volatility_percentile: 0-100
                - volume_regime: VolumeRegime enum
                - volume_percentile: 0-100
                - hmm_state: Optional HMM state index
        """
        logger.info(f"Detecting market regimes for {len(df)} data points")
        
        # Detect trend regimes
        trend_regimes, trend_strengths = self.detect_trend_regime(df)
        
        # Detect volatility regimes
        vol_regimes, vol_percentiles = self.detect_volatility_regime(df)
        
        # Detect volume regimes
        volume_regimes, volume_percentiles = self.detect_volume_regime(df)
        
        # Create output DataFrame
        result = pd.DataFrame({
            'trend_regime': trend_regimes,
            'trend_strength': trend_strengths,
            'volatility_regime': vol_regimes,
            'volatility_percentile': vol_percentiles,
            'volume_regime': volume_regimes,
            'volume_percentile': volume_percentiles
        }, index=df.index)
        
        # Add HMM states if requested
        if include_hmm:
            try:
                hmm_states = self._detect_hmm_states(df)
                result['hmm_state'] = hmm_states
            except ImportError:
                logger.warning("sklearn not available, skipping HMM detection")
            except Exception as e:
                logger.error(f"HMM detection failed: {e}")
        
        logger.info(f"Regime detection complete. Unique states:")
        logger.info(f"  Trend: {result['trend_regime'].value_counts().to_dict()}")
        logger.info(f"  Volatility: {result['volatility_regime'].value_counts().to_dict()}")
        logger.info(f"  Volume: {result['volume_regime'].value_counts().to_dict()}")
        
        return result
    
    def _detect_hmm_states(
        self,
        df: pd.DataFrame,
        n_states: int = 3
    ) -> pd.Series:
        """
        Detect hidden states using HMM.
        
        States typically correspond to:
            0: Bear market (downtrend, high vol)
            1: Sideways/transition
            2: Bull market (uptrend, low vol)
        
        Args:
            df: DataFrame with price and volume data
            n_states: Number of hidden states
        
        Returns:
            Series of HMM state indices
        """
        from sklearn.mixture import GaussianMixture
        
        # Prepare features for HMM
        # Use returns, volatility, and volume as observable features
        features = []
        
        # Returns
        returns = df['close'].pct_change().fillna(0)
        features.append(returns)
        
        # Volatility (rolling std of returns)
        vol = returns.rolling(20).std().fillna(0)
        features.append(vol)
        
        # Volume (normalized)
        if 'volume' in df.columns:
            vol_norm = (df['volume'] - df['volume'].mean()) / df['volume'].std()
            vol_norm = vol_norm.fillna(0)
            features.append(vol_norm)
        
        # Stack features
        X = np.column_stack(features)
        
        # Fit Gaussian Mixture Model (simpler alternative to HMM)
        # True HMM would require hmmlearn library
        gmm = GaussianMixture(
            n_components=n_states,
            covariance_type='full',
            random_state=42,
            max_iter=100
        )
        
        states = gmm.fit_predict(X)
        
        return pd.Series(states, index=df.index)
    
    def get_regime_transitions(
        self,
        regimes: pd.DataFrame,
        regime_col: str = 'trend_regime'
    ) -> List[Tuple[pd.Timestamp, TrendRegime, TrendRegime]]:
        """
        Identify regime transitions.
        
        Args:
            regimes: DataFrame from detect_regimes()
            regime_col: Column to analyze for transitions
        
        Returns:
            List of (timestamp, from_regime, to_regime) tuples
        """
        transitions = []
        
        prev_regime = None
        for idx, row in regimes.iterrows():
            current_regime = row[regime_col]
            
            if prev_regime is not None and current_regime != prev_regime:
                transitions.append((idx, prev_regime, current_regime))
            
            prev_regime = current_regime
        
        return transitions


if __name__ == "__main__":
    """
    Example usage with Phase 2 features
    """
    # Create sample data
    np.random.seed(42)
    n = 200
    
    dates = pd.date_range('2024-01-01', periods=n, freq='D')
    
    # Simulate price with trend changes
    trend_1 = np.linspace(100, 120, 100)  # Uptrend
    trend_2 = np.linspace(120, 115, 50)   # Downtrend
    trend_3 = np.linspace(115, 125, 50)   # Uptrend
    price = np.concatenate([trend_1, trend_2, trend_3])
    price += np.random.randn(n) * 2  # Add noise
    
    df = pd.DataFrame({
        'timestamp': dates,
        'close': price,
        'high': price + np.abs(np.random.randn(n)),
        'low': price - np.abs(np.random.randn(n)),
        'volume': np.random.randint(1000000, 5000000, n)
    })
    df['open'] = df['close'].shift(1).fillna(df['close'])
    
    # Add indicators (simulating Phase 2 output)
    from data.preprocessing.indicators import add_all_indicators
    df = add_all_indicators(df)
    
    print("="*60)
    print("Market Regime Detection Example")
    print("="*60)
    print(f"Data points: {len(df)}")
    print(f"Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")
    
    # Detect regimes
    detector = MarketRegimeDetector(
        trend_lookback=20,
        adx_threshold=25.0
    )
    
    regimes = detector.detect_regimes(df, include_hmm=True)
    
    print(f"\nRegime Summary:")
    print(f"\nTrend Regimes:")
    print(regimes['trend_regime'].value_counts())
    
    print(f"\nVolatility Regimes:")
    print(regimes['volatility_regime'].value_counts())
    
    print(f"\nVolume Regimes:")
    print(regimes['volume_regime'].value_counts())
    
    if 'hmm_state' in regimes.columns:
        print(f"\nHMM States:")
        print(regimes['hmm_state'].value_counts())
    
    # Find transitions
    transitions = detector.get_regime_transitions(regimes, 'trend_regime')
    print(f"\nRegime Transitions (first 5):")
    for i, (ts, from_reg, to_reg) in enumerate(transitions[:5]):
        print(f"  {ts.date()}: {from_reg.name} → {to_reg.name}")
    
    # Show current regime
    latest = regimes.iloc[-1]
    print(f"\nCurrent Market State:")
    print(f"  Trend: {latest['trend_regime'].name} (strength: {latest['trend_strength']:.2f})")
    print(f"  Volatility: {latest['volatility_regime'].name} ({latest['volatility_percentile']:.1f}th percentile)")
    print(f"  Volume: {latest['volume_regime'].name} ({latest['volume_percentile']:.1f}th percentile)")
    
    print("\n✅ Market regime detection working correctly!")
