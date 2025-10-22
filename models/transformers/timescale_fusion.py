"""
TimeScaleFusion Extension for TFT

Extends the Temporal Fusion Transformer to handle multi-scale temporal data
using the volatility gating mechanism from Phase 2.

Based on:
    - arXiv:2302.11939 "Multi-Scale Temporal Fusion Transformers"
    - Phase 2 MultiResolutionPipeline implementation

Key Features:
    - Multi-scale feature processing
    - Volatility-based scale weighting
    - Cross-scale attention
    - Scale-aware positional encoding
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, List, Optional, Tuple
import math

from .temporal_fusion_transformer import (
    GatedResidualNetwork,
    InterpretableMultiHeadAttention
)


class ScaleAwarePositionalEncoding(nn.Module):
    """
    Positional encoding that varies based on timescale.
    
    Different scales (1min vs daily) need different position representations
    since the same position index represents different absolute times.
    
    Args:
        d_model: Dimension of embeddings
        max_len: Maximum sequence length
        num_scales: Number of timescales
    """
    
    def __init__(self, d_model: int, max_len: int = 5000, num_scales: int = 5):
        super().__init__()
        
        self.d_model = d_model
        self.num_scales = num_scales
        
        # Create separate positional encodings for each scale
        # Each scale has different temporal granularity
        self.scale_encodings = nn.ParameterList([
            self._create_positional_encoding(d_model, max_len, scale_idx)
            for scale_idx in range(num_scales)
        ])
    
    def _create_positional_encoding(
        self, d_model: int, max_len: int, scale_idx: int
    ) -> nn.Parameter:
        """
        Create sinusoidal positional encoding for a specific scale.
        
        Higher scales (longer timescales) use lower frequencies.
        """
        position = torch.arange(max_len).unsqueeze(1)
        
        # Scale-dependent frequency adjustment
        scale_factor = 2.0 ** scale_idx  # Each scale doubles the period
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * (-math.log(10000.0 * scale_factor) / d_model)
        )
        
        pe = torch.zeros(max_len, d_model)
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        
        return nn.Parameter(pe.unsqueeze(0), requires_grad=False)  # [1, max_len, d_model]
    
    def forward(self, x: torch.Tensor, scale_idx: int) -> torch.Tensor:
        """
        Add scale-aware positional encoding.
        
        Args:
            x: Input tensor [batch, seq_len, d_model]
            scale_idx: Index of the timescale (0=fastest, higher=slower)
        
        Returns:
            x with positional encoding added
        """
        seq_len = x.shape[1]
        pe = self.scale_encodings[scale_idx][:, :seq_len, :]
        return x + pe


class CrossScaleAttention(nn.Module):
    """
    Cross-scale attention mechanism.
    
    Allows model to attend from one timescale to another, capturing
    relationships like "1min volatility spike affects hourly trend".
    
    Architecture:
        - Query from target scale
        - Key/Value from source scale
        - Temporal alignment via interpolation
    
    Args:
        embed_size: Dimension of embeddings
        num_heads: Number of attention heads
        dropout: Dropout rate
    """
    
    def __init__(self, embed_size: int, num_heads: int = 4, dropout: float = 0.1):
        super().__init__()
        
        self.attention = InterpretableMultiHeadAttention(
            embed_size=embed_size,
            num_heads=num_heads,
            dropout=dropout
        )
        
        # Scale alignment: interpolate between different temporal resolutions
        self.alignment_grn = GatedResidualNetwork(
            input_size=embed_size,
            hidden_size=embed_size,
            output_size=embed_size,
            dropout=dropout
        )
    
    def forward(
        self,
        query_scale: torch.Tensor,
        key_value_scale: torch.Tensor,
        scale_ratio: float = 1.0
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Cross-scale attention.
        
        Args:
            query_scale: Query from target scale [batch, time_q, embed]
            key_value_scale: Key/Value from source scale [batch, time_kv, embed]
            scale_ratio: Ratio of timescales (e.g., 15min/1min = 15)
        
        Returns:
            output: Attended features [batch, time_q, embed]
            attention_weights: Cross-scale attention [batch, time_q, time_kv]
        """
        # If scales have different lengths, interpolate key/value to match query
        if query_scale.shape[1] != key_value_scale.shape[1]:
            # Simple linear interpolation (can use more sophisticated methods)
            kv_aligned = F.interpolate(
                key_value_scale.transpose(1, 2),  # [batch, embed, time]
                size=query_scale.shape[1],
                mode='linear',
                align_corners=False
            ).transpose(1, 2)  # [batch, time_q, embed]
        else:
            kv_aligned = key_value_scale
        
        # Apply attention
        attended, weights = self.attention(
            query=query_scale,
            key=kv_aligned,
            value=kv_aligned
        )
        
        # Post-process with GRN
        output = self.alignment_grn(attended)
        
        return output, weights


class TimeScaleFusion(nn.Module):
    """
    TimeScaleFusion: Multi-scale Temporal Fusion Transformer
    
    Extends TFT to handle multiple timescales simultaneously using:
    1. Scale-specific processing per timescale
    2. Volatility-based scale weighting (from Phase 2)
    3. Cross-scale attention for inter-scale relationships
    4. Hierarchical feature fusion
    
    Architecture:
        Input (multi-scale) → Per-scale TFT encoders → Cross-scale attention →
        Volatility gating → Fusion → Multi-horizon prediction
    
    Args:
        temporal_input_size: Dimension of temporal features per scale
        hidden_size: Hidden dimension
        num_scales: Number of timescales (e.g., 5: 1min, 15min, 1hour, 12hour, daily)
        num_heads: Number of attention heads
        num_quantiles: Number of quantiles to predict
        dropout: Dropout rate
        use_volatility_gates: Use volatility gating from Phase 2
    """
    
    def __init__(
        self,
        temporal_input_size: int,
        hidden_size: int = 256,
        num_scales: int = 5,
        num_heads: int = 4,
        num_quantiles: int = 3,
        dropout: float = 0.1,
        use_volatility_gates: bool = True
    ):
        super().__init__()
        
        self.temporal_input_size = temporal_input_size
        self.hidden_size = hidden_size
        self.num_scales = num_scales
        self.num_heads = num_heads
        self.num_quantiles = num_quantiles
        self.use_volatility_gates = use_volatility_gates
        
        # 1. Scale-aware positional encoding
        self.positional_encoding = ScaleAwarePositionalEncoding(
            d_model=hidden_size,
            num_scales=num_scales
        )
        
        # 2. Per-scale feature encoding
        self.scale_encoders = nn.ModuleList([
            nn.Sequential(
                nn.Linear(temporal_input_size, hidden_size),
                nn.LayerNorm(hidden_size),
                nn.Dropout(dropout)
            )
            for _ in range(num_scales)
        ])
        
        # 3. Per-scale LSTM processing
        self.scale_lstms = nn.ModuleList([
            nn.LSTM(
                input_size=hidden_size,
                hidden_size=hidden_size,
                num_layers=2,
                dropout=dropout,
                batch_first=True
            )
            for _ in range(num_scales)
        ])
        
        # 4. Cross-scale attention (between adjacent scales)
        self.cross_scale_attentions = nn.ModuleList([
            CrossScaleAttention(
                embed_size=hidden_size,
                num_heads=num_heads,
                dropout=dropout
            )
            for _ in range(num_scales - 1)  # n-1 connections
        ])
        
        # 5. Volatility gate processing (if enabled)
        if use_volatility_gates:
            self.gate_projection = nn.Linear(num_scales, num_scales)
        
        # 6. Multi-scale fusion
        self.fusion_grn = GatedResidualNetwork(
            input_size=hidden_size * num_scales,
            hidden_size=hidden_size * 2,
            output_size=hidden_size,
            dropout=dropout
        )
        
        # 7. Self-attention over time
        self.temporal_attention = InterpretableMultiHeadAttention(
            embed_size=hidden_size,
            num_heads=num_heads,
            dropout=dropout
        )
        
        # 8. Quantile output heads
        self.quantile_heads = nn.ModuleList([
            nn.Linear(hidden_size, 1)
            for _ in range(num_quantiles)
        ])
    
    def forward(
        self,
        scale_inputs: Dict[str, torch.Tensor],
        volatility_gates: Optional[torch.Tensor] = None
    ) -> Dict[str, torch.Tensor]:
        """
        Forward pass through TimeScaleFusion.
        
        Args:
            scale_inputs: Dict mapping scale name to tensor [batch, time, features]
                         Keys: '1min', '15min', '1hour', '12hour', 'daily'
            volatility_gates: Optional gate weights [batch, time, num_scales]
                             From Phase 2 MultiResolutionPipeline
        
        Returns:
            Dictionary containing:
                - predictions: Quantile predictions [batch, 1, num_quantiles]
                - scale_attention: Per-scale attention weights
                - cross_scale_attention: Cross-scale attention weights
                - scale_contributions: Contribution of each scale
        """
        batch_size = list(scale_inputs.values())[0].shape[0]
        
        # 1. Encode each scale with positional encoding
        scale_encoded = []
        scale_lstm_outs = []
        
        for scale_idx, (scale_name, scale_data) in enumerate(scale_inputs.items()):
            # Encode features
            encoded = self.scale_encoders[scale_idx](scale_data)
            
            # Add scale-aware positional encoding
            encoded = self.positional_encoding(encoded, scale_idx)
            scale_encoded.append(encoded)
            
            # Process with LSTM
            lstm_out, _ = self.scale_lstms[scale_idx](encoded)
            scale_lstm_outs.append(lstm_out)
        
        # 2. Cross-scale attention (between adjacent scales)
        cross_scale_features = []
        cross_scale_weights = []
        
        for i in range(len(scale_lstm_outs) - 1):
            # Attend from coarser scale to finer scale
            attended, weights = self.cross_scale_attentions[i](
                query_scale=scale_lstm_outs[i + 1],  # Coarser (e.g., 15min)
                key_value_scale=scale_lstm_outs[i],  # Finer (e.g., 1min)
                scale_ratio=2.0 ** (i + 1)  # Approximate ratio
            )
            cross_scale_features.append(attended)
            cross_scale_weights.append(weights)
        
        # 3. Combine scale features with cross-scale information
        combined_scales = []
        for i, lstm_out in enumerate(scale_lstm_outs):
            # Use last timestep from each scale
            last_step = lstm_out[:, -1:, :]  # [batch, 1, hidden]
            
            # Add cross-scale information if available
            if i < len(cross_scale_features):
                last_step = last_step + cross_scale_features[i][:, -1:, :]
            
            combined_scales.append(last_step)
        
        # 4. Apply volatility gates (if provided)
        if volatility_gates is not None and self.use_volatility_gates:
            # Use last timestep gates
            gates = volatility_gates[:, -1, :]  # [batch, num_scales]
            
            # Optional projection for learned gating
            gates = torch.sigmoid(self.gate_projection(gates))
            
            # Apply gates to each scale
            gates_expanded = gates.unsqueeze(-1)  # [batch, num_scales, 1]
            for i in range(len(combined_scales)):
                combined_scales[i] = combined_scales[i] * gates_expanded[:, i:i+1, :]
        
        # 5. Concatenate all scales for fusion
        all_scales = torch.cat(combined_scales, dim=-1)  # [batch, 1, hidden*num_scales]
        
        # 6. Fuse multi-scale features
        fused = self.fusion_grn(all_scales)  # [batch, 1, hidden]
        
        # 7. Temporal self-attention (optional refinement)
        attended, attention_weights = self.temporal_attention(
            query=fused,
            key=fused,
            value=fused
        )
        
        # 8. Quantile predictions
        quantile_outputs = []
        for head in self.quantile_heads:
            q_pred = head(attended).squeeze(-1)  # [batch, 1]
            quantile_outputs.append(q_pred)
        
        quantile_predictions = torch.stack(quantile_outputs, dim=-1)  # [batch, 1, num_quantiles]
        
        return {
            'predictions': quantile_predictions,
            'scale_attention': attention_weights,
            'cross_scale_attention': cross_scale_weights,
            'scale_contributions': [s.mean().item() for s in combined_scales]
        }


if __name__ == "__main__":
    """
    Example usage with multi-scale data from Phase 2
    """
    # Configuration matching Phase 2 output
    batch_size = 16
    num_scales = 4  # 1min, 15min, 1hour, daily
    temporal_features = 32  # Features per scale from Phase 2
    hidden_size = 128
    num_quantiles = 3
    
    # Simulate multi-scale data (different sequence lengths)
    scale_inputs = {
        '1min': torch.randn(batch_size, 60, temporal_features),    # 60 minutes
        '15min': torch.randn(batch_size, 24, temporal_features),   # 6 hours (24 * 15min)
        '1hour': torch.randn(batch_size, 24, temporal_features),   # 1 day
        'daily': torch.randn(batch_size, 7, temporal_features)     # 1 week
    }
    
    # Simulate volatility gates from Phase 2
    volatility_gates = torch.randn(batch_size, 60, num_scales).softmax(dim=-1)
    
    # Initialize model
    model = TimeScaleFusion(
        temporal_input_size=temporal_features,
        hidden_size=hidden_size,
        num_scales=num_scales,
        num_heads=4,
        num_quantiles=num_quantiles,
        use_volatility_gates=True
    )
    
    print("="*60)
    print("TimeScaleFusion Model Example")
    print("="*60)
    print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")
    print(f"Scales: {list(scale_inputs.keys())}")
    print(f"Input shapes:")
    for scale, data in scale_inputs.items():
        print(f"  {scale}: {data.shape}")
    
    # Forward pass
    output = model(
        scale_inputs=scale_inputs,
        volatility_gates=volatility_gates
    )
    
    predictions = output['predictions']
    scale_attention = output['scale_attention']
    cross_scale_attention = output['cross_scale_attention']
    scale_contributions = output['scale_contributions']
    
    print(f"\nOutput shapes:")
    print(f"  Predictions: {predictions.shape} (batch, horizon, quantiles)")
    print(f"  Scale attention: {scale_attention.shape}")
    print(f"  Cross-scale attention: {len(cross_scale_attention)} connections")
    
    print(f"\nScale contributions (mean activation):")
    for i, contrib in enumerate(scale_contributions):
        scale_name = list(scale_inputs.keys())[i]
        print(f"  {scale_name}: {contrib:.4f}")
    
    print("\n✅ TimeScaleFusion model working correctly!")
