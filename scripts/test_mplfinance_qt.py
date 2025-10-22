#!/usr/bin/env python3
"""
Test if mplfinance can embed in PyQt6 properly
"""

import sys
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
import pandas as pd
import mplfinance as mpf
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

# Create sample OHLCV data
data = {
    'Open': [100, 102, 101, 103, 102],
    'High': [105, 106, 104, 107, 106],
    'Low': [99, 101, 100, 102, 101],
    'Close': [103, 104, 102, 105, 104],
    'Volume': [1000, 1100, 900, 1200, 1050]
}
df = pd.DataFrame(data, index=pd.date_range('2024-01-01', periods=5, freq='1h'))

class TestWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mplfinance + PyQt6 Test")
        self.setGeometry(100, 100, 1200, 800)
        
        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        # Create matplotlib figure
        fig = Figure(figsize=(12, 8), facecolor='#1e1e1e')
        canvas = FigureCanvas(fig)
        layout.addWidget(canvas)
        
        # Plot using mplfinance
        try:
            mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350')
            s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', facecolor='#1e1e1e')
            
            # mplfinance creates its own figure - we need to extract it
            fig_mpf, axes = mpf.plot(df, type='candle', style=s, volume=True,
                                     returnfig=True, figsize=(12, 8))
            
            # Now we need to embed fig_mpf into our canvas
            # Actually, mplfinance returns a NEW figure - we can't use our pre-created one
            # Let's create a new canvas with the returned figure
            layout.removeWidget(canvas)
            canvas.deleteLater()
            
            new_canvas = FigureCanvas(fig_mpf)
            layout.addWidget(new_canvas)
            new_canvas.draw()
            
            print("✅ mplfinance embedded successfully!")
            
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = TestWindow()
    window.show()
    print("Window opened - close it to exit")
    sys.exit(app.exec())
