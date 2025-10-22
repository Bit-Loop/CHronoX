# GUI Table Population Fixes

## Issues Fixed

### 1. **Pipeline Monitor & Processing Queue Tables Were Empty**
**Problem:** DEBUG logs weren't being parsed, so Phase 4 queue/stack operations never populated the tables.

**Solution:** Added comprehensive parsing for all DEBUG log formats:
- `DEBUG: Queue Pull -> file0043 [2025-01-31] from download_queue (qsize=99)`
- `DEBUG: Stack Update -> file0043 [2025-01-31] → DOWNLOADING`
- `DEBUG: Stack Push -> file0043 [2025-01-31] to process_queue (qsize=1)`
- `DEBUG: Stack Pop -> file0043 [2025-01-31] → COMPLETED`

**File:** `backfill_visualizer.py` lines ~390-520

---

### 2. **Daily Bars Table Showed Phase 3 Data Before Phase 4 Started**
**Problem:** 
- Daily Bars table was showing Phase 3 (REST API daily bars) results
- Phase 3 completes immediately, so table populated before Phase 4 (flatfiles) even started
- User saw "AMD | 2020-2025 | 0 bars | Complete | 100%" before any downloads

**Solution:** 
- Separated Phase 3 (REST API daily bars) from Phase 4 (flatfile minute bars)
- Added new `phase4_data` tracking dictionary for individual file processing
- In **normal mode**: Shows Phase 3 aggregate data (ticker-level summary)
- In **debug mode**: Shows Phase 4 file-by-file processing (date-level detail)

**Column Headers:**
- **Normal Mode:** `["Ticker", "Date Range", "Bars", "Status", "Progress"]`
- **Debug Mode:** `["Date", "Tickers", "Bars", "Status", "Progress"]`

**File:** `backfill_visualizer.py` lines ~1295-1372

---

### 3. **No Per-File Bar Counts in Debug Mode**
**Problem:** No logging to show how many bars were written from each flatfile.

**Solution:** Added debug logging in processing worker:
```python
if self.debug and total_bars_in_file > 0:
    ticker_list = ', '.join(sorted(groups.keys())[:5])  # Show first 5 tickers
    if len(groups) > 5:
        ticker_list += f" +{len(groups)-5} more"
    logger.info(f"DEBUG: Wrote {total_bars_in_file:,} bars from {task.date} ({ticker_list})")
```

**Example Output:**
```
DEBUG: Wrote 12,345 bars from 2025-01-31 (AMD, AAPL, GOOGL, MSFT, TSLA +25 more)
```

**File:** `backfill_historical_data.py` lines ~873-880

---

## New Features

### Debug Mode Indicator
When debug mode is enabled, the GUI shows:
```
🔍 Debug mode ENABLED - showing detailed queue/stack operations
```

This appears in the log immediately after starting backfill.

---

## Data Structures

### Phase Tracking (in `BackfillVisualizer.__init__`)

```python
self.phase1_data: Dict[str, Dict] = {}  
# ticker -> {status, name, exchange, type, market_cap}

self.phase2_data: Dict[str, Dict] = {}  
# ticker -> {dividends: count, splits: count, div_details: [], split_details: []}

self.phase3_data: Dict[str, Dict] = {}  
# ticker -> {bars, status, progress, date_range}
# Used in NORMAL mode for Daily Bars table

self.phase4_data: Dict[str, Dict] = {}  
# date -> {bars, tickers, status}
# Used in DEBUG mode for Daily Bars table

self.debug_mode: bool = False  
# Tracks current debug state
```

---

## Phase Signal Handlers

### New: `phase4_bars_written`

**Triggered by:** `DEBUG: Wrote 12,345 bars from 2025-01-31 (AMD, AAPL, ...)`

**Data:**
```python
{
    'bars': 12345,
    'date': '2025-01-31',
    'tickers': 'AMD, AAPL, GOOGL, MSFT, TSLA +25 more'
}
```

**Action:** Updates `phase4_data[date]` and refreshes Daily Bars table in debug mode.

---

## Debug Log Parsing Summary

### Download Worker Logs Parsed:

| Log Format | State Update | Table |
|------------|-------------|--------|
| `DEBUG: Queue Pull ... from download_queue` | WAITING | Pipeline Monitor (Download Queue) |
| `DEBUG: Stack Update ... → DOWNLOADING` | DOWNLOADING | Pipeline Monitor |
| `DEBUG: Stack Update ... → DOWNLOADED` | DOWNLOADED | Pipeline Monitor |
| `DEBUG: Stack Push ... to process_queue` | DOWNLOADED | Pipeline Monitor → Processing Queue |
| `DEBUG: Stack Update ... → RETRYING (attempt 2/3)` | RETRYING | Pipeline Monitor |
| `DEBUG: Stack Update ... → FAILED (404)` | FAILED | Pipeline Monitor |

### Processing Worker Logs Parsed:

| Log Format | State Update | Table |
|------------|-------------|--------|
| `DEBUG: Queue Pull ... from process_queue` | DOWNLOADED | Processing Queue |
| `DEBUG: Stack Update ... → PROCESSING` | PROCESSING | Processing Queue |
| `DEBUG: Stack Pop ... → COMPLETED` | COMPLETED | Processing Queue |
| `DEBUG: Stack Update ... → FAILED (processing error)` | FAILED | Processing Queue |
| `DEBUG: Wrote 12,345 bars from 2025-01-31` | (bars count) | Daily Bars (debug mode) |

---

## Testing

### Test 1: Normal Mode (No Debug)
1. Uncheck "Debug Mode" checkbox
2. Start backfill for AMD
3. **Expected:** 
   - Phase 1-3 tables populate normally
   - Daily Bars shows: `AMD | 2020-01-01 to 2024-12-31 | 1,234 bars | Complete | 100%`
   - Pipeline Monitor shows file state transitions (legacy format)
   - Processing Queue shows files being processed

### Test 2: Debug Mode
1. Check "Debug Mode" checkbox
2. Start backfill for AMD
3. **Expected:**
   - Log shows: `🔍 Debug mode ENABLED - showing detailed queue/stack operations`
   - Pipeline Monitor shows detailed DEBUG logs with queue sizes
   - Processing Queue shows DEBUG logs with stack operations
   - Daily Bars table switches to date-based view:
     ```
     Date       | Tickers                              | Bars    | Status   | Progress
     2020-01-02 | AMD, AAPL, GOOGL +20 more            | 12,345  | Complete | 100%
     2020-01-03 | AMD, AAPL, GOOGL +20 more            | 11,987  | Complete | 100%
     ...
     ```

### Test 3: Phase Progression
1. Start backfill with debug enabled
2. **Expected progression:**
   - **Phase 1:** Reference Data table populates (ticker, name, exchange, type, market cap)
   - **Phase 2:** Corporate Actions table populates (dividends/splits with hierarchical details)
   - **Phase 3:** Daily Bars table shows ticker-level summary (normal mode) OR stays empty (debug mode)
   - **Phase 4:** 
     - Pipeline Monitor updates in real-time with file states
     - Processing Queue shows files being processed
     - Daily Bars table populates with individual dates (debug mode only)

---

## File Changes Summary

### `backfill_historical_data.py`
- **Lines ~873-880:** Added `total_bars_in_file` counter and debug logging for bars written per file
- **Output:** `DEBUG: Wrote 12,345 bars from 2025-01-31 (AMD, AAPL, GOOGL +20 more)`

### `backfill_visualizer.py`
- **Lines ~390-520:** Added parsing for all DEBUG log formats (Queue Pull, Stack Update, Stack Push, Stack Pop)
- **Lines ~520-545:** Added parsing for `DEBUG: Wrote ... bars from ...` format
- **Line ~570:** Added `self.phase4_data` and `self.debug_mode` tracking
- **Lines ~825-840:** Updated `_start_backfill()` to set `self.debug_mode` and show debug indicator
- **Lines ~1145-1158:** Added `phase4_bars_written` handler in `_update_phase()`
- **Lines ~1295-1372:** Rewrote `_refresh_daily_table()` to handle both normal and debug modes with dynamic column headers

---

## Expected Behavior

### Normal Mode (Debug OFF)
```
=== Phase 3 Complete ===
✓ AMD: 1,234 daily bars | 2020-01-01 to 2024-12-31

[Daily Bars Table]
Ticker | Date Range                   | Bars  | Status   | Progress
AMD    | 2020-01-01 to 2024-12-31     | 1,234 | Complete | 100%
```

### Debug Mode (Debug ON)
```
=== Phase 4 Starting ===
🔍 Debug mode ENABLED - showing detailed queue/stack operations

DEBUG: Queue Pull -> file0001 [2020-01-02] from download_queue (qsize=99)
DEBUG: Stack Update -> file0001 [2020-01-02] → DOWNLOADING
DEBUG: Stack Update -> file0001 [2020-01-02] → DOWNLOADED
DEBUG: Stack Push -> file0001 [2020-01-02] to process_queue (qsize=1)
DEBUG: Queue Pull -> file0001 [2020-01-02] from process_queue (qsize=0)
DEBUG: Stack Update -> file0001 [2020-01-02] → PROCESSING
DEBUG: Wrote 12,345 bars from 2020-01-02 (AMD, AAPL, GOOGL +20 more)
DEBUG: Stack Pop -> file0001 [2020-01-02] → COMPLETED

[Daily Bars Table - Debug Mode]
Date       | Tickers                              | Bars    | Status   | Progress
2020-01-02 | AMD, AAPL, GOOGL, MSFT, TSLA +20 more| 12,345  | Complete | 100%
2020-01-03 | AMD, AAPL, GOOGL, MSFT, TSLA +20 more| 11,987  | Complete | 100%
2020-01-06 | AMD, AAPL, GOOGL, MSFT, TSLA +20 more| 13,421  | Complete | 100%
```

---

## Conclusion

✅ **Pipeline Monitor** - Now populates with DEBUG logs showing queue sizes and state transitions

✅ **Processing Queue** - Now populates with DEBUG logs showing stack operations

✅ **Reference Data** - Already working (Phase 1)

✅ **Corporate Actions** - Already working (Phase 2)

✅ **Daily Bars** - Now shows:
- **Normal Mode:** Phase 3 aggregate data (ticker-level summary)
- **Debug Mode:** Phase 4 file-by-file processing (date-level detail)

**The GUI now properly tracks all phases and updates tables in real-time!** 🎉
