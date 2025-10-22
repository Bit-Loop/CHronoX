#!/usr/bin/env python3
"""
ChronoX Real-Time Dashboard

Live visualization of market data during backfill process:
- Real-time candlestick charts (1-min and daily)
- Technical indicators (SMA, EMA, RSI, MACD, Bollinger Bands)
- Corporate actions overlay (dividends, splits)
- Live news feed from Polygon
- System metrics (queues, CPU, memory, throughput)

Architecture:
- FastAPI + Plotly Dash for web interface
- WebSocket client for Polygon real-time feed
- Async queues for non-blocking data flow
- Shared memory buffers for backfill integration
- Rolling window (500 candles) for performance

Usage:
    python scripts/realtime_dashboard.py --tickers AMD,NVDA --port 8050
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

import aiohttp
import dash
from dash import dcc, html, Input, Output, State
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import psutil
import websockets

# ============================================================================
# Configuration
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('./logs/realtime_dashboard.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Polygon WebSocket endpoint (use delayed feed for starter/basic plans)
POLYGON_WS_URL = "wss://delayed.polygon.io/stocks"  # 15-minute delayed data

# Rolling window size (number of candles to keep in memory)
ROLLING_WINDOW_SIZE = 500

# UI refresh rates (Hz)
CHART_REFRESH_RATE = 1.0  # 1 update per second
NEWS_REFRESH_RATE = 0.2  # 1 update per 5 seconds
METRICS_REFRESH_RATE = 0.1  # 1 update per 10 seconds

# Technical indicators configuration
INDICATORS_CONFIG = {
    'SMA_20': {'type': 'sma', 'timespan': 'minute', 'window': 20},
    'EMA_50': {'type': 'ema', 'timespan': 'minute', 'window': 50},
    'RSI_14': {'type': 'rsi', 'timespan': 'minute', 'window': 14},
    'MACD': {'type': 'macd', 'short_window': 12, 'long_window': 26, 'signal_window': 9},
    'BB_20': {'type': 'bbands', 'timespan': 'minute', 'window': 20, 'std_dev': 2},
}

# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class Candle:
    """OHLCV candle with timestamp"""
    timestamp: int  # Unix timestamp (milliseconds)
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: Optional[float] = None
    transactions: Optional[int] = None
    
    def to_dict(self):
        return {
            't': self.timestamp,
            'o': self.open,
            'h': self.high,
            'l': self.low,
            'c': self.close,
            'v': self.volume,
            'vw': self.vwap,
            'n': self.transactions
        }


@dataclass
class CorporateAction:
    """Corporate action event (dividend or split)"""
    ticker: str
    action_type: str  # 'dividend' or 'split'
    ex_date: str  # YYYY-MM-DD
    execution_date: Optional[str] = None
    declaration_date: Optional[str] = None
    pay_date: Optional[str] = None
    
    # Dividend fields
    cash_amount: Optional[float] = None
    currency: Optional[str] = None
    frequency: Optional[int] = None
    
    # Split fields
    split_from: Optional[float] = None
    split_to: Optional[float] = None
    
    def to_marker(self):
        """Convert to chart marker dict"""
        return {
            'x': self.ex_date,
            'y': 0,  # Will be positioned on chart
            'text': self.get_label(),
            'type': self.action_type
        }
    
    def get_label(self):
        if self.action_type == 'dividend':
            return f"DIV: ${self.cash_amount:.2f}"
        else:
            return f"SPLIT: {self.split_from}:{self.split_to}"


@dataclass
class NewsItem:
    """News article from Polygon"""
    id: str
    ticker: str
    timestamp: datetime
    headline: str
    author: str
    source: str
    url: str
    summary: Optional[str] = None
    
    def to_dict(self):
        return {
            'id': self.id,
            'ticker': self.ticker,
            'timestamp': self.timestamp.isoformat(),
            'headline': self.headline,
            'author': self.author,
            'source': self.source,
            'url': self.url,
            'summary': self.summary
        }


@dataclass
class SystemMetrics:
    """System performance metrics"""
    timestamp: datetime
    cpu_percent: float
    memory_percent: float
    download_queue_size: int
    process_queue_size: int
    download_queue_max: int
    process_queue_max: int
    files_downloaded: int
    files_processed: int
    files_failed: int
    throughput_files_per_sec: float
    active_futures: int
    
    def to_dict(self):
        return {
            'timestamp': self.timestamp.isoformat(),
            'cpu_percent': self.cpu_percent,
            'memory_percent': self.memory_percent,
            'download_queue': f"{self.download_queue_size}/{self.download_queue_max}",
            'process_queue': f"{self.process_queue_size}/{self.process_queue_max}",
            'files_downloaded': self.files_downloaded,
            'files_processed': self.files_processed,
            'files_failed': self.files_failed,
            'throughput': f"{self.throughput_files_per_sec:.2f} files/s",
            'active_futures': self.active_futures
        }


# ============================================================================
# Real-Time Data Manager
# ============================================================================

class RealTimeDataManager:
    """
    Manages real-time data streams and historical backfill integration.
    
    Features:
    - WebSocket client for Polygon real-time feed
    - Rolling window buffer for each symbol
    - Technical indicators calculation
    - Corporate actions caching
    - News feed aggregation
    - Async queues for non-blocking updates
    """
    
    def __init__(self, api_key: str, tickers: List[str], backfill_metrics: Optional[Dict] = None):
        self.api_key = api_key
        self.tickers = [t.upper() for t in tickers]
        self.backfill_metrics = backfill_metrics or {}
        
        # Data buffers (thread-safe)
        self.candles: Dict[str, Deque[Candle]] = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW_SIZE))
        self.candles_lock = Lock()
        
        self.indicators: Dict[str, Dict[str, List]] = defaultdict(dict)
        self.indicators_lock = Lock()
        
        self.corporate_actions: Dict[str, List[CorporateAction]] = defaultdict(list)
        self.actions_lock = Lock()
        
        self.news_feed: Deque[NewsItem] = deque(maxlen=100)
        self.news_lock = Lock()
        
        self.system_metrics: Deque[SystemMetrics] = deque(maxlen=60)  # Last 60 samples
        self.metrics_lock = Lock()
        
        # WebSocket state
        self.ws_client: Optional[websockets.WebSocketClientProtocol] = None
        self.ws_connected = False
        self.ws_reconnect_delay = 5
        
        # Background tasks
        self.tasks: List[asyncio.Task] = []
        self.running = False
        
        logger.info(f"RealTimeDataManager initialized for tickers: {', '.join(self.tickers)}")
    
    async def start(self):
        """Start all background tasks"""
        self.running = True
        
        # Start WebSocket client
        self.tasks.append(asyncio.create_task(self.websocket_client()))
        
        # Start data fetchers
        self.tasks.append(asyncio.create_task(self.fetch_indicators_loop()))
        self.tasks.append(asyncio.create_task(self.fetch_corporate_actions_loop()))
        self.tasks.append(asyncio.create_task(self.fetch_news_loop()))
        self.tasks.append(asyncio.create_task(self.collect_system_metrics_loop()))
        
        logger.info("All background tasks started")
    
    async def stop(self):
        """Stop all background tasks"""
        self.running = False
        
        # Close WebSocket
        if self.ws_client:
            await self.ws_client.close()
        
        # Cancel all tasks
        for task in self.tasks:
            task.cancel()
        
        await asyncio.gather(*self.tasks, return_exceptions=True)
        
        logger.info("All background tasks stopped")
    
    # ========================================================================
    # WebSocket Client
    # ========================================================================
    
    async def websocket_client(self):
        """
        Connect to Polygon WebSocket and stream real-time data.
        
        Handles:
        - Authentication
        - Subscription to ticker streams
        - Heartbeat/ping-pong
        - Reconnection on failure
        - Message parsing and routing
        """
        while self.running:
            try:
                async with websockets.connect(
                    POLYGON_WS_URL,
                    ping_interval=20,
                    ping_timeout=10
                ) as websocket:
                    self.ws_client = websocket
                    logger.info("WebSocket connected to Polygon")
                    
                    # Authenticate
                    auth_msg = {"action": "auth", "params": self.api_key}
                    await websocket.send(json.dumps(auth_msg))
                    
                    # Wait for auth response
                    auth_response = await websocket.recv()
                    auth_data = json.loads(auth_response)
                    
                    # Check for successful connection and auth
                    # Response format: [{"ev": "status", "status": "connected", "message": "Connected Successfully"}]
                    # followed by: [{"ev": "status", "status": "auth_success", "message": "authenticated"}]
                    if isinstance(auth_data, list) and len(auth_data) > 0:
                        first_msg = auth_data[0]
                        if first_msg.get('status') == 'connected':
                            logger.info("WebSocket connected, waiting for auth confirmation...")
                            # Get the actual auth response
                            auth_response = await websocket.recv()
                            auth_data = json.loads(auth_response)
                        
                        # Now check for auth_success
                        if isinstance(auth_data, list) and len(auth_data) > 0:
                            auth_status = auth_data[0].get('status')
                            if auth_status != 'auth_success':
                                logger.error(f"WebSocket auth failed: {auth_data}")
                                await asyncio.sleep(self.ws_reconnect_delay)
                                continue
                    
                    logger.info("WebSocket authenticated successfully")
                    self.ws_connected = True
                    
                    # Subscribe to aggregate streams (1-minute bars)
                    for ticker in self.tickers:
                        subscribe_msg = {
                            "action": "subscribe",
                            "params": f"AM.{ticker}"  # Aggregate minute bars
                        }
                        await websocket.send(json.dumps(subscribe_msg))
                        logger.info(f"Subscribed to AM.{ticker}")
                    
                    # Process messages
                    async for message in websocket:
                        if not self.running:
                            break
                        
                        try:
                            data = json.loads(message)
                            await self.process_websocket_message(data)
                        except Exception as e:
                            logger.error(f"Error processing WebSocket message: {e}")
            
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket connection closed, reconnecting...")
                self.ws_connected = False
                await asyncio.sleep(self.ws_reconnect_delay)
            
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                self.ws_connected = False
                await asyncio.sleep(self.ws_reconnect_delay)
    
    async def process_websocket_message(self, data: List[Dict]):
        """Process incoming WebSocket messages"""
        for msg in data:
            ev_type = msg.get('ev')
            
            if ev_type == 'AM':  # Aggregate minute bar
                ticker = msg.get('sym')
                if ticker in self.tickers:
                    candle = Candle(
                        timestamp=msg.get('e'),  # End timestamp
                        open=msg.get('o'),
                        high=msg.get('h'),
                        low=msg.get('l'),
                        close=msg.get('c'),
                        volume=msg.get('v'),
                        vwap=msg.get('vw'),
                        transactions=msg.get('n')
                    )
                    
                    with self.candles_lock:
                        self.candles[ticker].append(candle)
                    
                    logger.debug(f"Received candle for {ticker}: {candle.close}")
            
            elif ev_type == 'status':
                logger.info(f"WebSocket status: {msg.get('message')}")
    
    # ========================================================================
    # Data Fetchers
    # ========================================================================
    
    async def fetch_indicators_loop(self):
        """Fetch technical indicators from Polygon API (REST fallback)"""
        while self.running:
            try:
                for ticker in self.tickers:
                    # Calculate indicators client-side for now
                    # Polygon's indicator API has limited coverage
                    await self.calculate_indicators_client_side(ticker)
                
                await asyncio.sleep(60)  # Update every minute
            
            except Exception as e:
                logger.error(f"Error fetching indicators: {e}")
                await asyncio.sleep(60)
    
    async def calculate_indicators_client_side(self, ticker: str):
        """Calculate technical indicators from candle data"""
        with self.candles_lock:
            candles = list(self.candles[ticker])
        
        if len(candles) < 50:
            return  # Not enough data
        
        df = pd.DataFrame([c.to_dict() for c in candles])
        df['timestamp'] = pd.to_datetime(df['t'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # SMA 20
        df['SMA_20'] = df['c'].rolling(window=20).mean()
        
        # EMA 50
        df['EMA_50'] = df['c'].ewm(span=50, adjust=False).mean()
        
        # RSI 14
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI_14'] = 100 - (100 / (1 + rs))
        
        # MACD
        ema_12 = df['c'].ewm(span=12, adjust=False).mean()
        ema_26 = df['c'].ewm(span=26, adjust=False).mean()
        df['MACD'] = ema_12 - ema_26
        df['MACD_signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_hist'] = df['MACD'] - df['MACD_signal']
        
        # Bollinger Bands
        df['BB_middle'] = df['c'].rolling(window=20).mean()
        df['BB_std'] = df['c'].rolling(window=20).std()
        df['BB_upper'] = df['BB_middle'] + (df['BB_std'] * 2)
        df['BB_lower'] = df['BB_middle'] - (df['BB_std'] * 2)
        
        # Store indicators
        with self.indicators_lock:
            self.indicators[ticker] = {
                'timestamps': df.index.tolist(),
                'SMA_20': df['SMA_20'].dropna().tolist(),
                'EMA_50': df['EMA_50'].dropna().tolist(),
                'RSI_14': df['RSI_14'].dropna().tolist(),
                'MACD': df['MACD'].dropna().tolist(),
                'MACD_signal': df['MACD_signal'].dropna().tolist(),
                'MACD_hist': df['MACD_hist'].dropna().tolist(),
                'BB_upper': df['BB_upper'].dropna().tolist(),
                'BB_middle': df['BB_middle'].dropna().tolist(),
                'BB_lower': df['BB_lower'].dropna().tolist(),
            }
    
    async def fetch_corporate_actions_loop(self):
        """Fetch corporate actions (dividends and splits)"""
        # Fetch once at startup, then update daily
        for ticker in self.tickers:
            await self.fetch_corporate_actions(ticker)
        
        while self.running:
            await asyncio.sleep(86400)  # Update daily
            for ticker in self.tickers:
                await self.fetch_corporate_actions(ticker)
    
    async def fetch_corporate_actions(self, ticker: str):
        """Fetch corporate actions for a ticker"""
        try:
            # TODO: Use Polygon's corporate actions API
            # For now, placeholder
            logger.info(f"Fetching corporate actions for {ticker}")
        except Exception as e:
            logger.error(f"Error fetching corporate actions for {ticker}: {e}")
    
    async def fetch_news_loop(self):
        """Fetch latest news from Polygon"""
        while self.running:
            try:
                for ticker in self.tickers:
                    await self.fetch_news(ticker)
                
                await asyncio.sleep(30)  # Update every 30 seconds
            
            except Exception as e:
                logger.error(f"Error fetching news: {e}")
                await asyncio.sleep(30)
    
    async def fetch_news(self, ticker: str):
        """Fetch news for a ticker"""
        try:
            # Use Polygon REST API
            url = f"https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=10&apiKey={self.api_key}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        articles = data.get('results', [])
                        
                        for article in articles:
                            news_item = NewsItem(
                                id=article.get('id'),
                                ticker=ticker,
                                timestamp=datetime.fromisoformat(article.get('published_utc').replace('Z', '+00:00')),
                                headline=article.get('title'),
                                author=article.get('author', 'Unknown'),
                                source=article.get('publisher', {}).get('name', 'Unknown'),
                                url=article.get('article_url'),
                                summary=article.get('description')
                            )
                            
                            with self.news_lock:
                                # Check if already exists
                                if not any(n.id == news_item.id for n in self.news_feed):
                                    self.news_feed.append(news_item)
                    
                    else:
                        logger.warning(f"News API returned status {response.status}")
        
        except Exception as e:
            logger.error(f"Error fetching news for {ticker}: {e}")
    
    async def collect_system_metrics_loop(self):
        """Collect system performance metrics"""
        while self.running:
            try:
                metrics = SystemMetrics(
                    timestamp=datetime.now(),
                    cpu_percent=psutil.cpu_percent(interval=1),
                    memory_percent=psutil.virtual_memory().percent,
                    download_queue_size=self.backfill_metrics.get('download_qsize', 0),
                    process_queue_size=self.backfill_metrics.get('process_qsize', 0),
                    download_queue_max=self.backfill_metrics.get('download_maxsize', 0),
                    process_queue_max=self.backfill_metrics.get('process_maxsize', 0),
                    files_downloaded=self.backfill_metrics.get('downloaded', 0),
                    files_processed=self.backfill_metrics.get('processed', 0),
                    files_failed=self.backfill_metrics.get('failed', 0),
                    throughput_files_per_sec=self.backfill_metrics.get('throughput', 0.0),
                    active_futures=self.backfill_metrics.get('active_futures', 0)
                )
                
                with self.metrics_lock:
                    self.system_metrics.append(metrics)
            
            except Exception as e:
                logger.error(f"Error collecting system metrics: {e}")
            
            await asyncio.sleep(10)  # Update every 10 seconds
    
    # ========================================================================
    # Data Access Methods (Thread-Safe)
    # ========================================================================
    
    def get_candles(self, ticker: str) -> List[Candle]:
        """Get candles for a ticker (thread-safe copy)"""
        with self.candles_lock:
            return list(self.candles[ticker])
    
    def get_indicators(self, ticker: str) -> Dict:
        """Get indicators for a ticker (thread-safe copy)"""
        with self.indicators_lock:
            return dict(self.indicators.get(ticker, {}))
    
    def get_corporate_actions(self, ticker: str) -> List[CorporateAction]:
        """Get corporate actions for a ticker (thread-safe copy)"""
        with self.actions_lock:
            return list(self.corporate_actions[ticker])
    
    def get_news(self) -> List[NewsItem]:
        """Get recent news (thread-safe copy)"""
        with self.news_lock:
            return list(self.news_feed)
    
    def get_system_metrics(self) -> List[SystemMetrics]:
        """Get system metrics (thread-safe copy)"""
        with self.metrics_lock:
            return list(self.system_metrics)


# ============================================================================
# Dash Application
# ============================================================================

def create_dash_app(data_manager: RealTimeDataManager, tickers: List[str]):
    """Create Plotly Dash application"""
    
    app = dash.Dash(
        __name__,
        title="ChronoX Real-Time Dashboard",
        update_title=None,
        suppress_callback_exceptions=True
    )
    
    # Dark theme CSS
    app.index_string = '''
    <!DOCTYPE html>
    <html>
        <head>
            {%metas%}
            <title>{%title%}</title>
            {%favicon%}
            {%css%}
            <style>
                body {
                    background-color: #1e1e1e;
                    color: #d4d4d4;
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    margin: 0;
                    padding: 0;
                }
                .header {
                    background-color: #252526;
                    padding: 15px 30px;
                    border-bottom: 2px solid #007acc;
                }
                .header h1 {
                    margin: 0;
                    color: #007acc;
                    font-size: 28px;
                }
                .container {
                    padding: 20px;
                }
                .metric-card {
                    background-color: #252526;
                    border-radius: 8px;
                    padding: 15px;
                    margin: 10px 0;
                    border-left: 4px solid #007acc;
                }
                .news-item {
                    background-color: #2d2d30;
                    border-radius: 6px;
                    padding: 12px;
                    margin: 8px 0;
                    border-left: 3px solid #4ec9b0;
                }
                .news-item:hover {
                    background-color: #3e3e42;
                }
            </style>
        </head>
        <body>
            {%app_entry%}
            <footer>
                {%config%}
                {%scripts%}
                {%renderer%}
            </footer>
        </body>
    </html>
    '''
    
    # Layout
    app.layout = html.Div([
        # Header
        html.Div([
            html.H1("ChronoX Real-Time Dashboard"),
            html.P(f"Tracking: {', '.join(tickers)}", style={'margin': '5px 0 0 0', 'color': '#858585'})
        ], className='header'),
        
        # Main container
        html.Div([
            # Top row: Candlestick charts
            html.Div([
                html.H2("Live Candlestick Chart", style={'color': '#d4d4d4'}),
                dcc.Dropdown(
                    id='ticker-selector',
                    options=[{'label': t, 'value': t} for t in tickers],
                    value=tickers[0],
                    style={'width': '200px', 'margin-bottom': '10px'}
                ),
                dcc.Graph(
                    id='candlestick-chart',
                    config={'displayModeBar': True},
                    style={'height': '500px'}
                ),
                dcc.Interval(
                    id='chart-interval',
                    interval=int(1000 / CHART_REFRESH_RATE),  # milliseconds
                    n_intervals=0
                )
            ], style={'margin-bottom': '30px'}),
            
            # Middle row: Indicators
            html.Div([
                html.H2("Technical Indicators", style={'color': '#d4d4d4'}),
                dcc.Graph(
                    id='indicators-chart',
                    config={'displayModeBar': False},
                    style={'height': '300px'}
                )
            ], style={'margin-bottom': '30px'}),
            
            # Bottom row: News feed and metrics
            html.Div([
                # News feed (left)
                html.Div([
                    html.H2("Live News Feed", style={'color': '#d4d4d4'}),
                    html.Div(id='news-feed', style={
                        'height': '400px',
                        'overflow-y': 'scroll',
                        'background-color': '#1e1e1e',
                        'padding': '10px',
                        'border-radius': '8px'
                    }),
                    dcc.Interval(
                        id='news-interval',
                        interval=int(1000 / NEWS_REFRESH_RATE),
                        n_intervals=0
                    )
                ], style={'width': '60%', 'display': 'inline-block', 'vertical-align': 'top'}),
                
                # System metrics (right)
                html.Div([
                    html.H2("System Metrics", style={'color': '#d4d4d4'}),
                    html.Div(id='system-metrics', style={
                        'height': '400px',
                        'overflow-y': 'scroll',
                        'background-color': '#1e1e1e',
                        'padding': '10px',
                        'border-radius': '8px'
                    }),
                    dcc.Interval(
                        id='metrics-interval',
                        interval=int(1000 / METRICS_REFRESH_RATE),
                        n_intervals=0
                    )
                ], style={'width': '38%', 'display': 'inline-block', 'vertical-align': 'top', 'margin-left': '2%'})
            ])
        ], className='container')
    ])
    
    # Callbacks
    @app.callback(
        [Output('candlestick-chart', 'figure'),
         Output('indicators-chart', 'figure')],
        [Input('chart-interval', 'n_intervals'),
         Input('ticker-selector', 'value')]
    )
    def update_charts(n, ticker):
        """Update candlestick and indicator charts"""
        try:
            candles = data_manager.get_candles(ticker)
            indicators = data_manager.get_indicators(ticker)
            
            if not candles:
                raise PreventUpdate
            
            # Prepare candlestick data
            timestamps = [datetime.fromtimestamp(c.timestamp / 1000) for c in candles]
            opens = [c.open for c in candles]
            highs = [c.high for c in candles]
            lows = [c.low for c in candles]
            closes = [c.close for c in candles]
            volumes = [c.volume for c in candles]
            
            # Create candlestick chart with volume
            fig1 = make_subplots(
                rows=2, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.03,
                row_heights=[0.7, 0.3],
                subplot_titles=(f'{ticker} - 1 Minute Candles', 'Volume')
            )
            
            # Candlestick
            fig1.add_trace(go.Candlestick(
                x=timestamps,
                open=opens,
                high=highs,
                low=lows,
                close=closes,
                name='OHLC',
                increasing_line_color='#26a69a',
                decreasing_line_color='#ef5350'
            ), row=1, col=1)
            
            # Add SMA 20 if available
            if 'SMA_20' in indicators and len(indicators['SMA_20']) > 0:
                ind_timestamps = indicators.get('timestamps', timestamps[-len(indicators['SMA_20']):])
                fig1.add_trace(go.Scatter(
                    x=ind_timestamps,
                    y=indicators['SMA_20'],
                    name='SMA 20',
                    line=dict(color='#ffa726', width=1)
                ), row=1, col=1)
            
            # Add EMA 50 if available
            if 'EMA_50' in indicators and len(indicators['EMA_50']) > 0:
                ind_timestamps = indicators.get('timestamps', timestamps[-len(indicators['EMA_50']):])
                fig1.add_trace(go.Scatter(
                    x=ind_timestamps,
                    y=indicators['EMA_50'],
                    name='EMA 50',
                    line=dict(color='#42a5f5', width=1)
                ), row=1, col=1)
            
            # Add Bollinger Bands if available
            if all(k in indicators for k in ['BB_upper', 'BB_middle', 'BB_lower']):
                if len(indicators['BB_upper']) > 0:
                    ind_timestamps = indicators.get('timestamps', timestamps[-len(indicators['BB_upper']):])
                    fig1.add_trace(go.Scatter(
                        x=ind_timestamps,
                        y=indicators['BB_upper'],
                        name='BB Upper',
                        line=dict(color='#ab47bc', width=1, dash='dash'),
                        showlegend=False
                    ), row=1, col=1)
                    fig1.add_trace(go.Scatter(
                        x=ind_timestamps,
                        y=indicators['BB_lower'],
                        name='BB Lower',
                        line=dict(color='#ab47bc', width=1, dash='dash'),
                        fill='tonexty',
                        fillcolor='rgba(171, 71, 188, 0.1)',
                        showlegend=True
                    ), row=1, col=1)
            
            # Volume bars
            colors = ['#26a69a' if closes[i] >= opens[i] else '#ef5350' for i in range(len(closes))]
            fig1.add_trace(go.Bar(
                x=timestamps,
                y=volumes,
                name='Volume',
                marker_color=colors,
                showlegend=False
            ), row=2, col=1)
            
            fig1.update_layout(
                template='plotly_dark',
                xaxis_rangeslider_visible=False,
                height=500,
                margin=dict(l=50, r=50, t=50, b=50),
                hovermode='x unified'
            )
            
            # Create indicators chart (RSI and MACD)
            fig2 = make_subplots(
                rows=2, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.1,
                subplot_titles=('RSI (14)', 'MACD (12, 26, 9)')
            )
            
            # RSI
            if 'RSI_14' in indicators and len(indicators['RSI_14']) > 0:
                ind_timestamps = indicators.get('timestamps', timestamps[-len(indicators['RSI_14']):])
                fig2.add_trace(go.Scatter(
                    x=ind_timestamps,
                    y=indicators['RSI_14'],
                    name='RSI',
                    line=dict(color='#ffa726', width=2)
                ), row=1, col=1)
                
                # RSI overbought/oversold lines
                fig2.add_hline(y=70, line_dash="dash", line_color="red", row=1, col=1)
                fig2.add_hline(y=30, line_dash="dash", line_color="green", row=1, col=1)
            
            # MACD
            if all(k in indicators for k in ['MACD', 'MACD_signal', 'MACD_hist']):
                if len(indicators['MACD']) > 0:
                    ind_timestamps = indicators.get('timestamps', timestamps[-len(indicators['MACD']):])
                    fig2.add_trace(go.Scatter(
                        x=ind_timestamps,
                        y=indicators['MACD'],
                        name='MACD',
                        line=dict(color='#42a5f5', width=2)
                    ), row=2, col=1)
                    fig2.add_trace(go.Scatter(
                        x=ind_timestamps,
                        y=indicators['MACD_signal'],
                        name='Signal',
                        line=dict(color='#ef5350', width=2)
                    ), row=2, col=1)
                    fig2.add_trace(go.Bar(
                        x=ind_timestamps,
                        y=indicators['MACD_hist'],
                        name='Histogram',
                        marker_color=['#26a69a' if v >= 0 else '#ef5350' for v in indicators['MACD_hist']]
                    ), row=2, col=1)
            
            fig2.update_layout(
                template='plotly_dark',
                height=300,
                margin=dict(l=50, r=50, t=50, b=50),
                hovermode='x unified'
            )
            
            return fig1, fig2
        
        except Exception as e:
            logger.error(f"Error updating charts: {e}")
            raise PreventUpdate
    
    @app.callback(
        Output('news-feed', 'children'),
        Input('news-interval', 'n_intervals')
    )
    def update_news(n):
        """Update news feed"""
        try:
            news_items = data_manager.get_news()
            
            if not news_items:
                return html.Div("No news available", style={'color': '#858585'})
            
            # Sort by timestamp (newest first)
            news_items = sorted(news_items, key=lambda x: x.timestamp, reverse=True)
            
            children = []
            for item in news_items[:20]:  # Show last 20 items
                children.append(html.Div([
                    html.Div([
                        html.Span(item.ticker, style={
                            'background-color': '#007acc',
                            'padding': '2px 8px',
                            'border-radius': '4px',
                            'margin-right': '10px',
                            'font-size': '12px'
                        }),
                        html.Span(item.timestamp.strftime('%Y-%m-%d %H:%M'), style={
                            'color': '#858585',
                            'font-size': '12px'
                        })
                    ]),
                    html.H4(item.headline, style={'margin': '8px 0', 'font-size': '14px'}),
                    html.P(item.summary[:150] + '...' if item.summary and len(item.summary) > 150 else item.summary or '',
                           style={'margin': '5px 0', 'font-size': '12px', 'color': '#a0a0a0'}),
                    html.A('Read more', href=item.url, target='_blank', style={
                        'color': '#4ec9b0',
                        'text-decoration': 'none',
                        'font-size': '12px'
                    })
                ], className='news-item'))
            
            return children
        
        except Exception as e:
            logger.error(f"Error updating news: {e}")
            return html.Div(f"Error loading news: {str(e)}", style={'color': '#ef5350'})
    
    @app.callback(
        Output('system-metrics', 'children'),
        Input('metrics-interval', 'n_intervals')
    )
    def update_metrics(n):
        """Update system metrics"""
        try:
            metrics_list = data_manager.get_system_metrics()
            
            if not metrics_list:
                return html.Div("No metrics available", style={'color': '#858585'})
            
            latest = metrics_list[-1]
            
            return html.Div([
                html.Div([
                    html.H4("CPU Usage", style={'margin': '0 0 5px 0', 'font-size': '14px'}),
                    html.Div(f"{latest.cpu_percent:.1f}%", style={
                        'font-size': '24px',
                        'font-weight': 'bold',
                        'color': '#4ec9b0' if latest.cpu_percent < 70 else '#ffa726' if latest.cpu_percent < 90 else '#ef5350'
                    })
                ], className='metric-card'),
                
                html.Div([
                    html.H4("Memory Usage", style={'margin': '0 0 5px 0', 'font-size': '14px'}),
                    html.Div(f"{latest.memory_percent:.1f}%", style={
                        'font-size': '24px',
                        'font-weight': 'bold',
                        'color': '#4ec9b0' if latest.memory_percent < 70 else '#ffa726' if latest.memory_percent < 90 else '#ef5350'
                    })
                ], className='metric-card'),
                
                html.Div([
                    html.H4("Download Queue", style={'margin': '0 0 5px 0', 'font-size': '14px'}),
                    html.Div(f"{latest.download_queue_size}/{latest.download_queue_max}", style={
                        'font-size': '20px',
                        'font-weight': 'bold',
                        'color': '#007acc'
                    })
                ], className='metric-card'),
                
                html.Div([
                    html.H4("Process Queue", style={'margin': '0 0 5px 0', 'font-size': '14px'}),
                    html.Div(f"{latest.process_queue_size}/{latest.process_queue_max}", style={
                        'font-size': '20px',
                        'font-weight': 'bold',
                        'color': '#007acc'
                    })
                ], className='metric-card'),
                
                html.Div([
                    html.H4("Files Processed", style={'margin': '0 0 5px 0', 'font-size': '14px'}),
                    html.Div(f"{latest.files_processed}", style={
                        'font-size': '20px',
                        'font-weight': 'bold',
                        'color': '#4ec9b0'
                    }),
                    html.Div(f"Downloaded: {latest.files_downloaded} | Failed: {latest.files_failed}",
                             style={'font-size': '12px', 'color': '#858585', 'margin-top': '5px'})
                ], className='metric-card'),
                
                html.Div([
                    html.H4("Throughput", style={'margin': '0 0 5px 0', 'font-size': '14px'}),
                    html.Div(f"{latest.throughput_files_per_sec:.2f} files/s", style={
                        'font-size': '20px',
                        'font-weight': 'bold',
                        'color': '#4ec9b0'
                    })
                ], className='metric-card'),
                
                html.Div([
                    html.H4("Active Futures", style={'margin': '0 0 5px 0', 'font-size': '14px'}),
                    html.Div(f"{latest.active_futures}", style={
                        'font-size': '20px',
                        'font-weight': 'bold',
                        'color': '#42a5f5'
                    })
                ], className='metric-card'),
            ])
        
        except Exception as e:
            logger.error(f"Error updating metrics: {e}")
            return html.Div(f"Error loading metrics: {str(e)}", style={'color': '#ef5350'})
    
    return app


# ============================================================================
# Main Entry Point
# ============================================================================

def start_dashboard(
    tickers: List[str],
    api_key: Optional[str] = None,
    port: int = 8050,
    backfill_metrics: Optional[Dict] = None
):
    """
    Start the real-time dashboard.
    
    Args:
        tickers: List of ticker symbols to track
        api_key: Polygon API key (or None to load from env)
        port: Dashboard port (default 8050)
        backfill_metrics: Shared dict for backfill pipeline metrics
    """
    # Load API key
    if not api_key:
        api_key = os.getenv('POLYGON_API_KEY')
        if not api_key:
            logger.error("POLYGON_API_KEY not found in environment")
            return
    
    # Create data manager
    data_manager = RealTimeDataManager(api_key, tickers, backfill_metrics)
    
    # Create Dash app
    app = create_dash_app(data_manager, tickers)
    
    # Start async event loop in separate thread
    loop = asyncio.new_event_loop()
    
    def run_async_loop():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(data_manager.start())
        loop.run_forever()
    
    async_thread = Thread(target=run_async_loop, daemon=True)
    async_thread.start()
    
    logger.info(f"Starting dashboard on http://localhost:{port}")
    
    try:
        # Start Dash server
        app.run(debug=False, host='0.0.0.0', port=port)
    
    except KeyboardInterrupt:
        logger.info("Shutting down dashboard...")
    
    finally:
        # Cleanup
        loop.call_soon_threadsafe(loop.create_task, data_manager.stop())
        loop.call_soon_threadsafe(loop.stop)
        async_thread.join(timeout=5)


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='ChronoX Real-Time Dashboard')
    parser.add_argument('--tickers', type=str, required=True, help='Comma-separated list of tickers')
    parser.add_argument('--port', type=int, default=8050, help='Dashboard port (default: 8050)')
    parser.add_argument('--api-key', type=str, help='Polygon API key (or use POLYGON_API_KEY env var)')
    
    args = parser.parse_args()
    
    tickers = [t.strip().upper() for t in args.tickers.split(',')]
    
    start_dashboard(tickers, api_key=args.api_key, port=args.port)
