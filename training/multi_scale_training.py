"""
Multi-Scale Training Module

Implements training strategy for multi-timescale Temporal Fusion Transformer,
including multi-task loss, regime-aware calibration, and interpretability tracking.

Key Features:
- Multi-scale loss with per-scale auxiliary heads
- Coherence regularization for consistent predictions
- Regime-based parameter adaptation
- Per-scale performance tracking
- Interpretability logging (scale weights, mixing coefficients)

Training Strategy:
1. Train on multiple timescales simultaneously
2. Use auxiliary losses per scale + fused head
3. Add coherence regularizer when scales are correlated
4. Adapt fusion parameters (β, γ) based on volatility regime
5. Track per-scale attribution for interpretability

References:
- Multi-task learning: Caruana (1997)
- Coherence regularization: Custom innovation
- Regime-based adaptation: Integrated with Phase 2
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, List, Tuple, Optional
import numpy as np
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class VolatilityRegime(Enum):
    """Volatility regimes for adaptive parameter tuning."""
    CALM = 'calm'          # vol < 0.5 * historical_avg
    NORMAL = 'normal'      # 0.5 * avg <= vol <= 1.5 * avg
    VOLATILE = 'volatile'  # 1.5 * avg < vol <= 2.5 * avg
    CRISIS = 'crisis'      # vol > 2.5 * avg


@dataclass
class RegimeParameters:
    """Parameters for each volatility regime."""
    beta: float   # Volatility penalty weight
    gamma: float  # Coherence boost weight
    lambda_coherence: float  # Coherence regularization strength


# Regime-specific parameter sets
REGIME_PARAMS = {
    VolatilityRegime.CALM: RegimeParameters(
        beta=0.3,    # Low penalty on micro-scales
        gamma=0.3,   # Moderate coherence boost
        lambda_coherence=0.05
    ),
    VolatilityRegime.NORMAL: RegimeParameters(
        beta=0.5,    # Standard penalty
        gamma=0.5,   # Standard coherence boost
        lambda_coherence=0.1
    ),
    VolatilityRegime.VOLATILE: RegimeParameters(
        beta=0.7,    # Higher penalty on noisy scales
        gamma=0.5,   # Standard coherence boost
        lambda_coherence=0.15
    ),
    VolatilityRegime.CRISIS: RegimeParameters(
        beta=1.0,    # Maximum penalty on micro-scales
        gamma=0.8,   # Strong coherence boost (rely on macro)
        lambda_coherence=0.2
    )
}


class MultiScaleLoss(nn.Module):
    """
    Multi-scale loss function with auxiliary heads and coherence regularization.
    
    Components:
    1. Fused head loss (primary): MSE on fused prediction
    2. Per-scale auxiliary losses: MSE on each scale's prediction
    3. Coherence regularizer: Penalize disagreement when scales are correlated
    
    The coherence regularizer is key: when two scales are highly correlated,
    their predictions should be similar. This prevents the model from learning
    contradictory patterns.
    
    Loss = L_fused + λ_aux * L_scales + λ_coherence * L_coherence
    
    where:
        L_coherence = Σ_i,j corr_ij * |pred_i - pred_j|²
    
    Args:
        d_model: Model embedding dimension
        n_scales: Number of timescales
        lambda_aux: Weight for auxiliary losses (default: 0.3)
        lambda_coherence: Weight for coherence regularizer (default: 0.1)
        use_quantile: Use quantile loss instead of MSE (default: False)
    """
    
    def __init__(
        self,
        d_model: int,
        n_scales: int = 5,
        lambda_aux: float = 0.3,
        lambda_coherence: float = 0.1,
        use_quantile: bool = False
    ):
        super().__init__()
        
        self.n_scales = n_scales
        self.lambda_aux = lambda_aux
        self.lambda_coherence = lambda_coherence
        self.use_quantile = use_quantile
        
        # Fused prediction head
        self.fused_head = nn.Linear(d_model, 1)
        
        # Per-scale auxiliary prediction heads
        self.scale_heads = nn.ModuleList([
            nn.Linear(d_model, 1) for _ in range(n_scales)
        ])
        
        logger.info(f"MultiScaleLoss initialized: n_scales={n_scales}, "
                   f"λ_aux={lambda_aux}, λ_coherence={lambda_coherence}")
    
    def forward(
        self,
        h_scales: List[torch.Tensor],
        h_fused: torch.Tensor,
        y_true: torch.Tensor,
        corr_matrix: Optional[torch.Tensor] = None,
        return_breakdown: bool = False
    ) -> Tuple[torch.Tensor, Dict[str, float]]:
        """
        Compute multi-scale loss.
        
        Args:
            h_scales: List of [B, d_model] scale embeddings
            h_fused: [B, d_model] fused representation
            y_true: [B] or [B, 1] ground truth values
            corr_matrix: Optional [B, n_scales, n_scales] correlation matrix
            return_breakdown: Return detailed loss breakdown
        
        Returns:
            loss: Scalar total loss
            loss_dict: Dictionary with loss components
        """
        if y_true.dim() == 1:
            y_true = y_true.unsqueeze(-1)  # [B, 1]
        
        B = h_fused.shape[0]
        
        # 1. Fused head loss (primary)
        pred_fused = self.fused_head(h_fused)  # [B, 1]
        loss_fused = F.mse_loss(pred_fused, y_true)
        
        # 2. Per-scale auxiliary losses
        loss_scales = 0.0
        pred_scales_list = []
        
        for i, (head, h) in enumerate(zip(self.scale_heads, h_scales)):
            pred_scale = head(h)  # [B, 1]
            pred_scales_list.append(pred_scale)
            loss_scales += F.mse_loss(pred_scale, y_true)
        
        loss_scales /= self.n_scales
        
        # 3. Coherence regularizer (if correlation matrix provided)
        loss_coherence_val = 0.0  # Start as float
        
        if corr_matrix is not None and self.lambda_coherence > 0:
            # Stack predictions [B, n_scales, 1]
            pred_scales = torch.stack(pred_scales_list, dim=1)
            
            # Compute pairwise prediction disagreement weighted by correlation
            disagreement = 0.0
            count = 0
            
            for i in range(self.n_scales):
                for j in range(i + 1, self.n_scales):
                    # Correlation strength (averaged over batch)
                    if corr_matrix.dim() == 3:
                        # [B, n_scales, n_scales]
                        corr_ij = corr_matrix[:, i, j].abs().mean()
                    else:
                        # [n_scales, n_scales]
                        corr_ij = corr_matrix[i, j].abs()
                    
                    # Prediction difference
                    pred_diff = (pred_scales[:, i] - pred_scales[:, j]).pow(2).mean()
                    
                    # Weighted disagreement
                    disagreement += corr_ij * pred_diff
                    count += 1
            
            if count > 0:
                disagreement /= count
            
            loss_coherence_val = disagreement
        
        # Convert to tensor if still float
        loss_coherence = torch.tensor(loss_coherence_val, device=h_fused.device) if isinstance(loss_coherence_val, float) else loss_coherence_val
        
        # 4. Total loss
        total_loss = loss_fused + self.lambda_aux * loss_scales + self.lambda_coherence * loss_coherence
        
        # Loss breakdown for logging
        loss_dict = {
            'total': total_loss.item(),
            'fused': loss_fused.item(),
            'scales': loss_scales,
            'coherence': loss_coherence.item() if torch.is_tensor(loss_coherence) else float(loss_coherence_val)
        }
        
        # Optional detailed breakdown
        if return_breakdown:
            for i, pred in enumerate(pred_scales_list):
                loss_dict[f'scale_{i}'] = F.mse_loss(pred, y_true).item()
        
        return total_loss, loss_dict
    
    def get_predictions(
        self,
        h_scales: List[torch.Tensor],
        h_fused: torch.Tensor
    ) -> Tuple[torch.Tensor, List[torch.Tensor]]:
        """
        Get predictions from all heads.
        
        Args:
            h_scales: List of [B, d_model] scale embeddings
            h_fused: [B, d_model] fused representation
        
        Returns:
            pred_fused: [B, 1] fused prediction
            pred_scales: List of [B, 1] per-scale predictions
        """
        with torch.no_grad():
            pred_fused = self.fused_head(h_fused)
            pred_scales = [head(h) for head, h in zip(self.scale_heads, h_scales)]
        
        return pred_fused, pred_scales


class RegimeAdaptiveTrainer:
    """
    Trainer with regime-based parameter adaptation.
    
    Automatically adjusts fusion layer parameters (β, γ) based on
    detected volatility regime during training.
    
    Strategy:
    - CALM: Trust all scales equally (low β, moderate γ)
    - NORMAL: Standard fusion (standard β, γ)
    - VOLATILE: Penalize micro-scales (high β)
    - CRISIS: Rely on macro-scales heavily (very high β, high γ)
    
    Args:
        model: TFT model with TimeScaleFusion layer
        loss_fn: MultiScaleLoss instance
        optimizer: PyTorch optimizer
        vol_history_len: Window for historical volatility calculation
    """
    
    def __init__(
        self,
        model: nn.Module,
        loss_fn: MultiScaleLoss,
        optimizer: torch.optim.Optimizer,
        vol_history_len: int = 100
    ):
        self.model = model
        self.loss_fn = loss_fn
        self.optimizer = optimizer
        
        self.vol_history_len = vol_history_len
        self.volatility_history = []
        
        # Regime tracking
        self.current_regime = VolatilityRegime.NORMAL
        self.regime_counts = {regime: 0 for regime in VolatilityRegime}
        
        # Performance tracking
        self.regime_losses = {regime: [] for regime in VolatilityRegime}
        self.scale_weights_history = []
        
        logger.info("RegimeAdaptiveTrainer initialized")
    
    def detect_regime(self, current_vol: float) -> VolatilityRegime:
        """
        Detect volatility regime based on current volatility.
        
        Args:
            current_vol: Current realized volatility
        
        Returns:
            Detected regime
        """
        # Store volatility
        self.volatility_history.append(current_vol)
        if len(self.volatility_history) > self.vol_history_len:
            self.volatility_history.pop(0)
        
        # Compute historical average
        if len(self.volatility_history) < 10:
            return VolatilityRegime.NORMAL
        
        historical_avg = np.mean(self.volatility_history)
        ratio = current_vol / (historical_avg + 1e-8)
        
        # Classify regime
        if ratio < 0.5:
            regime = VolatilityRegime.CALM
        elif ratio <= 1.5:
            regime = VolatilityRegime.NORMAL
        elif ratio <= 2.5:
            regime = VolatilityRegime.VOLATILE
        else:
            regime = VolatilityRegime.CRISIS
        
        return regime
    
    def adapt_parameters(self, regime: VolatilityRegime) -> None:
        """
        Adapt fusion layer parameters based on regime.
        
        Args:
            regime: Current volatility regime
        """
        params = REGIME_PARAMS[regime]
        
        # Update fusion layer parameters
        if hasattr(self.model, 'fusion'):
            self.model.fusion.beta.data = torch.tensor(params.beta)
            self.model.fusion.gamma.data = torch.tensor(params.gamma)
        
        # Update coherence regularization weight
        if hasattr(self.loss_fn, 'lambda_coherence'):
            self.loss_fn.lambda_coherence = params.lambda_coherence
        
        logger.debug(f"Adapted to {regime.value}: β={params.beta:.2f}, "
                    f"γ={params.gamma:.2f}, λ_coh={params.lambda_coherence:.2f}")
    
    def train_step(
        self,
        x_scales: List[torch.Tensor],
        aux_stats: torch.Tensor,
        y_true: torch.Tensor,
        corr_matrix: Optional[torch.Tensor] = None
    ) -> Dict[str, float]:
        """
        Single training step with regime adaptation.
        
        Args:
            x_scales: List of [B, T, F_i] scale inputs
            aux_stats: [B, n_scales, 3] auxiliary statistics
            y_true: [B] ground truth values
            corr_matrix: Optional [B, n_scales, n_scales] correlation matrix
        
        Returns:
            Dictionary with loss and metrics
        """
        # Detect regime from auxiliary stats (volatility is first column)
        current_vol = aux_stats[:, :, 0].mean().item()
        regime = self.detect_regime(current_vol)
        
        # Adapt parameters if regime changed
        if regime != self.current_regime:
            logger.info(f"Regime shift: {self.current_regime.value} → {regime.value}")
            self.adapt_parameters(regime)
            self.current_regime = regime
        
        self.regime_counts[regime] += 1
        
        # Forward pass
        self.model.train()
        forecast, signal, scale_weights = self.model(x_scales, aux_stats)
        
        # Get scale embeddings for loss (if available)
        h_scales = []
        h_fused = None
        
        # Note: This assumes model returns internal representations
        # Adjust based on actual model architecture
        # For now, we'll use the forecast directly
        
        # Compute loss
        # Simplified: just use forecast for fused head
        # Full implementation would extract scale embeddings
        loss = F.mse_loss(forecast[:, 1], y_true)  # Use median quantile
        
        # Backward pass
        self.optimizer.zero_grad()
        loss.backward()
        
        # Gradient clipping
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)
        
        self.optimizer.step()
        
        # Track metrics
        metrics = {
            'loss': loss.item(),
            'regime': regime.value,
            'volatility': current_vol,
            'scale_weights_mean': scale_weights.mean(dim=0).cpu().tolist()
        }
        
        # Store regime-specific loss
        self.regime_losses[regime].append(loss.item())
        
        # Store scale weights for analysis
        self.scale_weights_history.append({
            'regime': regime.value,
            'weights': scale_weights.mean(dim=0).cpu().tolist(),
            'volatility': current_vol
        })
        
        return metrics
    
    def get_regime_statistics(self) -> Dict[str, Dict]:
        """
        Get statistics per regime for analysis.
        
        Returns:
            Dictionary with per-regime statistics
        """
        stats = {}
        
        for regime in VolatilityRegime:
            losses = self.regime_losses[regime]
            
            stats[regime.value] = {
                'count': self.regime_counts[regime],
                'avg_loss': np.mean(losses) if losses else 0.0,
                'std_loss': np.std(losses) if losses else 0.0,
                'min_loss': np.min(losses) if losses else 0.0,
                'max_loss': np.max(losses) if losses else 0.0
            }
        
        return stats
    
    def analyze_scale_weights(self) -> Dict[str, List[float]]:
        """
        Analyze scale weight distribution per regime.
        
        Returns:
            Dictionary mapping regime to average scale weights
        """
        regime_weights = {regime.value: [] for regime in VolatilityRegime}
        
        for entry in self.scale_weights_history:
            regime_weights[entry['regime']].append(entry['weights'])
        
        # Average weights per regime
        avg_weights = {}
        for regime, weights_list in regime_weights.items():
            if weights_list:
                avg_weights[regime] = np.mean(weights_list, axis=0).tolist()
            else:
                avg_weights[regime] = [0.0] * 5
        
        return avg_weights


def compute_auxiliary_stats(
    volatilities: torch.Tensor,
    divergences: torch.Tensor,
    correlations: torch.Tensor
) -> torch.Tensor:
    """
    Prepare auxiliary statistics tensor for TimeScaleFusion.
    
    Args:
        volatilities: [B, n_scales] realized volatility per scale
        divergences: [B, n_scales] divergence flags (0 or 1)
        correlations: [B, n_scales, n_scales] correlation matrix
    
    Returns:
        aux_stats: [B, n_scales, 3] tensor with
                   [..., 0] = volatility (normalized)
                   [..., 1] = divergence_flag
                   [..., 2] = mean correlation with other scales
    """
    B, n_scales = volatilities.shape
    
    aux_stats = torch.zeros(B, n_scales, 3, device=volatilities.device)
    
    # Volatility (already computed)
    aux_stats[:, :, 0] = volatilities
    
    # Divergence flags
    aux_stats[:, :, 1] = divergences
    
    # Mean correlation with other scales
    for i in range(n_scales):
        # Average correlation of scale i with all others
        mask = torch.ones(n_scales, dtype=torch.bool)
        mask[i] = False
        aux_stats[:, i, 2] = correlations[:, i, mask].abs().mean(dim=1)
    
    return aux_stats


if __name__ == '__main__':
    # Test multi-scale training components
    logging.basicConfig(level=logging.INFO)
    
    print("\n" + "="*60)
    print("Testing Multi-Scale Training Components")
    print("="*60)
    
    # Configuration
    batch_size = 16
    n_scales = 5
    d_model = 256
    
    # Create loss function
    print("\n✓ Creating MultiScaleLoss...")
    loss_fn = MultiScaleLoss(
        d_model=d_model,
        n_scales=n_scales,
        lambda_aux=0.3,
        lambda_coherence=0.1
    )
    
    # Create dummy data
    h_scales = [torch.randn(batch_size, d_model) for _ in range(n_scales)]
    h_fused = torch.randn(batch_size, d_model)
    y_true = torch.randn(batch_size)
    
    # Create correlation matrix
    corr_matrix = torch.rand(batch_size, n_scales, n_scales)
    # Make symmetric
    corr_matrix = (corr_matrix + corr_matrix.transpose(1, 2)) / 2
    # Set diagonal to 1
    for b in range(batch_size):
        corr_matrix[b].fill_diagonal_(1.0)
    
    # Test loss computation
    print("✓ Computing loss...")
    loss, loss_dict = loss_fn(h_scales, h_fused, y_true, corr_matrix, return_breakdown=True)
    
    print(f"  Total loss: {loss.item():.4f}")
    print(f"  Fused loss: {loss_dict['fused']:.4f}")
    print(f"  Scales loss: {loss_dict['scales']:.4f}")
    print(f"  Coherence loss: {loss_dict['coherence']:.4f}")
    
    # Test regime detection
    print("\n✓ Testing regime detection...")
    
    # Create simple model stub
    class SimpleModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.fusion = type('Fusion', (), {
                'beta': nn.Parameter(torch.tensor(0.5)),
                'gamma': nn.Parameter(torch.tensor(0.5))
            })()
        
        def forward(self, x, aux):
            return torch.randn(len(x), 3), torch.randn(len(x), 3), torch.softmax(torch.randn(len(x), 5), dim=1)
    
    model = SimpleModel()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    
    trainer = RegimeAdaptiveTrainer(model, loss_fn, optimizer)
    
    # Simulate different volatility regimes
    regimes_to_test = [
        (0.01, VolatilityRegime.CALM),
        (0.05, VolatilityRegime.NORMAL),
        (0.15, VolatilityRegime.VOLATILE),
        (0.30, VolatilityRegime.CRISIS)
    ]
    
    for vol, expected_regime in regimes_to_test:
        detected = trainer.detect_regime(vol)
        print(f"  Volatility {vol:.2f} → {detected.value} (expected: {expected_regime.value})")
    
    # Test auxiliary stats computation
    print("\n✓ Testing auxiliary stats computation...")
    volatilities = torch.rand(batch_size, n_scales)
    divergences = torch.randint(0, 2, (batch_size, n_scales)).float()
    
    aux_stats = compute_auxiliary_stats(volatilities, divergences, corr_matrix)
    
    print(f"  Aux stats shape: {aux_stats.shape}")
    print(f"  Sample stats (batch 0, scale 0):")
    print(f"    Volatility: {aux_stats[0, 0, 0]:.4f}")
    print(f"    Divergence: {aux_stats[0, 0, 1]:.4f}")
    print(f"    Avg correlation: {aux_stats[0, 0, 2]:.4f}")
    
    print("\n" + "="*60)
    print("Multi-Scale Training Test Complete!")
    print("="*60)
