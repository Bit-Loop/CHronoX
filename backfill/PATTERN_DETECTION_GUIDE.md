# Pattern Detection - User Guide

## Overview

The backfill module includes **optional** chart pattern detection functionality. This feature is **disabled by default** because:

1. **Computational Cost**: Pattern detection is expensive and can slow down chart rendering
2. **External Dependency**: Requires the `tradingpatterns` library which may not be installed
3. **Optional Feature**: Most users don't need real-time pattern detection

## Enabling Pattern Detection

### Method 1: Environment Variable (Recommended)

Set the environment variable before starting the application:

```bash
# Linux/macOS
export ENABLE_PATTERN_DETECTION=true
python -m backfill.main

# Or inline
ENABLE_PATTERN_DETECTION=true python backfill/visualizer.py
```

```powershell
# Windows PowerShell
$env:ENABLE_PATTERN_DETECTION="true"
python -m backfill.main
```

### Method 2: .env File

Add to your `.env` file:

```ini
ENABLE_PATTERN_DETECTION=true
```

### Method 3: Programmatically

```python
import os
os.environ['ENABLE_PATTERN_DETECTION'] = 'true'

from backfill.chart_utils import get_aggregated_bars

# Now pattern detection will be enabled
df = get_aggregated_bars('AAPL', interval='1d', with_patterns=True)
```

## Installing Required Dependencies

Pattern detection requires the `tradingpatterns` library:

```bash
pip install tradingpatterns
```

If not installed, the module will gracefully degrade and return empty pattern results.

## Using Pattern Detection

### Basic Usage

```python
from backfill.chart_utils import get_aggregated_bars

# Fetch data WITH pattern detection (if enabled via env var)
df = get_aggregated_bars(
    ticker='AAPL',
    interval='1d',
    with_indicators=True,
    with_patterns=True  # Explicitly request pattern detection
)

# Access detected patterns
if hasattr(df, 'attrs') and 'chart_patterns' in df.attrs:
    patterns = df.attrs['chart_patterns']
    print(f"Found {len(patterns['patterns'])} patterns")
    
    # Pattern types
    for p in patterns['patterns']:
        print(f"- {p['type']}: {p['subtype']} (confidence: {p['confidence']})")
    
    # Support/Resistance levels
    if patterns['support_resistance']:
        print(f"Support/Resistance levels: {patterns['support_resistance']}")
    
    # Trendlines
    if patterns['trendlines']:
        print(f"Detected {len(patterns['trendlines'])} trendlines")
```

### Batch Pattern Detection

For offline/background processing:

```python
from backfill.chart_utils import detect_and_store_patterns

# Detect and store patterns for a ticker
count = detect_and_store_patterns(
    ticker='AAPL',
    interval='1d',
    start_time=datetime(2024, 1, 1),
    end_time=datetime(2024, 12, 31)
)

print(f"Stored {count} patterns in database")
```

## Detected Pattern Types

### 1. Head & Shoulders
- **Type**: `head_shoulders`
- **Subtypes**: `bullish` (inverse), `bearish`
- **Confidence**: 0.7 (default)

### 2. Double Top/Bottom
- **Type**: `double_top_bottom`
- **Subtypes**: `bullish` (bottom), `bearish` (top)
- **Confidence**: 0.7 (default)

### 3. Triangle Patterns
- **Type**: `triangle`
- **Subtypes**: `bullish` (ascending), `bearish` (descending), `neutral` (symmetrical)
- **Confidence**: 0.7 (default)

### 4. Support/Resistance Levels
Automatically calculated price levels where the stock tends to bounce or reverse.

### 5. Trendlines
Detected trend lines connecting significant price points.

## Pattern Data Structure

```python
{
    'patterns': [
        {
            'type': 'head_shoulders',
            'subtype': 'bearish',
            'confidence': 0.7,
            'start_index': 80,
            'end_index': 105,
            'indices': [100],
            'metadata': {'pattern_name': 'Head and Shoulders'}
        },
        # ... more patterns
    ],
    'support_resistance': {
        'support': [100.50, 95.25],
        'resistance': [110.75, 115.00]
    },
    'trendlines': [
        # ... trendline data
    ]
}
```

## Performance Considerations

### Computational Cost

Pattern detection can be slow for large datasets:

| Data Size | Detection Time |
|-----------|---------------|
| 50 bars   | ~0.1s         |
| 200 bars  | ~0.5s         |
| 1000 bars | ~2-3s         |
| 10000 bars| ~10-15s       |

### Recommendations

1. **Real-time Charts**: Keep pattern detection **disabled** (default)
2. **Historical Analysis**: Enable for specific analysis sessions
3. **Batch Processing**: Use `detect_and_store_patterns()` for offline processing
4. **Cache Results**: Store detected patterns in database for reuse

## Disabling Pattern Detection

Pattern detection is **already disabled by default**. To explicitly ensure it's off:

```bash
# Unset the environment variable
unset ENABLE_PATTERN_DETECTION

# Or set to false
export ENABLE_PATTERN_DETECTION=false
```

In code:

```python
from backfill.chart_utils import get_aggregated_bars

# Explicitly disable pattern detection
df = get_aggregated_bars(
    ticker='AAPL',
    interval='1d',
    with_indicators=True,
    with_patterns=False  # Disabled (this is the default)
)
```

## Checking Status

```python
from backfill.chart_utils import ENABLE_PATTERN_DETECTION

if ENABLE_PATTERN_DETECTION:
    print("Pattern detection is ENABLED")
else:
    print("Pattern detection is DISABLED (default)")
```

## Troubleshooting

### Pattern Detection Not Working

1. **Check Environment Variable**:
   ```bash
   echo $ENABLE_PATTERN_DETECTION  # Should output "true"
   ```

2. **Check Library Installation**:
   ```python
   import tradingpatterns
   print("tradingpatterns installed successfully")
   ```

3. **Check Logs**:
   Pattern detection logs at DEBUG level:
   ```python
   import logging
   logging.basicConfig(level=logging.DEBUG)
   ```

### Performance Issues

If charts are loading slowly:

1. **Disable Pattern Detection** (set `with_patterns=False`)
2. **Reduce Data Size** (use smaller time ranges)
3. **Use Cached Patterns** (run batch detection offline)

## Integration with Visualizer

The visualizer automatically respects the `ENABLE_PATTERN_DETECTION` flag:

```bash
# Start visualizer WITHOUT pattern detection (fast, default)
python backfill/visualizer.py

# Start visualizer WITH pattern detection (slower)
ENABLE_PATTERN_DETECTION=true python backfill/visualizer.py
```

Pattern overlays will only appear if:
1. Pattern detection is enabled (`ENABLE_PATTERN_DETECTION=true`)
2. Patterns are explicitly requested (`with_patterns=True`)
3. Sufficient data is available (≥50 bars)

## API Reference

### Functions

#### `detect_chart_patterns(df)`
Detect patterns in OHLCV DataFrame. Returns empty dict if disabled.

#### `calculate_indicators(df)`
Calculate technical indicators (always enabled).

#### `resample_ohlcv(df, interval)`
Resample OHLCV data to different timeframes (always enabled).

#### `get_aggregated_bars(...)`
Main data fetching function with optional pattern detection.

**Parameters**:
- `ticker`: Stock symbol
- `interval`: Timeframe ('1m', '5m', '15m', '1h', '1d', etc.)
- `with_indicators`: Calculate indicators (default: True)
- `with_patterns`: Detect patterns (default: False) **← KEY PARAMETER**
- `limit`: Max bars to return (default: 10000)

#### `detect_and_store_patterns(...)`
Batch pattern detection with database storage.

### Configuration

#### `ENABLE_PATTERN_DETECTION` (bool)
Global flag controlling pattern detection availability.

---

## Summary

✅ **Pattern detection is DISABLED by default**  
✅ **Enable with `ENABLE_PATTERN_DETECTION=true`**  
✅ **Requires `tradingpatterns` library**  
✅ **Use `with_patterns=True` parameter**  
✅ **Graceful degradation if library missing**  

For most users, keep pattern detection **disabled** for optimal performance. Enable only when you need specific pattern analysis.
