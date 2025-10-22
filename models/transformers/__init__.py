"""
ChronoX Transformer Models

This package contains the neural network architectures for time series forecasting.

Models:
    - Stockformer: Stock price forecasting with dual-head (price + signal)
    - TemporalFusionTransformer: Base TFT architecture (arXiv:1912.09363)
    - TimeScaleFusion: Multi-scale extension with volatility gating
    - GatedResidualNetwork: Flexible non-linear processing block
    - VariableSelectionNetwork: Interpretable feature selection
    - InterpretableMultiHeadAttention: Attention with interpretability

Usage:
    from models.transformers import Stockformer, TemporalFusionTransformer
    
    # Stockformer for price forecasting
    model = Stockformer(
        input_dim=5,  # OHLCV
        d_model=512,
        n_heads=8,
        n_layers=6
    )
    
    # Single-scale TFT model
    tft = TemporalFusionTransformer(
        static_input_size=8,
        temporal_input_size=32,
        hidden_size=256
    )
    
    # Multi-scale model
    tsf = TimeScaleFusion(
        temporal_input_size=32,
        hidden_size=256,
        num_scales=5
    )
"""

from .stockformer import (
    Stockformer,
    StockformerLoss,
    PositionalEncoding,
    MultiHeadAttention,
    TransformerEncoderLayer
)

from .finbert_sentiment import (
    FinBERTSentiment,
    SentimentAggregator
)

from .event_impact_analyzer import (
    EventImpactAnalyzer,
    EventType,
    ImpactTimeframe
)

from .temporal_fusion_transformer import (
    TemporalFusionTransformer,
    GatedResidualNetwork,
    VariableSelectionNetwork,
    InterpretableMultiHeadAttention,
    quantile_loss
)

from .timescale_fusion import (
    TimeScaleFusion,
    ScaleAwarePositionalEncoding,
    CrossScaleAttention
)

__all__ = [
    # Stockformer components
    'Stockformer',
    'StockformerLoss',
    'PositionalEncoding',
    'MultiHeadAttention',
    'TransformerEncoderLayer',
    
    # FinBERT Sentiment
    'FinBERTSentiment',
    'SentimentAggregator',
    
    # Event Impact Analysis
    'EventImpactAnalyzer',
    'EventType',
    'ImpactTimeframe',
    
    # TFT components
    'TemporalFusionTransformer',
    'GatedResidualNetwork',
    'VariableSelectionNetwork',
    'InterpretableMultiHeadAttention',
    'quantile_loss',
    
    # TimeScaleFusion components
    'TimeScaleFusion',
    'ScaleAwarePositionalEncoding',
    'CrossScaleAttention',
]
