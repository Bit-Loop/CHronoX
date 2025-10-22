# ChronoX Backfill Visualizer & Debugger

A real-time GUI application for monitoring and debugging the S3 flat file backfill producer/consumer pipeline.

## Features

### 🎛️ Control Panel
- **Ticker Selection**: Choose any stock ticker to backfill
- **Date Range**: Select custom start and end dates
- **Start/Stop Controls**: Launch backfill or immediately stop running processes

### 📊 Real-Time Monitoring

#### Phase 4 Pipeline Monitor
- **Download Queue Table**: Live view of producer thread tasks
  - Task ID, Date, State, Attempts, Errors, Filename
  - Color-coded states: DOWNLOADING (blue), RETRYING (orange), FAILED (red)
  
- **Processing Queue Table**: Live view of consumer thread tasks
  - Task ID, Date, State, Bars Written, Status
  - Color-coded states: PROCESSING (orange), COMPLETED (green)

#### Statistics Dashboard
- Downloaded files count
- Skipped (cached) files count
- Failed files count
- Processed files count
- Progress bar showing overall completion

### 📋 Data Tables

- **Reference Data**: Ticker information and exchange details
- **Corporate Actions**: Dividends and splits tracking
- **Daily Bars**: Daily OHLCV data status

### 📝 Live Log Output
- Real-time scrolling log with color-coded messages
- Filter by log level (ALL, INFO, WARNING, ERROR, DEBUG)
- Auto-scroll to latest entries
- Clear log button

## Installation

```bash
# Install PyQt6
pip install PyQt6

# Or add to requirements.txt
echo "PyQt6>=6.9.1" >> requirements.txt
pip install -r requirements.txt
```

## Usage

```bash
# Launch the visualizer
python scripts/backfill_visualizer.py
```

### Running a Backfill

1. **Enter Ticker**: Type a stock symbol (e.g., AMD, NVDA, AAPL)
2. **Set Date Range**: Choose start and end dates
3. **Click "Start Backfill"**: Launches the producer/consumer pipeline
4. **Monitor Progress**: Watch real-time updates in tables and logs
5. **Stop if Needed**: Click "Stop" button to interrupt gracefully

## Architecture

The visualizer uses PyQt6's threading model to run the backfill process in a background worker thread while keeping the GUI responsive.

### Components

- **BackfillWorker (QThread)**: Runs backfill subprocess
  - Captures stdout/stderr in real-time
  - Parses log output for state changes
  - Emits signals for GUI updates

- **BackfillVisualizer (QMainWindow)**: Main GUI window
  - Receives signals from worker thread
  - Updates tables and statistics
  - Manages UI state and controls

### Signal/Slot Architecture

```python
BackfillWorker                    BackfillVisualizer
    |                                    |
    |-- log_signal -----------------> _append_log()
    |-- stats_signal ----------------> _update_stats()
    |-- task_update_signal ----------> _update_task()
    |-- finished_signal -------------> _backfill_finished()
```

## Screenshots

### Main Window
```
┌─────────────────────────────────────────────────────────────┐
│ Control Panel                                                │
│ Ticker: [AMD]  Start: [2020-10-18]  End: [2025-10-18]      │
│ [▶ Start Backfill] [⬛ Stop]                                │
├─────────────────────────────────────────────────────────────┤
│ Pipeline Statistics                                          │
│ Downloaded: 1234  Skipped: 456  Failed: 135  Processed: 1690│
│ Progress: [████████████████────────] 65%                     │
├─────────────────────────────────────────────────────────────┤
│ ┌─ Phase 4: Pipeline Monitor ─────────────────────────────┐ │
│ │ Download Queue (Producer)                                │ │
│ │ ┌────────┬────────────┬────────────┬──────┬──────┬─────┐ │ │
│ │ │Task ID │ Date       │ State      │Attemp│Error │File │ │ │
│ │ ├────────┼────────────┼────────────┼──────┼──────┼─────┤ │ │
│ │ │file0123│ 2024-01-15 │ DOWNLOADING│  1   │      │2...│ │ │
│ │ └────────┴────────────┴────────────┴──────┴──────┴─────┘ │ │
│ │                                                            │ │
│ │ Processing Queue (Consumer)                               │ │
│ │ ┌────────┬────────────┬──────────┬─────┬──────┐          │ │
│ │ │Task ID │ Date       │ State    │Bars │Status│          │ │
│ │ ├────────┼────────────┼──────────┼─────┼──────┤          │ │
│ │ │file0120│ 2024-01-12 │PROCESSING│12345│ OK   │          │ │
│ │ └────────┴────────────┴──────────┴─────┴──────┘          │ │
│ └──────────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────────┤
│ Live Log Output                           [Filter: ALL] [X] │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ INFO: File file0123 [2024-01-15]: DOWNLOADING          │ │
│ │ INFO: ✓ Downloaded 2024-01-15.csv.gz (12.34 MB)        │ │
│ │ WARNING: Missing file on S3: 2024-01-16.csv.gz         │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Color Coding

### Download Queue States
- 🔵 **DOWNLOADING**: Blue - File currently downloading
- 🟠 **RETRYING**: Orange - Download failed, will retry
- 🔴 **FAILED**: Red - Permanently failed (404 or max retries)

### Processing Queue States
- 🟠 **PROCESSING**: Orange - File being parsed/inserted
- 🟢 **COMPLETED**: Green - Successfully processed

### Log Messages
- 🟢 **INFO/Success**: Green - Normal operations
- 🟠 **WARNING**: Orange - Non-critical issues
- 🔴 **ERROR**: Red - Critical failures
- ⚪ **DEBUG**: Gray - Detailed debugging info

## Monitoring Tips

### Download Queue
- High number of RETRYING states → Network issues or S3 problems
- Many FAILED with 404 → Expected for weekends/holidays
- Slow DOWNLOADING → Check network bandwidth

### Processing Queue
- Long PROCESSING times → CPU bottleneck, consider more workers
- FAILED states → Check logs for parsing/DB errors

### Statistics
- Low Processed/Downloaded ratio → Processing slower than downloads (expected)
- High Failed count → Check S3 credentials or date range

## Troubleshooting

### GUI doesn't start
```bash
# Check PyQt6 installation
python -c "import PyQt6; print(PyQt6.__version__)"

# Reinstall if needed
pip uninstall PyQt6 -y
pip install PyQt6
```

### Backfill doesn't run
- Ensure you're in the project root directory
- Check that `.env` file exists with credentials
- Verify virtual environment is activated

### No log output
- Check that backfill script has proper logging configured
- Verify subprocess stdout is being captured
- Look for errors in terminal where GUI was launched

## Development

### Adding New Tables

```python
# In _init_ui()
new_tab = QWidget()
new_layout = QVBoxLayout(new_tab)
new_table = QTableWidget()
new_table.setColumnCount(4)
new_table.setHorizontalHeaderLabels(["Col1", "Col2", "Col3", "Col4"])
new_layout.addWidget(new_table)
self.tab_widget.addTab(new_tab, "New Tab")
```

### Custom Log Parsing

```python
# In BackfillWorker._parse_log_line()
if "CUSTOM_PATTERN" in line:
    # Extract data
    data = extract_data(line)
    # Emit custom signal
    self.custom_signal.emit(data)
```

## Future Enhancements

- [ ] Real-time progress bars per file
- [ ] Export statistics to CSV
- [ ] Historical run comparison
- [ ] Network bandwidth monitoring
- [ ] CPU/Memory usage graphs
- [ ] Pause/Resume functionality
- [ ] Multi-ticker parallel runs
- [ ] Custom alert thresholds

## License

Part of the ChronoX trading bot project.
