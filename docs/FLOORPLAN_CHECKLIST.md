# ChronoX Trading Bot - Implementation Floorplan Checklist

**Document Version:** 1.0  
**Last Updated:** 2025-10-16  
**System:** Ryzen 9 9950X | RTX 5070 | 96GB DDR5 | Samsung 990 Pro NVMe

---

## 📋 Overview

This floorplan provides a comprehensive, phase-by-phase checklist for implementing the ChronoX trading bot from foundation to production deployment. Each item is designed to be actionable, measurable, and optimally sequenced.

---

## 🖥️ System Architecture & Hardware Utilization

### Asset Scaling & Workload Optimization

**Hardware Context:**
- **CPU:** AMD Ryzen 9 9950X (16 cores / 32 threads, 5.7 GHz boost)
- **RAM:** 96 GB DDR5-6000
- **GPU:** NVIDIA RTX 5070 (12 GB VRAM; ~10 GB usable after display allocation)
- **Storage:** Samsung 990 Pro NVMe SSD (7 GB/s sequential read)

This section provides concrete guidance on how many assets (symbols/tickers) can be processed concurrently across different workloads, leveraging the available hardware efficiently while avoiding out-of-memory (OOM) conditions or thrashing.

---

#### Workload-Specific Asset Scaling

The following table summarizes recommended asset counts per workload type, optimized for the Ryzen 9 9950X + RTX 5070 + 96GB RAM configuration:

| Task                                   | Device           | Recommended Asset Count | Notes                                                                 |
|----------------------------------------|------------------|-------------------------|-----------------------------------------------------------------------|
| Feature generation / aggregation       | CPU + RAM        | Up to 100 symbols       | Batch processing via pandas or Dask; multiprocessing per symbol       |
| Short-term model training (15 min)     | GPU              | 10–15 symbols           | Use FP16, batch size ≤ 256; ~6-8 GB VRAM per training run            |
| Long-term (12 hr / daily)              | GPU              | 5–10 symbols            | Larger sequence length eats memory; consider gradient checkpointing   |
| Backtesting / Simulation               | CPU              | Up to 100 symbols       | Threaded per-symbol; disk I/O becomes bottleneck before CPU          |
| Inference (real-time)                  | GPU              | 20–30 symbols           | Use saved models with `torch.no_grad()`; no gradient computation      |

**Rationale:**

1. **Feature Generation (CPU + RAM):**
   - Pandas DataFrames scale efficiently up to ~10-50 GB in memory
   - With 96 GB RAM, can hold ~5 years of minute bars for 50-100 symbols simultaneously
   - Use [Dask](https://docs.dask.org/) for larger-than-memory operations:
     ```python
     import dask.dataframe as dd
     
     # Partitioned read for 100 symbols, 5 years of data
     df = dd.read_parquet(
         "data/market_data/*.parquet",
         columns=["timestamp", "ticker", "open", "high", "low", "close", "volume"],
         filters=[("ticker", "in", target_tickers)]
     )
     
     # Compute features in parallel across partitions
     features = df.groupby("ticker").apply(compute_technical_indicators, meta=...)
     features.compute()  # Triggers parallel execution
     ```
   - Dask enables out-of-core computation by chunking data and distributing across CPU cores
   - Reference: [Dask Documentation - Why Dask?](https://docs.dask.org/en/stable/why.html)

2. **Short-Term Training (15 min timeframe, GPU):**
   - Sequence length: ~500-1,000 timesteps (15 min bars over ~1 week)
   - Hidden dimension: 256-512
   - Batch size: 128-256
   - **Memory estimation per batch (FP32):**
     $$\text{VRAM} \approx 2 \times (\text{batch} \times \text{seq} \times \text{hidden} \times 4 \text{ bytes}) + \text{gradients} + \text{optimizer states}$$
     $$\text{VRAM} \approx 2 \times (256 \times 1000 \times 512 \times 4) \approx 2 \times 524 \text{ MB} \approx 1 \text{ GB per batch}$$
   - **With FP16 (Automatic Mixed Precision):** ~50% memory reduction → **0.5 GB per batch**
   - Can fit 10-15 symbols concurrently with staggered batching
   - Reference: [NVIDIA Mixed Precision Training Guide](https://docs.nvidia.com/deeplearning/performance/mixed-precision-training/index.html)

3. **Long-Term Training (12 hr / daily timeframe, GPU):**
   - Sequence length: 2,000-5,000 timesteps (12 hr bars over months)
   - Memory scales linearly with sequence length → ~4-8 GB VRAM per training run
   - Use **gradient checkpointing** to trade compute for memory:
     ```python
     from torch.utils.checkpoint import checkpoint
     
     class TransformerBlock(nn.Module):
         def forward(self, x):
             # Checkpoint this block: recomputes forward pass during backward
             return checkpoint(self._forward_impl, x, use_reentrant=False)
     ```
   - Saves ~40-50% memory on long sequences by recomputing activations during backward pass
   - Reference: [Gradient Checkpointing Paper - "Training Deep Nets with Sublinear Memory Cost"](https://arxiv.org/abs/1604.06174)
   - Reference: [Gradient Checkpointing in PyTorch](https://pytorch.org/docs/stable/checkpoint.html)

4. **Backtesting (CPU):**
   - Each symbol backtested independently in separate thread/process
   - Disk I/O (reading OHLCV from Parquet) becomes bottleneck before CPU
   - NVMe @ 7 GB/s → can stream ~100-200 symbols worth of data per second
   - Use multiprocessing pool:
     ```python
     from multiprocessing import Pool
     
     def backtest_symbol(ticker):
         # Load data, run strategy, compute metrics
         return results
     
     with Pool(16) as pool:  # 16 CPU cores
         results = pool.map(backtest_symbol, tickers[:100])
     ```

5. **Inference (GPU):**
   - No gradient computation → ~60% less memory than training
   - Can batch 20-30 symbols in a single forward pass
   - Use static TorchScript for faster execution:
     ```python
     model = torch.jit.script(model)  # Static computation graph
     
     with torch.no_grad(), torch.cuda.amp.autocast():
         predictions = model(input_tensor)  # FP16 inference
     ```
   - Reference: [PyTorch Inference Optimization](https://pytorch.org/tutorials/recipes/recipes/tuning_guide.html#inference-specific-optimizations)

---

#### Scaling Tricks & Architectural Patterns

**1. Cluster Symbols by Correlation → One Model Per Cluster**

Instead of training a single massive model on 100 symbols, cluster correlated symbols and train specialized models:

```python
from sklearn.cluster import AgglomerativeClustering
import pandas as pd

# Compute pairwise correlation matrix
returns = df.pivot(index="timestamp", columns="ticker", values="returns")
corr_matrix = returns.corr()

# Hierarchical clustering
clustering = AgglomerativeClustering(n_clusters=5, metric="precomputed", linkage="average")
clusters = clustering.fit_predict(1 - corr_matrix)  # Distance = 1 - correlation

# Train one model per cluster
# e.g., 5 models × 20 symbols each = 100 symbols total
for cluster_id in range(5):
    cluster_tickers = tickers[clusters == cluster_id]
    train_model_for_cluster(cluster_id, cluster_tickers)
```

**Benefits:**
- Each model specializes on correlated assets (e.g., tech stocks, energy, financials)
- Reduces model complexity → faster training, better generalization
- Parallelizable: train 5 models concurrently on different GPU runs

---

**2. Hierarchical Training: Sector Models → Meta-Model**

Train base models on individual sectors, then a meta-model that ensembles predictions:

```python
# Stage 1: Train sector-specific models
sector_models = {
    "tech": train_transformer(tech_stocks),
    "energy": train_transformer(energy_stocks),
    "finance": train_transformer(financial_stocks),
}

# Stage 2: Meta-model combines predictions
class MetaModel(nn.Module):
    def __init__(self, sector_models):
        super().__init__()
        self.sector_models = sector_models
        self.attention = nn.MultiheadAttention(embed_dim=128, num_heads=4)
        self.fc = nn.Linear(128, 3)  # Buy/Hold/Sell
    
    def forward(self, inputs):
        # Get predictions from each sector model
        sector_preds = [model(inputs[sector]) for sector, model in self.sector_models.items()]
        
        # Attention-based fusion
        fused, _ = self.attention(
            torch.stack(sector_preds),  # [num_sectors, batch, hidden]
            torch.stack(sector_preds),
            torch.stack(sector_preds)
        )
        
        return self.fc(fused.mean(dim=0))
```

**Benefits:**
- Each sector model trained on homogeneous data → better feature learning
- Meta-model learns cross-sector dynamics and regime switching
- Reference: [Temporal Fusion Transformer (TFT) for Multi-Horizon Forecasting](https://arxiv.org/abs/1912.09363)

---

**3. Parquet + PyArrow: Columnar Storage for Efficient I/O**

Store time-series data in Parquet format partitioned by symbol and year:

```
data/market_data/
├── ticker=AAPL/
│   ├── year=2020/
│   │   └── data.parquet
│   ├── year=2021/
│   │   └── data.parquet
│   ...
├── ticker=TSLA/
│   ├── year=2020/
│   │   └── data.parquet
...
```

**Why Parquet + Arrow?**
- **Columnar format:** Only read required columns (e.g., `close`, `volume`), skip rest → 5-10x faster than row-based CSV
- **Compression:** Parquet achieves 10-20x compression on time-series data (similar to TimescaleDB)
- **Zero-copy reads:** PyArrow → NumPy/PyTorch tensors without serialization overhead

```python
import pyarrow.parquet as pq
import torch

# Read specific symbol + date range + columns
table = pq.read_table(
    "data/market_data/ticker=AAPL/year=2023/data.parquet",
    columns=["timestamp", "close", "volume"]
)

# Zero-copy conversion to NumPy → PyTorch
np_array = table.to_pandas().to_numpy()
tensor = torch.from_numpy(np_array).float()
```

**References:**
- [Apache Arrow - Why Columnar?](https://arrow.apache.org/overview/)
- [Parquet Format Specification](https://parquet.apache.org/docs/)
- [InfluxData: Why Columnar Compression Works](https://www.influxdata.com/blog/columnar-compression-time-series/)

---

**4. NVMe Tensor Caching**

Cache preprocessed tensors on NVMe for instant loading:

```python
import torch

# Save preprocessed tensors
def cache_tensors(ticker, year):
    tensor = preprocess_data(ticker, year)
    torch.save(tensor, f"cache/{ticker}_{year}.pt")

# Load from NVMe (7 GB/s) instead of recomputing
tensor = torch.load(f"cache/{ticker}_{year}.pt", map_location="cuda")
```

**Speedup:** NVMe @ 7 GB/s → load 1 GB tensor in ~150 ms vs. seconds for CSV parsing + feature engineering

---

**5. Mixed-Precision Training (AMP): 35-40% VRAM Savings**

Use PyTorch Automatic Mixed Precision (AMP) for FP16 training:

```python
from torch.cuda.amp import autocast, GradScaler

model = TransformerModel().cuda()
optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
scaler = GradScaler()  # Handles loss scaling for FP16

for batch in dataloader:
    optimizer.zero_grad()
    
    # Forward pass in FP16
    with autocast():
        loss = model(batch).loss
    
    # Backward pass with scaled gradients
    scaler.scale(loss).backward()
    scaler.step(optimizer)
    scaler.update()
```

**Benefits:**
- **Memory:** 35-40% reduction (FP16 = 2 bytes vs. FP32 = 4 bytes)
- **Throughput:** 2-3x faster on RTX 5070 (Tensor Cores optimized for FP16)
- **Accuracy:** Minimal degradation (<0.1% validation loss difference) when using loss scaling

**References:**
- [NVIDIA Automatic Mixed Precision Whitepaper](https://developer.nvidia.com/automatic-mixed-precision)
- [PyTorch AMP Tutorial](https://pytorch.org/docs/stable/amp.html)
- [Mixed Precision Training Performance Study](https://developer.nvidia.com/blog/mixed-precision-training-deep-neural-networks/)

---

**6. Gradient Checkpointing for Long Sequences**

For 12 hr / daily models with sequences > 2,000 timesteps:

```python
from torch.utils.checkpoint import checkpoint_sequential

class LongSequenceTransformer(nn.Module):
    def __init__(self, num_layers=12):
        super().__init__()
        self.layers = nn.ModuleList([TransformerLayer() for _ in range(num_layers)])
    
    def forward(self, x):
        # Checkpoint every 3 layers (trade 3x recompute for 4x memory savings)
        segments = 4
        x = checkpoint_sequential(self.layers, segments, x, use_reentrant=False)
        return x
```

**Memory Savings:** ~40-50% for long sequences at cost of ~30% slower training

**Reference:** [Chen et al., "Training Deep Nets with Sublinear Memory Cost", arXiv:1604.06174](https://arxiv.org/abs/1604.06174)

---

#### GPU/VRAM Capacity Planning

**Per-Symbol Memory Footprint (FP16, Training):**

$$\text{VRAM per symbol} = \underbrace{\text{batch} \times \text{seq} \times \text{hidden} \times 2}_{\text{activations}} + \underbrace{2 \times \text{params} \times 2}_{\text{gradients + momentum}} + \underbrace{\text{overhead}}_{\text{~20\%}}$$

**Example: Transformer with 50M parameters, seq=1000, hidden=512, batch=128:**
- Activations: $128 \times 1000 \times 512 \times 2 = 131$ MB
- Parameters: $50M \times 2 = 100$ MB
- Gradients: $50M \times 2 = 100$ MB
- Optimizer states (AdamW): $2 \times 50M \times 2 = 200$ MB
- **Total:** ~530 MB per training run

**RTX 5070 with 10 GB usable:**
- Concurrent training runs: $10\,\text{GB} / 0.53\,\text{GB} \approx 18$ symbols
- **Conservative estimate:** 10-15 symbols (leaves headroom for cached data)

**Inference (no gradients):**
- Memory = activations + parameters only
- $131\,\text{MB} + 100\,\text{MB} = 231\,\text{MB}$ per symbol
- **Concurrent inference:** $10\,\text{GB} / 0.23\,\text{GB} \approx 43$ symbols
- **Conservative estimate:** 20-30 symbols

---

#### CPU/RAM Path for Data Engineering

**Dask for Larger-Than-Memory Operations:**

When working with 100 symbols × 5 years × 1-minute bars (~50-100 GB uncompressed):

```python
import dask.dataframe as dd
from dask.diagnostics import ProgressBar

# Read partitioned Parquet (lazy evaluation)
df = dd.read_parquet(
    "data/market_data/",
    columns=["timestamp", "ticker", "close", "volume"],
    engine="pyarrow"
)

# Compute features in parallel (distributed across 16 CPU cores)
def compute_rsi(partition):
    # Compute RSI for this chunk
    return partition.assign(rsi=calculate_rsi(partition["close"]))

with ProgressBar():
    df_with_features = df.map_partitions(compute_rsi)
    df_with_features.to_parquet("data/features/", write_index=False)
```

**Benefits:**
- Operates on chunks that fit in memory (~1-2 GB per partition)
- Parallelizes across all 16 CPU cores
- Spills to disk automatically if RAM exhausted
- **Throughput:** Process 100 GB dataset in ~10-15 minutes on Ryzen 9 9950X

**Reference:** [Dask Documentation - Why Dask?](https://docs.dask.org/en/stable/why.html)

---

#### Multi-Timescale Modeling: Hierarchical Attention & Volatility Gating

For models operating across multiple timescales (1 min / 15 min / 1 hr / 12 hr / 1 day), use **Temporal Fusion Transformer (TFT)** architecture with interpretable gating:

**Key Insight:** Micro-scales (1 min) are noisy during high volatility; macro-scales (1 day) capture trend but miss intraday dynamics. Adaptive gating learns when to weight each scale.

```python
class TimeScaleFusion(nn.Module):
    def __init__(self, num_scales=5, hidden=256):
        super().__init__()
        self.attention = nn.MultiheadAttention(hidden, num_heads=8)
        self.volatility_gate = nn.Sequential(
            nn.Linear(num_scales, 64),
            nn.ReLU(),
            nn.Linear(64, num_scales),
            nn.Softmax(dim=-1)
        )
    
    def forward(self, multi_scale_embeddings, volatility_indicators):
        # multi_scale_embeddings: [num_scales, batch, hidden]
        # volatility_indicators: [batch, num_scales]
        
        # Attention-based fusion
        fused, attn_weights = self.attention(
            multi_scale_embeddings,
            multi_scale_embeddings,
            multi_scale_embeddings
        )
        
        # Volatility-aware gating (down-weight noisy micro-scales)
        gate_weights = self.volatility_gate(volatility_indicators)
        gated_fused = fused * gate_weights.unsqueeze(-1)
        
        return gated_fused, attn_weights, gate_weights
```

**Interpretability:**
- `attn_weights` show which timescales contribute to each prediction
- `gate_weights` reveal regime-dependent weighting (e.g., down-weight 1min during volatility spikes)

**Reference:** [Lim et al., "Temporal Fusion Transformers for Interpretable Multi-horizon Time Series Forecasting", arXiv:1912.09363](https://arxiv.org/abs/1912.09363)

**Volatility Regime Gating:**
- **Low volatility (calm market):** Trust micro-scales (1 min, 15 min) for precision
- **High volatility (turbulent market):** Lean on macro-scales (12 hr, 1 day) for stability
- **Crisis regime:** Macro-only (ignore noisy intraday fluctuations)

This aligns with the **Section 3.5.3: Adaptive TimeScale Fusion Layer** already documented in this checklist.

---

#### Storage & I/O Optimization Best Practices

**1. Parquet Partitioning Strategy:**

```bash
data/market_data/
├── ticker=AAPL/
│   ├── year=2020/timeframe=1min/*.parquet
│   ├── year=2020/timeframe=15min/*.parquet
│   ├── year=2020/timeframe=1hour/*.parquet
│   ...
├── ticker=TSLA/
...
```

**Benefits:**
- Partition pruning: Only read relevant ticker + year + timeframe
- Parallel reads: Load multiple tickers concurrently
- Compression: 10-20x size reduction vs. CSV

**2. Arrow Zero-Copy Conversion:**

```python
import pyarrow as pa
import torch

# Parquet → Arrow Table (zero-copy from disk)
table = pq.read_table("data.parquet")

# Arrow → NumPy (zero-copy view of Arrow buffers)
np_array = table["close"].to_numpy()

# NumPy → PyTorch (zero-copy if contiguous)
tensor = torch.from_numpy(np_array)
```

**Speedup:** Avoids serialization/deserialization overhead → 5-10x faster than pandas CSV reads

**References:**
- [Apache Arrow - Zero-Copy Reads](https://arrow.apache.org/docs/python/memory.html)
- [Julien Le Dem - Parquet & Arrow Efficiency](https://www.slideshare.net/julienledem/parquet-and-arrow)

**3. NVMe Caching for Hot Data:**

Samsung 990 Pro @ 7 GB/s sequential read → cache frequently accessed tensors:

```python
import torch
from pathlib import Path

CACHE_DIR = Path("/fast_cache")  # Mount NVMe to /fast_cache

def load_with_cache(ticker, year, recompute=False):
    cache_path = CACHE_DIR / f"{ticker}_{year}.pt"
    
    if cache_path.exists() and not recompute:
        # Load from NVMe cache (150 ms for 1 GB)
        return torch.load(cache_path, map_location="cuda")
    else:
        # Compute from Parquet (2-3 seconds for 1 GB)
        tensor = preprocess_data(ticker, year)
        torch.save(tensor, cache_path)
        return tensor
```

**Cache Hit Rate:** Aim for >80% hit rate during training → 10-20x speedup on data loading

---

#### Concrete Recommendations by Workload

**1. GPU Training (Short-Horizon: 15 min / 1 hour):**
- **Symbols:** 10-15 concurrently
- **Batch size:** 128-256
- **Sequence length:** 500-1,000 timesteps
- **Precision:** FP16 (AMP)
- **Memory:** ~6-8 GB VRAM total
- **Expected throughput:** 500-1,000 samples/sec (RTX 5070)

**Code Pattern:**
```python
from torch.cuda.amp import autocast, GradScaler

model = TransformerModel(hidden=512, num_layers=8).cuda()
optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
scaler = GradScaler()

for epoch in range(100):
    for batch in dataloader:  # batch_size=256
        optimizer.zero_grad()
        
        with autocast():  # FP16 forward pass
            loss = model(batch).loss
        
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
```

---

**2. GPU Training (Long-Horizon: 12 hr / daily):**
- **Symbols:** 5-10 concurrently
- **Batch size:** 64-128
- **Sequence length:** 2,000-5,000 timesteps
- **Precision:** FP16 + Gradient Checkpointing
- **Memory:** ~8-10 GB VRAM total
- **Expected throughput:** 200-400 samples/sec

**Code Pattern:**
```python
from torch.utils.checkpoint import checkpoint_sequential

class LongHorizonTransformer(nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = nn.ModuleList([TransformerLayer() for _ in range(12)])
    
    def forward(self, x):
        # Checkpoint every 3 layers
        return checkpoint_sequential(self.layers, segments=4, input=x, use_reentrant=False)

model = LongHorizonTransformer().cuda()

for batch in dataloader:  # batch_size=128
    with autocast():
        loss = model(batch).loss  # Gradient checkpointing active
    
    scaler.scale(loss).backward()
    scaler.step(optimizer)
    scaler.update()
```

---

**3. GPU Inference (Real-Time):**
- **Symbols:** 20-30 per forward pass
- **Batch size:** 32-64
- **Precision:** FP16
- **Memory:** ~2-3 GB VRAM
- **Expected throughput:** 2,000-5,000 samples/sec (no gradients)
- **Latency:** <50 ms per batch

**Code Pattern:**
```python
model = torch.jit.script(model)  # TorchScript for static graph
model.eval()

with torch.no_grad(), torch.cuda.amp.autocast():
    predictions = model(batch_of_30_symbols)  # Single forward pass
```

---

**4. CPU Feature Engineering (Dask):**
- **Symbols:** Up to 100 concurrently
- **Workers:** 16 (one per CPU core)
- **Memory:** 96 GB (Dask manages spilling to disk if exceeded)
- **Expected throughput:** Process 100 GB dataset in 10-15 minutes

**Code Pattern:**
```python
import dask.dataframe as dd
from dask.diagnostics import ProgressBar

df = dd.read_parquet(
    "data/market_data/",
    columns=["timestamp", "ticker", "close", "volume"],
    engine="pyarrow"
).set_index("timestamp")

# Compute technical indicators in parallel
def compute_features(partition):
    partition["rsi"] = calculate_rsi(partition["close"])
    partition["macd"] = calculate_macd(partition["close"])
    return partition

with ProgressBar():
    df_features = df.map_partitions(compute_features, meta=...)
    df_features.to_parquet("data/features/")
```

---

**5. CPU Backtesting (Multiprocessing):**
- **Symbols:** Up to 100 in parallel
- **Workers:** 16 (one per CPU core)
- **I/O bottleneck:** NVMe @ 7 GB/s → can stream data for 100+ symbols
- **Expected throughput:** 10-50 symbols/minute depending on strategy complexity

**Code Pattern:**
```python
from multiprocessing import Pool

def backtest_symbol(ticker):
    # Load data from Parquet
    data = pd.read_parquet(f"data/market_data/ticker={ticker}/")
    
    # Run strategy simulation
    results = simulate_strategy(data)
    return results

with Pool(16) as pool:
    results = pool.map(backtest_symbol, tickers[:100])
```

---

#### Acceptance Criteria

To validate optimal hardware utilization and scaling, implement the following monitoring and benchmarks:

**1. GPU VRAM Usage Tracking:**
```python
import torch

def log_gpu_memory():
    allocated = torch.cuda.memory_allocated() / 1e9  # GB
    reserved = torch.cuda.memory_reserved() / 1e9    # GB
    print(f"GPU Memory: {allocated:.2f} GB allocated, {reserved:.2f} GB reserved")
    
    # Log to MLflow
    mlflow.log_metrics({
        "gpu_memory_allocated_gb": allocated,
        "gpu_memory_reserved_gb": reserved
    })

# Call after each training batch
log_gpu_memory()
```

**Target:** <10 GB total VRAM usage during training; <3 GB during inference

---

**2. Tokens-Per-Second (Throughput) Benchmarking:**
```python
import time

def benchmark_throughput(model, dataloader, num_batches=100):
    model.eval()
    total_tokens = 0
    start_time = time.time()
    
    with torch.no_grad(), torch.cuda.amp.autocast():
        for i, batch in enumerate(dataloader):
            if i >= num_batches:
                break
            
            predictions = model(batch)
            total_tokens += batch.size(0) * batch.size(1)  # batch × seq_len
    
    elapsed = time.time() - start_time
    tokens_per_sec = total_tokens / elapsed
    
    print(f"Throughput: {tokens_per_sec:.0f} tokens/sec")
    mlflow.log_metric("tokens_per_second", tokens_per_sec)
    
    return tokens_per_sec

# Run benchmark
throughput = benchmark_throughput(model, test_dataloader)
```

**Target:** 
- Training: 50,000-100,000 tokens/sec (FP16)
- Inference: 200,000-500,000 tokens/sec (FP16, no gradients)

---

**3. Per-Symbol Throughput & Backtest Wall-Time:**
```python
import time

def benchmark_backtest(tickers):
    start_time = time.time()
    
    with Pool(16) as pool:
        results = pool.map(backtest_symbol, tickers)
    
    elapsed = time.time() - start_time
    symbols_per_minute = len(tickers) / (elapsed / 60)
    
    print(f"Backtested {len(tickers)} symbols in {elapsed:.1f}s ({symbols_per_minute:.1f} symbols/min)")
    mlflow.log_metrics({
        "backtest_wall_time_sec": elapsed,
        "symbols_per_minute": symbols_per_minute
    })

benchmark_backtest(tickers[:100])
```

**Target:** 10-50 symbols/minute (depends on strategy complexity and date range)

---

**4. FP16 vs. FP32 Validation Metric Parity:**
```python
def validate_precision_parity(model_fp32, model_fp16, val_dataloader):
    """
    Ensure FP16 model achieves similar validation metrics as FP32.
    Tolerance: <1% relative difference in loss/accuracy.
    """
    loss_fp32 = evaluate_model(model_fp32, val_dataloader, dtype=torch.float32)
    loss_fp16 = evaluate_model(model_fp16, val_dataloader, dtype=torch.float16)
    
    relative_diff = abs(loss_fp32 - loss_fp16) / loss_fp32
    
    print(f"FP32 Loss: {loss_fp32:.4f}")
    print(f"FP16 Loss: {loss_fp16:.4f}")
    print(f"Relative Difference: {relative_diff:.2%}")
    
    assert relative_diff < 0.01, f"FP16 diverged from FP32 by {relative_diff:.2%}"
    
    mlflow.log_metrics({
        "val_loss_fp32": loss_fp32,
        "val_loss_fp16": loss_fp16,
        "fp16_relative_error": relative_diff
    })

validate_precision_parity(model_fp32, model_fp16, val_dataloader)
```

**Target:** <1% relative error between FP16 and FP32 on validation metrics

---

**5. End-to-End Pipeline Profiling:**

Use PyTorch Profiler to identify bottlenecks:

```python
from torch.profiler import profile, record_function, ProfilerActivity

with profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
    record_shapes=True,
    profile_memory=True
) as prof:
    with record_function("model_training"):
        for batch in dataloader:
            with autocast():
                loss = model(batch).loss
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

# Export profiling results
prof.export_chrome_trace("trace.json")

# Print summary
print(prof.key_averages().table(sort_by="cuda_time_total", row_limit=10))
```

**Target:** 
- <10% time spent on data loading (I/O)
- >80% time spent on GPU compute (forward + backward)

---

#### Summary: Recommended Configurations

| Workload                     | Device | Assets | Batch | Seq Len | Precision | VRAM/RAM | Throughput               |
|------------------------------|--------|--------|-------|---------|-----------|----------|--------------------------|
| Short-term training (15 min) | GPU    | 10-15  | 128-256 | 500-1K | FP16 + AMP | 6-8 GB   | 500-1K samples/sec       |
| Long-term training (daily)   | GPU    | 5-10   | 64-128  | 2K-5K  | FP16 + GC  | 8-10 GB  | 200-400 samples/sec      |
| Inference (real-time)        | GPU    | 20-30  | 32-64   | 500-1K | FP16 + JIT | 2-3 GB   | 2K-5K samples/sec        |
| Feature engineering          | CPU    | 100    | N/A     | N/A    | FP32       | 96 GB    | 100 GB in 10-15 min      |
| Backtesting                  | CPU    | 100    | N/A     | N/A    | FP32       | 96 GB    | 10-50 symbols/min        |

**Key Takeaways:**
- **FP16 (AMP)** is essential for GPU efficiency → 35-40% VRAM savings + 2-3x speedup
- **Gradient checkpointing** enables long sequences (>2K timesteps) without OOM
- **Dask + Parquet + Arrow** scales feature engineering to 100+ symbols on CPU
- **NVMe caching** (7 GB/s) eliminates data loading bottlenecks
- **Hierarchical/cluster-based training** parallelizes across symbols and sectors
- **Multi-timescale gating** (TFT-style) handles volatility regimes interpretably

---

## Phase 0: Historical Data Acquisition & Database Setup (Weeks 1-2) **[PRIORITY]**

> **🎯 OBJECTIVE:** Build a comprehensive historical database with 5 years of market data from Polygon.io. This is the foundation for all ML training and backtesting.

**What you'll have by the end:**
- InfluxDB running with 5 years of OHLCV data
- Reference data (ticker metadata, sectors, exchanges)
- Corporate actions (dividends, splits)
- Recent news articles (6 months)
- Complete Polygon.io API client library
- Data quality verification tools

**Estimated Time:** 1-2 weeks (depending on data volume and API speed)

---

### 0.0 Quick Start Setup

#### Install Prerequisites
- [x] Verify Docker is installed: `docker --version`
- [x] Verify Python 3.9+ is installed: `python3 --version`
- [x] Sign up for Polygon.io Starter plan ($29/month)
- [x] Obtain Polygon.io API key from dashboard

#### Initial Project Setup
- [x] Create project directory: `mkdir -p ~/ChronoX && cd ~/ChronoX`
- [x] Clone/initialize Git repository
- [x] Create directory structure:
```bash
mkdir -p data/ingestion/polygon data/preprocessing data/storage
mkdir -p models/transformers models/rl_agents models/ensembles
mkdir -p training inference backtesting trading
mkdir -p monitoring/exporters pipelines/airflow_dags docker
mkdir -p tests/unit tests/integration configs notebooks scripts logs
mkdir -p data/flat_files docs
```
- [x] Create Python virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```
- [x] Create initial `requirements.txt`:
```
# Data acquisition
requests>=2.31.0
aiohttp>=3.9.0
websocket-client>=1.6.0
tenacity>=8.2.0
ratelimit>=2.2.1

# Database
influxdb-client>=1.38.0

# Data manipulation
pandas>=2.1.0
numpy>=1.24.0

# Utilities
python-dotenv>=1.0.0
tqdm>=4.66.0

# Development
pytest>=7.4.0
pytest-cov>=4.1.0
black>=23.7.0
pylint>=2.17.0
```
- [x] Install dependencies: `pip install -r requirements.txt`
- [x] Create `.gitignore`:
```
.env
.env.local
__pycache__/
*.pyc
venv/
data/flat_files/*.gz
logs/*.log
models/saved_models/
.ipynb_checkpoints/
```

#### Start InfluxDB
- [x] Pull and run InfluxDB container:
```bash
docker run -d \
  --name chronox-influxdb \
  -p 8086:8086 \
  -v chronox-influxdb-data:/var/lib/influxdb2 \
  -v chronox-influxdb-config:/etc/influxdb2 \
  -e DOCKER_INFLUXDB_INIT_MODE=setup \
  -e DOCKER_INFLUXDB_INIT_USERNAME=admin \
  -e DOCKER_INFLUXDB_INIT_PASSWORD=your-secure-password \
  -e DOCKER_INFLUXDB_INIT_ORG=chronox \
  -e DOCKER_INFLUXDB_INIT_BUCKET=market_data_5y \
  -e DOCKER_INFLUXDB_INIT_RETENTION=1825d \
  -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=your-super-secret-token \
  influxdb:2.7
```
- [x] Verify InfluxDB is running: `docker ps | grep chronox-influxdb`
- [x] Access InfluxDB UI: http://localhost:8086 (login: admin / your-secure-password)

> **⚠️ NOTE:** InfluxDB has been replaced with TimescaleDB. See `TIMESCALEDB_MIGRATION.md` for details.

#### Create Environment File
- [x] Create `.env` file in project root:
```bash
# Polygon.io API
POLYGON_API_KEY=YOUR_POLYGON_KEY_HERE

# InfluxDB Configuration
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=your-super-secret-token
INFLUX_ORG=chronox
INFLUX_BUCKET_MARKET=market_data_5y
INFLUX_BUCKET_INDICATORS=indicators_1y
INFLUX_BUCKET_CORP=corporate_actions
INFLUX_BUCKET_REF=reference_data
INFLUX_BUCKET_NEWS=news_6mo
INFLUX_BUCKET_SNAPSHOTS=snapshots_30d
```
- [x] **IMPORTANT:** Replace YOUR_POLYGON_KEY_HERE with your actual API key
- [x] Verify `.env` is in `.gitignore`

---

### 0.1 Polygon.io Account Setup
- [x] Store API key in `.env` file (never commit to Git)
- [x] Verify API access with test call
- [x] Document API rate limits and capabilities:
  - [x] 15-minute delayed data
  - [x] 5 years historical data
  - [x] Unlimited API calls
  - [x] Unlimited file downloads
  - [x] Minute aggregates available

### 0.2 Database Architecture Design

> **⚠️ NOTE:** The project has migrated from InfluxDB to TimescaleDB. See `TIMESCALEDB_MIGRATION.md` for details.

#### TimescaleDB Installation & Setup (NEW)
- [x] Create docker-compose.yml with TimescaleDB service
- [x] Create init_timescale_schema.sql (500 lines)
- [ ] **TODO:** Start TimescaleDB container and verify:
  ```bash
  docker-compose up -d timescaledb
  docker-compose logs -f timescaledb
  # Should see: "TimescaleDB schema initialized successfully!"
  ```
- [ ] **TODO:** Test database connection:
  ```bash
  python -c "from data.storage.timescale_writer import TimescaleWriter; w = TimescaleWriter(); print('✓ Connected!' if w.test_connection() else '✗ Failed'); w.close()"
  ```

#### InfluxDB Installation & Setup (DEPRECATED - kept for reference)
- [x] Install InfluxDB 2.x (Docker recommended):
  ```bash
  docker run -d -p 8086:8086 \
    -v influxdb-data:/var/lib/influxdb2 \
    -v influxdb-config:/etc/influxdb2 \
    influxdb:2.7
  ```
- [x] Access InfluxDB UI (http://localhost:8086)
- [x] Create organization: `chronox`
- [x] Create buckets with retention policies:
  - [x] `market_data_5y` (5 year retention) - Raw OHLCV data
  - [x] `indicators_1y` (1 year retention) - Calculated technical indicators
  - [x] `corporate_actions` (infinite retention) - Splits, dividends
  - [x] `reference_data` (infinite retention) - Ticker metadata
  - [x] `news_6mo` (6 month retention) - News articles
  - [x] `snapshots_30d` (30 day retention) - Real-time snapshots
- [x] Generate API token with read/write access
- [x] Store token in `.env` file
- [x] Test write/query operations

#### Schema Design
- [x] Design measurement schemas:
  - [x] **market_data**: tags(ticker, timeframe), fields(open, high, low, close, volume, vwap, transactions), timestamp
  - [x] **indicators**: tags(ticker, indicator_type, timeframe), fields(value, signal), timestamp
  - [x] **corporate_actions**: tags(ticker, action_type), fields(ratio, amount, declaration_date, ex_date, pay_date), timestamp
  - [x] **reference**: tags(ticker), fields(name, market, locale, type, active, currency, exchange, sector, industry, market_cap)
  - [x] **news**: tags(ticker, publisher), fields(title, author, article_url, tickers_list, keywords, sentiment_score), timestamp
- [x] Document schema in `docs/database_schema.md`

### 0.3 Polygon.io API Client Implementation

#### Core Client Setup
- [x] Create `data/ingestion/polygon/` directory structure:
  - [x] `__init__.py`
  - [x] `client.py` - Main API client (215 lines)
  - [x] `aggregates.py` - OHLCV bars (333 lines)
  - [x] `indicators.py` - Technical indicators (256 lines)
  - [x] `corporate_actions.py` - Dividends, splits (210 lines)
  - [x] `reference.py` - Ticker metadata (268 lines)
  - [x] `news.py` - News fetching (217 lines)
  - [x] `websocket_client.py` - WebSocket handler (320 lines)
  - [x] `flat_files.py` - Bulk download handler (310 lines)
  - [x] `flat_file_parser.py` - File parsing (314 lines)
  - [x] ~~`utils.py`~~ - NOT NEEDED (retry/rate limiting built into client.py)

#### Implement Base Client
- [x] Create `data/ingestion/polygon/client.py`:
```python
import requests
import time
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential

class PolygonClient:
    BASE_URL = "https://api.polygon.io"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})
    
    @sleep_and_retry
    @limits(calls=5, period=1)  # Conservative rate limiting
    @retry(stop=stop_after_attempt(3), 
           wait=wait_exponential(multiplier=1, min=4, max=10))
    def _get(self, endpoint: str, params: dict = None):
        """Make GET request with retry logic and rate limiting"""
        params = params or {}
        params['apiKey'] = self.api_key
        
        response = self.session.get(
            f"{self.BASE_URL}{endpoint}",
            params=params,
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    
    def get_with_pagination(self, endpoint: str, params: dict = None):
        """Handle cursor-based pagination"""
        all_results = []
        params = params or {}
        
        while True:
            data = self._get(endpoint, params)
            results = data.get('results', [])
            all_results.extend(results)
            
            next_url = data.get('next_url')
            if not next_url:
                break
            
            # Extract cursor from next_url
            if 'cursor=' in next_url:
                cursor = next_url.split('cursor=')[1].split('&')[0]
                params['cursor'] = cursor
            else:
                break
        
        return all_results
```
- [x] Test client with sample API call
- [x] Validate retry logic (simulate network failures)
- [x] Test pagination with large result sets

#### Implement Aggregates Module
- [x] Create `data/ingestion/polygon/aggregates.py`:
```python
from typing import List, Dict
from datetime import datetime, timedelta

class AggregatesClient:
    def __init__(self, polygon_client):
        self.client = polygon_client
    
    def get_minute_bars(self, ticker: str, start_date: str, end_date: str, 
                       adjusted: bool = True, limit: int = 50000) -> List[Dict]:
        """
        Fetch minute bars for a ticker
        
        Args:
            ticker: Stock symbol (e.g., 'AAPL')
            start_date: Start date 'YYYY-MM-DD'
            end_date: End date 'YYYY-MM-DD'
            adjusted: Include split adjustments
            limit: Max bars per request (max 50000)
        
        Returns:
            List of bars with keys: t, o, h, l, c, v, vw, n
        """
        endpoint = f"/v2/aggs/ticker/{ticker}/range/1/minute/{start_date}/{end_date}"
        params = {
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": limit
        }
        
        data = self.client._get(endpoint, params)
        return data.get('results', [])
    
    def get_daily_bars(self, ticker: str, start_date: str, end_date: str,
                      adjusted: bool = True) -> List[Dict]:
        """Fetch daily bars"""
        endpoint = f"/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}"
        params = {
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": 50000
        }
        
        data = self.client._get(endpoint, params)
        return data.get('results', [])
    
    def chunk_date_range(self, start_date: str, end_date: str, 
                        chunk_days: int = 7) -> List[tuple]:
        """
        Split large date range into chunks to avoid timeouts
        
        Args:
            start_date: Start date 'YYYY-MM-DD'
            end_date: End date 'YYYY-MM-DD'
            chunk_days: Days per chunk (7 for minute data to stay under 50k limit)
        
        Returns:
            List of (start, end) date tuples
        """
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        
        chunks = []
        current = start
        
        while current < end:
            chunk_end = min(current + timedelta(days=chunk_days), end)
            chunks.append((
                current.strftime('%Y-%m-%d'),
                chunk_end.strftime('%Y-%m-%d')
            ))
            current = chunk_end + timedelta(days=1)
        
        return chunks
```
- [ ] Test minute bars fetching for 1 week
- [ ] Test daily bars fetching for 5 years
- [ ] Validate data completeness (no gaps)

#### Implement Corporate Actions Module
- [ ] Create `data/ingestion/polygon/corporate_actions.py`:
```python
class CorporateActionsClient:
    def __init__(self, polygon_client):
        self.client = polygon_client
    
    def get_dividends(self, ticker: str = None, since: str = "2020-01-01") -> List[Dict]:
        """Fetch dividend history"""
        endpoint = "/v3/reference/dividends"
        params = {"limit": 1000}
        
        if ticker:
            params["ticker"] = ticker
        if since:
            params["ex_dividend_date.gte"] = since
        
        return self.client.get_with_pagination(endpoint, params)
    
    def get_splits(self, ticker: str = None, since: str = "2020-01-01") -> List[Dict]:
        """Fetch stock split history"""
        endpoint = "/v3/reference/splits"
        params = {"limit": 1000}
        
        if ticker:
            params["ticker"] = ticker
        if since:
            params["execution_date.gte"] = since
        
        return self.client.get_with_pagination(endpoint, params)
```
- [ ] Test dividend fetching for major stocks
- [ ] Test split fetching (AAPL, TSLA historical splits)

#### Implement Reference Data Module
- [ ] Create `data/ingestion/polygon/reference.py`:
```python
class ReferenceClient:
    def __init__(self, polygon_client):
        self.client = polygon_client
    
    def get_ticker_details(self, ticker: str) -> Dict:
        """Get comprehensive ticker metadata"""
        endpoint = f"/v3/reference/tickers/{ticker}"
        return self.client._get(endpoint)
    
    def get_all_tickers(self, market: str = "stocks", active: bool = True) -> List[Dict]:
        """Get all available tickers"""
        endpoint = "/v3/reference/tickers"
        params = {
            "market": market,
            "active": str(active).lower(),
            "limit": 1000
        }
        return self.client.get_with_pagination(endpoint, params)
    
    def get_exchanges(self) -> List[Dict]:
        """Get exchange reference data"""
        endpoint = "/v3/reference/exchanges"
        return self.client._get(endpoint).get('results', [])
```
- [ ] Fetch full ticker list (all US stocks)
- [ ] Fetch exchange metadata
- [ ] Store in reference database

#### Implement News Module
- [ ] Create `data/ingestion/polygon/news.py`:
```python
class NewsClient:
    def __init__(self, polygon_client):
        self.client = polygon_client
    
    def get_news(self, ticker: str = None, limit: int = 50, 
                since: str = None, order: str = "desc") -> List[Dict]:
        """Fetch news articles"""
        endpoint = "/v2/reference/news"
        params = {
            "limit": limit,
            "order": order
        }
        
        if ticker:
            params["ticker"] = ticker
        if since:
            params["published_utc.gte"] = since
        
        return self.client.get_with_pagination(endpoint, params)
```
- [ ] Test news fetching for sample ticker
- [ ] Validate news data structure

#### Implement Technical Indicators Module
- [ ] Create `data/ingestion/polygon/indicators.py`:
```python
class IndicatorsClient:
    def __init__(self, polygon_client):
        self.client = polygon_client
    
    def get_sma(self, ticker: str, timespan: str = "day", 
               window: int = 20, series_type: str = "close") -> Dict:
        """Get Simple Moving Average"""
        endpoint = f"/v1/indicators/sma/{ticker}"
        params = {
            "timespan": timespan,
            "window": window,
            "series_type": series_type,
            "expand_underlying": "true"
        }
        return self.client._get(endpoint, params)
    
    def get_ema(self, ticker: str, timespan: str = "day",
               window: int = 20, series_type: str = "close") -> Dict:
        """Get Exponential Moving Average"""
        endpoint = f"/v1/indicators/ema/{ticker}"
        params = {
            "timespan": timespan,
            "window": window,
            "series_type": series_type,
            "expand_underlying": "true"
        }
        return self.client._get(endpoint, params)
    
    def get_macd(self, ticker: str, timespan: str = "day",
                short_window: int = 12, long_window: int = 26,
                signal_window: int = 9, series_type: str = "close") -> Dict:
        """Get MACD"""
        endpoint = f"/v1/indicators/macd/{ticker}"
        params = {
            "timespan": timespan,
            "short_window": short_window,
            "long_window": long_window,
            "signal_window": signal_window,
            "series_type": series_type,
            "expand_underlying": "true"
        }
        return self.client._get(endpoint, params)
    
    def get_rsi(self, ticker: str, timespan: str = "day",
               window: int = 14, series_type: str = "close") -> Dict:
        """Get RSI"""
        endpoint = f"/v1/indicators/rsi/{ticker}"
        params = {
            "timespan": timespan,
            "window": window,
            "series_type": series_type,
            "expand_underlying": "true"
        }
        return self.client._get(endpoint, params)
```
- [ ] Test each indicator type
- [ ] Validate indicator calculations

#### Implement WebSocket Client
- [ ] Create `data/ingestion/polygon/websocket_client.py`:
```python
import json
import websocket
import threading
from typing import Callable, Dict, List
import time

class PolygonWebSocketClient:
    """
    WebSocket client for real-time (delayed) data from Polygon.io
    """
    WS_URL = "wss://socket.polygon.io/stocks"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.ws = None
        self.is_connected = False
        self.subscriptions = []
        self.callbacks = {}
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
    
    def on_open(self, ws):
        """Called when WebSocket connection opens"""
        print("WebSocket connection opened")
        self.is_connected = True
        self.reconnect_attempts = 0
        
        # Authenticate
        auth_message = json.dumps({
            "action": "auth",
            "params": self.api_key
        })
        ws.send(auth_message)
        
        # Resubscribe to previous subscriptions
        if self.subscriptions:
            self.subscribe(self.subscriptions)
    
    def on_message(self, ws, message):
        """Called when WebSocket receives a message"""
        try:
            data = json.loads(message)
            
            # Handle different message types
            if isinstance(data, list):
                for item in data:
                    event_type = item.get('ev')
                    if event_type in self.callbacks:
                        self.callbacks[event_type](item)
            elif isinstance(data, dict):
                # Status messages
                if data.get('status') == 'auth_success':
                    print("WebSocket authenticated successfully")
                elif data.get('status') == 'success':
                    print(f"Subscription successful: {data.get('message', '')}")
        except Exception as e:
            print(f"Error processing message: {e}")
    
    def on_error(self, ws, error):
        """Called when WebSocket encounters an error"""
        print(f"WebSocket error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        """Called when WebSocket connection closes"""
        print(f"WebSocket connection closed: {close_status_code} - {close_msg}")
        self.is_connected = False
        
        # Attempt reconnection
        if self.reconnect_attempts < self.max_reconnect_attempts:
            self.reconnect_attempts += 1
            wait_time = 2 ** self.reconnect_attempts  # Exponential backoff
            print(f"Reconnecting in {wait_time} seconds... (Attempt {self.reconnect_attempts})")
            time.sleep(wait_time)
            self.connect()
        else:
            print("Max reconnection attempts reached")
    
    def connect(self):
        """Establish WebSocket connection"""
        self.ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        
        # Run in separate thread
        wst = threading.Thread(target=self.ws.run_forever)
        wst.daemon = True
        wst.start()
    
    def subscribe(self, channels: List[str]):
        """
        Subscribe to WebSocket channels
        
        Args:
            channels: List of channels to subscribe to
                     e.g., ['A.AAPL', 'A.TSLA'] for delayed minute aggregates
        """
        if not isinstance(channels, list):
            channels = [channels]
        
        self.subscriptions.extend(channels)
        
        if self.is_connected and self.ws:
            subscribe_message = json.dumps({
                "action": "subscribe",
                "params": ",".join(channels)
            })
            self.ws.send(subscribe_message)
            print(f"Subscribed to: {', '.join(channels)}")
    
    def unsubscribe(self, channels: List[str]):
        """Unsubscribe from WebSocket channels"""
        if not isinstance(channels, list):
            channels = [channels]
        
        for channel in channels:
            if channel in self.subscriptions:
                self.subscriptions.remove(channel)
        
        if self.is_connected and self.ws:
            unsubscribe_message = json.dumps({
                "action": "unsubscribe",
                "params": ",".join(channels)
            })
            self.ws.send(unsubscribe_message)
            print(f"Unsubscribed from: {', '.join(channels)}")
    
    def register_callback(self, event_type: str, callback: Callable):
        """
        Register callback for specific event types
        
        Args:
            event_type: Event type (e.g., 'A' for aggregates, 'T' for trades)
            callback: Function to call when event is received
        """
        self.callbacks[event_type] = callback
    
    def close(self):
        """Close WebSocket connection"""
        if self.ws:
            self.ws.close()
            self.is_connected = False

# Example usage
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    api_key = os.getenv("POLYGON_API_KEY")
    
    # Create WebSocket client
    ws_client = PolygonWebSocketClient(api_key)
    
    # Define callback for aggregate data
    def on_aggregate(data):
        print(f"Aggregate: {data.get('sym')} - Open: {data.get('o')}, Close: {data.get('c')}, Volume: {data.get('v')}")
    
    # Register callback
    ws_client.register_callback('A', on_aggregate)
    
    # Connect and subscribe
    ws_client.connect()
    time.sleep(2)  # Wait for connection
    ws_client.subscribe(['A.AAPL', 'A.TSLA', 'A.NVDA'])
    
    # Keep alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Closing WebSocket connection...")
        ws_client.close()
```
- [ ] Test WebSocket connection
- [ ] Test subscribing to multiple tickers
- [ ] Test reconnection logic (simulate disconnection)
- [ ] Validate data format from WebSocket

#### Implement Snapshot API
- [ ] Add snapshot methods to `data/ingestion/polygon/aggregates.py` or create separate file:
```python
class SnapshotClient:
    def __init__(self, polygon_client):
        self.client = polygon_client
    
    def get_ticker_snapshot(self, ticker: str) -> Dict:
        """
        Get current snapshot for a ticker (delayed ~15 min on Starter)
        Includes: last trade, last quote, previous day stats, today's stats
        
        Returns:
            Dict with keys: ticker, day (OHLCV), lastTrade, lastQuote, min, prevDay
        """
        endpoint = f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
        data = self.client._get(endpoint)
        
        if 'ticker' in data:
            return data['ticker']
        return data
    
    def get_all_tickers_snapshot(self) -> List[Dict]:
        """
        Get snapshots for all tickers (use sparingly - returns lots of data)
        """
        endpoint = "/v2/snapshot/locale/us/markets/stocks/tickers"
        data = self.client._get(endpoint)
        return data.get('tickers', [])
    
    def get_gainers_losers(self, direction: str = "gainers") -> List[Dict]:
        """
        Get top gainers or losers
        
        Args:
            direction: 'gainers' or 'losers'
        """
        endpoint = f"/v2/snapshot/locale/us/markets/stocks/{direction}"
        data = self.client._get(endpoint)
        return data.get('tickers', [])
```
- [ ] Test snapshot API for single ticker
- [ ] Test gainers/losers endpoints
- [ ] Document snapshot data structure

### 0.4 Flat File Download Strategy

#### Flat File Handler Implementation
- [ ] Create `data/ingestion/polygon/flat_files.py`:
```python
import aiohttp
import asyncio
from pathlib import Path
from typing import List
import hashlib

class FlatFileDownloader:
    """
    Efficiently download Polygon.io flat files (bulk historical data)
    """
    def __init__(self, api_key: str, download_dir: str = "./data/flat_files"):
        self.api_key = api_key
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.concurrent_downloads = 6  # Parallel downloads
    
    async def download_file(self, session: aiohttp.ClientSession, 
                          url: str, output_path: Path) -> bool:
        """Download single file with retry logic"""
        for attempt in range(3):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as response:
                    if response.status == 200:
                        # Stream to disk to avoid RAM spikes
                        with open(output_path, 'wb') as f:
                            async for chunk in response.content.iter_chunked(8192):
                                f.write(chunk)
                        return True
                    elif response.status == 429:
                        # Rate limited, exponential backoff
                        await asyncio.sleep(2 ** attempt)
                    else:
                        print(f"Error {response.status} for {url}")
                        return False
            except Exception as e:
                print(f"Download failed (attempt {attempt+1}): {e}")
                await asyncio.sleep(2 ** attempt)
        
        return False
    
    async def download_batch(self, file_urls: List[str]) -> List[Path]:
        """Download multiple files in parallel"""
        connector = aiohttp.TCPConnector(limit=self.concurrent_downloads)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = []
            
            for url in file_urls:
                filename = url.split('/')[-1]
                output_path = self.download_dir / filename
                
                if output_path.exists():
                    print(f"Skipping existing file: {filename}")
                    continue
                
                task = self.download_file(session, url, output_path)
                tasks.append(task)
            
            results = await asyncio.gather(*tasks)
            return [self.download_dir / url.split('/')[-1] for url in file_urls if results[file_urls.index(url)]]
    
    def verify_file_integrity(self, filepath: Path, expected_sha: str = None) -> bool:
        """Verify file integrity with SHA256"""
        if not filepath.exists():
            return False
        
        if expected_sha:
            sha256 = hashlib.sha256()
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256.update(chunk)
            
            return sha256.hexdigest() == expected_sha
        
        return True  # If no expected SHA, just check existence
```
- [ ] Test single file download
- [ ] Test parallel download (6 concurrent)
- [ ] Test resumable downloads
- [ ] Validate file integrity checks

#### Flat File Unpacking & Parsing
- [ ] Create `data/ingestion/polygon/flat_file_parser.py`:
```python
import gzip
import json
import csv
from pathlib import Path
from typing import Iterator, Dict

class FlatFileParser:
    """
    Parse and extract data from Polygon flat files
    """
    
    @staticmethod
    def parse_trades_file(filepath: Path) -> Iterator[Dict]:
        """Parse trades flat file (compressed JSON/CSV)"""
        if filepath.suffix == '.gz':
            with gzip.open(filepath, 'rt') as f:
                if filepath.stem.endswith('.json'):
                    for line in f:
                        yield json.loads(line)
                elif filepath.stem.endswith('.csv'):
                    reader = csv.DictReader(f)
                    for row in reader:
                        yield row
        else:
            with open(filepath, 'r') as f:
                if filepath.suffix == '.json':
                    for line in f:
                        yield json.loads(line)
                elif filepath.suffix == '.csv':
                    reader = csv.DictReader(f)
                    for row in reader:
                        yield row
    
    @staticmethod
    def parse_aggregates_file(filepath: Path) -> Iterator[Dict]:
        """Parse aggregates flat file"""
        # Similar to trades but for OHLCV bars
        return FlatFileParser.parse_trades_file(filepath)
    
    @staticmethod
    def batch_records(records: Iterator[Dict], batch_size: int = 5000) -> Iterator[List[Dict]]:
        """Batch records for efficient database insertion"""
        batch = []
        for record in records:
            batch.append(record)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        
        if batch:
            yield batch
```
- [ ] Test parsing compressed files
- [ ] Test batching for DB insertion
- [ ] Validate data integrity after parsing

### 0.5 Database Writer Implementation

#### InfluxDB Writer
- [ ] Create `data/storage/influx_writer.py`:
```python
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS, ASYNCHRONOUS
from typing import List, Dict
from datetime import datetime

class InfluxWriter:
    def __init__(self, url: str, token: str, org: str, bucket: str):
        self.client = InfluxDBClient(url=url, token=token, org=org)
        self.write_api = self.client.write_api(write_options=ASYNCHRONOUS)
        self.bucket = bucket
        self.org = org
    
    def write_ohlcv_bars(self, ticker: str, bars: List[Dict], timeframe: str = "1min"):
        """
        Write OHLCV bars to InfluxDB
        
        Args:
            ticker: Stock symbol
            bars: List of bar dicts with keys: t, o, h, l, c, v, vw, n
            timeframe: Bar timeframe (1min, 5min, 1day, etc.)
        """
        points = []
        
        for bar in bars:
            point = Point("market_data") \
                .tag("ticker", ticker) \
                .tag("timeframe", timeframe) \
                .field("open", float(bar['o'])) \
                .field("high", float(bar['h'])) \
                .field("low", float(bar['l'])) \
                .field("close", float(bar['c'])) \
                .field("volume", float(bar['v'])) \
                .time(bar['t'], WritePrecision.MS)  # Epoch milliseconds
            
            if 'vw' in bar:
                point.field("vwap", float(bar['vw']))
            if 'n' in bar:
                point.field("transactions", int(bar['n']))
            
            points.append(point)
        
        # Batch write for efficiency
        self.write_api.write(bucket=self.bucket, org=self.org, record=points)
    
    def write_corporate_actions(self, ticker: str, actions: List[Dict], action_type: str):
        """Write dividends or splits"""
        points = []
        
        for action in actions:
            point = Point("corporate_actions") \
                .tag("ticker", ticker) \
                .tag("action_type", action_type)
            
            if action_type == "dividend":
                point.field("amount", float(action.get('cash_amount', 0)))
                point.field("declaration_date", action.get('declaration_date', ''))
                point.field("ex_date", action.get('ex_dividend_date', ''))
                point.field("pay_date", action.get('pay_date', ''))
                timestamp = action.get('ex_dividend_date')
            elif action_type == "split":
                point.field("ratio", float(action.get('split_from', 1) / action.get('split_to', 1)))
                point.field("execution_date", action.get('execution_date', ''))
                timestamp = action.get('execution_date')
            
            # Convert date string to timestamp
            if timestamp:
                ts = datetime.strptime(timestamp, '%Y-%m-%d')
                point.time(ts, WritePrecision.S)
                points.append(point)
        
        self.write_api.write(bucket=self.bucket, org=self.org, record=points)
    
    def write_reference_data(self, ticker: str, metadata: Dict):
        """Write ticker metadata"""
        point = Point("reference") \
            .tag("ticker", ticker) \
            .field("name", metadata.get('name', '')) \
            .field("market", metadata.get('market', '')) \
            .field("locale", metadata.get('locale', '')) \
            .field("type", metadata.get('type', '')) \
            .field("active", metadata.get('active', True)) \
            .field("currency", metadata.get('currency_name', '')) \
            .field("exchange", metadata.get('primary_exchange', '')) \
            .field("sector", metadata.get('sic_description', '')) \
            .time(datetime.utcnow(), WritePrecision.S)
        
        self.write_api.write(bucket=self.bucket, org=self.org, record=point)
    
    def write_news(self, articles: List[Dict]):
        """Write news articles"""
        points = []
        
        for article in articles:
            tickers = article.get('tickers', [])
            if not tickers:
                continue
            
            for ticker in tickers:
                point = Point("news") \
                    .tag("ticker", ticker) \
                    .tag("publisher", article.get('publisher', {}).get('name', '')) \
                    .field("title", article.get('title', '')) \
                    .field("author", article.get('author', '')) \
                    .field("article_url", article.get('article_url', '')) \
                    .field("tickers_list", ','.join(tickers)) \
                    .field("keywords", ','.join(article.get('keywords', [])))
                
                # Timestamp from article
                published = article.get('published_utc')
                if published:
                    ts = datetime.strptime(published.replace('Z', '+00:00'), '%Y-%m-%dT%H:%M:%S%z')
                    point.time(ts, WritePrecision.S)
                    points.append(point)
        
        if points:
            self.write_api.write(bucket=self.bucket, org=self.org, record=points)
    
    def close(self):
        """Close client connection"""
        self.write_api.close()
        self.client.close()
```
- [ ] Test OHLCV writing (1000 bars)
- [ ] Test corporate actions writing
- [ ] Test reference data writing
- [ ] Test news writing
- [ ] Benchmark write throughput (target: 10k+ points/sec)

### 0.6 Historical Data Backfill Orchestrator

#### Master Backfill Script
- [ ] Create `scripts/backfill_historical_data.py`:
```python
import asyncio
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from data.ingestion.polygon.client import PolygonClient
from data.ingestion.polygon.aggregates import AggregatesClient
from data.ingestion.polygon.corporate_actions import CorporateActionsClient
from data.ingestion.polygon.reference import ReferenceClient
from data.ingestion.polygon.news import NewsClient
from data.storage.influx_writer import InfluxWriter

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class HistoricalDataBackfill:
    """
    Orchestrate complete historical data backfill from Polygon.io
    """
    
    def __init__(self, polygon_api_key: str, influx_url: str, influx_token: str, influx_org: str):
        self.polygon = PolygonClient(polygon_api_key)
        self.agg_client = AggregatesClient(self.polygon)
        self.corp_client = CorporateActionsClient(self.polygon)
        self.ref_client = ReferenceClient(self.polygon)
        self.news_client = NewsClient(self.polygon)
        
        self.influx_market = InfluxWriter(influx_url, influx_token, influx_org, "market_data_5y")
        self.influx_corp = InfluxWriter(influx_url, influx_token, influx_org, "corporate_actions")
        self.influx_ref = InfluxWriter(influx_url, influx_token, influx_org, "reference_data")
        self.influx_news = InfluxWriter(influx_url, influx_token, influx_org, "news_6mo")
    
    def get_target_tickers(self, limit: int = None) -> List[str]:
        """
        Get list of tickers to backfill
        Priority: Large cap stocks + popular cryptos
        """
        logger.info("Fetching active tickers...")
        all_tickers = self.ref_client.get_all_tickers(market="stocks", active=True)
        
        # Extract ticker symbols
        tickers = [t['ticker'] for t in all_tickers if 'ticker' in t]
        
        # Priority tickers (major indices, large caps)
        priority = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 
                   'META', 'BRK.B', 'JPM', 'V', 'JNJ', 'WMT', 'PG', 'MA', 'UNH', 'HD']
        
        # Move priority to front
        tickers = priority + [t for t in tickers if t not in priority]
        
        if limit:
            tickers = tickers[:limit]
        
        logger.info(f"Target tickers: {len(tickers)}")
        return tickers
    
    def backfill_ticker_daily(self, ticker: str, start_date: str, end_date: str) -> bool:
        """Backfill daily bars for one ticker"""
        try:
            logger.info(f"Backfilling daily bars: {ticker}")
            bars = self.agg_client.get_daily_bars(ticker, start_date, end_date, adjusted=True)
            
            if bars:
                self.influx_market.write_ohlcv_bars(ticker, bars, timeframe="1day")
                logger.info(f"✓ {ticker}: {len(bars)} daily bars")
                return True
            else:
                logger.warning(f"✗ {ticker}: No daily bars found")
                return False
        except Exception as e:
            logger.error(f"✗ {ticker} daily bars failed: {e}")
            return False
    
    def backfill_ticker_minute(self, ticker: str, start_date: str, end_date: str) -> bool:
        """Backfill minute bars for one ticker (chunked to avoid timeouts)"""
        try:
            logger.info(f"Backfilling minute bars: {ticker}")
            
            # Split into weekly chunks (7 days ~= 2500 minute bars, well under 50k limit)
            chunks = self.agg_client.chunk_date_range(start_date, end_date, chunk_days=7)
            total_bars = 0
            
            for chunk_start, chunk_end in tqdm(chunks, desc=f"{ticker} minute", leave=False):
                bars = self.agg_client.get_minute_bars(ticker, chunk_start, chunk_end, adjusted=True)
                
                if bars:
                    self.influx_market.write_ohlcv_bars(ticker, bars, timeframe="1min")
                    total_bars += len(bars)
                
                # Small delay to avoid hammering API
                await asyncio.sleep(0.1)
            
            logger.info(f"✓ {ticker}: {total_bars} minute bars")
            return True
        except Exception as e:
            logger.error(f"✗ {ticker} minute bars failed: {e}")
            return False
    
    def backfill_corporate_actions(self, ticker: str, since: str = "2020-01-01") -> bool:
        """Backfill dividends and splits"""
        try:
            # Dividends
            dividends = self.corp_client.get_dividends(ticker=ticker, since=since)
            if dividends:
                self.influx_corp.write_corporate_actions(ticker, dividends, "dividend")
                logger.info(f"✓ {ticker}: {len(dividends)} dividends")
            
            # Splits
            splits = self.corp_client.get_splits(ticker=ticker, since=since)
            if splits:
                self.influx_corp.write_corporate_actions(ticker, splits, "split")
                logger.info(f"✓ {ticker}: {len(splits)} splits")
            
            return True
        except Exception as e:
            logger.error(f"✗ {ticker} corporate actions failed: {e}")
            return False
    
    def backfill_reference_data(self, ticker: str) -> bool:
        """Backfill ticker metadata"""
        try:
            details = self.ref_client.get_ticker_details(ticker)
            if 'results' in details:
                metadata = details['results']
                self.influx_ref.write_reference_data(ticker, metadata)
                logger.info(f"✓ {ticker}: metadata stored")
                return True
        except Exception as e:
            logger.error(f"✗ {ticker} reference data failed: {e}")
            return False
    
    def backfill_news(self, ticker: str, since: str = None, limit: int = 100) -> bool:
        """Backfill recent news (last 6 months)"""
        try:
            if not since:
                # Default: last 6 months
                since = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
            
            articles = self.news_client.get_news(ticker=ticker, since=since, limit=limit)
            if articles:
                self.influx_news.write_news(articles)
                logger.info(f"✓ {ticker}: {len(articles)} news articles")
                return True
        except Exception as e:
            logger.error(f"✗ {ticker} news failed: {e}")
            return False
    
    def backfill_all(self, tickers: List[str], start_date: str, end_date: str, 
                    include_minute: bool = True, max_workers: int = 4):
        """
        Master backfill function - orchestrate all data collection
        
        Args:
            tickers: List of ticker symbols
            start_date: Start date 'YYYY-MM-DD' (5 years ago)
            end_date: End date 'YYYY-MM-DD' (today)
            include_minute: Whether to fetch minute bars (time-consuming)
            max_workers: Parallel workers for ticker processing
        """
        logger.info(f"Starting historical backfill: {len(tickers)} tickers")
        logger.info(f"Date range: {start_date} to {end_date}")
        logger.info(f"Include minute bars: {include_minute}")
        
        # Phase 1: Reference data (fast, sequential)
        logger.info("\n=== Phase 1: Reference Data ===")
        for ticker in tqdm(tickers, desc="Reference data"):
            self.backfill_reference_data(ticker)
        
        # Phase 2: Corporate actions (fast, parallel)
        logger.info("\n=== Phase 2: Corporate Actions ===")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.backfill_corporate_actions, ticker, start_date): ticker 
                      for ticker in tickers}
            for future in tqdm(as_completed(futures), total=len(tickers), desc="Corporate actions"):
                pass
        
        # Phase 3: Daily bars (medium speed, parallel)
        logger.info("\n=== Phase 3: Daily Bars ===")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.backfill_ticker_daily, ticker, start_date, end_date): ticker 
                      for ticker in tickers}
            for future in tqdm(as_completed(futures), total=len(tickers), desc="Daily bars"):
                pass
        
        # Phase 4: Minute bars (very slow, parallel with lower concurrency)
        if include_minute:
            logger.info("\n=== Phase 4: Minute Bars (This will take a while...) ===")
            with ThreadPoolExecutor(max_workers=2) as executor:  # Lower concurrency for minute data
                futures = {executor.submit(self.backfill_ticker_minute, ticker, start_date, end_date): ticker 
                          for ticker in tickers}
                for future in tqdm(as_completed(futures), total=len(tickers), desc="Minute bars"):
                    pass
        
        # Phase 5: News (last 6 months, parallel)
        logger.info("\n=== Phase 5: Recent News ===")
        news_start = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.backfill_news, ticker, news_start, 100): ticker 
                      for ticker in tickers}
            for future in tqdm(as_completed(futures), total=len(tickers), desc="News"):
                pass
        
        logger.info("\n=== Backfill Complete! ===")
        
        # Cleanup
        self.influx_market.close()
        self.influx_corp.close()
        self.influx_ref.close()
        self.influx_news.close()

# Main execution
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    # Configuration
    POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
    INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
    INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
    INFLUX_ORG = os.getenv("INFLUX_ORG", "chronox")
    
    # Date range: 5 years
    END_DATE = datetime.now().strftime('%Y-%m-%d')
    START_DATE = (datetime.now() - timedelta(days=5*365)).strftime('%Y-%m-%d')
    
    # Initialize backfill
    backfill = HistoricalDataBackfill(POLYGON_API_KEY, INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG)
    
    # Get top 50 tickers for initial backfill (expand later)
    tickers = backfill.get_target_tickers(limit=50)
    
    # Run backfill
    backfill.backfill_all(
        tickers=tickers,
        start_date=START_DATE,
        end_date=END_DATE,
        include_minute=True,  # Set False for faster initial test
        max_workers=4
    )
```
- [ ] Test backfill script with 1 ticker (end-to-end)
- [ ] Test backfill script with 5 tickers
- [ ] Run full backfill for top 50 tickers
- [ ] Monitor InfluxDB disk usage and query performance
- [ ] Verify data completeness (no gaps in date ranges)

### 0.7 Data Verification & Quality Checks
- [ ] Create `scripts/verify_data_quality.py`:
  - [ ] Check for date gaps in OHLCV data
  - [ ] Validate data completeness (expected bars per ticker)
  - [ ] Check for anomalies (zero volumes, price spikes)
  - [ ] Verify corporate actions are recorded
  - [ ] Validate reference data completeness
- [ ] Run verification script after backfill
- [ ] Document any data quality issues
- [ ] Create data quality dashboard in Grafana

### 0.8 Flat File Integration (Optional Bulk Download)
- [ ] Identify available flat file endpoints from Polygon docs
- [ ] Implement flat file discovery (list available files)
- [ ] Download flat files using async downloader
- [ ] Unpack and parse flat files
- [ ] Bulk insert into InfluxDB
- [ ] Compare flat file data vs API data (validate consistency)
- [ ] Use flat files for initial bulk load if API too slow

### 0.9 Environment Configuration
- [ ] Create `.env.example` template:
```bash
# Polygon.io
POLYGON_API_KEY=your_api_key_here

# InfluxDB
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=your_influx_token_here
INFLUX_ORG=chronox
INFLUX_BUCKET_MARKET=market_data_5y
INFLUX_BUCKET_CORP=corporate_actions
INFLUX_BUCKET_REF=reference_data
INFLUX_BUCKET_NEWS=news_6mo

# Trading (for later phases)
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```
- [ ] Document all environment variables in README
- [ ] Add `.env` to `.gitignore`

---

## Phase 1: Foundation & Infrastructure (Weeks 2-4)

### 1.1 Project Structure Setup
- [ ] Create root directory structure as per component architecture
  - [ ] `data/` (ingestion, preprocessing, storage)
  - [ ] `models/` (transformers, rl_agents, ensembles)
  - [ ] `training/` (train.py, evaluate.py, hyperparameter_tuning.py)
  - [ ] `inference/` (predictor.py, signal_generator.py)
  - [ ] `backtesting/` (strategies.py, analysis.py)
  - [ ] `trading/` (paper_trading.py, live_trading.py)
  - [ ] `monitoring/` (drift_detection.py, alerting.py)
  - [ ] `pipelines/` (airflow_dags/, kubeflow_pipelines/)
  - [ ] `docker/` (Dockerfiles, docker-compose.yml)
  - [ ] `tests/` (unit/, integration/)
  - [ ] `configs/` (YAML configs)
  - [ ] `notebooks/` (exploratory analysis)

### 1.2 Version Control & Git
- [ ] Initialize Git repository
- [ ] Create `.gitignore` (exclude API keys, models, data, .env)
- [ ] Set up branching strategy (main, develop, feature/*, hotfix/*)
- [ ] Create initial commit with structure
- [ ] Set up GitHub remote repository
- [ ] Configure branch protection rules (main requires PR + review)

### 1.3 Environment & Dependencies
- [ ] Create Python virtual environment (Python 3.9+)
- [ ] Create `requirements.txt` with core dependencies:
  - [ ] TensorFlow 2.11+ / PyTorch 2.0+
  - [ ] Ray RLlib / Stable Baselines3
  - [ ] pandas, numpy, scipy
  - [ ] influxdb-client
  - [ ] polygon-api-client
  - [ ] transformers (Hugging Face)
  - [ ] backtrader / backtesting.py
  - [ ] prometheus-client, grafana
  - [ ] pytest, pytest-cov
  - [ ] black, pylint, flake8
- [ ] Document installation instructions in `README.md`
- [ ] Test environment on local hardware (GPU detection)

### 1.4 Configuration Management
- [ ] Create `configs/data_config.yaml`:
  - [ ] Polygon.io API credentials (environment variables)
  - [ ] InfluxDB connection settings
  - [ ] Data sources (stocks list, crypto list)
  - [ ] Timeframes (5min, 15min, 1hr, 12hr)
- [ ] Create `configs/model_config.yaml`:
  - [ ] Transformer architecture params
  - [ ] Training hyperparameters (lr, batch_size, epochs)
  - [ ] Regularization settings (dropout, L2)
  - [ ] Data split ratios (70/15/15)
- [ ] Create `configs/deployment_config.yaml`:
  - [ ] Environment (local/cloud)
  - [ ] Monitoring settings
  - [ ] Alert thresholds
- [ ] Implement config loader utility (`utils/config_loader.py`)

### 1.5 Logging & Error Handling
- [ ] Set up centralized logging framework (Python logging module)
- [ ] Create log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- [ ] Implement log rotation (daily, max 100MB per file)
- [ ] Create custom exception classes:
  - [ ] `DataFetchError`
  - [ ] `ModelPredictionError`
  - [ ] `TradingExecutionError`
  - [ ] `ComplianceError`
- [ ] Implement global exception handler
- [ ] Add structured logging (JSON format for easy parsing)

### 1.6 Testing Framework
- [ ] Set up pytest configuration (`pytest.ini`)
- [ ] Create test utilities (fixtures, mocks)
- [ ] Write sample unit tests for utilities
- [ ] Set up code coverage tracking (pytest-cov)
- [ ] Configure CI for automated test runs (GitHub Actions)
- [ ] Set coverage threshold (minimum 70%)

---

## Phase 2: Data Pipeline (Weeks 1-4 Parallel)

### 2.1 Data Source Integration

#### Polygon.io API Client
- [ ] Implement `data/ingestion/polygon_client.py`:
  - [ ] Authentication with API key
  - [ ] Fetch OHLCV data (stocks + crypto)
  - [ ] Fetch real-time quotes
  - [ ] Implement rate limiting (5 req/min for free tier)
  - [ ] Add retry logic with exponential backoff
  - [ ] Handle API errors gracefully
- [ ] Test API client with sample symbols (AAPL, BTC-USD)
- [ ] Validate data schema (timestamp, open, high, low, close, volume)

#### Event Feed Integration
- [ ] Research and select event feed provider(s):
  - [ ] Financial news APIs (NewsAPI, Benzinga, Alpha Vantage)
  - [ ] Macro event calendars (economic indicators, Fed decisions)
  - [ ] Crypto-specific feeds (CoinGecko, CoinMarketCap events)
- [ ] Implement `data/ingestion/event_client.py`:
  - [ ] Fetch real-time news headlines
  - [ ] Fetch macro event calendar
  - [ ] Fetch crypto-specific events (listings, upgrades, regulatory)
  - [ ] Parse and normalize event data
- [ ] Test event fetching and parsing

#### Sentiment Data
- [ ] Set up FinBERT model loading (Hugging Face)
- [ ] Implement `data/ingestion/sentiment_client.py`:
  - [ ] Fetch news articles per asset
  - [ ] Process through FinBERT
  - [ ] Generate sentiment scores [-1, 1]
  - [ ] Aggregate sentiment by asset and timeframe
- [ ] Test sentiment analysis on sample news

### 2.2 Local Data Caching
- [ ] Implement `data/storage/cache_manager.py`:
  - [ ] Cache OHLCV data locally (SQLite or HDF5)
  - [ ] Cache news and sentiment data
  - [ ] Implement TTL (Time To Live) for cached data
  - [ ] Add cache invalidation logic
  - [ ] Implement fallback to cache on API failure
- [ ] Test cache read/write performance
- [ ] Measure cache hit rate

### 2.3 InfluxDB Setup
- [ ] Install InfluxDB locally (Docker recommended)
- [ ] Create database schema:
  - [ ] `market_data` bucket (OHLCV time-series)
  - [ ] `indicators` bucket (calculated indicators)
  - [ ] `sentiments` bucket (sentiment scores)
  - [ ] `events` bucket (event data)
  - [ ] `predictions` bucket (model outputs)
  - [ ] `trades` bucket (executed trades)
- [ ] Implement `data/storage/influx_client.py`:
  - [ ] Write methods for each data type
  - [ ] Query methods with time range filters
  - [ ] Batch write optimization
  - [ ] Connection pooling
- [ ] Test write throughput (target: >10k points/sec)
- [ ] Set up retention policies (e.g., 2 years for raw data)

### 2.4 Data Preprocessing
- [ ] Implement `data/preprocessing/feature_engineering.py`:
  - [ ] OHLCV normalization (z-score, min-max)
  - [ ] Calculate technical indicators:
    - [ ] MACD (12, 26, 9)
    - [ ] RSI (14)
    - [ ] Moving Averages (20, 50, 200)
    - [ ] Bollinger Bands (20, 2)
    - [ ] Volume indicators
    - [ ] Stochastic Oscillator
    - [ ] ADX (Average Directional Index)
    - [ ] OBV (On-Balance Volume)
    - [ ] ATR (Average True Range)
    - [ ] MFI (Money Flow Index)
    - [ ] Fibonacci retracement levels
    - [ ] Fibonacci diagonal channels
  - [ ] Multi-timeframe feature alignment
  - [ ] Handle missing data (forward fill, interpolation)
  - [ ] Outlier detection and capping

### 2.4.1 Multi-Resolution Aggregation Logic **[NEW - ADVANCED]**

> **🎯 OBJECTIVE:** Maintain synchronized time-series aggregates across 1min, 15min, 1hour, 12hour, and 1day resolutions with continuous cross-scale statistics for adaptive multi-timescale modeling.

**Integration Point:** This feeds into Phase 3.5 (Adaptive Multi-Timescale Indicator Evaluation)

- [ ] Implement `data/preprocessing/multi_resolution_pipeline.py`:
  ```python
  class MultiResolutionPipeline:
      """
      Maintains 5 synchronized timescale datasets with rolling statistics.
      Updates continuously as new 1min bars arrive.
      """
      
      TIMESCALES = {
          '1min': {'window_minutes': 1, 'ma_windows': [5, 10, 20]},
          '15min': {'window_minutes': 15, 'ma_windows': [10, 20, 50]},
          '1hour': {'window_minutes': 60, 'ma_windows': [20, 50, 100]},
          '12hour': {'window_minutes': 720, 'ma_windows': [7, 14, 30]},
          '1day': {'window_minutes': 1440, 'ma_windows': [20, 50, 200]}
      }
      
      def __init__(self, db_writer):
          self.db = db_writer
          self.scales = {}
          self.volatility_cache = {}
          self.divergence_cache = {}
      
      def aggregate_from_base(self, ticker, start_time, end_time):
          """
          Query 1min bars and aggregate up to all timescales.
          Uses SQL window functions for efficiency.
          """
          # Query 1min bars
          query = """
          SELECT 
              time,
              ticker,
              open, high, low, close, volume, vwap
          FROM market_data
          WHERE ticker = %s
            AND timeframe = '1min'
            AND time BETWEEN %s AND %s
          ORDER BY time
          """
          
          base_data = self.db.query(query, (ticker, start_time, end_time))
          
          # Aggregate to each timescale
          for scale, config in self.TIMESCALES.items():
              if scale == '1min':
                  self.scales[scale] = base_data
              else:
                  self.scales[scale] = self._resample_ohlcv(
                      base_data, 
                      freq=f"{config['window_minutes']}T"
                  )
          
          return self.scales
      
      def _resample_ohlcv(self, df, freq):
          """
          Resample OHLCV using proper aggregation:
              open: first, high: max, low: min, close: last, volume: sum
          """
          import pandas as pd
          df['time'] = pd.to_datetime(df['time'])
          df.set_index('time', inplace=True)
          
          resampled = df.resample(freq).agg({
              'open': 'first',
              'high': 'max',
              'low': 'min',
              'close': 'last',
              'volume': 'sum',
              'vwap': 'mean'
          }).dropna()
          
          return resampled
      
      def compute_indicators_all_scales(self, ticker):
          """
          Compute technical indicators with scale-appropriate windows.
          Store back to indicators table.
          """
          for scale, config in self.TIMESCALES.items():
              df = self.scales[scale]
              
              # Moving averages (multiple windows)
              for window in config['ma_windows']:
                  df[f'MA_{window}'] = df['close'].rolling(window).mean()
                  df[f'EMA_{window}'] = df['close'].ewm(span=window).mean()
              
              # RSI
              df['RSI_14'] = self._compute_rsi(df['close'], 14)
              
              # MACD
              df['MACD'], df['MACD_signal'], df['MACD_hist'] = self._compute_macd(df['close'])
              
              # Bollinger Bands
              df['BB_upper'], df['BB_middle'], df['BB_lower'] = self._compute_bollinger(df['close'], 20, 2)
              
              # ATR (volatility)
              df['ATR_14'] = self._compute_atr(df, 14)
              
              # Store to database
              self._write_indicators_to_db(ticker, scale, df)
      
      def compute_realized_volatility(self, scale, window=20):
          """
          Rolling realized volatility: sqrt(sum(log_returns^2) / window)
          """
          df = self.scales[scale]
          log_returns = np.log(df['close'] / df['close'].shift(1))
          realized_vol = np.sqrt((log_returns ** 2).rolling(window).sum() / window)
          
          self.volatility_cache[scale] = realized_vol
          return realized_vol
      
      def detect_trend_divergence_between_scales(self, fast_scale='1min', slow_scale='1hour'):
          """
          Detect when fast MA direction disagrees with slow MA direction.
          
          Returns:
              divergence_flag: pd.Series of bool, True when diverging
          """
          fast_df = self.scales[fast_scale]
          slow_df = self.scales[slow_scale]
          
          # Use fastest MA per scale
          fast_ma = fast_df[f"MA_{self.TIMESCALES[fast_scale]['ma_windows'][0]}"]
          slow_ma = slow_df[f"MA_{self.TIMESCALES[slow_scale]['ma_windows'][0]}"]
          
          # Align on common timestamps (resample fast to slow frequency)
          fast_ma_resampled = fast_ma.resample(slow_df.index.freq).last()
          
          # Trend direction: 1 if MA > price, -1 if MA < price
          fast_trend = np.sign(fast_ma_resampled - fast_df['close'].resample(slow_df.index.freq).last())
          slow_trend = np.sign(slow_ma - slow_df['close'])
          
          # Divergence when signs differ
          divergence = (fast_trend != slow_trend)
          
          self.divergence_cache[(fast_scale, slow_scale)] = divergence
          return divergence
  ```

- [ ] Implement cross-correlation computation (see Phase 3.5.4 for full implementation):
  - [ ] Rolling Pearson correlation with lag sweep
  - [ ] Correlation matrix [5x5] for all scale pairs
  - [ ] Lag matrix [5x5] for optimal lags per pair
  - [ ] Update every 5 minutes, store in TimescaleDB

- [ ] Create TimescaleDB table for cross-scale statistics:
  ```sql
  CREATE TABLE IF NOT EXISTS cross_scale_stats (
      time TIMESTAMPTZ NOT NULL,
      ticker TEXT NOT NULL,
      scale_a TEXT NOT NULL,
      scale_b TEXT NOT NULL,
      correlation DOUBLE PRECISION,
      optimal_lag INTEGER,
      divergence_flag BOOLEAN,
      vol_ratio DOUBLE PRECISION  -- vol_scale_a / vol_scale_b
  );
  
  SELECT create_hypertable('cross_scale_stats', 'time', if_not_exists => TRUE);
  CREATE INDEX idx_cross_scale_ticker ON cross_scale_stats (ticker, time DESC);
  ```

- [ ] Add continuous update scheduler:
  - [ ] Update 1min data: real-time or every 15 minutes (delayed data)
  - [ ] Recompute 15min aggregates: every 15 minutes
  - [ ] Recompute 1hour aggregates: every hour
  - [ ] Recompute 12hour aggregates: every 12 hours
  - [ ] Recompute 1day aggregates: at market close (4:00 PM ET)
  - [ ] Update cross-correlation matrix: every 5 minutes for active tickers

- [ ] Implement volatility regime classifier:
  ```python
  class VolatilityRegimeDetector:
      """
      Classifies current market regime based on realized volatility
      relative to historical average.
      """
      
      REGIMES = {
          'calm': (0, 0.5),
          'normal': (0.5, 1.5),
          'volatile': (1.5, 2.5),
          'crisis': (2.5, float('inf'))
      }
      
      def __init__(self, lookback_days=90):
          self.lookback_days = lookback_days
          self.historical_avg = {}
      
      def update_historical_avg(self, ticker, volatilities):
          """
          Compute rolling 90-day average volatility per scale.
          """
          for scale, vol_series in volatilities.items():
              self.historical_avg[(ticker, scale)] = vol_series.rolling(self.lookback_days).mean()
      
      def classify(self, ticker, scale, current_vol):
          """
          Classify current regime based on vol_ratio = current_vol / historical_avg.
          """
          avg_vol = self.historical_avg.get((ticker, scale))
          if avg_vol is None or avg_vol == 0:
              return 'normal'
          
          vol_ratio = current_vol / avg_vol
          
          for regime, (low, high) in self.REGIMES.items():
              if low <= vol_ratio < high:
                  return regime
          
          return 'normal'
  ```

- [ ] Test multi-resolution pipeline:
  - [ ] Verify 1min → 15min aggregation (OHLC logic correct)
  - [ ] Verify all 5 timescales align on timestamps
  - [ ] Test indicator calculation per scale (window sizes match spec)
  - [ ] Test volatility calculation (rolling window=20)
  - [ ] Test divergence detection (inject synthetic divergence)
  - [ ] Benchmark aggregation speed (<500ms for 1 ticker, 1 day of data)

### 2.5 Data Validation
  - [ ] Schema validation
  - [ ] Timestamp monotonicity check
  - [ ] Price sanity checks (high >= low, volume >= 0)
  - [ ] Spike detection (>50% single-bar move)
  - [ ] Stale data detection (>5 min old)
- [ ] Test preprocessing pipeline end-to-end
- [ ] Benchmark preprocessing speed (target: <100ms per asset)

### 2.5 Data Quality Monitoring
- [ ] Implement data quality metrics:
  - [ ] Completeness (% of expected data points)
  - [ ] Timeliness (lag from market close to ingestion)
  - [ ] Accuracy (cross-validation with alternate sources)
  - [ ] Consistency (schema compliance rate)
- [ ] Create data quality dashboard (Grafana)
- [ ] Set up alerts for quality degradation

### 2.6 Failover & Resilience
- [ ] Implement `data/ingestion/failover_manager.py`:
  - [ ] Primary source: Polygon.io
  - [ ] Fallback sources: Alpha Vantage, Yahoo Finance
  - [ ] Cache fallback when all APIs fail
  - [ ] Automatic source switching on failure
  - [ ] Health check for each data source
- [ ] Test failover scenarios:
  - [ ] API rate limit hit
  - [ ] Network disconnection
  - [ ] Invalid API response
  - [ ] Partial data corruption
- [ ] Implement offline inference mode
- [ ] Test continued operation with stale data

---

## Phase 3: ML Model Development (Weeks 5-10)

### 3.1 Data Preparation for ML

#### Dataset Creation
- [ ] Implement `training/data_loader.py`:
  - [ ] Load historical data from InfluxDB (2020-2024)
  - [ ] Implement time-series train/val/test split (70/15/15)
  - [ ] Ensure no look-ahead bias (chronological split)
  - [ ] Create sliding window sequences (e.g., 60 bars input)
  - [ ] Batch data efficiently for GPU training
- [ ] Create multi-timeframe datasets:
  - [ ] 5-minute bars dataset
  - [ ] 15-minute bars dataset
  - [ ] 1-hour bars dataset
  - [ ] 12-hour bars dataset
- [ ] Implement data augmentation (optional):
  - [ ] Time warping
  - [ ] Magnitude warping
  - [ ] SMOTE for imbalanced signal classes
- [ ] Validate dataset integrity
- [ ] Document dataset statistics (size, class distribution)

### 3.2 Transformer-Based Price Forecasting

#### Stockformer Architecture
- [ ] Research Stockformer/MASTER papers for architecture details
- [ ] Implement `models/transformers/stockformer.py`:
  - [ ] Multi-head self-attention layers (8 heads)
  - [ ] Positional encoding for time-series
  - [ ] Multi-asset attention (cross-asset correlations)
  - [ ] Encoder layers (6 layers, 512 hidden dim)
  - [ ] Decoder for price prediction
  - [ ] Signal classification head (buy/sell/hold)
- [ ] Add regularization:
  - [ ] Dropout (0.2) after each layer
  - [ ] Layer normalization
  - [ ] L2 weight decay (0.01)
- [ ] Implement custom loss function:
  - [ ] Price prediction: Huber loss (robust to outliers)
  - [ ] Signal classification: Focal loss (handle imbalance)
  - [ ] Multi-task loss weighting
- [ ] Test model forward pass on sample data
- [ ] Count trainable parameters (target: 10-50M)

#### Training Pipeline
- [ ] Implement `training/train.py`:
  - [ ] Load config from YAML
  - [ ] Initialize model and optimizer (Adam, lr=1e-4)
  - [ ] Training loop with progress tracking
  - [ ] Validation every N epochs
  - [ ] Early stopping (patience=10)
  - [ ] Model checkpointing (save best model)
  - [ ] Learning rate scheduling (ReduceLROnPlateau)
  - [ ] Mixed precision training (FP16) for speed
- [ ] Implement metrics calculation:
  - [ ] MAE (Mean Absolute Error) for prices
  - [ ] RMSE (Root Mean Squared Error)
  - [ ] MAPE (Mean Absolute Percentage Error)
  - [ ] F1-Score for signal classification
  - [ ] Confusion matrix for signals
- [ ] Set up TensorBoard logging
- [ ] Test training on small dataset (1 week of data)
- [ ] Benchmark training speed (batches/sec)

#### Hyperparameter Tuning
- [ ] Implement `training/hyperparameter_tuning.py`:
  - [ ] Use Ray Tune for distributed tuning
  - [ ] Define search space:
    - [ ] Learning rate: [1e-5, 1e-3]
    - [ ] Batch size: [32, 64, 128]
    - [ ] Dropout: [0.1, 0.3]
    - [ ] Hidden dim: [256, 512, 1024]
    - [ ] Num layers: [4, 6, 8]
  - [ ] Use ASHA scheduler for early stopping
  - [ ] Run 50-100 trials
- [ ] Parallelize tuning on CPU cores (16 cores available)
- [ ] Save best hyperparameters to config
- [ ] Document tuning results

### 3.3 FinBERT Sentiment Analysis

#### Model Integration
- [ ] Implement `models/transformers/finbert_sentiment.py`:
  - [ ] Load pre-trained FinBERT from Hugging Face
  - [ ] Tokenization for financial text
  - [ ] Inference method (batch processing)
  - [ ] Sentiment score extraction [-1, 1]
  - [ ] GPU acceleration for inference
- [ ] Test on sample financial news headlines
- [ ] Validate sentiment accuracy on labeled dataset (if available)
- [ ] Benchmark inference speed (headlines/sec)

#### Event Detection Integration
- [ ] Implement `models/transformers/event_impact_analyzer.py`:
  - [ ] Classify event types (tariff, Fed decision, crypto regulation)
  - [ ] Assess impact score (0-1) based on historical patterns
  - [ ] Identify affected assets/sectors
  - [ ] Estimate impact timeframe (short/medium/long)
  - [ ] Generate volatility spike flags
- [ ] Create event impact database (historical event outcomes)
- [ ] Test event detection on recent news

### 3.4 Multi-Timeframe Feature Integration
- [ ] Implement feature fusion across timeframes:
  - [ ] Align 5-min, 15-min, 1-hr, 12-hr data
  - [ ] Create hierarchical feature vectors
  - [ ] Test timeframe-specific models vs unified model
- [ ] Add Fibonacci indicator calculation per timeframe
- [ ] Validate short-term signal accuracy (5-min, 15-min)

### 3.5 Adaptive Multi-Timescale Indicator Evaluation **[NEW - ADVANCED]**

> **🎯 OBJECTIVE:** Build a hierarchical multi-scale fusion system that progressively evaluates indicators across 1min, 15min, 1hr, 12hr, and 1day timeframes. The system dynamically re-weights timescales based on volatility, trend divergence, and cross-scale correlation strength to improve signal confidence.

**Research Foundation:**
- Temporal Fusion Transformer (TFT) - Lim et al. 2021 ([arXiv:1912.09363](https://arxiv.org/abs/1912.09363))
- Multi-scale CNNs for stock prediction ([arXiv:2107.09441](https://arxiv.org/abs/2107.09441))
- Wavelet multiresolution analysis for stocks ([MDPI](https://www.mdpi.com/2227-7390/9/1/26))
- Hierarchical attention for time series ([SpringerLink](https://link.springer.com/article/10.1007/s10489-021-02533-0))
- Cross-correlation & lag estimation ([Wikipedia](https://en.wikipedia.org/wiki/Cross-correlation))

#### 3.5.1 Multi-Resolution Data Aggregation Pipeline

**Purpose:** Maintain synchronized time-series at multiple resolutions with continuous cross-scale statistics.

- [ ] Implement `data/preprocessing/multi_scale_aggregator.py`:
  ```python
  class MultiScaleAggregator:
      """
      Maintains synchronized OHLCV and indicators across 5 timescales.
      Computes rolling volatility, divergence flags, and cross-correlation metrics.
      """
      TIMESCALES = ['1min', '15min', '1hour', '12hour', '1day']
      
      def __init__(self, db_client):
          self.db = db_client
          self.scale_data = {scale: None for scale in self.TIMESCALES}
          self.volatility = {scale: None for scale in self.TIMESCALES}
          self.divergence_flags = {}
          self.corr_matrix = None
          self.lag_matrix = None
      
      def aggregate_from_minute_bars(self, ticker, start, end):
          """
          Aggregate 1min bars into 15min, 1h, 12h, 1d using FIRST(open), 
          MAX(high), MIN(low), LAST(close), SUM(volume)
          """
          pass
      
      def compute_indicators_per_scale(self, scale):
          """
          Calculate technical indicators with scale-appropriate windows:
          - 1min: MA(5,10,20), RSI(14), MACD(12,26,9), BB(20,2)
          - 15min: MA(10,20,50), RSI(14), MACD(12,26,9), BB(20,2)
          - 1hour: MA(20,50,100), RSI(14), MACD(12,26,9), BB(20,2)
          - 12hour: MA(7,14,30), RSI(14), MACD(12,26,9), BB(20,2)
          - 1day: MA(20,50,200), RSI(14), MACD(12,26,9), BB(20,2)
          """
          pass
      
      def compute_realized_volatility(self, scale, window=20):
          """
          Calculate rolling realized volatility per scale:
          vol = sqrt(sum(log_returns^2) / window)
          Also compute ATR (Average True Range) as alternative measure
          """
          pass
      
      def detect_trend_divergence(self, fast_scale='1min', slow_scale='1hour'):
          """
          Detect divergence between fast and slow MAs across scales:
          divergence_flag = sign(MA_fast - price) != sign(MA_slow - price)
          Returns binary flag per timestamp
          """
          pass
      
      def compute_cross_correlation(self, scale_a, scale_b, max_lag=10, window=50):
          """
          Compute rolling cross-correlation between returns of two scales
          with lag sweep [-max_lag, +max_lag]
          
          Returns:
              corr_strength: float, max |correlation| in lag range
              optimal_lag: int, lag at max correlation
              
          Implementation:
              1. Extract aligned returns for both scales
              2. Normalize to zero mean per window
              3. For each lag in [-max_lag, +max_lag]:
                  corr[lag] = pearson_corr(scale_a_returns, shifted(scale_b_returns, lag))
              4. Return argmax |corr| and its value
          """
          pass
      
      def build_correlation_matrix(self):
          """
          Build full 5x5 correlation matrix + lag matrix for all scale pairs.
          Update continuously (e.g., every 5 minutes)
          """
          pass
  ```

- [ ] Implement continuous aggregation scheduler:
  - [ ] Update 15min aggregates every 15 minutes
  - [ ] Update 1hour aggregates every hour
  - [ ] Update 12hour aggregates every 12 hours
  - [ ] Update 1day aggregates at market close
  - [ ] Keep 1min data always current (real-time or 15-min delayed)

- [ ] Add TimescaleDB continuous aggregates for efficiency:
  ```sql
  -- Already defined in init_timescale_schema.sql:
  -- daily_market_data, hourly_market_data
  
  -- Add 15min aggregate:
  CREATE MATERIALIZED VIEW fifteen_min_market_data
  WITH (timescaledb.continuous) AS
  SELECT
      time_bucket('15 minutes', time) AS bucket,
      ticker,
      FIRST(open, time) AS open,
      MAX(high) AS high,
      MIN(low) AS low,
      LAST(close, time) AS close,
      SUM(volume) AS volume,
      AVG(vwap) AS vwap
  FROM market_data
  WHERE timeframe = '1min'
  GROUP BY bucket, ticker;
  ```

- [ ] Implement volatility regime detection:
  - [ ] Calm: vol < 0.5 * historical_avg
  - [ ] Normal: 0.5 * avg <= vol <= 1.5 * avg
  - [ ] Volatile: 1.5 * avg < vol <= 2.5 * avg
  - [ ] Crisis: vol > 2.5 * avg

- [ ] Test multi-scale aggregation:
  - [ ] Verify alignment across all 5 timescales
  - [ ] Validate indicator calculations per scale
  - [ ] Test cross-correlation computation accuracy
  - [ ] Benchmark computation speed (<500ms per ticker)

#### 3.5.2 Hierarchical Multi-Scale Fusion Architecture

**Architecture Options (Choose Primary + Keep Alternates):**

**Option A: Temporal Fusion Transformer (TFT) [PRIMARY RECOMMENDATION]**
- [ ] Implement `models/transformers/temporal_fusion_transformer.py`:
  ```python
  class TemporalFusionTransformer(nn.Module):
      """
      TFT with TimeScaleFusion head for interpretable multi-scale modeling.
      
      Architecture:
          1. Variable Selection Networks (VSN) per scale
          2. LSTM encoder for local temporal dependencies per scale
          3. Multi-head self-attention for global dependencies
          4. TimeScaleFusion gating layer (see below)
          5. Quantile regression head for probabilistic forecasts
      
      References:
          - Lim et al. 2021: "Temporal Fusion Transformers" (arXiv:1912.09363)
          - Supports interpretable attention weights per scale
      """
      
      def __init__(self, d_model=256, n_scales=5, n_heads=4, n_layers=3):
          super().__init__()
          self.n_scales = n_scales
          
          # Per-scale Variable Selection Networks
          self.vsn = nn.ModuleList([
              VariableSelectionNetwork(d_model) for _ in range(n_scales)
          ])
          
          # LSTM encoders for local processing per scale
          self.lstm_encoders = nn.ModuleList([
              nn.LSTM(d_model, d_model, num_layers=2, batch_first=True)
              for _ in range(n_scales)
          ])
          
          # Global self-attention
          self.self_attn = nn.MultiheadAttention(d_model, n_heads, batch_first=True)
          
          # TimeScaleFusion module (volatility + correlation gating)
          self.fusion = TimeScaleFusion(d_model, n_scales)
          
          # Output heads
          self.forecast_head = nn.Linear(d_model, 3)  # [lower_quantile, median, upper_quantile]
          self.signal_head = nn.Linear(d_model, 3)    # [buy, hold, sell]
      
      def forward(self, x_scales, aux_stats):
          """
          Args:
              x_scales: List of [B, T, F] tensors per scale (B=batch, T=time, F=features)
              aux_stats: [B, n_scales, K] tensor with volatility, divergence, corr stats
          
          Returns:
              forecast: [B, 3] quantile predictions
              signal: [B, 3] signal logits
              scale_weights: [B, n_scales] interpretable weights
          """
          # Per-scale encoding
          h_scales = []
          for i, (vsn, lstm, x) in enumerate(zip(self.vsn, self.lstm_encoders, x_scales)):
              # Variable selection
              x_selected = vsn(x)
              # Local temporal encoding
              h, (_, _) = lstm(x_selected)
              h_scales.append(h[:, -1, :])  # Take last hidden state [B, d_model]
          
          # Stack scale embeddings [B, n_scales, d_model]
          H = torch.stack(h_scales, dim=1)
          
          # Global attention across scales
          H_attn, _ = self.self_attn(H, H, H)
          
          # Fusion with volatility/correlation gating
          H_fused, scale_weights = self.fusion(H_attn, aux_stats)
          
          # Output predictions
          forecast = self.forecast_head(H_fused)
          signal = self.signal_head(H_fused)
          
          return forecast, signal, scale_weights
  ```

- [ ] Implement `VariableSelectionNetwork` (from TFT paper):
  ```python
  class VariableSelectionNetwork(nn.Module):
      """
      Learns to select relevant features per scale using gating.
      """
      def __init__(self, d_model, n_features):
          super().__init__()
          self.feature_transform = nn.Linear(n_features, d_model)
          self.gate = nn.Sequential(
              nn.Linear(d_model, d_model),
              nn.ReLU(),
              nn.Linear(d_model, n_features),
              nn.Softmax(dim=-1)
          )
      
      def forward(self, x):
          # x: [B, T, n_features]
          weights = self.gate(x.mean(dim=1, keepdim=True))  # [B, 1, n_features]
          x_weighted = x * weights
          return self.feature_transform(x_weighted)
  ```

**Option B: Multi-Scale CNN/TCN Stacks [ALTERNATE]**
- [ ] Implement `models/transformers/multi_scale_tcn.py`:
  ```python
  class MultiScaleTCN(nn.Module):
      """
      Parallel Temporal Convolutional Networks with different dilations
      per timescale, followed by attention gating.
      
      References:
          - Bai et al. "Temporal Convolutional Networks" (arXiv:1803.01271)
          - Multi-scale CNN for stocks (arXiv:2107.09441)
      """
      
      def __init__(self, d_model=256, n_scales=5):
          super().__init__()
          # Different dilation rates per scale
          dilations = {
              '1min': [1, 2, 4, 8],
              '15min': [1, 2, 4],
              '1hour': [1, 2],
              '12hour': [1],
              '1day': [1]
          }
          
          self.tcn_branches = nn.ModuleDict({
              scale: TemporalConvNet(d_model, d_model, dilations[scale])
              for scale in ['1min', '15min', '1hour', '12hour', '1day']
          })
          
          self.fusion = TimeScaleFusion(d_model, n_scales)
      
      def forward(self, x_scales, aux_stats):
          h_scales = [tcn(x) for tcn, x in zip(self.tcn_branches.values(), x_scales)]
          H = torch.stack(h_scales, dim=1)
          return self.fusion(H, aux_stats)
  ```

**Option C: Wavelet Multiresolution Decomposition [ALTERNATE]**
- [ ] Implement `models/transformers/wavelet_fusion.py`:
  ```python
  class WaveletMultiresolutionFusion(nn.Module):
      """
      Uses Discrete Wavelet Transform (DWT) to decompose price series
      into low/high frequency components, then fuses with attention.
      
      References:
          - Multiresolution analysis for stocks (MDPI: 10.3390/math9010026)
          - DWT creates natural multi-scale representations
      """
      
      def __init__(self, d_model=256, wavelet='db4', levels=4):
          super().__init__()
          self.wavelet = wavelet
          self.levels = levels
          
          # Projection networks for each decomposition level
          self.projections = nn.ModuleList([
              nn.Linear(d_model, d_model) for _ in range(levels + 1)
          ])
          
          self.fusion = TimeScaleFusion(d_model, levels + 1)
      
      def forward(self, x, aux_stats):
          """
          Args:
              x: [B, T, F] input time series
          """
          import pywt
          
          # Apply DWT per sample
          h_scales = []
          for i in range(x.shape[0]):
              coeffs = pywt.wavedec(x[i].cpu().numpy(), self.wavelet, level=self.levels)
              h_scales_i = [torch.tensor(c).to(x.device) for c in coeffs]
              h_scales.append(h_scales_i)
          
          # Project each level
          # ... (implementation details)
          
          return self.fusion(H, aux_stats)
  ```

**Option D: Hierarchical Cross-Scale Attention [ALTERNATE]**
- [ ] Implement cross-scale attention layers that explicitly model dependencies:
  ```python
  class HierarchicalCrossScaleAttention(nn.Module):
      """
      Attention layers that allow shorter scales to query longer scales
      and vice versa for bidirectional information flow.
      
      References:
          - Hierarchical attention (SpringerLink: 10.1007/s10489-021-02533-0)
      """
      
      def __init__(self, d_model, n_scales=5):
          super().__init__()
          # Upward attention (short -> long)
          self.upward_attn = nn.ModuleList([
              nn.MultiheadAttention(d_model, 4) for _ in range(n_scales - 1)
          ])
          # Downward attention (long -> short)
          self.downward_attn = nn.ModuleList([
              nn.MultiheadAttention(d_model, 4) for _ in range(n_scales - 1)
          ])
      
      def forward(self, h_scales):
          # Bottom-up pass
          for i in range(len(h_scales) - 1):
              h_scales[i+1] = self.upward_attn[i](h_scales[i+1], h_scales[i], h_scales[i])[0]
          
          # Top-down pass
          for i in range(len(h_scales) - 1, 0, -1):
              h_scales[i-1] = self.downward_attn[i-1](h_scales[i-1], h_scales[i], h_scales[i])[0]
          
          return h_scales
  ```

#### 3.5.3 Adaptive TimeScale Fusion Layer

**Core Innovation:** Volatility-aware and correlation-aware gating mechanism.

- [ ] Implement `models/transformers/timescale_fusion.py`:
  ```python
  class TimeScaleFusion(nn.Module):
      """
      Adaptive gating that re-weights timescales based on:
          1. Self-attention scores (learned relevance)
          2. Volatility levels (down-weight noisy micro-scales in high vol)
          3. Cross-scale coherence (boost scales with strong lead-lag correlation)
      
      Mathematical Formulation:
          Per-scale weight: w_s = softmax_s(α·Attn(h_s) + β·φ(vol_s) + γ·ψ(c_s))
          
          Where:
              h_s: scale embedding [B, d_model]
              vol_s: realized volatility at scale s [B, 1]
              c_s: cross-correlation strength with other scales [B, 1]
              φ: volatility transform (e.g., sigmoid, down-weights high vol)
              ψ: coherence transform (e.g., ReLU, boosts high correlation)
              α, β, γ: learned mixing coefficients
          
          Fused representation: H = Σ_s w_s · h_s
      """
      
      def __init__(self, d_model, n_scales=5, K_aux=3):
          """
          Args:
              d_model: embedding dimension
              n_scales: number of timescales (default 5)
              K_aux: number of auxiliary stats per scale (vol, divergence, corr)
          """
          super().__init__()
          self.n_scales = n_scales
          self.K_aux = K_aux
          
          # Per-scale projection
          self.proj = nn.ModuleList([
              nn.Linear(d_model, d_model) for _ in range(n_scales)
          ])
          
          # Self-attention for learned relevance
          self.attn = nn.MultiheadAttention(d_model, num_heads=4, batch_first=True)
          
          # Gating network: combines attention + auxiliary stats
          self.gate = nn.Sequential(
              nn.Linear(d_model + K_aux, d_model),
              nn.ReLU(),
              nn.Dropout(0.1),
              nn.Linear(d_model, 1)
          )
          
          # Mixing coefficients (learnable)
          self.alpha = nn.Parameter(torch.tensor(1.0))  # Attention weight
          self.beta = nn.Parameter(torch.tensor(0.5))   # Volatility weight
          self.gamma = nn.Parameter(torch.tensor(0.5))  # Coherence weight
          
          # Volatility transform φ(vol): down-weight high volatility scales
          self.phi = nn.Sequential(
              nn.Linear(1, 16),
              nn.ReLU(),
              nn.Linear(16, 1),
              nn.Sigmoid()  # Output in [0, 1], lower for high vol
          )
          
          # Coherence transform ψ(corr): boost high correlation scales
          self.psi = nn.Sequential(
              nn.Linear(1, 16),
              nn.ReLU(),
              nn.Linear(16, 1),
              nn.ReLU()     # Output >= 0, higher for strong correlation
          )
      
      def forward(self, H_list, aux_stats):
          """
          Args:
              H_list: List of [B, T, d_model] per scale OR [B, d_model] if pooled
              aux_stats: [B, n_scales, K_aux] with [volatility, divergence, correlation]
          
          Returns:
              H_fused: [B, d_model] fused representation
              weights: [B, n_scales] interpretable attention weights (for logging)
          """
          # Project each scale
          H_proj = torch.stack([
              proj(H) for proj, H in zip(self.proj, H_list)
          ], dim=1)  # [B, n_scales, d_model] or [B, n_scales, T, d_model]
          
          # If temporal, pool to [B, n_scales, d_model]
          if H_proj.dim() == 4:
              # Use attention pooling over time
              H_pool = H_proj.mean(dim=2)  # Simple mean; can replace with attention
          else:
              H_pool = H_proj
          
          # Self-attention across scales [B, n_scales, d_model]
          attn_out, attn_weights = self.attn(H_pool, H_pool, H_pool)
          
          # Extract auxiliary stats
          vol = aux_stats[:, :, 0:1]       # [B, n_scales, 1]
          divergence = aux_stats[:, :, 1:2]
          correlation = aux_stats[:, :, 2:3]
          
          # Apply transforms
          vol_term = self.phi(vol)         # [B, n_scales, 1], lower for high vol
          corr_term = self.psi(correlation) # [B, n_scales, 1], higher for strong corr
          
          # Combine with learned mixing
          # Attention contribution
          attn_contrib = self.alpha * attn_out  # [B, n_scales, d_model]
          
          # Auxiliary contribution (broadcast and gate)
          aux_input = torch.cat([attn_out, vol, divergence, correlation], dim=-1)  # [B, n_scales, d_model+3]
          gate_logits = self.gate(aux_input).squeeze(-1)  # [B, n_scales]
          
          # Manual mixing: attention + volatility penalty + coherence boost
          manual_contrib = self.beta * (1 - vol.squeeze(-1)) + self.gamma * corr_term.squeeze(-1)
          
          # Final logits
          logits = gate_logits + manual_contrib  # [B, n_scales]
          
          # Softmax to get weights
          weights = torch.softmax(logits, dim=1)  # [B, n_scales]
          
          # Weighted fusion
          H_fused = (H_pool * weights.unsqueeze(-1)).sum(dim=1)  # [B, d_model]
          
          return H_fused, weights
      
      def get_mixing_coefficients(self):
          """Return current α, β, γ for logging/interpretation"""
          return {
              'alpha': self.alpha.item(),
              'beta': self.beta.item(),
              'gamma': self.gamma.item()
          }
  ```

- [ ] Add volatility/divergence trigger logic:
  ```python
  def compute_aux_stats(volatilities, divergences, correlations):
      """
      Prepare auxiliary statistics tensor for TimeScaleFusion.
      
      Args:
          volatilities: Dict[scale, float] - realized vol per scale
          divergences: Dict[(scale_a, scale_b), bool] - divergence flags
          correlations: np.array [n_scales, n_scales] - correlation matrix
      
      Returns:
          aux_stats: [B, n_scales, 3] tensor
      """
      scales = ['1min', '15min', '1hour', '12hour', '1day']
      batch_size = len(volatilities)  # Assuming batch processing
      
      aux_stats = torch.zeros(batch_size, len(scales), 3)
      
      for b in range(batch_size):
          for i, scale in enumerate(scales):
              # Volatility
              aux_stats[b, i, 0] = volatilities[b][scale]
              
              # Divergence flag (e.g., 1min vs 1hour)
              div_key = ('1min', '1hour') if scale == '1min' else (scale, '1day')
              aux_stats[b, i, 1] = float(divergences[b].get(div_key, False))
              
              # Average correlation with other scales
              aux_stats[b, i, 2] = correlations[b][i, :].mean()
      
      return aux_stats
  ```

- [ ] Implement dynamic re-weighting rules:
  ```python
  class VolatilityDivergenceTrigger:
      """
      Business logic for when to zoom out to longer timescales.
      """
      
      def __init__(self, vol_threshold=2.0, divergence_threshold=0.3):
          self.vol_threshold = vol_threshold  # Multiple of historical avg
          self.div_threshold = divergence_threshold
      
      def should_zoom_out(self, current_vol, historical_avg, divergence_score):
          """
          Decision rule: zoom out if micro-scale is too noisy AND diverging from macro.
          
          Returns:
              bool: True if should increase weight on longer timescales
          """
          high_vol = current_vol > self.vol_threshold * historical_avg
          high_divergence = divergence_score > self.div_threshold
          
          return high_vol and high_divergence
      
      def adjust_beta_gamma(self, regime):
          """
          Adjust β (volatility penalty) and γ (coherence boost) based on market regime.
          
          Regimes:
              - calm: Trust micro-scales, β=0.3, γ=0.3
              - volatile: Penalize micro-scales heavily, β=0.7, γ=0.5
              - crisis: Rely on macro-scales only, β=1.0, γ=0.8
          """
          if regime == 'calm':
              return 0.3, 0.3
          elif regime == 'volatile':
              return 0.7, 0.5
          elif regime == 'crisis':
              return 1.0, 0.8
          else:
              return 0.5, 0.5  # Default
  ```

#### 3.5.4 Cross-Scale Correlation & Lag Analysis

- [ ] Implement `models/transformers/cross_scale_correlator.py`:
  ```python
  import numpy as np
  from scipy.signal import correlate
  
  class CrossScaleCorrelator:
      """
      Continuously measures correlation strength and lag between timescales
      to refine signal confidence.
      """
      
      def __init__(self, max_lag=10, window_size=50):
          self.max_lag = max_lag
          self.window_size = window_size
          self.correlation_history = []
          self.lag_history = []
      
      def compute_lagged_correlation(self, x, y, max_lag):
          """
          Compute cross-correlation between series x and y with lag sweep.
          
          Uses Pearson correlation at each lag.
          
          Args:
              x: np.array, shape [T] - returns from scale A
              y: np.array, shape [T] - returns from scale B
              max_lag: int - maximum lag to test
          
          Returns:
              best_corr: float - maximum |correlation| found
              best_lag: int - lag at which max correlation occurs
                  (positive = y leads x, negative = x leads y)
          """
          # Normalize
          x = (x - x.mean()) / (x.std() + 1e-8)
          y = (y - y.mean()) / (y.std() + 1e-8)
          
          correlations = []
          lags = range(-max_lag, max_lag + 1)
          
          for lag in lags:
              if lag < 0:
                  # x leads y
                  corr = np.corrcoef(x[:lag], y[-lag:])[0, 1]
              elif lag > 0:
                  # y leads x
                  corr = np.corrcoef(x[lag:], y[:-lag])[0, 1]
              else:
                  # No lag
                  corr = np.corrcoef(x, y)[0, 1]
              
              correlations.append(corr if not np.isnan(corr) else 0.0)
          
          # Find best correlation
          abs_corrs = [abs(c) for c in correlations]
          best_idx = np.argmax(abs_corrs)
          best_corr = correlations[best_idx]
          best_lag = list(lags)[best_idx]
          
          return best_corr, best_lag
      
      def build_correlation_matrix(self, returns_dict):
          """
          Build full N x N correlation matrix for all scale pairs.
          
          Args:
              returns_dict: Dict[scale, np.array] - returns per timescale
          
          Returns:
              corr_matrix: [N, N] - correlation strengths
              lag_matrix: [N, N] - optimal lags
          """
          scales = list(returns_dict.keys())
          n = len(scales)
          
          corr_matrix = np.zeros((n, n))
          lag_matrix = np.zeros((n, n), dtype=int)
          
          for i, scale_a in enumerate(scales):
              for j, scale_b in enumerate(scales):
                  if i == j:
                      corr_matrix[i, j] = 1.0
                      lag_matrix[i, j] = 0
                  else:
                      # Align series (resample to common frequency if needed)
                      x, y = self._align_series(
                          returns_dict[scale_a],
                          returns_dict[scale_b],
                          scale_a,
                          scale_b
                      )
                      
                      corr, lag = self.compute_lagged_correlation(x, y, self.max_lag)
                      corr_matrix[i, j] = corr
                      lag_matrix[i, j] = lag
          
          return corr_matrix, lag_matrix
      
      def _align_series(self, x, y, scale_a, scale_b):
          """
          Resample series to common frequency for correlation calculation.
          
          Strategy: Downsample higher frequency to match lower frequency.
          """
          # Frequency mapping (in minutes)
          freq_map = {'1min': 1, '15min': 15, '1hour': 60, '12hour': 720, '1day': 1440}
          
          freq_a = freq_map[scale_a]
          freq_b = freq_map[scale_b]
          
          if freq_a < freq_b:
              # Downsample x (higher freq) to match y
              factor = freq_b // freq_a
              x = x[::factor]
          elif freq_b < freq_a:
              # Downsample y to match x
              factor = freq_a // freq_b
              y = y[::factor]
          
          # Trim to same length
          min_len = min(len(x), len(y))
          return x[-min_len:], y[-min_len:]
      
      def update_rolling(self, ticker, timestamp, returns_dict):
          """
          Update correlation and lag estimates on a rolling window basis.
          Store in time-series database for later analysis.
          """
          corr_matrix, lag_matrix = self.build_correlation_matrix(returns_dict)
          
          self.correlation_history.append({
              'ticker': ticker,
              'timestamp': timestamp,
              'correlations': corr_matrix,
              'lags': lag_matrix
          })
          
          return corr_matrix, lag_matrix
  ```

- [ ] Add unit tests for cross-correlation:
  ```python
  def test_cross_correlation_with_known_lag():
      """
      Test that corr_lag correctly identifies lag in synthetic data.
      """
      # Create two sine waves with known lag
      t = np.linspace(0, 10, 100)
      x = np.sin(t)
      lag_true = 5
      y = np.sin(t - lag_true * 0.1)  # Lag by 5 timesteps
      
      correlator = CrossScaleCorrelator()
      corr, lag_found = correlator.compute_lagged_correlation(x, y, max_lag=10)
      
      assert abs(lag_found - lag_true) <= 1, f"Expected lag ~{lag_true}, got {lag_found}"
      assert corr > 0.9, f"Expected high correlation, got {corr}"
  ```

#### 3.5.5 Training Strategy for Multi-Timescale Model

- [ ] Implement multi-task training objective:
  ```python
  class MultiScaleLoss(nn.Module):
      """
      Joint loss with per-scale auxiliary heads + fused head + coherence regularizer.
      """
      
      def __init__(self, n_scales=5, lambda_coherence=0.1):
          super().__init__()
          self.n_scales = n_scales
          self.lambda_coherence = lambda_coherence
          
          # Per-scale prediction heads
          self.scale_heads = nn.ModuleList([
              nn.Linear(256, 1) for _ in range(n_scales)
          ])
      
      def forward(self, H_scales, H_fused, y_true, corr_matrix):
          """
          Args:
              H_scales: List of [B, d_model] per scale
              H_fused: [B, d_model] fused representation
              y_true: [B, 1] ground truth (e.g., next-period return)
              corr_matrix: [B, n_scales, n_scales] cross-scale correlations
          
          Returns:
              total_loss: scalar
              loss_dict: breakdown for logging
          """
          # Fused head prediction loss (primary)
          pred_fused = self.fused_head(H_fused)
          loss_fused = F.mse_loss(pred_fused, y_true)
          
          # Per-scale auxiliary losses
          loss_scales = 0.0
          for i, (head, h) in enumerate(zip(self.scale_heads, H_scales)):
              pred_scale = head(h)
              loss_scales += F.mse_loss(pred_scale, y_true)
          loss_scales /= self.n_scales
          
          # Coherence regularizer: penalize inconsistent predictions when correlation is high
          pred_scales_all = torch.stack([head(h) for head, h in zip(self.scale_heads, H_scales)], dim=1)  # [B, n_scales, 1]
          
          # Compute pairwise prediction disagreement
          disagreement = 0.0
          for i in range(self.n_scales):
              for j in range(i + 1, self.n_scales):
                  # Weight by correlation strength
                  corr_ij = corr_matrix[:, i, j].abs().mean()
                  pred_diff = (pred_scales_all[:, i] - pred_scales_all[:, j]).pow(2).mean()
                  disagreement += corr_ij * pred_diff
          
          disagreement /= (self.n_scales * (self.n_scales - 1) / 2)
          loss_coherence = self.lambda_coherence * disagreement
          
          # Total loss
          total_loss = loss_fused + 0.3 * loss_scales + loss_coherence
          
          return total_loss, {
              'loss_fused': loss_fused.item(),
              'loss_scales': loss_scales.item(),
              'loss_coherence': loss_coherence.item()
          }
  ```

- [ ] Implement walk-forward with regime tagging:
  ```python
  def walk_forward_train_with_regimes(model, data_loader, n_epochs=50):
      """
      Train model with regime-specific weight calibration.
      
      Strategy:
          1. Detect volatility regime per training window
          2. Calibrate β (volatility penalty) and γ (coherence boost) per regime
          3. Track per-regime performance for adaptive deployment
      """
      regime_detector = VolatilityRegimeDetector()
      
      for epoch in range(n_epochs):
          for batch in data_loader:
              x_scales, y, metadata = batch
              
              # Detect current regime
              current_vol = metadata['volatility'].mean()
              regime = regime_detector.classify(current_vol)
              
              # Adjust fusion layer parameters based on regime
              beta, gamma = model.fusion.adjust_beta_gamma(regime)
              model.fusion.beta.data = torch.tensor(beta)
              model.fusion.gamma.data = torch.tensor(gamma)
              
              # Forward pass
              pred, signal, weights = model(x_scales, metadata['aux_stats'])
              
              # Compute loss
              loss = criterion(pred, y)
              
              # Backward pass
              optimizer.zero_grad()
              loss.backward()
              optimizer.step()
              
              # Log per-regime loss
              logger.log({
                  f'loss_{regime}': loss.item(),
                  f'weights_{regime}': weights.mean(dim=0).tolist()
              })
  ```

- [ ] Track per-scale attribution with SHAP:
  ```python
  import shap
  
  def compute_scale_attribution(model, x_scales, aux_stats):
      """
      Use SHAP to compute per-scale contribution to final prediction.
      
      TFT architecture naturally supports interpretable attention,
      but SHAP provides additional validation.
      """
      # Wrap model for SHAP
      def model_predict(x_combined):
          # Unpack into scales
          x_scales_unpacked = [x_combined[:, i*seq_len:(i+1)*seq_len] for i in range(5)]
          pred, _, _ = model(x_scales_unpacked, aux_stats)
          return pred.detach().cpu().numpy()
      
      # Background data (sample from training set)
      background = shap.sample(x_combined_train, 100)
      
      # Create explainer
      explainer = shap.KernelExplainer(model_predict, background)
      
      # Compute SHAP values
      shap_values = explainer.shap_values(x_combined_test)
      
      # Aggregate by scale
      scale_contributions = []
      for i in range(5):
          scale_shap = shap_values[:, i*seq_len:(i+1)*seq_len].mean()
          scale_contributions.append(scale_shap)
      
      return scale_contributions
  ```

#### 3.5.6 Implementation & Testing Checklist

- [ ] **Data Pipeline:**
  - [ ] Implement MultiScaleAggregator with all 5 timescales
  - [ ] Add TimescaleDB continuous aggregates for 15min, 1hour, 12hour, 1day
  - [ ] Implement rolling volatility calculation per scale (window=20)
  - [ ] Implement trend divergence detection (fast vs slow MA across scales)
  - [ ] Implement CrossScaleCorrelator with lag sweep
  - [ ] Build and update correlation matrix continuously (every 5 min)
  - [ ] Test alignment and synchronization across all scales
  - [ ] Benchmark computation speed (<500ms per ticker)

- [ ] **Model Architecture:**
  - [ ] Choose primary architecture: **TFT (recommended)** or Multi-scale TCN or Wavelet or Hierarchical Attention
  - [ ] Implement TemporalFusionTransformer with TimeScaleFusion layer
  - [ ] Implement VariableSelectionNetwork per scale
  - [ ] Implement TimeScaleFusion with volatility-aware and coherence-aware gating
  - [ ] Add learnable mixing coefficients (α, β, γ)
  - [ ] Implement volatility transform φ(vol) and coherence transform ψ(corr)
  - [ ] Test forward pass with synthetic multi-scale data
  - [ ] Validate attention weights sum to 1.0
  - [ ] Check that high volatility down-weights micro-scales

- [ ] **Training:**
  - [ ] Implement MultiScaleLoss with per-scale heads + fused head + coherence regularizer
  - [ ] Implement walk-forward training with regime tagging
  - [ ] Calibrate β and γ per regime (calm/volatile/crisis)
  - [ ] Track per-scale loss during training
  - [ ] Log scale weights to TensorBoard for interpretability
  - [ ] Validate coherence regularizer reduces divergence when corr > 0.7
  - [ ] Train on 2020-2023 data, validate on 2024
  - [ ] Hyperparameter tuning: λ_coherence, α_init, β_init, γ_init

- [ ] **Evaluation & Interpretability:**
  - [ ] Log per-scale attention weights over time (export as CSV)
  - [ ] Compute SHAP values per scale for sample predictions
  - [ ] Create visualization: scale weights vs volatility regime
  - [ ] Create visualization: scale weights vs cross-correlation strength
  - [ ] Validate that fusion shifts to longer scales during high volatility
  - [ ] Validate that highly correlated scales get boosted together

- [ ] **Unit Tests:**
  - [ ] Test cross-correlation with synthetic lagged sine waves (known lag)
  - [ ] Test that high micro-scale noise triggers zoom-out to macro-scales
  - [ ] Test coherence regularizer: inject anti-correlated predictions, check penalty increases
  - [ ] Test regime-based β/γ adjustment logic
  - [ ] Test that scale weights are non-negative and sum to 1.0

- [ ] **Ablation Studies:**
  - [ ] **Baseline:** Single-scale model (1hour only)
  - [ ] **Ablation 1:** Multi-scale without volatility gating (β=0)
  - [ ] **Ablation 2:** Multi-scale without coherence boost (γ=0)
  - [ ] **Ablation 3:** Multi-scale without coherence regularizer (λ_coherence=0)
  - [ ] **Full model:** Multi-scale with all components
  - [ ] Compare Sharpe ratio, max drawdown, win rate across ablations
  - [ ] Target: >15% lift in Sharpe from single-scale to full multi-scale

- [ ] **Backtesting Integration:**
  - [ ] Report per-scale contribution to PnL (e.g., 1min contributed +5%, 1day +8%)
  - [ ] Regime-wise performance: calm markets vs volatile vs crisis
  - [ ] Track when system zooms out (volatility trigger events)
  - [ ] Validate that longer scales dominate during crisis periods

#### 3.5.7 Acceptance Criteria (PRD Addition)

**Functional Requirements:**
- [ ] Model logs per-scale attention/gate weights on every prediction
- [ ] Weights are exposed as metrics in monitoring dashboard (Grafana)
- [ ] Cross-correlation matrix updated every 5 minutes and stored in TimescaleDB
- [ ] Volatility regime detection triggers automatic β/γ adjustment

**Performance Requirements:**
- [ ] Multi-scale aggregation completes in <500ms per ticker
- [ ] Cross-correlation computation completes in <200ms per scale pair
- [ ] Model inference with fusion layer completes in <100ms per batch

**Backtesting Requirements:**
- [ ] Per-scale PnL attribution reported in backtest results
- [ ] Regime-wise performance breakdown (calm/volatile/crisis)
- [ ] Ablation studies show >15% Sharpe lift from fusion vs single-scale

**Testing Requirements:**
- [ ] Unit test: synthetic lagged data correctly identifies lag ±1 timestep
- [ ] Integration test: inject micro-scale noise, verify zoom-out to macro
- [ ] Stress test: extreme volatility spike, verify macro-scales dominate weights

**Interpretability Requirements:**
- [ ] TensorBoard dashboard shows scale weight evolution over time
- [ ] SHAP attribution per scale computed for validation set
- [ ] Model explanation: "Currently relying 60% on 1hour, 30% on 1day due to high 1min volatility and strong 1hour-1day correlation"

### 3.6 Model Evaluation
- [ ] Implement `training/evaluate.py`:
  - [ ] Load trained model
  - [ ] Run inference on test set
  - [ ] Calculate all metrics (MAE, RMSE, F1, etc.)
  - [ ] Generate confusion matrix visualization
  - [ ] Plot prediction vs actual prices
  - [ ] Save evaluation report (JSON + PDF)
- [ ] Set baseline performance targets:
  - [ ] MAE < 2% of asset price
  - [ ] F1-Score > 0.75 for signals
  - [ ] Validation loss not increasing (no overfitting)
- [ ] Test model on out-of-sample data (2025 YTD)
- [ ] Analyze failure cases

---

## Phase 4: Reinforcement Learning (Weeks 15-18)

### 4.1 RL Environment Setup

#### Trading Environment
- [ ] Implement `models/rl_agents/trading_env.py` (Gym-compatible):
  - [ ] State space definition:
    - [ ] Current positions (long/short per asset)
    - [ ] Account balance
    - [ ] Available margin
    - [ ] Market features (OHLCV, indicators)
    - [ ] Model predictions (supervised model outputs)
    - [ ] Liquidity metrics (bid-ask spread, volume depth)
    - [ ] Current leverage ratio
    - [ ] Volatility regime (calm/volatile/crisis)
    - [ ] Event signals (real-time impact scores)
  - [ ] Action space definition:
    - [ ] Buy amount [0, max_position]
    - [ ] Sell amount [0, current_position]
    - [ ] Short position [0, max_short]
    - [ ] Leverage ratio [1.0, max_leverage]
    - [ ] Stop-loss distance (% from entry)
    - [ ] Take-profit distance (% from entry)
  - [ ] Reward function:
    - [ ] Base: profit from trades
    - [ ] Penalty: transaction costs (0.1% per trade)
    - [ ] Penalty: risk (drawdown severity)
    - [ ] Penalty: liquidity (slippage in illiquid markets)
    - [ ] Bonus: risk-adjusted returns (Sharpe ratio)
  - [ ] Episode termination conditions
  - [ ] Reset functionality
- [ ] Test environment with random agent
- [ ] Validate state/action/reward computation

### 4.2 Position Sizing Logic
- [ ] Implement `models/rl_agents/position_sizer.py`:
  - [ ] Kelly Criterion calculation
  - [ ] Volatility adjustment (reduce in high volatility)
  - [ ] Liquidity adjustment (reduce in illiquid assets)
  - [ ] Leverage adjustment (scale down if leveraged)
  - [ ] Hard limits (max position size per asset)
- [ ] Test position sizing with various market conditions
- [ ] Validate that position sizes never exceed limits

### 4.3 Short/Long Bias Manager
- [ ] Implement `models/rl_agents/bias_manager.py`:
  - [ ] Detect market trend (uptrend/downtrend/sideways)
  - [ ] Determine bias (long_bias/short_bias/neutral)
  - [ ] Bias percentage (e.g., 70% long in uptrend)
  - [ ] Dynamic adjustment based on regime changes
- [ ] Test bias calculation on historical trends
- [ ] Validate bias switches appropriately

### 4.4 Leverage Management
- [ ] Implement `models/rl_agents/leverage_manager.py`:
  - [ ] Base leverage calculation
  - [ ] Regime-based adjustment:
    - [ ] Calm: up to 2x leverage
    - [ ] Volatile: max 1.2x leverage
    - [ ] Crisis: no leverage (1x)
  - [ ] Confidence multiplier (higher confidence = higher leverage)
  - [ ] Drawdown check (cut leverage during losses)
  - [ ] Hard leverage caps per asset class
- [ ] Test leverage adjustments in simulation
- [ ] Validate leverage never exceeds regulatory limits

### 4.5 RL Agent Training

#### PPO/SAC Agent
- [ ] Choose RL algorithm (PPO recommended for stability)
- [ ] Implement `models/rl_agents/ppo_agent.py` (or use Ray RLlib):
  - [ ] Policy network architecture
  - [ ] Value network architecture
  - [ ] PPO update algorithm
  - [ ] Experience buffer
  - [ ] Advantage estimation (GAE)
- [ ] Configure training:
  - [ ] Number of environments (parallel rollouts)
  - [ ] Steps per episode
  - [ ] PPO hyperparameters (clip_epsilon, gamma, lambda)
  - [ ] Learning rate schedule
- [ ] Train agent on historical data (2020-2023):
  - [ ] Run 1M+ timesteps
  - [ ] Monitor reward curve
  - [ ] Track policy entropy (exploration)
  - [ ] Save checkpoints every 100k steps
- [ ] Test trained agent on validation set (2024)
- [ ] Compare RL agent vs supervised-only baseline

### 4.6 RL Integration with Supervised Models
- [ ] Implement hybrid approach:
  - [ ] Supervised model generates price forecasts
  - [ ] RL agent uses forecasts as state features
  - [ ] RL optimizes trading decisions
- [ ] Test combined performance
- [ ] Measure improvement over supervised-only

---

## Phase 5: Backtesting & Validation (Weeks 11-14)

### 5.1 Backtesting Framework Setup

#### Backtrader Integration
- [ ] Install Backtrader library
- [ ] Implement `backtesting/strategies.py`:
  - [ ] Base strategy class
  - [ ] Load ML model predictions
  - [ ] Implement signal execution logic:
    - [ ] Buy on strong_buy signal
    - [ ] Sell on strong_sell signal
    - [ ] Hold on neutral
    - [ ] Short on short_signal (if enabled)
  - [ ] Position sizing integration
  - [ ] Stop-loss and take-profit orders
  - [ ] Track performance metrics
- [ ] Implement `backtesting/data_feed.py`:
  - [ ] Load historical data from InfluxDB
  - [ ] Convert to Backtrader format
  - [ ] Support multiple timeframes
- [ ] Test strategy on sample data (1 month)

### 5.2 Multi-Timeframe Backtesting
- [ ] Create separate strategies per timeframe:
  - [ ] `Strategy5Min` (5-minute bars)
  - [ ] `Strategy15Min` (15-minute bars)
  - [ ] `Strategy1Hr` (1-hour bars)
  - [ ] `Strategy12Hr` (12-hour bars)
- [ ] Run backtests for each timeframe:
  - [ ] Train period: 2020-2023
  - [ ] Test period: 2024
- [ ] Calculate metrics per timeframe:
  - [ ] Sharpe Ratio
  - [ ] Max Drawdown
  - [ ] Win Rate
  - [ ] Profit Factor
  - [ ] Average Trade Duration
  - [ ] Total Return
- [ ] Compare timeframe performance
- [ ] Identify optimal timeframe(s)

### 5.3 Fibonacci Signal Validation
- [ ] Implement Fibonacci-based triggers:
  - [ ] Retracement levels (23.6%, 38.2%, 50%, 61.8%)
  - [ ] Extension levels (127.2%, 161.8%)
  - [ ] Diagonal channels
- [ ] Track Fibonacci signal accuracy:
  - [ ] Hit rate on retracement bounces
  - [ ] Hit rate on extension targets
  - [ ] Diagonal channel breakouts
- [ ] Validate per timeframe (5-min, 15-min, 1-hr, 12-hr)
- [ ] Target: >60% accuracy on Fibonacci signals

### 5.4 Walk-Forward Analysis
- [ ] Implement walk-forward validation:
  - [ ] Train on N months, test on next M months
  - [ ] Roll forward window (e.g., train 12mo, test 3mo)
  - [ ] Repeat across entire dataset
- [ ] Aggregate walk-forward results
- [ ] Check for consistent performance across windows
- [ ] Identify periods of failure (market regime mismatches)

### 5.5 Stress Testing
- [ ] Test strategy on extreme market conditions:
  - [ ] 2020 COVID crash (Feb-Mar)
  - [ ] Crypto winter 2022
  - [ ] Bull runs (2021)
  - [ ] High volatility periods
- [ ] Measure max drawdown in each scenario
- [ ] Validate risk controls activate appropriately
- [ ] Adjust risk parameters if needed

### 5.6 Performance Analysis
- [ ] Implement `backtesting/analysis.py`:
  - [ ] Generate performance report (HTML/PDF)
  - [ ] Plot equity curve
  - [ ] Plot drawdown curve
  - [ ] Trade distribution analysis (wins vs losses)
  - [ ] Monthly/yearly returns breakdown
  - [ ] Risk-adjusted metrics (Sharpe, Sortino, Calmar)
  - [ ] Trade statistics (avg win, avg loss, expectancy)
- [ ] Compare against benchmarks:
  - [ ] Buy-and-hold S&P 500
  - [ ] Buy-and-hold Bitcoin
  - [ ] 60/40 stock/bond portfolio
- [ ] Set performance targets:
  - [ ] Sharpe Ratio > 1.5
  - [ ] Max Drawdown < 15%
  - [ ] Win Rate > 55%
  - [ ] Profit Factor > 1.5
- [ ] Document findings in report

---

## Phase 6: Signal Ensemble & Optimization (Weeks 14-16)

### 6.1 Ensemble Architecture

#### Multi-Model Integration
- [ ] Implement `models/ensembles/signal_ensemble.py`:
  - [ ] Load supervised model (Stockformer)
  - [ ] Load RL agent (PPO)
  - [ ] Load technical analyzer (indicators)
  - [ ] Load sentiment analyzer (FinBERT)
  - [ ] Load event detector
- [ ] Implement signal aggregation:
  - [ ] Collect signals from all sources
  - [ ] Compute confidence for each signal
  - [ ] Apply regime-based weighting
  - [ ] Weighted ensemble calculation
- [ ] Test ensemble on validation data

### 6.2 Confidence Scoring
- [ ] Implement confidence calculation per source:
  - [ ] Supervised: softmax probability
  - [ ] RL: Q-value magnitude
  - [ ] Technical: indicator agreement rate
  - [ ] Sentiment: sentiment strength
  - [ ] Events: impact score
- [ ] Validate confidence calibration
- [ ] Test confidence correlation with accuracy

### 6.3 Regime-Based Weighting
- [ ] Implement regime detector:
  - [ ] Calm: low volatility, stable trend
  - [ ] Volatile: high volatility, rapid changes
  - [ ] Trending: strong directional move
  - [ ] Crisis: extreme volatility, panic selling
- [ ] Define weight sets per regime:
  - [ ] Calm: supervised 30%, RL 20%, technical 30%, sentiment 15%, events 5%
  - [ ] Volatile: supervised 15%, RL 35%, technical 20%, sentiment 10%, events 20%
  - [ ] Trending: supervised 35%, RL 15%, technical 35%, sentiment 10%, events 5%
  - [ ] Crisis: supervised 10%, RL 20%, technical 10%, sentiment 20%, events 40%
- [ ] Test regime detection accuracy
- [ ] Validate weight switching improves performance

### 6.4 Conflict Resolution
- [ ] Implement `models/ensembles/conflict_resolver.py`:
  - [ ] Detect signal disagreement
  - [ ] Check timeframe alignment
  - [ ] Apply priority rules:
    - [ ] High disagreement → reduce confidence or go neutral
    - [ ] Timeframe conflict → prefer longer timeframe in calm, shorter in volatile
    - [ ] Low confidence → default to neutral
  - [ ] Generate final signal with confidence score
- [ ] Create conflict resolution matrix
- [ ] Test on historical conflicting signals

### 6.5 Meta-Learning
- [ ] Implement `models/ensembles/meta_learner.py`:
  - [ ] Track performance history per signal source
  - [ ] Log which sources were correct/incorrect
  - [ ] Compute optimal weights per regime (logistic regression)
  - [ ] Update weights periodically (weekly)
- [ ] Train meta-learner on historical data
- [ ] Test adaptive weighting vs static weights
- [ ] Measure improvement in accuracy

### 6.6 Multi-Timeframe Alignment
- [ ] Implement timeframe signal checker:
  - [ ] Collect signals from 5-min, 15-min, 1-hr, 12-hr
  - [ ] Calculate agreement rate (% of signals in same direction)
  - [ ] Require 75% agreement before trading
  - [ ] If disagreement, use longest timeframe signal
- [ ] Test alignment requirement impact on:
  - [ ] Win rate (should increase)
  - [ ] Trade frequency (will decrease)
  - [ ] Sharpe ratio (should improve)

---

## Phase 7: Production Orchestration (Weeks 19-22)

### 7.1 Containerization

#### Docker Setup
- [ ] Create `docker/Dockerfile.base`:
  - [ ] Python 3.9 base image
  - [ ] Install system dependencies
  - [ ] Install Python packages
  - [ ] Set up non-root user
- [ ] Create `docker/Dockerfile.training`:
  - [ ] Extend base image
  - [ ] Add GPU support (CUDA)
  - [ ] Copy training code
  - [ ] Set entrypoint for training
- [ ] Create `docker/Dockerfile.inference`:
  - [ ] Extend base image
  - [ ] Copy inference code
  - [ ] Expose API port
  - [ ] Set entrypoint for inference server
- [ ] Create `docker/Dockerfile.monitoring`:
  - [ ] Prometheus + Grafana
  - [ ] Custom dashboards
- [ ] Create `docker/docker-compose.yml`:
  - [ ] InfluxDB service
  - [ ] Training service
  - [ ] Inference service
  - [ ] Monitoring stack
  - [ ] Network configuration
  - [ ] Volume mounts
- [ ] Test Docker build locally
- [ ] Test docker-compose up

### 7.2 Airflow Pipeline Orchestration

#### Airflow Setup
- [ ] Install Apache Airflow (Docker)
- [ ] Configure Airflow settings (airflow.cfg)
- [ ] Set up Airflow database (PostgreSQL)
- [ ] Create Airflow admin user

#### DAG Creation
- [ ] Create `pipelines/airflow_dags/data_ingestion_dag.py`:
  - [ ] Schedule: Every 5 minutes
  - [ ] Tasks:
    - [ ] Fetch OHLCV data (Polygon.io)
    - [ ] Fetch news/events
    - [ ] Process sentiment (FinBERT)
    - [ ] Write to InfluxDB
    - [ ] Validate data quality
  - [ ] Error handling and retries
- [ ] Create `pipelines/airflow_dags/training_dag.py`:
  - [ ] Schedule: Daily at 02:00 AM
  - [ ] Tasks:
    - [ ] Prepare training data
    - [ ] Train supervised model
    - [ ] Train RL agent
    - [ ] Evaluate models
    - [ ] Save best models
    - [ ] Update production models if improved
  - [ ] Conditional execution (only if new data available)
- [ ] Create `pipelines/airflow_dags/inference_dag.py`:
  - [ ] Schedule: Every 15 minutes (or based on trading timeframe)
  - [ ] Tasks:
    - [ ] Load latest data
    - [ ] Run model inference
    - [ ] Generate trading signals
    - [ ] Execute trades (paper/live)
    - [ ] Log results
- [ ] Create `pipelines/airflow_dags/monitoring_dag.py`:
  - [ ] Schedule: Hourly
  - [ ] Tasks:
    - [ ] Check model drift
    - [ ] Validate data quality
    - [ ] Check system health
    - [ ] Send alerts if issues detected
- [ ] Test each DAG independently
- [ ] Test DAG dependencies and scheduling

### 7.3 CI/CD Pipeline

#### GitHub Actions Setup
- [ ] Create `.github/workflows/ci.yml`:
  - [ ] Trigger: on push, pull_request
  - [ ] Jobs:
    - [ ] Linting (black, flake8, pylint)
    - [ ] Unit tests (pytest)
    - [ ] Integration tests
    - [ ] Code coverage report
    - [ ] Build Docker images
    - [ ] Push to registry (on main branch)
- [ ] Create `.github/workflows/cd.yml`:
  - [ ] Trigger: on release tag
  - [ ] Jobs:
    - [ ] Deploy to staging environment
    - [ ] Run smoke tests
    - [ ] Manual approval gate
    - [ ] Deploy to production
    - [ ] Post-deployment verification
- [ ] Set up GitHub secrets (API keys, credentials)
- [ ] Test CI pipeline with dummy commit
- [ ] Test CD pipeline with staging deployment

### 7.4 Monitoring & Alerting

#### Prometheus Setup
- [ ] Install Prometheus (Docker)
- [ ] Configure Prometheus (`prometheus.yml`):
  - [ ] Scrape targets (inference API, training jobs)
  - [ ] Scrape interval (15s)
  - [ ] Retention period (30 days)
- [ ] Implement custom metrics exporters:
  - [ ] `monitoring/exporters/model_metrics.py`:
    - [ ] Prediction latency (histogram)
    - [ ] Prediction error (gauge)
    - [ ] Signal confidence (histogram)
  - [ ] `monitoring/exporters/trading_metrics.py`:
    - [ ] P&L (gauge)
    - [ ] Drawdown (gauge)
    - [ ] Trade count (counter)
    - [ ] Win rate (gauge)
  - [ ] `monitoring/exporters/system_metrics.py`:
    - [ ] CPU/GPU utilization
    - [ ] Memory usage
    - [ ] Disk I/O
    - [ ] API latency
- [ ] Test metrics collection

#### Grafana Dashboards
- [ ] Install Grafana (Docker)
- [ ] Configure Grafana data source (Prometheus)
- [ ] Create dashboards:
  - [ ] **Model Performance Dashboard**:
    - [ ] Prediction accuracy over time
    - [ ] Confidence score distribution
    - [ ] Model drift indicator
  - [ ] **Trading Performance Dashboard**:
    - [ ] Real-time P&L
    - [ ] Cumulative returns
    - [ ] Drawdown curve
    - [ ] Win rate by timeframe
    - [ ] Position exposure
  - [ ] **System Health Dashboard**:
    - [ ] CPU/GPU utilization
    - [ ] Memory usage
    - [ ] API latency (p50, p95, p99)
    - [ ] Data ingestion rate
    - [ ] Error rate
  - [ ] **Data Quality Dashboard**:
    - [ ] Data completeness %
    - [ ] Stale data alerts
    - [ ] API failures
    - [ ] Cache hit rate
- [ ] Set up dashboard auto-refresh (30s)
- [ ] Export dashboards as JSON

#### Alerting Rules
- [ ] Configure Prometheus alerting (`alerts.yml`):
  - [ ] **Critical Alerts**:
    - [ ] Model drift detected (p-value < 0.05)
    - [ ] Drawdown > 15%
    - [ ] API failures > 10% in 5 minutes
    - [ ] Inference latency > 5s
    - [ ] GPU/CPU utilization > 95% for 5 minutes
  - [ ] **Warning Alerts**:
    - [ ] Win rate < 50% for 24 hours
    - [ ] Data ingestion lag > 5 minutes
    - [ ] Model accuracy drop > 10%
    - [ ] Cache hit rate < 80%
- [ ] Configure Alertmanager:
  - [ ] Email notifications
  - [ ] Slack integration (optional)
  - [ ] Alert grouping and throttling
- [ ] Test alert triggering with simulated issues

### 7.5 Model Drift Detection
- [ ] Implement `monitoring/drift_detection.py`:
  - [ ] Kolmogorov-Smirnov test for distribution shift
  - [ ] Population Stability Index (PSI)
  - [ ] Compare recent predictions vs training distribution
  - [ ] Trigger retraining if drift detected
- [ ] Schedule drift detection (daily)
- [ ] Test with intentional distribution changes
- [ ] Automate retraining workflow on drift alert

### 7.6 Automated Retraining
- [ ] Implement retraining trigger logic:
  - [ ] Drift detected
  - [ ] Performance degradation
  - [ ] Scheduled (weekly/monthly)
- [ ] Create retraining pipeline:
  - [ ] Fetch latest data
  - [ ] Retrain models
  - [ ] Validate on recent data
  - [ ] A/B test new model vs old model
  - [ ] Deploy if better performance
- [ ] Test automated retraining end-to-end
- [ ] Monitor retraining frequency and outcomes

---

## Phase 8: Paper Trading (Weeks 23-26)

### 8.1 Alpaca API Integration

#### API Setup
- [ ] Sign up for Alpaca paper trading account
- [ ] Get API keys (paper trading)
- [ ] Install alpaca-trade-api library
- [ ] Implement `trading/paper_trading.py`:
  - [ ] Authentication
  - [ ] Account info retrieval
  - [ ] Order placement (market, limit, stop-loss)
  - [ ] Position tracking
  - [ ] Order status checking
  - [ ] Portfolio value tracking
- [ ] Test basic order execution (buy/sell 1 share)

### 8.2 Trading Logic Implementation
- [ ] Implement `trading/executor.py`:
  - [ ] Receive signal from inference pipeline
  - [ ] Validate signal (confidence threshold)
  - [ ] Check account balance and buying power
  - [ ] Calculate position size (from position_sizer)
  - [ ] Check risk limits (max exposure per asset)
  - [ ] Place order with appropriate parameters
  - [ ] Set stop-loss and take-profit orders
  - [ ] Log trade execution
- [ ] Implement order types:
  - [ ] Market orders (immediate execution)
  - [ ] Limit orders (price targets)
  - [ ] Stop-loss orders (risk management)
  - [ ] Trailing stops (lock in profits)
- [ ] Test order execution with various signal types

### 8.3 Risk Management Integration
- [ ] Implement real-time risk checks:
  - [ ] Position size limits (% of portfolio per asset)
  - [ ] Total exposure limits (max % invested)
  - [ ] Leverage limits (per asset class)
  - [ ] Drawdown circuit breaker (stop trading if DD > 20%)
  - [ ] Concentration limits (max % in single sector)
- [ ] Implement position monitoring:
  - [ ] Track open positions
  - [ ] Update P&L in real-time
  - [ ] Check stop-loss triggers
  - [ ] Rebalance if needed
- [ ] Test risk controls with edge cases

### 8.4 Paper Trading Deployment
- [ ] Deploy inference pipeline to production mode
- [ ] Connect inference to Alpaca paper trading
- [ ] Start with conservative settings:
  - [ ] Small position sizes (1-5% per trade)
  - [ ] High confidence threshold (>0.7)
  - [ ] Tight risk limits
- [ ] Monitor 24/7 (set up monitoring stack)
- [ ] Run paper trading for 4+ weeks
- [ ] Collect data on:
  - [ ] Trade execution quality (slippage)
  - [ ] Signal accuracy in live conditions
  - [ ] System stability (uptime, errors)
  - [ ] Performance metrics (Sharpe, drawdown, etc.)

### 8.5 Performance Tracking
- [ ] Implement `trading/performance_tracker.py`:
  - [ ] Daily P&L calculation
  - [ ] Cumulative returns
  - [ ] Sharpe ratio (rolling 30-day)
  - [ ] Max drawdown tracking
  - [ ] Win rate calculation
  - [ ] Trade statistics (avg win/loss)
- [ ] Create performance report (daily email/dashboard)
- [ ] Compare paper trading vs backtest results
- [ ] Identify discrepancies and investigate

### 8.6 Iterative Improvement
- [ ] Collect edge cases and failures:
  - [ ] False signals
  - [ ] Missed opportunities
  - [ ] Execution errors
  - [ ] Risk breaches
- [ ] Analyze root causes
- [ ] Implement fixes:
  - [ ] Model retraining with new data
  - [ ] Signal threshold adjustments
  - [ ] Risk parameter tuning
  - [ ] Error handling improvements
- [ ] Deploy updates and continue monitoring
- [ ] Iterate weekly based on learnings

---

## Phase 9: Compliance & Regulatory (Weeks 23-26 Parallel)

### 9.1 Regulatory Framework Setup

#### Compliance Module
- [ ] Implement `trading/compliance/compliance_monitor.py`:
  - [ ] Pattern Day Trader (PDT) rule checker (US stocks)
  - [ ] Wash sale detection (30-day window)
  - [ ] Leverage limit enforcement
  - [ ] Geo-restriction checker
  - [ ] Large transaction flagging
- [ ] Implement `trading/compliance/audit_logger.py`:
  - [ ] Log all trades with full details
  - [ ] Log all compliance checks
  - [ ] Log all risk incidents
  - [ ] Log all model decisions
  - [ ] Structured format (JSON) for easy querying
- [ ] Test compliance checks with edge cases

### 9.2 Audit Trail
- [ ] Set up audit database (PostgreSQL or append-only log)
- [ ] Implement comprehensive logging:
  - [ ] Trade entries (timestamp, symbol, type, amount, price, exchange, user_id, IP)
  - [ ] Compliance check results
  - [ ] Risk limit breaches
  - [ ] Model predictions and confidence
  - [ ] System errors and warnings
- [ ] Implement log retention policy (7 years for financial records)
- [ ] Test audit log completeness
- [ ] Implement log querying API for audits

### 9.3 Tax Reporting
- [ ] Implement `trading/compliance/tax_reporter.py`:
  - [ ] Generate Form 8949 data (capital gains/losses)
  - [ ] Separate short-term vs long-term gains
  - [ ] Track cost basis (FIFO, LIFO, or specific ID)
  - [ ] Identify wash sales
  - [ ] Calculate total tax liability
- [ ] Test tax report generation with sample trades
- [ ] Export to CSV format for import into tax software

### 9.4 Crypto-Specific Compliance
- [ ] Implement crypto compliance checks:
  - [ ] Securities classification check (per token)
  - [ ] Exchange licensing validation
  - [ ] KYC/AML on exchanges
  - [ ] Jurisdictional restrictions (e.g., no China)
  - [ ] Suspicious activity detection (pump-and-dump patterns)
- [ ] Maintain list of restricted tokens/jurisdictions
- [ ] Update list quarterly or as regulations change
- [ ] Test crypto compliance on sample trades

### 9.5 Cross-Asset Compliance
- [ ] Implement unified compliance validator:
  - [ ] Check asset-specific rules (stocks vs crypto)
  - [ ] Validate portfolio-level rules (total leverage)
  - [ ] Enforce concentration limits
  - [ ] Check correlation exposure
- [ ] Test cross-asset portfolio compliance
- [ ] Document all compliance rules in wiki

### 9.6 Legal Consultation
- [ ] Consult with legal expert on trading bot regulations:
  - [ ] Jurisdictional requirements (US, EU, etc.)
  - [ ] Licensing requirements (if applicable)
  - [ ] Liability considerations
  - [ ] Terms of service for users (if multi-user)
- [ ] Document legal advice and integrate into system
- [ ] Review compliance module with legal expert
- [ ] Obtain sign-off on compliance approach

---

## Phase 10: Production Readiness (Weeks 27-30)

### 10.1 Security Audit

#### Code Security
- [ ] Run security scanner (Bandit for Python)
- [ ] Fix all high-severity vulnerabilities
- [ ] Review dependencies for known CVEs (safety check)
- [ ] Update vulnerable packages
- [ ] Implement input validation for all external inputs
- [ ] Sanitize user inputs (if applicable)
- [ ] Protect against SQL injection (use parameterized queries)

#### API Key Management
- [ ] Move all API keys to environment variables
- [ ] Use secrets management (AWS Secrets Manager, HashiCorp Vault)
- [ ] Rotate API keys periodically (quarterly)
- [ ] Implement key expiration monitoring
- [ ] Test with expired/invalid keys (fail gracefully)

#### Access Control
- [ ] Implement authentication for inference API (JWT or OAuth)
- [ ] Implement role-based access control (RBAC)
- [ ] Restrict access to sensitive endpoints
- [ ] Log all access attempts
- [ ] Test unauthorized access scenarios

#### Network Security
- [ ] Use HTTPS for all API communication
- [ ] Implement rate limiting on APIs
- [ ] Set up firewall rules (whitelist IPs)
- [ ] Enable VPN for remote access (if cloud deployment)
- [ ] Test against common attacks (DDOS, injection)

### 10.2 Disaster Recovery

#### Backup Strategy
- [ ] Implement automated backups:
  - [ ] Code repository (GitHub already handles)
  - [ ] InfluxDB data (daily backups)
  - [ ] Model checkpoints (daily backups)
  - [ ] Configuration files (version control)
  - [ ] Audit logs (replicate to separate storage)
- [ ] Store backups off-site (cloud storage)
- [ ] Test backup restoration process
- [ ] Document recovery procedures

#### Failover Plan
- [ ] Document component failure scenarios:
  - [ ] Data source failure → failover to alternate source
  - [ ] Model inference failure → use last known good model
  - [ ] Database failure → failover to replica
  - [ ] Internet failure → offline mode with cached data
- [ ] Implement automatic failover where possible
- [ ] Test failover scenarios
- [ ] Create runbook for manual failover procedures

#### High Availability
- [ ] Implement health checks for all services
- [ ] Set up service auto-restart on failure (systemd, Docker restart policy)
- [ ] Consider redundant hardware (optional for local setup)
- [ ] Plan for cloud failover (if scaling beyond local)

### 10.3 Load Testing

#### Performance Benchmarking
- [ ] Implement load testing scripts:
  - [ ] Simulate high data ingestion rate (1000 points/sec)
  - [ ] Simulate concurrent inference requests (100 req/sec)
  - [ ] Simulate database query load
- [ ] Measure performance under load:
  - [ ] Latency (p50, p95, p99)
  - [ ] Throughput (requests/sec)
  - [ ] Resource utilization (CPU, GPU, memory)
  - [ ] Error rate
- [ ] Identify bottlenecks
- [ ] Optimize hot paths (caching, query optimization)
- [ ] Re-test after optimizations

#### Stress Testing
- [ ] Push system beyond normal capacity
- [ ] Identify breaking points
- [ ] Implement graceful degradation:
  - [ ] Queue requests when overloaded
  - [ ] Return cached results if inference slow
  - [ ] Throttle non-critical operations
- [ ] Test recovery after stress

### 10.4 Documentation

#### Technical Documentation
- [ ] Write comprehensive `README.md`:
  - [ ] Project overview
  - [ ] Architecture diagram
  - [ ] Installation instructions
  - [ ] Configuration guide
  - [ ] Running locally
  - [ ] Deployment guide
- [ ] Document all APIs (Swagger/OpenAPI spec)
- [ ] Create developer guide:
  - [ ] Code organization
  - [ ] Adding new models
  - [ ] Adding new indicators
  - [ ] Extending features
- [ ] Document data schemas (InfluxDB, APIs)
- [ ] Create troubleshooting guide (common errors)

#### Operational Documentation
- [ ] Create runbooks:
  - [ ] System startup procedure
  - [ ] System shutdown procedure
  - [ ] Deployment process
  - [ ] Model retraining process
  - [ ] Incident response procedures
  - [ ] Failover procedures
- [ ] Document monitoring and alerting:
  - [ ] Dashboard usage guide
  - [ ] Alert interpretation
  - [ ] Response procedures per alert type
- [ ] Create change management process:
  - [ ] How to propose changes
  - [ ] Testing requirements
  - [ ] Approval process
  - [ ] Rollback procedures

#### User Documentation (if applicable)
- [ ] Write user guide for paper/live trading
- [ ] Document risk disclaimers
- [ ] Create FAQ
- [ ] Provide example configurations

### 10.5 Pre-Production Checklist
- [ ] All Phase 1-9 tasks completed
- [ ] All tests passing (unit, integration, end-to-end)
- [ ] Code coverage > 70%
- [ ] Security audit passed
- [ ] Performance benchmarks met
- [ ] Paper trading successful for 4+ weeks:
  - [ ] Sharpe ratio > 1.5
  - [ ] Max drawdown < 15%
  - [ ] Win rate > 55%
  - [ ] System uptime > 99%
- [ ] All documentation complete
- [ ] Disaster recovery tested
- [ ] Legal/compliance review passed
- [ ] Stakeholder approval obtained

### 10.6 Production Deployment

#### Gradual Rollout
- [ ] **Week 1: Minimum Capital**
  - [ ] Start with $500-1000 in live account
  - [ ] Trade smallest position sizes
  - [ ] Monitor 24/7
  - [ ] Validate execution quality
- [ ] **Week 2-3: Low Capital**
  - [ ] If Week 1 successful, increase to $2000-5000
  - [ ] Gradually increase position sizes
  - [ ] Continue intensive monitoring
  - [ ] Track vs paper trading performance
- [ ] **Week 4+: Scaled Capital**
  - [ ] If metrics met, scale up to target capital
  - [ ] Maintain risk limits (% per trade)
  - [ ] Transition to routine monitoring

#### Production Monitoring
- [ ] Set up 24/7 on-call rotation (if team)
- [ ] Configure critical alerts to phone/SMS
- [ ] Review performance daily:
  - [ ] P&L
  - [ ] Risk metrics
  - [ ] System health
  - [ ] Errors/warnings
- [ ] Weekly performance review meeting
- [ ] Monthly deep-dive analysis

#### Risk Controls in Production
- [ ] Enable all circuit breakers:
  - [ ] Max daily loss limit
  - [ ] Max drawdown limit
  - [ ] Max position size
  - [ ] Emergency stop button (pause trading)
- [ ] Manual override capability
- [ ] Require manual approval for large trades (optional)

---

## Phase 11: Continuous Improvement (Ongoing)

### 11.1 Performance Review Cadence
- [ ] **Daily:**
  - [ ] Review P&L and trades
  - [ ] Check system health
  - [ ] Investigate errors/alerts
- [ ] **Weekly:**
  - [ ] Analyze trade statistics
  - [ ] Review model performance
  - [ ] Identify improvement opportunities
  - [ ] Update watchlist of assets
- [ ] **Monthly:**
  - [ ] Deep performance analysis
  - [ ] Model retraining with latest data
  - [ ] Risk parameter tuning
  - [ ] Compliance review
- [ ] **Quarterly:**
  - [ ] Architecture review
  - [ ] Technology stack updates
  - [ ] Legal/regulatory updates
  - [ ] Disaster recovery drill

### 11.2 Model Improvement
- [ ] Collect new training data continuously
- [ ] Retrain models monthly with latest data
- [ ] Experiment with new architectures:
  - [ ] Attention mechanism variants
  - [ ] Graph neural networks (asset relationships)
  - [ ] Ensemble methods
- [ ] A/B test new models vs production models
- [ ] Deploy improvements incrementally

### 11.3 Feature Engineering
- [ ] Analyze feature importance regularly
- [ ] Add new indicators as identified:
  - [ ] Market microstructure features
  - [ ] Order book imbalance
  - [ ] Social media sentiment (Twitter, Reddit)
- [ ] Test new features in backtest before production
- [ ] Remove low-value features (reduce noise)

### 11.4 Expansion Opportunities
- [ ] Add new asset classes:
  - [ ] Commodities (gold, oil)
  - [ ] Forex pairs
  - [ ] Options (advanced)
- [ ] Add new exchanges:
  - [ ] International exchanges
  - [ ] DEX for crypto (Uniswap, etc.)
- [ ] Add new strategies:
  - [ ] Mean reversion
  - [ ] Statistical arbitrage
  - [ ] Market making
- [ ] Geographic expansion (if compliant)

### 11.5 Cloud Migration (Optional)
- [ ] Evaluate cloud providers (AWS, GCP, Azure)
- [ ] Cost-benefit analysis (cloud vs local)
- [ ] Plan migration strategy:
  - [ ] Data migration (InfluxDB to cloud)
  - [ ] Model deployment (SageMaker, Vertex AI)
  - [ ] Pipeline orchestration (cloud-native)
  - [ ] Monitoring (CloudWatch, Stackdriver)
- [ ] Test in cloud staging environment
- [ ] Gradual migration (keep local as backup)

### 11.6 Community & Research
- [ ] Follow latest research in financial ML:
  - [ ] arXiv papers on transformers in finance
  - [ ] RL advancements
  - [ ] MLOps best practices
- [ ] Participate in trading/ML communities:
  - [ ] QuantConnect, Quantopian forums
  - [ ] Reddit (r/algotrading, r/MachineLearning)
  - [ ] Twitter financial ML community
- [ ] Open-source non-sensitive components (optional)
- [ ] Contribute to libraries used (bug fixes, features)

---

## 📊 Success Metrics & KPIs

### Model Performance
- [ ] **Training Metrics:**
  - [ ] MAE < 2% of asset price
  - [ ] F1-Score > 0.75 for signals
  - [ ] Validation loss stable (no overfitting)
- [ ] **Inference Metrics:**
  - [ ] Latency p95 < 500ms
  - [ ] Prediction accuracy > 70% on unseen data

### Trading Performance
- [ ] **Backtest Results:**
  - [ ] Sharpe Ratio > 1.5
  - [ ] Max Drawdown < 15%
  - [ ] Win Rate > 55%
  - [ ] Profit Factor > 1.5
- [ ] **Paper Trading Results:**
  - [ ] Sharpe Ratio > 1.5
  - [ ] Max Drawdown < 15%
  - [ ] 4+ weeks without critical failures
- [ ] **Live Trading Results:**
  - [ ] Sharpe Ratio > 1.5
  - [ ] Max Drawdown < 15%
  - [ ] Win Rate > 55%
  - [ ] Positive returns for 3+ consecutive months

### System Performance
- [ ] **Uptime:** > 99.5%
- [ ] **Data Ingestion Success Rate:** > 99.5%
- [ ] **API Failure Recovery:** < 1 minute
- [ ] **Deployment Success Rate:** > 95%

### Risk Metrics
- [ ] **Position Limits:** Never breached
- [ ] **Leverage Limits:** Never breached
- [ ] **Drawdown Circuit Breaker:** Activates appropriately
- [ ] **Compliance Violations:** Zero

---

## 🚨 Risk Mitigation Checklist

### Technical Risks
- [ ] Model overfitting → Regularization, cross-validation, RL adaptation
- [ ] Sentiment inaccuracy → Use FinBERT, validate on labeled corpus
- [ ] API rate limiting → Caching, throttling, fallback sources
- [ ] Internet failures → Retry logic, offline mode, cached data
- [ ] Hardware failures → Backups, cloud failover option
- [ ] Model drift → Automated detection, scheduled retraining

### Financial Risks
- [ ] High drawdown → Position sizing limits, stop-losses, RL risk penalties
- [ ] False signals → Extensive backtesting, confidence thresholds
- [ ] Slippage → Model transaction costs in RL reward, optimize frequency
- [ ] Regulatory issues → Audit logs, legal consultation, compliance module

### Operational Risks
- [ ] Key person dependency → Documentation, automation
- [ ] Configuration errors → Version control, staging environment
- [ ] Deployment failures → CI/CD pipeline, rollback procedures
- [ ] Data corruption → Validation, checksums, backups

---

## 📚 Additional Resources

### Learning Materials
- [ ] **Books:**
  - [ ] "Advances in Financial Machine Learning" by Marcos López de Prado
  - [ ] "Machine Learning for Algorithmic Trading" by Stefan Jansen
  - [ ] "Reinforcement Learning" by Sutton & Barto
- [ ] **Courses:**
  - [ ] Coursera: Machine Learning for Trading
  - [ ] Udacity: AI for Trading Nanodegree
  - [ ] QuantInsti: EPAT program
- [ ] **Papers:**
  - [ ] Read all 27 cited papers in PRD
  - [ ] Follow arXiv cs.LG and q-fin.TR

### Tools & Libraries
- [ ] **Data:** Polygon.io, Alpha Vantage, Yahoo Finance, Quandl
- [ ] **ML:** TensorFlow, PyTorch, scikit-learn, XGBoost
- [ ] **RL:** Ray RLlib, Stable Baselines3, OpenAI Gym
- [ ] **Backtesting:** Backtrader, Backtesting.py, Zipline
- [ ] **Orchestration:** Airflow, Kubeflow, Prefect
- [ ] **Monitoring:** Prometheus, Grafana, ELK stack
- [ ] **Trading:** Alpaca, Interactive Brokers API, CCXT (crypto)

---

## ✅ Phase Completion Checklist

### Phase 0: Historical Data & Database ✓ **[PRIORITY - START HERE]**
- [ ] All 0.1-0.9 tasks completed
- [ ] InfluxDB running with all buckets created
- [ ] Polygon.io API client fully implemented and tested
- [ ] Historical backfill complete for top 50 tickers (5 years)
- [ ] Data quality verification passed
- [ ] Database query performance acceptable (<100ms typical queries)
- [ ] Flat files unpacked and loaded (if applicable)
- [ ] Environment variables configured

### Phase 1: Foundation ✓
- [ ] All 1.1-1.6 tasks completed
- [ ] Code repository set up
- [ ] Environment tested on local hardware
- [ ] Logging and error handling working

### Phase 2: Data Pipeline ✓
- [ ] All 2.1-2.6 tasks completed
- [ ] Data flowing from Polygon.io to InfluxDB
- [ ] Caching and failover tested
- [ ] Preprocessing pipeline functional

### Phase 3: ML Models ✓
- [ ] All 3.1-3.5 tasks completed
- [ ] Stockformer trained and evaluated
- [ ] FinBERT integrated
- [ ] Models meet performance targets

### Phase 4: RL ✓
- [ ] All 4.1-4.6 tasks completed
- [ ] RL agent trained
- [ ] Position sizing and leverage working
- [ ] RL improves over supervised baseline

### Phase 5: Backtesting ✓
- [ ] All 5.1-5.6 tasks completed
- [ ] Multi-timeframe backtests done
- [ ] Sharpe > 1.5, Drawdown < 15%
- [ ] Walk-forward validation passed

### Phase 6: Ensemble ✓
- [ ] All 6.1-6.6 tasks completed
- [ ] Signal ensemble functional
- [ ] Conflict resolution working
- [ ] Meta-learning improving weights

### Phase 7: Orchestration ✓
- [ ] All 7.1-7.6 tasks completed
- [ ] Docker containers built
- [ ] Airflow DAGs running
- [ ] Monitoring stack operational

### Phase 8: Paper Trading ✓
- [ ] All 8.1-8.6 tasks completed
- [ ] 4+ weeks of successful paper trading
- [ ] Performance targets met
- [ ] No critical failures

### Phase 9: Compliance ✓
- [ ] All 9.1-9.6 tasks completed
- [ ] Compliance module implemented
- [ ] Audit trail complete
- [ ] Legal review passed

### Phase 10: Production ✓
- [ ] All 10.1-10.6 tasks completed
- [ ] Security audit passed
- [ ] Documentation complete
- [ ] Gradual rollout successful

### Phase 11: Continuous Improvement ✓
- [ ] Performance review cadence established
- [ ] Model retraining automated
- [ ] Feature engineering ongoing
- [ ] Community engagement active

---

## 🎯 Final Pre-Launch Checklist

- [ ] ✅ **Phase 0 completed** (Historical database with 5 years of data)
- [ ] ✅ All 12 phases completed (0-11)
- [ ] ✅ All tests passing (unit, integration, end-to-end)
- [ ] ✅ Code coverage > 70%
- [ ] ✅ Security audit passed (no high/critical vulnerabilities)
- [ ] ✅ Performance benchmarks met (latency, throughput)
- [ ] ✅ Paper trading successful (4+ weeks, metrics met)
- [ ] ✅ Documentation complete (technical, operational, user)
- [ ] ✅ Disaster recovery tested
- [ ] ✅ Compliance review passed
- [ ] ✅ Monitoring and alerting configured
- [ ] ✅ Risk controls enabled and tested
- [ ] ✅ Stakeholder approval obtained
- [ ] ✅ Capital allocated for live trading
- [ ] ✅ Emergency procedures documented
- [ ] ✅ On-call rotation established (if team)

---

**🚀 Ready for Production Launch!**

---

**Document Maintenance:**
- Review and update this checklist monthly
- Add new items as requirements evolve
- Archive completed items in `COMPLETED.md`
- Track blockers in `BLOCKERS.md`
- Celebrate milestones! 🎉

---

*This floorplan is a living document. Adapt it to your specific needs, timeline, and resources.*
