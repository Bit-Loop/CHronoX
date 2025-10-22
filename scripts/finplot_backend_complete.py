"""
Complete FinPlotBackend implementation using mplfinance
Replace the FinPlotBackend class in backfill_visualizer.py with this code
"""

import logging
import pandas as pd
import numpy as np


class FinPlotBackend:
    """FinPlot implementation using mplfinance - professional financial charting"""
    
    def __init__(self, parent_widget):
        self.parent = parent_widget
        import mplfinance as mpf
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
        from matplotlib.figure import Figure
        import matplotlib.pyplot as plt
        
        self.mpf = mpf
        self.plt = plt
        self.FigureCanvas = FigureCanvas
        self.Figure = Figure
        
        self.canvas = None
        self.figure = None
        self.axes = {}
        
        self.last_df = None
        self.last_indicators = {}
        self.last_patterns = None
        self.current_display_mode = 'price'
        self.current_reference_price = None
        
        # Configure matplotlib style for dark theme
        plt.style.use('dark_background')
    
    def create_widget(self):
        """Create matplotlib canvas widget for embedding charts"""
        from PyQt6.QtWidgets import QWidget, QVBoxLayout
        
        # Create a container widget
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 0, 0)
        
        # Placeholder canvas (will be replaced when data is plotted)
        self.figure = self.Figure(figsize=(12, 8), facecolor='#1e1e1e')
        self.canvas = self.FigureCanvas(self.figure)
        self.container_layout.addWidget(self.canvas)
        
        return self.container
    
    def update_data(self, df, indicators, patterns=None, ticker='', timeframe='', display_mode='price', reference_price=None):
        """Update chart with OHLCV data using mplfinance"""
        if df is None or df.empty:
            return
        
        try:
            # Store for later use
            self.last_df = df.copy()
            self.last_indicators = indicators
            self.last_patterns = patterns
            self.current_display_mode = display_mode
            self.current_reference_price = reference_price
            
            # Clear previous plots
            self.figure.clear()
            
            # Prepare dataframe with datetime index (required by mplfinance)
            plot_df = df.copy()
            if not isinstance(plot_df.index, pd.DatetimeIndex):
                if 'timestamp' in plot_df.columns:
                    plot_df.index = pd.to_datetime(plot_df['timestamp'])
                else:
                    plot_df.index = pd.date_range(start='2024-01-01', periods=len(plot_df), freq='1min')
            
            # Apply percentage mode if needed
            if display_mode == 'percentage' and reference_price:
                ref = reference_price
                plot_df['open'] = (plot_df['open'] / ref - 1) * 100
                plot_df['high'] = (plot_df['high'] / ref - 1) * 100
                plot_df['low'] = (plot_df['low'] / ref - 1) * 100
                plot_df['close'] = (plot_df['close'] / ref - 1) * 100
                ylabel = '% Change'
            else:
                ylabel = 'Price ($)'
            
            # Build list of additional plots (indicators)
            add_plots = []
            
            # Add moving averages
            if indicators.get('show_sma'):
                if 'sma_20' in plot_df.columns:
                    sma20 = plot_df['sma_20'].copy()
                    if display_mode == 'percentage' and reference_price:
                        sma20 = (sma20 / reference_price - 1) * 100
                    add_plots.append(self.mpf.make_addplot(sma20, color='#FFA726', width=2))
                
                if 'sma_50' in plot_df.columns:
                    sma50 = plot_df['sma_50'].copy()
                    if display_mode == 'percentage' and reference_price:
                        sma50 = (sma50 / reference_price - 1) * 100
                    add_plots.append(self.mpf.make_addplot(sma50, color='#FF7043', width=2))
            
            if indicators.get('show_ema'):
                if 'ema_20' in plot_df.columns:
                    ema20 = plot_df['ema_20'].copy()
                    if display_mode == 'percentage' and reference_price:
                        ema20 = (ema20 / reference_price - 1) * 100
                    add_plots.append(self.mpf.make_addplot(ema20, color='#42A5F5', width=2))
                
                if 'ema_50' in plot_df.columns:
                    ema50 = plot_df['ema_50'].copy()
                    if display_mode == 'percentage' and reference_price:
                        ema50 = (ema50 / reference_price - 1) * 100
                    add_plots.append(self.mpf.make_addplot(ema50, color='#1E88E5', width=2))
                
                if 'ema_100' in plot_df.columns:
                    ema100 = plot_df['ema_100'].copy()
                    if display_mode == 'percentage' and reference_price:
                        ema100 = (ema100 / reference_price - 1) * 100
                    add_plots.append(self.mpf.make_addplot(ema100, color='#1565C0', width=1))
                
                if 'ema_200' in plot_df.columns:
                    ema200 = plot_df['ema_200'].copy()
                    if display_mode == 'percentage' and reference_price:
                        ema200 = (ema200 / reference_price - 1) * 100
                    add_plots.append(self.mpf.make_addplot(ema200, color='#0D47A1', width=1))
            
            # Add Bollinger Bands
            if indicators.get('show_bb'):
                if 'bb_upper' in plot_df.columns and 'bb_lower' in plot_df.columns:
                    bb_upper = plot_df['bb_upper'].copy()
                    bb_lower = plot_df['bb_lower'].copy()
                    
                    if display_mode == 'percentage' and reference_price:
                        bb_upper = (bb_upper / reference_price - 1) * 100
                        bb_lower = (bb_lower / reference_price - 1) * 100
                    
                    add_plots.append(self.mpf.make_addplot(bb_upper, color='#78909C', linestyle='--', width=1))
                    add_plots.append(self.mpf.make_addplot(bb_lower, color='#78909C', linestyle='--', width=1))
                    
                    # Fill between (approximation using matplotlib after plot)
                    # Will be added post-plotting
            
            # Add VWAP
            if indicators.get('show_vwap'):
                if 'vwap' in plot_df.columns:
                    vwap = plot_df['vwap'].copy()
                    if display_mode == 'percentage' and reference_price:
                        vwap = (vwap / reference_price - 1) * 100
                    add_plots.append(self.mpf.make_addplot(vwap, color='#FFEE58', width=2))
            
            # Create custom style
            mc = self.mpf.make_marketcolors(up='#26a69a', down='#ef5350',
                                           edge='inherit',
                                           wick={'up':'#26a69a','down':'#ef5350'},
                                           volume={'up':'#26a69a80','down':'#ef535080'})
            
            s = self.mpf.make_mpf_style(marketcolors=mc, gridstyle=':', 
                                       facecolor='#1e1e1e', figcolor='#1e1e1e',
                                       edgecolor='#4e4e4e', gridcolor='#4e4e4e')
            
            # Determine panel ratios
            panel_ratios = [3, 1]  # Main chart + volume
            if indicators.get('show_rsi'):
                panel_ratios.append(1)
            if indicators.get('show_macd'):
                panel_ratios.append(1)
            
            # Plot using mplfinance
            kwargs = {
                'type': 'candle',
                'style': s,
                'volume': True,
                'figsize': (12, 8),
                'title': f'{ticker} - {timeframe}',
                'ylabel': ylabel,
                'returnfig': True,
                'fig': self.figure,
                'panel_ratios': tuple(panel_ratios)
            }
            
            if add_plots:
                kwargs['addplot'] = add_plots
            
            # Create the plot (mplfinance creates its own figure)
            self.figure, axes = self.mpf.plot(plot_df, **kwargs)
            
            # Replace canvas with new one containing the updated figure
            if self.canvas:
                self.container_layout.removeWidget(self.canvas)
                self.canvas.deleteLater()
            
            self.canvas = self.FigureCanvas(self.figure)
            self.container_layout.addWidget(self.canvas)
            
            # Store axes for later use
            self.axes = {'main': axes[0], 'volume': axes[1] if len(axes) > 1 else None}
            panel_idx = 2
            
            # Add RSI panel
            if indicators.get('show_rsi') and 'rsi_14' in plot_df.columns:
                ax_rsi = axes[panel_idx] if panel_idx < len(axes) else None
                if ax_rsi:
                    ax_rsi.plot(plot_df.index, plot_df['rsi_14'], color='#ab47bc', linewidth=2, label='RSI 14')
                    ax_rsi.axhline(70, color='#ef5350', linestyle='--', linewidth=1)
                    ax_rsi.axhline(30, color='#66bb6a', linestyle='--', linewidth=1)
                    ax_rsi.set_ylim(0, 100)
                    ax_rsi.set_ylabel('RSI')
                    ax_rsi.legend(loc='upper left')
                    self.axes['rsi'] = ax_rsi
                    panel_idx += 1
            
            # Add MACD panel
            if indicators.get('show_macd') and 'macd' in plot_df.columns:
                ax_macd = axes[panel_idx] if panel_idx < len(axes) else None
                if ax_macd:
                    ax_macd.plot(plot_df.index, plot_df['macd'], color='#2196F3', linewidth=2, label='MACD')
                    if 'macd_signal' in plot_df.columns:
                        ax_macd.plot(plot_df.index, plot_df['macd_signal'], color='#FF9800', linewidth=2, label='Signal')
                    
                    if 'macd_hist' in plot_df.columns:
                        colors = ['#4caf50' if h >= 0 else '#f44336' for h in plot_df['macd_hist']]
                        ax_macd.bar(plot_df.index, plot_df['macd_hist'], color=colors, width=0.8, alpha=0.3)
                    
                    ax_macd.set_ylabel('MACD')
                    ax_macd.legend(loc='upper left')
                    ax_macd.axhline(0, color='#ffffff', linestyle=':', linewidth=1)
                    self.axes['macd'] = ax_macd
            
            # Add pattern overlays
            if patterns and patterns.get('patterns') and self.axes.get('main'):
                pattern_list = patterns['patterns'][:20]  # Limit to 20
                for pattern in pattern_list:
                    self.add_pattern_overlay(pattern, plot_df, self.axes['main'])
            
            # Refresh canvas
            self.canvas.draw()
            
        except Exception as e:
            logging.error(f"FinPlot update error: {e}", exc_info=True)
    
    def add_pattern_overlay(self, pattern, df, ax):
        """Add pattern visualization as overlay"""
        try:
            start_idx = pattern.get('start_index', 0)
            end_idx = pattern.get('end_index', len(df)-1)
            start_idx = max(0, min(start_idx, len(df)-1))
            end_idx = max(0, min(end_idx, len(df)-1))
            
            if start_idx >= end_idx:
                return
            
            pattern_type = pattern.get('type', 'unknown')
            subtype = pattern.get('subtype', '')
            confidence = pattern.get('confidence', 0.5)
            
            # Determine color
            if 'bullish' in subtype.lower() or pattern_type in ['ascending_triangle', 'inverse_head_shoulders']:
                color = '#4caf50'
                alpha = 0.2
            elif 'bearish' in subtype.lower() or pattern_type in ['descending_triangle', 'head_shoulders']:
                color = '#f44336'
                alpha = 0.2
            else:
                color = '#ffeb3b'
                alpha = 0.2
            
            # Get price range for pattern
            pattern_df = df.iloc[start_idx:end_idx+1]
            y_min = pattern_df['low'].min() * 0.995
            y_max = pattern_df['high'].max() * 1.005
            
            # Draw shaded rectangle
            from matplotlib.patches import Rectangle
            from matplotlib.dates import date2num
            
            x_start = date2num(df.index[start_idx])
            x_end = date2num(df.index[end_idx])
            
            rect = Rectangle((x_start, y_min), x_end - x_start, y_max - y_min,
                           facecolor=color, alpha=alpha, edgecolor=color, linewidth=1)
            ax.add_patch(rect)
            
            # Add text label
            label_text = f"{pattern_type.replace('_', ' ').title()}\n{confidence:.0%}"
            mid_x = date2num(df.index[int((start_idx + end_idx) / 2)])
            
            ax.text(mid_x, y_max, label_text, 
                   ha='center', va='bottom', color='white',
                   fontsize=8, bbox=dict(boxstyle='round,pad=0.3', facecolor=color, alpha=0.7))
            
        except Exception as e:
            logging.error(f"Pattern overlay error: {e}")
    
    def clear(self):
        """Clear all chart data"""
        try:
            if self.figure:
                self.figure.clear()
                self.axes = {}
        except Exception as e:
            logging.debug(f"Clear error: {e}")
    
    def set_mode(self, mode):
        """Set display mode - re-render with new mode"""
        if self.last_df is not None:
            ref_price = self.last_df['close'].iloc[0] if mode == 'percentage' else None
            self.update_data(
                self.last_df,
                self.last_indicators,
                self.last_patterns,
                display_mode=mode,
                reference_price=ref_price
            )
