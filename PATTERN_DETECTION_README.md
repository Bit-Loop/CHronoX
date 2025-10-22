# Chart Pattern Detection Integration

## Overview

This implementation adds automated chart pattern detection to ChronoX using the PatternPy library (`tradingpatterns`). Patterns are detected on historical data and displayed as colored overlays on the chart visualizations.

## Architecture (Option A-plus)

Following the approved architecture:
1. ✅ **PatternPy Integration**: Installed and integrated via `tradingpatterns` library
2. ✅ **Chart Backend Abstraction**: Created `ChartBackend` base class for future flexibility
3. ✅ **PyQtGraph Current**: Existing PyQtGraph system preserved with pattern overlay support
4. ⏳ **FinPlot Future**: Stub created (`FinPlotBackend`) for later migration

This design allows the current system to continue working while providing a clean migration path for future FinPlot integration.

## Dependencies

### Installed Packages
```bash
# PatternPy (chart pattern detection)
pip install git+https://github.com/keithorange/PatternPy.git

# FinPlot (future migration, not yet used)
pip install finplot
```

### Updated Files
- `scripts/install_market_data_deps.py`: Added tradingpatterns and finplot to required packages

## Pattern Detection

### Supported Patterns

The system detects 5 major chart pattern types:

1. **Head and Shoulders** (bullish/bearish)
2. **Double Top/Bottom** (bearish/bullish)
3. **Triangles** (ascending/descending/symmetrical)
4. **Wedges** (rising/falling)
5. **Channels** (parallel/regression)

Each pattern includes:
- Pattern type and subtype
- Confidence score (0.0 - 1.0)
- Start and end indices
- Complete metadata (pivot points, trendlines, etc.)

### Detection Function

**Location**: `scripts/backfill_historical_data.py` (lines ~1945-2045)

```python
def detect_chart_patterns(df):
    """
    Detect chart patterns using tradingpatterns library.
    
    Args:
        df: DataFrame with columns [time, open, high, low, close, volume]
    
    Returns:
        Dict with pattern detections: {
            'patterns': List of detected patterns with metadata,
            'support_resistance': Dict of S/R levels,
            'trendlines': List of detected trendlines
        }
    """
```

**Key Features**:
- Converts lowercase → Capital column names for library compatibility
- Handles exceptions gracefully with empty dict fallback
- Detects support/resistance levels and trendlines
- Returns structured metadata for storage and visualization

### Automatic Integration

Pattern detection is automatically triggered in `get_aggregated_bars()`:

```python
# Detect chart patterns if we have enough data
patterns = {}
if len(df) >= 50:  # Minimum data for pattern detection
    patterns = detect_chart_patterns(df)

# Store patterns as attribute for later access
if patterns and patterns.get('patterns'):
    df.attrs['chart_patterns'] = patterns
```

**Minimum Requirements**:
- At least 50 bars for pattern detection
- At least 200 bars for technical indicators

## Database Storage

### Schema

**Table**: `chart_patterns`

| Column | Type | Description |
|--------|------|-------------|
| `ticker` | TEXT | Stock symbol (e.g., "AAPL") |
| `timeframe` | TEXT | Chart timeframe (e.g., "1h", "1d") |
| `pattern_type` | TEXT | Pattern category (e.g., "head_shoulder") |
| `pattern_subtype` | TEXT | Variant (e.g., "bullish", "bearish") |
| `confidence` | FLOAT | Confidence score (0.0 - 1.0) |
| `start_index` | INT | Starting bar index |
| `end_index` | INT | Ending bar index |
| `indices` | INT[] | Array of all involved bar indices |
| `metadata` | JSONB | Full pattern metadata |
| `detected_at` | TIMESTAMPTZ | Detection timestamp |

**Indices**:
- `(ticker, timeframe, detected_at)`: Fast lookups by ticker/timeframe
- `(pattern_type, pattern_subtype)`: Pattern type filtering

### Storage Method

**Location**: `data/storage/timescale_writer.py` (lines ~620-730)

```python
def write_chart_patterns(
    self,
    ticker: str,
    timeframe: str,
    patterns: Dict,
    detected_at: datetime
) -> int:
    """
    Write chart patterns to TimescaleDB.
    
    Args:
        ticker: Stock symbol
        timeframe: Timeframe (e.g., '1h', '1d')
        patterns: Dict from detect_chart_patterns()
        detected_at: Detection timestamp
    
    Returns:
        Number of patterns written
    """
```

**Features**:
- Creates table automatically if not exists
- Uses `psycopg2.extras.execute_batch` for performance
- `ON CONFLICT DO NOTHING` for idempotency
- Returns count of patterns inserted

### Manual Pattern Storage

Use the utility function to manually trigger detection and storage:

```python
from scripts.backfill_historical_data import detect_and_store_patterns

# Detect and store patterns for AAPL on daily timeframe
count = detect_and_store_patterns(
    ticker='AAPL',
    interval='1d',
    start_time=datetime(2023, 1, 1),
    end_time=datetime.now()
)
print(f"Stored {count} patterns")
```

## Visualization

### Chart Backend Abstraction

**Location**: `scripts/backfill_visualizer.py` (lines ~1129-1390)

Three classes provide the abstraction layer:

#### 1. ChartBackend (Abstract Base Class)

```python
class ChartBackend:
    """Abstract base class for chart rendering backends"""
    
    @abstractmethod
    def create_widget(self) -> QWidget:
        """Create and return chart widget"""
        pass
    
    @abstractmethod
    def update_data(self, df, indicators, patterns):
        """Update chart with new data"""
        pass
    
    @abstractmethod
    def add_pattern_overlay(self, pattern, x, closes):
        """Draw pattern overlay on chart"""
        pass
```

#### 2. PyQtGraphBackend (Current Implementation)

Full implementation using PyQtGraph for rendering:

**Features**:
- Unified TradingView-style chart layout
- Three panels: Main (price + volume), RSI, MACD
- Candlestick rendering with color-coded volume overlay
- Technical indicator overlays (SMA, EMA, BB, VWAP)
- **Pattern overlays**: Shaded regions with labels

**Pattern Visualization**:
```python
def add_pattern_overlay(self, pattern, x, closes):
    """
    Draw pattern overlay on chart
    - Green: Bullish patterns
    - Red: Bearish patterns
    - Yellow: Neutral/pending patterns
    """
    # Shaded region over pattern range
    # Text label with pattern name + confidence
```

#### 3. FinPlotBackend (Future Migration)

Stub class for future FinPlot integration:

```python
class FinPlotBackend(ChartBackend):
    """Future: FinPlot backend (not yet implemented)"""
    
    def create_widget(self):
        raise NotImplementedError("FinPlot backend not yet implemented")
```

**Migration Path**: When ready to use FinPlot:
1. Implement methods in `FinPlotBackend`
2. Change backend selection in visualizer
3. No changes needed in GUI or data loading code

### Pattern Overlay Rendering

**Location**: `scripts/backfill_visualizer.py` → `_update_unified_chart()` (lines ~2091-2178)

```python
# === Chart Pattern Overlays ===
if hasattr(self, 'chart_patterns') and self.chart_patterns:
    patterns_list = self.chart_patterns.get('patterns', [])
    for pattern in patterns_list:
        # Determine color based on pattern type
        if 'bullish' in subtype:
            color = (76, 175, 80, 50)  # Green
        elif 'bearish' in subtype:
            color = (244, 67, 54, 50)  # Red
        else:
            color = (255, 235, 59, 50)  # Yellow
        
        # Draw shaded region
        rect = pg.QtWidgets.QGraphicsRectItem(...)
        rect.setBrush(pg.mkBrush(*color))
        
        # Add text label
        label = pg.TextItem(f"{pattern_type}\n{confidence:.1%}")
```

**Visual Design**:
- **Shaded Region**: Semi-transparent rectangle over pattern range
- **Text Label**: Pattern name + confidence percentage above region
- **Color Coding**:
  - 🟢 Green: Bullish patterns (ascending triangles, rising wedges)
  - 🔴 Red: Bearish patterns (descending triangles, falling wedges)
  - 🟡 Yellow: Neutral/uncertain patterns

### Data Flow

```
get_aggregated_bars()
    ↓
detect_chart_patterns()
    ↓
df.attrs['chart_patterns'] = patterns
    ↓
ChartDataLoader.run()
    ↓
MarketDataWidget._on_data_loaded()
    ↓
self.chart_patterns = df.attrs['chart_patterns']
    ↓
MarketDataWidget._update_unified_chart()
    ↓
Pattern overlay rendering
```

## Usage Examples

### 1. View Chart with Patterns

Run the visualizer (patterns automatically detected and displayed):

```bash
python scripts/backfill_visualizer.py
```

Select ticker (e.g., "AAPL") and timeframe (e.g., "1d"). Pattern overlays will appear automatically if patterns are detected in the data.

### 2. Batch Pattern Detection

Detect and store patterns for multiple tickers/timeframes:

```python
from scripts.backfill_historical_data import detect_and_store_patterns
from datetime import datetime, timedelta

tickers = ['AAPL', 'TSLA', 'NVDA', 'AMD']
timeframes = ['1h', '4h', '1d']

for ticker in tickers:
    for tf in timeframes:
        count = detect_and_store_patterns(
            ticker=ticker,
            interval=tf,
            start_time=datetime.now() - timedelta(days=365),
            end_time=datetime.now()
        )
        print(f"{ticker} {tf}: {count} patterns")
```

### 3. Query Stored Patterns

```python
from data.storage.timescale_writer import TimescaleWriter

db = TimescaleWriter()

# Get all patterns for AAPL on 1d timeframe
query = """
    SELECT pattern_type, pattern_subtype, confidence, 
           start_index, end_index, detected_at
    FROM chart_patterns
    WHERE ticker = %s AND timeframe = %s
    ORDER BY detected_at DESC
"""
patterns = db.execute_query(query, ('AAPL', '1d'))

for p in patterns:
    print(f"{p[0]} ({p[1]}): {p[2]:.1%} confidence")
```

## Testing Checklist

- [ ] **Install Dependencies**
  ```bash
  python scripts/install_market_data_deps.py
  ```

- [ ] **Test Pattern Detection**
  ```python
  from scripts.backfill_historical_data import get_aggregated_bars
  df = get_aggregated_bars('AAPL', '1d')
  print(df.attrs.get('chart_patterns'))
  ```

- [ ] **Test Database Storage**
  ```python
  from scripts.backfill_historical_data import detect_and_store_patterns
  count = detect_and_store_patterns('AAPL', '1d')
  print(f"Stored {count} patterns")
  ```

- [ ] **Test Visualization**
  ```bash
  python scripts/backfill_visualizer.py
  # Select AAPL, 1d timeframe
  # Verify pattern overlays appear
  ```

- [ ] **Verify Chart Backend Abstraction**
  - Pattern overlays render correctly
  - Switching timeframes updates patterns
  - No performance degradation

## Performance Considerations

### Pattern Detection
- **Time Complexity**: O(n²) for most pattern algorithms
- **Memory**: Minimal (stores indices, not data)
- **Threshold**: Requires ≥50 bars minimum
- **Optimization**: Pre-computed during data aggregation

### Database Storage
- **Batch Insert**: Uses `execute_batch` for 10-100x speedup
- **Indices**: Optimized for ticker/timeframe/time queries
- **JSONB**: Efficient storage with queryable metadata
- **Idempotency**: `ON CONFLICT DO NOTHING` prevents duplicates

### Visualization
- **Lazy Rendering**: Patterns only rendered when visible
- **Throttled Updates**: 30 FPS limit prevents overdraw
- **Selective Overlays**: Only current timeframe patterns shown

## Future Enhancements

### Immediate (Phase 1 Complete ✅)
- [x] PatternPy integration
- [x] Pattern detection in data pipeline
- [x] Database storage schema
- [x] Chart backend abstraction
- [x] Pattern visualization overlays

### Near-term (Phase 2)
- [ ] Background worker for continuous pattern detection
- [ ] Pattern detection during live streaming
- [ ] Pattern cache for real-time updates
- [ ] Pattern alerts/notifications

### Long-term (Phase 3)
- [ ] Complete FinPlot backend implementation
- [ ] A/B test PyQtGraph vs FinPlot performance
- [ ] Pattern confidence tuning with ML
- [ ] Historical pattern performance tracking

## Migration Path: FinPlot Integration

When ready to migrate to FinPlot:

1. **Implement FinPlotBackend**:
   ```python
   class FinPlotBackend(ChartBackend):
       def create_widget(self):
           # Create finplot chart widget
           
       def update_data(self, df, indicators, patterns):
           # Render with finplot
           
       def add_pattern_overlay(self, pattern, x, closes):
           # Draw pattern overlay in finplot
   ```

2. **Switch Backend**:
   ```python
   # In MarketDataWidget.__init__()
   # Old: self.backend = PyQtGraphBackend()
   self.backend = FinPlotBackend()  # New
   ```

3. **Test & Compare**:
   - Performance benchmarks
   - Visual quality comparison
   - User feedback

4. **Gradual Rollout**:
   - Feature flag for backend selection
   - Monitor stability in production
   - Deprecate PyQtGraph once stable

## Troubleshooting

### Pattern Detection Issues

**Problem**: No patterns detected
```python
# Check data length
df = get_aggregated_bars('AAPL', '1d')
print(f"Data length: {len(df)}")  # Need ≥50 bars
```

**Problem**: Wrong column names
```python
# Verify columns
print(df.columns.tolist())
# Should have: ['time', 'open', 'high', 'low', 'close', 'volume']
```

### Database Storage Issues

**Problem**: Patterns not stored
```python
# Check connection
from data.storage.timescale_writer import TimescaleWriter
db = TimescaleWriter()
print(db.test_connection())
```

**Problem**: Duplicate patterns
```
# Already handled via ON CONFLICT DO NOTHING
# Patterns with same ticker/timeframe/indices are ignored
```

### Visualization Issues

**Problem**: Patterns not rendering
```python
# Check chart_patterns attribute
if hasattr(df, 'attrs'):
    print(df.attrs.get('chart_patterns'))
```

**Problem**: Wrong colors
```python
# Pattern colors determined by subtype:
# - 'bullish' → Green
# - 'bearish' → Red
# - other → Yellow
```

## Support

For issues or questions:
1. Check this README
2. Review code comments in source files
3. Check logs for error messages
4. Test with minimal example (AAPL, 1d timeframe)

## Credits

- **PatternPy**: https://github.com/keithorange/PatternPy
- **FinPlot**: https://github.com/highfestiva/finplot
- **PyQtGraph**: http://pyqtgraph.org/
- **TimescaleDB**: https://www.timescale.com/
