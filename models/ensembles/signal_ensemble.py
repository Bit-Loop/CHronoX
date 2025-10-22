"""
Signal Ensemble Module

Combines signals from multiple sources with regime-based weighting.

Signal Sources:
- Supervised model (Stockformer/TFT)
- RL agent (PPO)
- Technical indicators
- Sentiment analysis (FinBERT)
- Event detection

Weighting Strategies:
- CALM regime: supervised 30%, RL 20%, technical 30%, sentiment 15%, events 5%
- VOLATILE regime: supervised 15%, RL 35%, technical 20%, sentiment 10%, events 20%
- TRENDING regime: supervised 35%, RL 15%, technical 35%, sentiment 10%, events 5%
- CRISIS regime: supervised 10%, RL 20%, technical 10%, sentiment 20%, events 40%

Usage:
    ensemble = SignalEnsemble(models, config)
    signal, confidence = ensemble.generate_signal(market_data)
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """Market regime classification."""
    CALM = 0
    VOLATILE = 1
    TRENDING = 2
    CRISIS = 3


class SignalType(Enum):
    """Trading signal types."""
    STRONG_BUY = 2
    BUY = 1
    NEUTRAL = 0
    SELL = -1
    STRONG_SELL = -2


@dataclass
class SignalSource:
    """Individual signal source."""
    name: str
    signal: SignalType
    confidence: float  # [0, 1]
    metadata: Dict[str, Any]


@dataclass
class EnsembleConfig:
    """Configuration for signal ensemble."""
    
    # Regime-based weights
    regime_weights: Optional[Dict[str, Dict[str, float]]] = None
    
    # Confidence thresholds
    min_confidence: float = 0.55  # Min 55% to trade
    high_confidence: float = 0.75  # 75% for strong signals
    
    # Consensus requirements
    min_agreement_pct: float = 0.60  # Need 60% agreement
    require_timeframe_alignment: bool = True
    min_timeframe_agreement: float = 0.75  # 75% timeframe agreement
    
    # Conflict resolution
    prefer_longer_timeframe: bool = True
    default_on_conflict: SignalType = SignalType.NEUTRAL
    
    def __post_init__(self):
        if self.regime_weights is None:
            self.regime_weights = {
                'CALM': {
                    'supervised': 0.30,
                    'rl_agent': 0.20,
                    'technical': 0.30,
                    'sentiment': 0.15,
                    'events': 0.05
                },
                'VOLATILE': {
                    'supervised': 0.15,
                    'rl_agent': 0.35,
                    'technical': 0.20,
                    'sentiment': 0.10,
                    'events': 0.20
                },
                'TRENDING': {
                    'supervised': 0.35,
                    'rl_agent': 0.15,
                    'technical': 0.35,
                    'sentiment': 0.10,
                    'events': 0.05
                },
                'CRISIS': {
                    'supervised': 0.10,
                    'rl_agent': 0.20,
                    'technical': 0.10,
                    'sentiment': 0.20,
                    'events': 0.40
                }
            }


class SignalEnsemble:
    """
    Multi-model signal ensemble with regime-aware weighting.
    
    Aggregates signals from multiple sources and produces
    a unified trading signal with confidence score.
    
    Args:
        config: Ensemble configuration
    """
    
    def __init__(self, config: Optional[EnsembleConfig] = None):
        self.config = config or EnsembleConfig()
        
        # Track performance per source
        self.source_performance: Dict[str, Dict[str, float]] = {}
        self.signal_history: List[Dict] = []
        
        # Ensure regime_weights is not None
        if self.config.regime_weights is None:
            self.config.regime_weights = {}
        
        logger.info("SignalEnsemble initialized")
        logger.info(f"Regimes: {list(self.config.regime_weights.keys())}")
    
    def generate_signal(
        self,
        signals: List[SignalSource],
        regime: MarketRegime,
        timeframe_signals: Optional[Dict[str, SignalType]] = None
    ) -> Tuple[SignalType, float]:
        """
        Generate ensemble signal.
        
        Args:
            signals: List of signals from different sources
            regime: Current market regime
            timeframe_signals: Optional dict of signals per timeframe
        
        Returns:
            (final_signal, confidence)
        """
        # Get regime weights
        weights = self._get_regime_weights(regime)
        
        # Check timeframe alignment if required
        if self.config.require_timeframe_alignment and timeframe_signals:
            alignment_ok, aligned_signal = self._check_timeframe_alignment(timeframe_signals)
            if not alignment_ok:
                logger.debug("Timeframe misalignment detected")
                return SignalType.NEUTRAL, 0.0
        
        # Calculate weighted signal
        weighted_signal, confidence = self._calculate_weighted_signal(signals, weights)
        
        # Check confidence threshold
        if confidence < self.config.min_confidence:
            logger.debug(f"Confidence too low: {confidence:.2f}")
            return SignalType.NEUTRAL, confidence
        
        # Check consensus
        if not self._check_consensus(signals):
            logger.debug("Insufficient consensus")
            return self.config.default_on_conflict, confidence
        
        # Adjust signal strength based on confidence
        final_signal = self._adjust_signal_strength(weighted_signal, confidence)
        
        # Record signal
        self._record_signal(signals, regime, final_signal, confidence)
        
        logger.debug(f"Ensemble signal: {final_signal.name} (conf={confidence:.2f}, regime={regime.name})")
        
        return final_signal, confidence
    
    def _get_regime_weights(self, regime: MarketRegime) -> Dict[str, float]:
        """Get weights for current regime."""
        regime_name = regime.name
        
        # Ensure regime_weights exists
        if self.config.regime_weights is None:
            return {}
        
        if regime_name not in self.config.regime_weights:
            logger.warning(f"Unknown regime: {regime_name}, using CALM")
            regime_name = 'CALM'
        
        return self.config.regime_weights[regime_name]
    
    def _calculate_weighted_signal(
        self,
        signals: List[SignalSource],
        weights: Dict[str, float]
    ) -> Tuple[float, float]:
        """
        Calculate weighted average signal.
        
        Returns:
            (weighted_signal_value, ensemble_confidence)
        """
        weighted_sum = 0.0
        total_weight = 0.0
        confidence_sum = 0.0
        
        for signal in signals:
            # Get weight for this source
            weight = weights.get(signal.name, 0.0)
            
            # Signal value (-2 to +2)
            signal_value = signal.signal.value
            
            # Weight by confidence
            effective_weight = weight * signal.confidence
            
            weighted_sum += signal_value * effective_weight
            total_weight += effective_weight
            confidence_sum += signal.confidence * weight
        
        # Normalize
        if total_weight > 0:
            weighted_signal = weighted_sum / total_weight
            ensemble_confidence = confidence_sum / sum(weights.values())
        else:
            weighted_signal = 0.0
            ensemble_confidence = 0.0
        
        return weighted_signal, ensemble_confidence
    
    def _adjust_signal_strength(
        self,
        weighted_signal: float,
        confidence: float
    ) -> SignalType:
        """
        Convert weighted signal to discrete signal type.
        
        Args:
            weighted_signal: Continuous signal value
            confidence: Signal confidence
        
        Returns:
            SignalType enum
        """
        # Thresholds
        if confidence >= self.config.high_confidence:
            # Strong signals
            if weighted_signal >= 1.5:
                return SignalType.STRONG_BUY
            elif weighted_signal <= -1.5:
                return SignalType.STRONG_SELL
        
        # Normal signals
        if weighted_signal >= 0.5:
            return SignalType.BUY
        elif weighted_signal <= -0.5:
            return SignalType.SELL
        else:
            return SignalType.NEUTRAL
    
    def _check_consensus(self, signals: List[SignalSource]) -> bool:
        """
        Check if there's sufficient consensus among signals.
        
        Requires majority agreement in direction.
        """
        if not signals:
            return False
        
        # Count signals by direction
        bullish = sum(1 for s in signals if s.signal.value > 0)
        bearish = sum(1 for s in signals if s.signal.value < 0)
        neutral = sum(1 for s in signals if s.signal.value == 0)
        
        total = len(signals)
        
        # Check if majority agrees
        max_count = max(bullish, bearish, neutral)
        agreement_pct = max_count / total
        
        return agreement_pct >= self.config.min_agreement_pct
    
    def _check_timeframe_alignment(
        self,
        timeframe_signals: Dict[str, SignalType]
    ) -> Tuple[bool, Optional[SignalType]]:
        """
        Check alignment across timeframes.
        
        Returns:
            (alignment_ok, aligned_signal)
        """
        if not timeframe_signals:
            return True, None
        
        # Count signals by direction
        signals_list = list(timeframe_signals.values())
        bullish = sum(1 for s in signals_list if s.value > 0)
        bearish = sum(1 for s in signals_list if s.value < 0)
        
        total = len(signals_list)
        
        # Check agreement percentage
        max_direction = max(bullish, bearish)
        agreement = max_direction / total
        
        if agreement >= self.config.min_timeframe_agreement:
            # Determine aligned signal
            if bullish > bearish:
                aligned_signal = SignalType.BUY
            else:
                aligned_signal = SignalType.SELL
            
            return True, aligned_signal
        else:
            # Use longest timeframe on disagreement
            if self.config.prefer_longer_timeframe:
                # Assume timeframes are ordered from short to long
                longest_tf = list(timeframe_signals.keys())[-1]
                return True, timeframe_signals[longest_tf]
            else:
                return False, None
    
    def _record_signal(
        self,
        signals: List[SignalSource],
        regime: MarketRegime,
        final_signal: SignalType,
        confidence: float
    ) -> None:
        """Record signal for later analysis."""
        record = {
            'timestamp': None,  # Would be set by caller
            'regime': regime.name,
            'final_signal': final_signal.name,
            'confidence': confidence,
            'sources': {s.name: s.signal.name for s in signals}
        }
        
        self.signal_history.append(record)
    
    def update_source_performance(
        self,
        source_name: str,
        was_correct: bool
    ) -> None:
        """
        Update performance tracking for a signal source.
        
        Args:
            source_name: Name of the source
            was_correct: Whether the signal was correct
        """
        if source_name not in self.source_performance:
            self.source_performance[source_name] = {
                'total': 0,
                'correct': 0,
                'accuracy': 0.0
            }
        
        perf = self.source_performance[source_name]
        perf['total'] += 1
        if was_correct:
            perf['correct'] += 1
        perf['accuracy'] = perf['correct'] / perf['total']
        
        logger.debug(f"Updated {source_name}: accuracy={perf['accuracy']:.2%}")
    
    def get_source_statistics(self) -> Dict[str, Dict]:
        """Get performance statistics for all sources."""
        return self.source_performance.copy()
    
    def adapt_weights(
        self,
        regime: MarketRegime,
        learning_rate: float = 0.1
    ) -> None:
        """
        Adapt weights based on historical performance.
        
        Uses simple performance-based reweighting.
        
        Args:
            regime: Regime to adapt weights for
            learning_rate: Adaptation speed
        """
        regime_name = regime.name
        
        # Ensure regime_weights exists
        if self.config.regime_weights is None:
            return
        
        if regime_name not in self.config.regime_weights:
            return
        
        current_weights = self.config.regime_weights[regime_name]
        
        # Calculate performance-based adjustments
        total_accuracy = sum(
            perf['accuracy'] for perf in self.source_performance.values()
        )
        
        if total_accuracy == 0:
            return
        
        # Adjust weights proportional to accuracy
        for source_name, perf in self.source_performance.items():
            if source_name in current_weights:
                # Calculate target weight (proportional to accuracy)
                target_weight = perf['accuracy'] / total_accuracy
                
                # Gradual adjustment
                current = current_weights[source_name]
                new_weight = current + learning_rate * (target_weight - current)
                current_weights[source_name] = new_weight
        
        # Normalize weights to sum to 1.0
        total = sum(current_weights.values())
        if total > 0:
            for name in current_weights:
                current_weights[name] /= total
        
        logger.info(f"Adapted weights for {regime_name}: {current_weights}")


if __name__ == '__main__':
    # Test signal ensemble
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Signal Ensemble")
    print("="*60)
    
    # Initialize ensemble
    print("\n✓ Initializing SignalEnsemble...")
    config = EnsembleConfig()
    ensemble = SignalEnsemble(config)
    
    print(f"  Min confidence: {config.min_confidence:.0%}")
    print(f"  High confidence: {config.high_confidence:.0%}")
    print(f"  Min agreement: {config.min_agreement_pct:.0%}")
    
    # Test with different regimes
    print("\n✓ Testing signal generation across regimes...")
    
    test_signals = [
        SignalSource('supervised', SignalType.BUY, 0.80, {}),
        SignalSource('rl_agent', SignalType.BUY, 0.70, {}),
        SignalSource('technical', SignalType.NEUTRAL, 0.60, {}),
        SignalSource('sentiment', SignalType.BUY, 0.65, {}),
        SignalSource('events', SignalType.NEUTRAL, 0.50, {})
    ]
    
    for regime in [MarketRegime.CALM, MarketRegime.VOLATILE, MarketRegime.TRENDING, MarketRegime.CRISIS]:
        signal, confidence = ensemble.generate_signal(test_signals, regime)
        
        weights = ensemble._get_regime_weights(regime)
        print(f"\n  {regime.name}:")
        print(f"    Weights: {weights}")
        print(f"    Signal: {signal.name}")
        print(f"    Confidence: {confidence:.2f}")
    
    # Test consensus checking
    print("\n✓ Testing consensus requirements...")
    
    scenarios = [
        {
            'name': 'Strong consensus',
            'signals': [
                SignalSource('s1', SignalType.BUY, 0.80, {}),
                SignalSource('s2', SignalType.BUY, 0.75, {}),
                SignalSource('s3', SignalType.BUY, 0.70, {}),
                SignalSource('s4', SignalType.NEUTRAL, 0.60, {})
            ]
        },
        {
            'name': 'Weak consensus',
            'signals': [
                SignalSource('s1', SignalType.BUY, 0.80, {}),
                SignalSource('s2', SignalType.SELL, 0.75, {}),
                SignalSource('s3', SignalType.NEUTRAL, 0.70, {}),
                SignalSource('s4', SignalType.BUY, 0.60, {})
            ]
        }
    ]
    
    for scenario in scenarios:
        has_consensus = ensemble._check_consensus(scenario['signals'])
        signal, conf = ensemble.generate_signal(scenario['signals'], MarketRegime.CALM)
        
        print(f"\n  {scenario['name']}:")
        print(f"    Consensus: {'✓ Yes' if has_consensus else '✗ No'}")
        print(f"    Final signal: {signal.name} (conf={conf:.2f})")
    
    # Test timeframe alignment
    print("\n✓ Testing timeframe alignment...")
    
    alignment_tests = [
        {
            'name': 'Aligned',
            'timeframes': {
                '5min': SignalType.BUY,
                '15min': SignalType.BUY,
                '1hr': SignalType.BUY,
                '12hr': SignalType.BUY
            }
        },
        {
            'name': 'Misaligned',
            'timeframes': {
                '5min': SignalType.BUY,
                '15min': SignalType.SELL,
                '1hr': SignalType.BUY,
                '12hr': SignalType.SELL
            }
        }
    ]
    
    for test in alignment_tests:
        aligned, aligned_signal = ensemble._check_timeframe_alignment(test['timeframes'])
        
        print(f"\n  {test['name']}:")
        print(f"    Aligned: {'✓ Yes' if aligned else '✗ No'}")
        if aligned_signal:
            print(f"    Aligned signal: {aligned_signal.name}")
    
    # Test performance tracking
    print("\n✓ Testing performance tracking...")
    
    ensemble.update_source_performance('supervised', True)
    ensemble.update_source_performance('supervised', True)
    ensemble.update_source_performance('supervised', False)
    ensemble.update_source_performance('rl_agent', True)
    ensemble.update_source_performance('technical', False)
    
    stats = ensemble.get_source_statistics()
    print("\n  Source performance:")
    for source, perf in stats.items():
        print(f"    {source}: {perf['accuracy']:.0%} ({perf['correct']}/{perf['total']})")
    
    # Test weight adaptation
    print("\n✓ Testing weight adaptation...")
    
    if ensemble.config.regime_weights is not None:
        old_weights = ensemble.config.regime_weights['CALM'].copy()
        ensemble.adapt_weights(MarketRegime.CALM, learning_rate=0.2)
        new_weights = ensemble.config.regime_weights['CALM']
        
        print("\n  Weight changes:")
        for source in old_weights:
            old = old_weights[source]
            new = new_weights[source]
            change = new - old
            print(f"    {source}: {old:.2f} → {new:.2f} ({change:+.2f})")
    else:
        print("  Skipped: No regime weights configured")
    
    print("\n" + "="*60)
    print("Signal Ensemble Test Complete!")
    print("="*60)
