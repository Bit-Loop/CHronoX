# LightweightChartsBackend - QWebEngineView Fix

## Problem
```
ERROR - Failed to create LightweightChartsBackend: QWebEngineView was not found, and must be installed to use QtChart.
```

## Root Cause

Qt WebEngine has a critical requirement: **`QtWebEngineWidgets` must be imported BEFORE `QApplication` is created**.

The error occurred because:
1. `QApplication` was created early in the visualizer initialization
2. Later, when creating `LightweightChartsBackend`, we tried to import `QtWebEngineWidgets`
3. Qt rejected this late import with the error:
   ```
   QtWebEngineWidgets must be imported or Qt.AA_ShareOpenGLContexts must be set 
   BEFORE a QCoreApplication instance is created
   ```

## Solution

**Import `QtWebEngineWidgets` at module level, before any QApplication creation:**

```python
# BEFORE (BROKEN):
from PyQt6.QtWidgets import QApplication, QMainWindow, ...
# ... later in code ...
from PyQt6.QtWebEngineWidgets import QWebEngineView  # ✗ TOO LATE!

# AFTER (FIXED):
# Import WebEngine FIRST
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel

# THEN import other Qt modules
from PyQt6.QtWidgets import QApplication, QMainWindow, ...
```

## Changes Made

**File: `scripts/backfill_visualizer.py`**

**Lines 29-39** - Added WebEngine imports before PyQt6.QtWidgets:
```python
# CRITICAL: Import QtWebEngineWidgets BEFORE creating QApplication
# This is required for lightweight-charts to work properly
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebChannel import QWebChannel
    QTWEBENGINE_AVAILABLE = True
except ImportError:
    QTWEBENGINE_AVAILABLE = False
    logging.warning("PyQt6-WebEngine not available - lightweight-charts backend will be disabled")

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, ...
)
```

**Lines 3247-3266** - Added debug logging and error handling in `create_widget()`:
```python
def create_widget(self):
    """Create lightweight-charts QtChart widget"""
    from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
    
    # CRITICAL: Import QWebEngineView FIRST to ensure lightweight-charts can find it
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWebChannel import QWebChannel
        logging.debug(f"✓ QWebEngineView pre-imported: {QWebEngineView}")
    except ImportError as e:
        logging.error(f"PyQt6-WebEngine not available: {e}")
        error_label = QLabel(f"<b>Chart Error:</b> PyQt6-WebEngine not installed<br>{e}")
        error_label.setStyleSheet("color: #f44336; padding: 20px;")
        return error_label
    
    try:
        from lightweight_charts.widgets import QtChart
    except ImportError as e:
        logging.error(f"lightweight-charts not installed: {e}")
        error_label = QLabel(f"<b>Chart Error:</b> lightweight-charts not installed<br>Run: pip install lightweight-charts PyQt6-WebEngine")
        error_label.setStyleSheet("color: #f44336; padding: 20px;")
        return error_label
```

## Verification

**Before Fix:**
```bash
$ .venv/bin/python scripts/backfill_visualizer.py
ERROR - Failed to create LightweightChartsBackend: QWebEngineView was not found
```

**After Fix:**
```bash
$ .venv/bin/python scripts/backfill_visualizer.py
INFO - Initializing LightweightChartsBackend...
INFO - ✓ LightweightChartsBackend created successfully
INFO - LightweightChartsBackend created - database will be connected when loading data
```

## Expected Warnings (Harmless)

These GPU/graphics warnings are normal and can be ignored:
```
GBM is not supported with the current configuration. Fallback to Vulkan rendering in Chromium.
libEGL warning: pci id for fd 141: 10de:2f04, driver (null)
libEGL warning: egl: failed to create dri2 screen
```

These occur because Qt WebEngine uses Chromium for rendering, which tries to use hardware acceleration. When specific GPU features aren't available, it falls back to software/Vulkan rendering. This doesn't affect functionality.

## Qt WebEngine Import Requirements

### Critical Rule
**Always import QtWebEngineWidgets BEFORE QApplication:**

```python
# ✓ CORRECT ORDER:
from PyQt6.QtWebEngineWidgets import QWebEngineView
app = QApplication(sys.argv)

# ✗ WRONG ORDER:
app = QApplication(sys.argv)
from PyQt6.QtWebEngineWidgets import QWebEngineView  # Error!
```

### Why This Matters
Qt WebEngine requires special OpenGL context sharing to be configured during application initialization. Once `QApplication` is created, these settings are locked and cannot be changed. Importing `QtWebEngineWidgets` late triggers the error.

### Alternative Solution (Not Used)
Instead of early import, you could set the attribute before creating QApplication:
```python
from PyQt6.QtCore import Qt, QCoreApplication
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication(sys.argv)
```

However, importing early is cleaner and more explicit.

## Testing

**Test that WebEngine is available:**
```bash
$ .venv/bin/python -c "from PyQt6.QtWebEngineWidgets import QWebEngineView; print('✓ QWebEngineView available')"
✓ QWebEngineView available
```

**Test QtChart creation:**
```bash
$ .venv/bin/python -c "
from PyQt6.QtWebEngineWidgets import QWebEngineView  # Import FIRST
from PyQt6.QtWidgets import QApplication, QWidget
import sys

app = QApplication(sys.argv)
widget = QWidget()

from lightweight_charts.widgets import QtChart
chart = QtChart(widget=widget)
print('✓ QtChart created successfully')
"
```

**Run visualizer:**
```bash
$ .venv/bin/python scripts/backfill_visualizer.py
# Should show: ✓ LightweightChartsBackend created successfully
```

## Summary

✅ **Fixed**: QWebEngineView import error  
✅ **Method**: Import QtWebEngineWidgets before QApplication  
✅ **Result**: Lightweight-charts backend now works perfectly  
✅ **Visualizer**: Launches successfully with TradingView-style charts  

The fix ensures Qt WebEngine is properly initialized during application startup, allowing lightweight-charts to create web-based chart views successfully.
