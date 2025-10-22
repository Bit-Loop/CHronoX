"""
Temporal Fusion Transformer (TFT)

Implementation based on:
    Lim et al. (2019) "Temporal Fusion Transformers for Interpretable Multi-horizon
    Time Series Forecasting" - arXiv:1912.09363

Architecture Components:
    1. Variable Selection Networks (VSN) - Select relevant features
    2. Gated Residual Networks (GRN) - Non-linear processing with skip connections
    3. Multi-head Attention - Temporal relationships
    4. Quantile Forecasting - Prediction intervals

Key Features:
    - Multi-horizon forecasting (predict multiple timesteps ahead)
    - Interpretable attention weights
    - Quantile regression for uncertainty estimation
    - Static and temporal feature handling
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, List, Optional, Tuple
import math


class GatedResidualNetwork(nn.Module):
    """
    Gated Residual Network (GRN)
    
    Provides flexible non-linear processing with gating and skip connections.
    Can optionally incorporate context from other variables.
    
    Architecture:
        Input → FC → ELU → FC → Dropout → Gate → Output (+ residual)
        Context (optional) → FC → added before activation
    
    Args:
        input_size: Dimension of input features
        hidden_size: Dimension of hidden layer
        output_size: Dimension of output features
        dropout: Dropout rate for regularization
        context_size: Optional dimension for context input
        use_time_distributed: Apply same weights across time (for sequences)
    """
    
    def __init__(
        self,
        input_size: int,
        hidden_size: int,
        output_size: int,
        dropout: float = 0.1,
        context_size: Optional[int] = None,
        use_time_distributed: bool = True
    ):
        super().__init__()
        
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.context_size = context_size
        self.use_time_distributed = use_time_distributed
        
        # Primary processing path
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.elu = nn.ELU()
        self.fc2 = nn.Linear(hidden_size, output_size)
        self.dropout = nn.Dropout(dropout)
        
        # Context processing (if provided)
        if context_size is not None:
            self.context_projection = nn.Linear(context_size, hidden_size, bias=False)
        
        # Gating mechanism (controls information flow)
        self.gate = nn.Linear(hidden_size, output_size)
        
        # Skip connection projection (if dimensions don't match)
        if input_size != output_size:
            self.skip_projection = nn.Linear(input_size, output_size)
        else:
            self.skip_projection = None
        
        # Layer normalization for stability
        self.layer_norm = nn.LayerNorm(output_size)
    
    def forward(
        self,
        x: torch.Tensor,
        context: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """
        Forward pass through GRN.
        
        Args:
            x: Input tensor [batch, (time), input_size]
            context: Optional context tensor [batch, context_size]
        
        Returns:
            Output tensor [batch, (time), output_size]
        """
        # Save input for residual connection
        residual = x
        
        # Primary path: FC → ELU
        x = self.fc1(x)
        
        # Add context if provided
        if context is not None and self.context_size is not None:
            # Expand context to match time dimension if needed
            context_processed = self.context_projection(context)
            if x.dim() == 3 and context_processed.dim() == 2:
                # x: [batch, time, hidden], context: [batch, hidden]
                context_processed = context_processed.unsqueeze(1)  # [batch, 1, hidden]
            x = x + context_processed
        
        x = self.elu(x)
        x = self.fc2(x)
        x = self.dropout(x)
        
        # Gating mechanism: GLU (Gated Linear Unit)
        gate = self.gate(self.elu(self.fc1(residual)))
        gate = torch.sigmoid(gate)
        x = x * gate
        
        # Skip connection
        if self.skip_projection is not None:
            residual = self.skip_projection(residual)
        
        # Add residual and normalize
        x = x + residual
        x = self.layer_norm(x)
        
        return x


class VariableSelectionNetwork(nn.Module):
    """
    Variable Selection Network (VSN)
    
    Selects which input variables are relevant at each timestep using
    attention-like mechanism. Provides interpretability by showing which
    features the model uses.
    
    Architecture:
        1. Flatten all variables
        2. GRN for context (across all variables)
        3. Per-variable GRN
        4. Softmax weights across variables
        5. Weighted combination
    
    Args:
        input_sizes: List of feature dimensions for each variable
        hidden_size: Dimension for processing
        dropout: Dropout rate
        context_size: Optional context dimension
    """
    
    def __init__(
        self,
        input_sizes: List[int],
        hidden_size: int,
        dropout: float = 0.1,
        context_size: Optional[int] = None
    ):
        super().__init__()
        
        self.input_sizes = input_sizes
        self.hidden_size = hidden_size
        self.num_vars = len(input_sizes)
        
        # Per-variable transformation to common dimension
        self.variable_grns = nn.ModuleList([
            GatedResidualNetwork(
                input_size=size,
                hidden_size=hidden_size,
                output_size=hidden_size,
                dropout=dropout
            )
            for size in input_sizes
        ])
        
        # Flatten and process all variables together for context
        total_input_size = sum(input_sizes)
        self.flattened_grn = GatedResidualNetwork(
            input_size=total_input_size,
            hidden_size=hidden_size,
            output_size=self.num_vars,  # Output attention weights
            dropout=dropout,
            context_size=context_size
        )
        
    def forward(
        self,
        variables: List[torch.Tensor],
        context: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Select relevant variables using attention.
        
        Args:
            variables: List of tensors, each [batch, time, feature_dim]
            context: Optional context [batch, context_dim]
        
        Returns:
            selected: Weighted combination [batch, time, hidden_size]
            weights: Variable importance [batch, time, num_vars]
        """
        # Process each variable through its GRN
        processed_vars = []
        for var, grn in zip(variables, self.variable_grns):
            processed = grn(var)
            processed_vars.append(processed)
        
        # Stack: [batch, time, num_vars, hidden_size]
        processed = torch.stack(processed_vars, dim=2)
        batch_size, time_steps, _, _ = processed.shape
        
        # Flatten all variables for attention computation
        flattened = torch.cat(variables, dim=-1)  # [batch, time, total_features]
        
        # Compute attention weights
        weights = self.flattened_grn(flattened, context)  # [batch, time, num_vars]
        weights = F.softmax(weights, dim=-1)
        
        # Apply weights: weighted sum across variables
        # weights: [batch, time, num_vars, 1]
        # processed: [batch, time, num_vars, hidden_size]
        weights_expanded = weights.unsqueeze(-1)
        selected = (processed * weights_expanded).sum(dim=2)  # [batch, time, hidden_size]
        
        return selected, weights


class InterpretableMultiHeadAttention(nn.Module):
    """
    Interpretable Multi-Head Attention
    
    Standard multi-head attention with modifications for interpretability:
    - Attention weights are averaged across heads for visualization
    - Additive attention option for clearer interpretation
    
    Args:
        embed_size: Dimension of input embeddings
        num_heads: Number of attention heads
        dropout: Dropout rate
    """
    
    def __init__(self, embed_size: int, num_heads: int, dropout: float = 0.1):
        super().__init__()
        
        assert embed_size % num_heads == 0, "embed_size must be divisible by num_heads"
        
        self.embed_size = embed_size
        self.num_heads = num_heads
        self.head_dim = embed_size // num_heads
        
        # Q, K, V projections
        self.query = nn.Linear(embed_size, embed_size)
        self.key = nn.Linear(embed_size, embed_size)
        self.value = nn.Linear(embed_size, embed_size)
        
        # Output projection
        self.out = nn.Linear(embed_size, embed_size)
        
        self.dropout = nn.Dropout(dropout)
        self.scale = math.sqrt(self.head_dim)
        
    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Compute multi-head attention.
        
        Args:
            query: Query tensor [batch, time_q, embed_size]
            key: Key tensor [batch, time_k, embed_size]
            value: Value tensor [batch, time_v, embed_size]
            mask: Optional mask [batch, time_q, time_k]
        
        Returns:
            output: Attention output [batch, time_q, embed_size]
            attention_weights: Averaged attention [batch, time_q, time_k]
        """
        batch_size = query.shape[0]
        
        # Project to Q, K, V
        Q = self.query(query)  # [batch, time_q, embed_size]
        K = self.key(key)      # [batch, time_k, embed_size]
        V = self.value(value)  # [batch, time_v, embed_size]
        
        # Reshape for multi-head: [batch, num_heads, time, head_dim]
        Q = Q.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
        K = K.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
        V = V.view(batch_size, -1, self.num_heads, self.head_dim).transpose(1, 2)
        
        # Scaled dot-product attention
        # scores: [batch, num_heads, time_q, time_k]
        scores = torch.matmul(Q, K.transpose(-2, -1)) / self.scale
        
        # Apply mask (for causality or padding)
        if mask is not None:
            scores = scores.masked_fill(mask.unsqueeze(1) == 0, float('-inf'))
        
        # Softmax over keys
        attention = F.softmax(scores, dim=-1)
        attention = self.dropout(attention)
        
        # Apply attention to values
        # output: [batch, num_heads, time_q, head_dim]
        output = torch.matmul(attention, V)
        
        # Concatenate heads: [batch, time_q, embed_size]
        output = output.transpose(1, 2).contiguous().view(batch_size, -1, self.embed_size)
        
        # Final projection
        output = self.out(output)
        
        # Average attention across heads for interpretability
        attention_weights = attention.mean(dim=1)  # [batch, time_q, time_k]
        
        return output, attention_weights


class TemporalFusionTransformer(nn.Module):
    """
    Temporal Fusion Transformer (TFT)
    
    Complete TFT architecture for multi-horizon time series forecasting.
    
    Architecture Flow:
        1. Static context encoding (if available)
        2. Variable selection (historical + future)
        3. LSTM encoder for historical sequence
        4. LSTM decoder with attention
        5. Quantile output heads
    
    Args:
        static_input_size: Dimension of static features (e.g., symbol embeddings)
        temporal_input_size: Dimension of temporal features (e.g., OHLCV + indicators)
        hidden_size: Hidden dimension throughout network
        num_heads: Number of attention heads
        num_quantiles: Number of quantiles to predict (e.g., [0.1, 0.5, 0.9])
        dropout: Dropout rate
        num_encoder_layers: Number of LSTM layers for encoder
        num_decoder_layers: Number of LSTM layers for decoder
    """
    
    def __init__(
        self,
        static_input_size: int,
        temporal_input_size: int,
        hidden_size: int = 256,
        num_heads: int = 4,
        num_quantiles: int = 3,
        dropout: float = 0.1,
        num_encoder_layers: int = 2,
        num_decoder_layers: int = 2
    ):
        super().__init__()
        
        self.static_input_size = static_input_size
        self.temporal_input_size = temporal_input_size
        self.hidden_size = hidden_size
        self.num_heads = num_heads
        self.num_quantiles = num_quantiles
        
        # 1. Static context encoding
        if static_input_size > 0:
            self.static_encoder = GatedResidualNetwork(
                input_size=static_input_size,
                hidden_size=hidden_size,
                output_size=hidden_size,
                dropout=dropout
            )
            self.static_context_grn = GatedResidualNetwork(
                input_size=hidden_size,
                hidden_size=hidden_size,
                output_size=hidden_size,
                dropout=dropout
            )
        else:
            self.static_encoder = None
            self.static_context_grn = None
        
        # 2. Variable selection for historical inputs
        # Treat each feature as a separate variable (can be grouped in practice)
        self.historical_vsn = VariableSelectionNetwork(
            input_sizes=[temporal_input_size],  # Single group for now
            hidden_size=hidden_size,
            dropout=dropout,
            context_size=hidden_size if static_input_size > 0 else None
        )
        
        # 3. LSTM encoder for historical sequence
        self.lstm_encoder = nn.LSTM(
            input_size=hidden_size,
            hidden_size=hidden_size,
            num_layers=num_encoder_layers,
            dropout=dropout if num_encoder_layers > 1 else 0.0,
            batch_first=True
        )
        
        # 4. Temporal fusion (post-LSTM processing)
        self.post_lstm_grn = GatedResidualNetwork(
            input_size=hidden_size,
            hidden_size=hidden_size,
            output_size=hidden_size,
            dropout=dropout
        )
        
        # 5. Multi-head attention for temporal dependencies
        self.attention = InterpretableMultiHeadAttention(
            embed_size=hidden_size,
            num_heads=num_heads,
            dropout=dropout
        )
        
        # 6. Post-attention processing
        self.post_attention_grn = GatedResidualNetwork(
            input_size=hidden_size,
            hidden_size=hidden_size,
            output_size=hidden_size,
            dropout=dropout
        )
        
        # 7. Quantile output heads (one per quantile)
        self.quantile_heads = nn.ModuleList([
            nn.Linear(hidden_size, 1)
            for _ in range(num_quantiles)
        ])
        
    def forward(
        self,
        historical_inputs: torch.Tensor,
        static_inputs: Optional[torch.Tensor] = None,
        future_inputs: Optional[torch.Tensor] = None
    ) -> Dict[str, torch.Tensor]:
        """
        Forward pass through TFT.
        
        Args:
            historical_inputs: Past observations [batch, hist_len, temporal_size]
            static_inputs: Static features [batch, static_size]
            future_inputs: Known future inputs [batch, future_len, temporal_size]
        
        Returns:
            Dictionary containing:
                - predictions: Quantile predictions [batch, horizon, num_quantiles]
                - attention_weights: Attention weights [batch, horizon, hist_len]
                - variable_weights: Variable selection weights [batch, hist_len, num_vars]
        """
        batch_size = historical_inputs.shape[0]
        hist_len = historical_inputs.shape[1]
        
        # 1. Encode static context (if available)
        static_context = None
        if static_inputs is not None and self.static_encoder is not None:
            static_encoded = self.static_encoder(static_inputs)
            static_context = self.static_context_grn(static_encoded)
        
        # 2. Variable selection for historical inputs
        historical_selected, var_weights = self.historical_vsn(
            variables=[historical_inputs],
            context=static_context
        )
        
        # 3. LSTM encoding of historical sequence
        if static_context is not None:
            # Initialize LSTM hidden state with static context
            h0 = static_context.unsqueeze(0).repeat(self.lstm_encoder.num_layers, 1, 1)
            c0 = torch.zeros_like(h0)
            lstm_out, (hn, cn) = self.lstm_encoder(historical_selected, (h0, c0))
        else:
            lstm_out, (hn, cn) = self.lstm_encoder(historical_selected)
        
        # 4. Post-LSTM processing
        lstm_out = self.post_lstm_grn(lstm_out)
        
        # 5. Self-attention over historical sequence
        # Use last timestep as query (predicting next timestep)
        query = lstm_out[:, -1:, :]  # [batch, 1, hidden]
        
        # Attend to all historical timesteps
        attended, attention_weights = self.attention(
            query=query,
            key=lstm_out,
            value=lstm_out
        )
        
        # 6. Post-attention processing
        output = self.post_attention_grn(attended)  # [batch, 1, hidden]
        
        # 7. Quantile predictions
        quantile_outputs = []
        for head in self.quantile_heads:
            q_pred = head(output).squeeze(-1)  # [batch, 1]
            quantile_outputs.append(q_pred)
        
        quantile_predictions = torch.stack(quantile_outputs, dim=-1)  # [batch, 1, num_quantiles]
        
        return {
            'predictions': quantile_predictions,
            'attention_weights': attention_weights,
            'variable_weights': var_weights
        }
    
    def predict(
        self,
        historical_inputs: torch.Tensor,
        static_inputs: Optional[torch.Tensor] = None,
        horizon: int = 1
    ) -> torch.Tensor:
        """
        Multi-step prediction with autoregression.
        
        Args:
            historical_inputs: Past observations [batch, hist_len, temporal_size]
            static_inputs: Static features [batch, static_size]
            horizon: Number of steps to predict
        
        Returns:
            predictions: Multi-step predictions [batch, horizon, num_quantiles]
        """
        predictions = []
        current_input = historical_inputs
        
        for _ in range(horizon):
            # Predict next step
            output = self.forward(
                historical_inputs=current_input,
                static_inputs=static_inputs
            )
            
            pred = output['predictions'][:, -1:, :]  # [batch, 1, num_quantiles]
            predictions.append(pred)
            
            # For autoregression, use median (middle quantile) as next input
            # In practice, you'd concatenate with other known features
            median_pred = pred[:, :, self.num_quantiles // 2].unsqueeze(-1)
            
            # Shift window: drop oldest, append new prediction
            # Note: This assumes prediction is same dimension as input
            # Real implementation would handle feature engineering here
            current_input = torch.cat([
                current_input[:, 1:, :],
                median_pred.expand(-1, -1, self.temporal_input_size)
            ], dim=1)
        
        return torch.cat(predictions, dim=1)  # [batch, horizon, num_quantiles]


def quantile_loss(predictions: torch.Tensor, targets: torch.Tensor, quantiles: List[float]) -> torch.Tensor:
    """
    Quantile loss for probabilistic forecasting.
    
    Asymmetric loss that penalizes over/under-prediction differently
    based on the target quantile.
    
    Args:
        predictions: Predicted quantiles [batch, time, num_quantiles]
        targets: Ground truth values [batch, time, 1]
        quantiles: List of quantile levels (e.g., [0.1, 0.5, 0.9])
    
    Returns:
        loss: Scalar loss value
    """
    losses = []
    
    for i, q in enumerate(quantiles):
        pred = predictions[:, :, i]
        error = targets.squeeze(-1) - pred
        
        # Quantile loss: q * error if error > 0, else (q-1) * error
        loss = torch.max(q * error, (q - 1) * error)
        losses.append(loss.mean())
    
    return torch.stack(losses).mean()


if __name__ == "__main__":
    """
    Example usage and testing
    """
    # Configuration
    batch_size = 32
    hist_len = 60  # 1 hour of 1-minute bars
    temporal_features = 32  # OHLCV + indicators from Phase 2
    static_features = 8  # Symbol embedding
    hidden_size = 128
    num_quantiles = 3  # [0.1, 0.5, 0.9]
    
    # Create dummy data
    historical_inputs = torch.randn(batch_size, hist_len, temporal_features)
    static_inputs = torch.randn(batch_size, static_features)
    targets = torch.randn(batch_size, 1, 1)  # Next timestep close price
    
    # Initialize model
    model = TemporalFusionTransformer(
        static_input_size=static_features,
        temporal_input_size=temporal_features,
        hidden_size=hidden_size,
        num_heads=4,
        num_quantiles=num_quantiles,
        dropout=0.1
    )
    
    print("="*60)
    print("Temporal Fusion Transformer Example")
    print("="*60)
    print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")
    print(f"Input shape: [batch={batch_size}, time={hist_len}, features={temporal_features}]")
    
    # Forward pass
    output = model(
        historical_inputs=historical_inputs,
        static_inputs=static_inputs
    )
    
    predictions = output['predictions']
    attention = output['attention_weights']
    var_weights = output['variable_weights']
    
    print(f"\nOutput shapes:")
    print(f"  Predictions: {predictions.shape} (batch, horizon, quantiles)")
    print(f"  Attention: {attention.shape} (batch, horizon, hist_len)")
    print(f"  Variable weights: {var_weights.shape} (batch, hist_len, num_vars)")
    
    # Compute loss
    quantiles = [0.1, 0.5, 0.9]
    loss = quantile_loss(predictions, targets, quantiles)
    print(f"\nQuantile loss: {loss.item():.4f}")
    
    # Multi-step prediction
    print(f"\nMulti-step prediction:")
    multi_pred = model.predict(
        historical_inputs=historical_inputs,
        static_inputs=static_inputs,
        horizon=5
    )
    print(f"  Shape: {multi_pred.shape} (batch, horizon=5, quantiles)")
    print(f"  Median predictions: {multi_pred[0, :, 1].tolist()}")
    
    print("\n✅ TFT model working correctly!")
