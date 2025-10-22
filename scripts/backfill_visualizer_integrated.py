#!/usr/bin/env python3
"""
ChronoX Integrated Backfill Visualizer

Lightweight in-process visualization for backfill pipeline.
Displays real-time charts, indicators, and metrics without a web server.

Features:
- Real-time candlestick charts (historical + live)
- Technical indicators (SMA, EMA, RSI, MACD, Bollinger Bands)
- Corporate actions overlay (dividends, splits)
- Live news feed from Polygon
- System metrics (queues, CPU, memory, throughput)
- Reference data (exchange, market cap, sector)

Architecture:
- Runs in-process with backfill_historical_data.py
- Uses asyncio for non-blocking updates
- Exports HTML visualizations locally
- Zero interference with download throughput

Usage:
    From backfill_historical_data.py:
    
    if args.visualize:
        visualizer = attach_visualizer(backfill_metrics, tickers, api_key)
"""

import asyncio
import json
import logging
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, List, Optional, Deque

import aiohttp
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import psutil
import websockets

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

# Polygon WebSocket endpoint (use delayed feed for starter/basic plans)
POLYGON_WS_URL = "wss://delayed.polygon.io/stocks"

# Rolling window size (number of candles to keep in memory)
ROLLING_WINDOW_SIZE = 1000

# Update intervals
CHART_UPDATE_INTERVAL = 2.0  # seconds (to avoid excessive I/O)
NEWS_UPDATE_INTERVAL = 30.0  # seconds
METRICS_UPDATE_INTERVAL = 5.0  # seconds
INDICATORS_UPDATE_INTERVAL = 60.0  # seconds

# Output directory for HTML charts
OUTPUT_DIR = Path("./visualizations")

# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class Candle:
    """OHLCV candle with metadata"""
    timestamp: int  # Unix timestamp (milliseconds)
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: Optional[float] = None
    transactions: Optional[int] = None
    source: str = "backfill"  # "backfill" or "websocket"
    
    def to_dict(self):
        return {
            't': self.timestamp,
            'o': self.open,
            'h': self.high,
            'l': self.low,
            'c': self.close,
            'v': self.volume,
            'vw': self.vwap,
            'n': self.transactions,
            'source': self.source
        }


@dataclass
class CorporateAction:
    """Corporate action event"""
    ticker: str
    action_type: str  # 'dividend' or 'split'
    ex_date: str  # YYYY-MM-DD
    execution_date: Optional[str] = None
    declaration_date: Optional[str] = None
    pay_date: Optional[str] = None
    cash_amount: Optional[float] = None
    currency: Optional[str] = None
    frequency: Optional[int] = None
    split_from: Optional[float] = None
    split_to: Optional[float] = None
    
    def get_label(self):
        if self.action_type == 'dividend':
            return f"DIV: ${self.cash_amount:.2f}" if self.cash_amount else "DIV"
        else:
            return f"SPLIT: {self.split_from}:{self.split_to}" if self.split_from else "SPLIT"


@dataclass
class NewsItem:
    """News article"""
    id: str
    ticker: str
    timestamp: datetime
    headline: str
    author: str
    source: str
    url: str
    summary: Optional[str] = None


@dataclass
class TickerReference:
    """Reference data for a ticker"""
    ticker: str
    name: str
    market: str
    locale: str
    primary_exchange: str
    type: str
    active: bool
    currency_name: str
    cik: Optional[str] = None
    composite_figi: Optional[str] = None
    share_class_figi: Optional[str] = None
    market_cap: Optional[float] = None
    phone_number: Optional[str] = None
    address: Optional[Dict] = None
    description: Optional[str] = None
    sic_code: Optional[str] = None
    sic_description: Optional[str] = None
    ticker_root: Optional[str] = None
    homepage_url: Optional[str] = None
    total_employees: Optional[int] = None
    list_date: Optional[str] = None
    branding: Optional[Dict] = None
    share_class_shares_outstanding: Optional[int] = None
    weighted_shares_outstanding: Optional[int] = None


# ============================================================================
# Integrated Visualizer
# ============================================================================

class IntegratedBackfillVisualizer:
    """
    In-process visualizer for backfill pipeline.
    
    Integrates with:
    - Backfill download/process queues
    - Polygon WebSocket (live candles)
    - Polygon REST API (indicators, corporate actions, news, reference data)
    
    Non-blocking async architecture with HTML export.
    """
    
    def __init__(
        self,
        api_key: str,
        tickers: List[str],
        backfill_metrics: Dict,
        output_dir: Path = OUTPUT_DIR
    ):
        self.api_key = api_key
        self.tickers = [t.upper() for t in tickers]
        self.backfill_metrics = backfill_metrics
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True, parents=True)
        
        # Data buffers (thread-safe)
        self.candles: Dict[str, Deque[Candle]] = defaultdict(
            lambda: deque(maxlen=ROLLING_WINDOW_SIZE)
        )
        self.candles_lock = Lock()
        
        self.indicators: Dict[str, Dict[str, List]] = defaultdict(dict)
        self.indicators_lock = Lock()
        
        self.corporate_actions: Dict[str, List[CorporateAction]] = defaultdict(list)
        self.actions_lock = Lock()
        
        self.news_feed: Deque[NewsItem] = deque(maxlen=100)
        self.news_lock = Lock()
        
        self.reference_data: Dict[str, TickerReference] = {}
        self.reference_lock = Lock()
        
        # WebSocket state
        self.ws_client: Optional[websockets.WebSocketClientProtocol] = None
        self.ws_connected = False
        self.ws_reconnect_delay = 5
        
        # Background tasks
        self.tasks: List[asyncio.Task] = []
        self.running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        
        logger.info(f"IntegratedBackfillVisualizer initialized for {', '.join(self.tickers)}")
    
    # ========================================================================
    # Lifecycle Management
    # ========================================================================
    
    async def start_async(self):
        """Start all async background tasks"""
        self.running = True
        
        # Fetch reference data first (blocking, once at startup)
        await self.fetch_all_reference_data()
        
        # Start WebSocket client
        self.tasks.append(asyncio.create_task(self.websocket_client()))
        
        # Start data fetchers
        self.tasks.append(asyncio.create_task(self.fetch_indicators_loop()))
        self.tasks.append(asyncio.create_task(self.fetch_corporate_actions_loop()))
        self.tasks.append(asyncio.create_task(self.fetch_news_loop()))
        
        # Start chart renderer
        self.tasks.append(asyncio.create_task(self.render_charts_loop()))
        
        logger.info("Visualizer: All background tasks started")
    
    async def stop_async(self):
        """Stop all background tasks"""
        self.running = False
        
        if self.ws_client:
            await self.ws_client.close()
        
        for task in self.tasks:
            task.cancel()
        
        await asyncio.gather(*self.tasks, return_exceptions=True)
        
        logger.info("Visualizer: All background tasks stopped")
    
    def start(self):
        """Start visualizer in background thread"""
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.start_async())
            self.loop.run_forever()
        
        thread = Thread(target=run_loop, daemon=True, name="VisualizerThread")
        thread.start()
        logger.info("Visualizer: Started in background thread")
    
    def stop(self):
        """Stop visualizer gracefully"""
        if self.loop:
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self.stop_async())
            )
            self.loop.call_soon_threadsafe(self.loop.stop)
    
    # ========================================================================
    # Data Ingestion from Backfill Pipeline
    # ========================================================================
    
    def add_candle_from_backfill(self, ticker: str, candle_data: Dict):
        """
        Add candle from backfill pipeline processing.
        
        Called by backfill orchestrator after processing each bar.
        Thread-safe, non-blocking.
        """
        try:
            candle = Candle(
                timestamp=candle_data['timestamp'],
                open=candle_data['open'],
                high=candle_data['high'],
                low=candle_data['low'],
                close=candle_data['close'],
                volume=candle_data['volume'],
                vwap=candle_data.get('vwap'),
                transactions=candle_data.get('transactions'),
                source='backfill'
            )
            
            with self.candles_lock:
                # Avoid duplicates (check timestamp)
                existing_timestamps = {c.timestamp for c in self.candles[ticker]}
                if candle.timestamp not in existing_timestamps:
                    self.candles[ticker].append(candle)
        
        except Exception as e:
            logger.error(f"Error adding candle from backfill: {e}")
    
    def add_candles_batch_from_backfill(self, ticker: str, candles_data: List[Dict]):
        """
        Add batch of candles from backfill pipeline.
        
        More efficient for bulk inserts.
        """
        try:
            with self.candles_lock:
                existing_timestamps = {c.timestamp for c in self.candles[ticker]}
                
                for candle_data in candles_data:
                    timestamp = candle_data['timestamp']
                    if timestamp not in existing_timestamps:
                        candle = Candle(
                            timestamp=timestamp,
                            open=candle_data['open'],
                            high=candle_data['high'],
                            low=candle_data['low'],
                            close=candle_data['close'],
                            volume=candle_data['volume'],
                            vwap=candle_data.get('vwap'),
                            transactions=candle_data.get('transactions'),
                            source='backfill'
                        )
                        self.candles[ticker].append(candle)
                        existing_timestamps.add(timestamp)
        
        except Exception as e:
            logger.error(f"Error adding candle batch from backfill: {e}")
    
    # ========================================================================
    # WebSocket Client (Live Data)
    # ========================================================================
    
    async def websocket_client(self):
        """Connect to Polygon WebSocket for live candles"""
        while self.running:
            try:
                async with websockets.connect(
                    POLYGON_WS_URL,
                    ping_interval=20,
                    ping_timeout=10
                ) as websocket:
                    self.ws_client = websocket
                    logger.info("Visualizer: WebSocket connected")
                    
                    # Authenticate
                    auth_msg = {"action": "auth", "params": self.api_key}
                    await websocket.send(json.dumps(auth_msg))
                    
                    # Wait for auth responses
                    auth_response = await websocket.recv()
                    auth_data = json.loads(auth_response)
                    
                    if isinstance(auth_data, list) and len(auth_data) > 0:
                        first_msg = auth_data[0]
                        if first_msg.get('status') == 'connected':
                            auth_response = await websocket.recv()
                            auth_data = json.loads(auth_response)
                        
                        if isinstance(auth_data, list) and len(auth_data) > 0:
                            auth_status = auth_data[0].get('status')
                            if auth_status != 'auth_success':
                                logger.error(f"Visualizer: WebSocket auth failed: {auth_data}")
                                await asyncio.sleep(self.ws_reconnect_delay)
                                continue
                    
                    logger.info("Visualizer: WebSocket authenticated")
                    self.ws_connected = True
                    
                    # Subscribe to aggregate streams
                    for ticker in self.tickers:
                        subscribe_msg = {
                            "action": "subscribe",
                            "params": f"AM.{ticker}"
                        }
                        await websocket.send(json.dumps(subscribe_msg))
                        logger.info(f"Visualizer: Subscribed to AM.{ticker}")
                    
                    # Process messages
                    async for message in websocket:
                        if not self.running:
                            break
                        
                        try:
                            data = json.loads(message)
                            await self.process_websocket_message(data)
                        except Exception as e:
                            logger.error(f"Visualizer: WebSocket message error: {e}")
            
            except websockets.exceptions.ConnectionClosed:
                logger.warning("Visualizer: WebSocket closed, reconnecting...")
                self.ws_connected = False
                await asyncio.sleep(self.ws_reconnect_delay)
            
            except Exception as e:
                logger.error(f"Visualizer: WebSocket error: {e}")
                self.ws_connected = False
                await asyncio.sleep(self.ws_reconnect_delay)
    
    async def process_websocket_message(self, data: List[Dict]):
        """Process WebSocket messages (live candles)"""
        for msg in data:
            ev_type = msg.get('ev')
            
            if ev_type == 'AM':  # Aggregate minute bar
                ticker = msg.get('sym')
                if ticker in self.tickers:
                    candle = Candle(
                        timestamp=msg.get('e'),
                        open=msg.get('o'),
                        high=msg.get('h'),
                        low=msg.get('l'),
                        close=msg.get('c'),
                        volume=msg.get('v'),
                        vwap=msg.get('vw'),
                        transactions=msg.get('n'),
                        source='websocket'
                    )
                    
                    with self.candles_lock:
                        existing_timestamps = {c.timestamp for c in self.candles[ticker]}
                        if candle.timestamp not in existing_timestamps:
                            self.candles[ticker].append(candle)
                            logger.debug(f"Visualizer: Live candle {ticker} @ {candle.close}")
            
            elif ev_type == 'status':
                logger.debug(f"Visualizer: WebSocket status: {msg.get('message')}")
    
    # ========================================================================
    # Reference Data
    # ========================================================================
    
    async def fetch_all_reference_data(self):
        """Fetch reference data for all tickers at startup"""
        for ticker in self.tickers:
            await self.fetch_reference_data(ticker)
    
    async def fetch_reference_data(self, ticker: str):
        """Fetch ticker reference data from Polygon"""
        try:
            url = f"https://api.polygon.io/v3/reference/tickers/{ticker}?apiKey={self.api_key}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        results = data.get('results', {})
                        
                        ref = TickerReference(
                            ticker=ticker,
                            name=results.get('name', ticker),
                            market=results.get('market', 'Unknown'),
                            locale=results.get('locale', 'US'),
                            primary_exchange=results.get('primary_exchange', 'Unknown'),
                            type=results.get('type', 'Unknown'),
                            active=results.get('active', True),
                            currency_name=results.get('currency_name', 'USD'),
                            cik=results.get('cik'),
                            market_cap=results.get('market_cap'),
                            description=results.get('description'),
                            sic_description=results.get('sic_description'),
                            homepage_url=results.get('homepage_url'),
                            total_employees=results.get('total_employees'),
                            list_date=results.get('list_date')
                        )
                        
                        with self.reference_lock:
                            self.reference_data[ticker] = ref
                        
                        logger.info(f"Visualizer: Fetched reference data for {ticker}")
                    
                    else:
                        logger.warning(f"Visualizer: Reference data API returned {response.status} for {ticker}")
        
        except Exception as e:
            logger.error(f"Visualizer: Error fetching reference data for {ticker}: {e}")
    
    # ========================================================================
    # Corporate Actions
    # ========================================================================
    
    async def fetch_corporate_actions_loop(self):
        """Fetch corporate actions once at startup, then daily"""
        for ticker in self.tickers:
            await self.fetch_corporate_actions(ticker)
        
        while self.running:
            await asyncio.sleep(86400)  # 24 hours
            for ticker in self.tickers:
                await self.fetch_corporate_actions(ticker)
    
    async def fetch_corporate_actions(self, ticker: str):
        """Fetch dividends and splits for a ticker"""
        try:
            # Fetch dividends
            div_url = f"https://api.polygon.io/v3/reference/dividends?ticker={ticker}&limit=100&apiKey={self.api_key}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(div_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        results = data.get('results', [])
                        
                        for div in results:
                            action = CorporateAction(
                                ticker=ticker,
                                action_type='dividend',
                                ex_date=div.get('ex_dividend_date'),
                                declaration_date=div.get('declaration_date'),
                                pay_date=div.get('pay_date'),
                                cash_amount=div.get('cash_amount'),
                                currency=div.get('currency'),
                                frequency=div.get('frequency')
                            )
                            
                            with self.actions_lock:
                                # Avoid duplicates
                                if not any(a.ex_date == action.ex_date and a.action_type == 'dividend' 
                                          for a in self.corporate_actions[ticker]):
                                    self.corporate_actions[ticker].append(action)
            
            # Fetch splits
            split_url = f"https://api.polygon.io/v3/reference/splits?ticker={ticker}&limit=100&apiKey={self.api_key}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(split_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        results = data.get('results', [])
                        
                        for split in results:
                            action = CorporateAction(
                                ticker=ticker,
                                action_type='split',
                                ex_date=split.get('execution_date'),
                                split_from=split.get('split_from'),
                                split_to=split.get('split_to')
                            )
                            
                            with self.actions_lock:
                                if not any(a.ex_date == action.ex_date and a.action_type == 'split'
                                          for a in self.corporate_actions[ticker]):
                                    self.corporate_actions[ticker].append(action)
            
            logger.info(f"Visualizer: Fetched corporate actions for {ticker}")
        
        except Exception as e:
            logger.error(f"Visualizer: Error fetching corporate actions for {ticker}: {e}")
    
    # ========================================================================
    # News Feed
    # ========================================================================
    
    async def fetch_news_loop(self):
        """Fetch news periodically"""
        while self.running:
            try:
                for ticker in self.tickers:
                    await self.fetch_news(ticker)
                
                await asyncio.sleep(NEWS_UPDATE_INTERVAL)
            
            except Exception as e:
                logger.error(f"Visualizer: Error in news loop: {e}")
                await asyncio.sleep(NEWS_UPDATE_INTERVAL)
    
    async def fetch_news(self, ticker: str):
        """Fetch news for a ticker"""
        try:
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
                                timestamp=datetime.fromisoformat(
                                    article.get('published_utc').replace('Z', '+00:00')
                                ),
                                headline=article.get('title'),
                                author=article.get('author', 'Unknown'),
                                source=article.get('publisher', {}).get('name', 'Unknown'),
                                url=article.get('article_url'),
                                summary=article.get('description')
                            )
                            
                            with self.news_lock:
                                if not any(n.id == news_item.id for n in self.news_feed):
                                    self.news_feed.append(news_item)
        
        except Exception as e:
            logger.error(f"Visualizer: Error fetching news for {ticker}: {e}")
    
    # ========================================================================
    # Technical Indicators
    # ========================================================================
    
    async def fetch_indicators_loop(self):
        """Calculate indicators periodically"""
        while self.running:
            try:
                for ticker in self.tickers:
                    await self.calculate_indicators_client_side(ticker)
                
                await asyncio.sleep(INDICATORS_UPDATE_INTERVAL)
            
            except Exception as e:
                logger.error(f"Visualizer: Error in indicators loop: {e}")
                await asyncio.sleep(INDICATORS_UPDATE_INTERVAL)
    
    async def calculate_indicators_client_side(self, ticker: str):
        """Calculate technical indicators from candle data"""
        try:
            with self.candles_lock:
                candles = list(self.candles[ticker])
            
            if len(candles) < 50:
                return
            
            df = pd.DataFrame([c.to_dict() for c in candles])
            df['timestamp'] = pd.to_datetime(df['t'], unit='ms')
            df = df.sort_values('timestamp')
            df.set_index('timestamp', inplace=True)
            
            # Calculate indicators
            df['SMA_20'] = df['c'].rolling(window=20).mean()
            df['EMA_50'] = df['c'].ewm(span=50, adjust=False).mean()
            
            # RSI
            delta = df['c'].diff()
            gain = delta.where(delta > 0, 0).rolling(window=14).mean()
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
            
            # Store
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
        
        except Exception as e:
            logger.error(f"Visualizer: Error calculating indicators for {ticker}: {e}")
    
    # ========================================================================
    # Chart Rendering
    # ========================================================================
    
    async def render_charts_loop(self):
        """Render charts periodically to HTML"""
        while self.running:
            try:
                for ticker in self.tickers:
                    await self.render_ticker_chart(ticker)
                
                # Render summary page
                await self.render_summary_page()
                
                await asyncio.sleep(CHART_UPDATE_INTERVAL)
            
            except Exception as e:
                logger.error(f"Visualizer: Error rendering charts: {e}")
                await asyncio.sleep(CHART_UPDATE_INTERVAL)
    
    async def render_ticker_chart(self, ticker: str):
        """Render chart for a single ticker"""
        try:
            # Get data
            with self.candles_lock:
                candles = list(self.candles[ticker])
            
            with self.indicators_lock:
                indicators = dict(self.indicators.get(ticker, {}))
            
            with self.actions_lock:
                actions = list(self.corporate_actions[ticker])
            
            with self.reference_lock:
                ref = self.reference_data.get(ticker)
            
            if not candles:
                return
            
            # Separate backfill vs websocket candles
            backfill_candles = [c for c in candles if c.source == 'backfill']
            ws_candles = [c for c in candles if c.source == 'websocket']
            
            # Create figure
            fig = make_subplots(
                rows=3, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.03,
                row_heights=[0.5, 0.25, 0.25],
                subplot_titles=(
                    f'{ticker} - Candlesticks (Historical + Live)',
                    'RSI (14)',
                    'MACD (12, 26, 9)'
                )
            )
            
            # Add candlestick trace (all candles)
            timestamps = [datetime.fromtimestamp(c.timestamp / 1000) for c in candles]
            opens = [c.open for c in candles]
            highs = [c.high for c in candles]
            lows = [c.low for c in candles]
            closes = [c.close for c in candles]
            volumes = [c.volume for c in candles]
            
            fig.add_trace(go.Candlestick(
                x=timestamps,
                open=opens,
                high=highs,
                low=lows,
                close=closes,
                name='OHLC',
                increasing_line_color='#26a69a',
                decreasing_line_color='#ef5350'
            ), row=1, col=1)
            
            # Add indicators
            if 'SMA_20' in indicators and len(indicators['SMA_20']) > 0:
                ind_ts = indicators.get('timestamps', timestamps[-len(indicators['SMA_20']):])
                fig.add_trace(go.Scatter(
                    x=ind_ts,
                    y=indicators['SMA_20'],
                    name='SMA 20',
                    line=dict(color='#ffa726', width=1)
                ), row=1, col=1)
            
            if 'EMA_50' in indicators and len(indicators['EMA_50']) > 0:
                ind_ts = indicators.get('timestamps', timestamps[-len(indicators['EMA_50']):])
                fig.add_trace(go.Scatter(
                    x=ind_ts,
                    y=indicators['EMA_50'],
                    name='EMA 50',
                    line=dict(color='#42a5f5', width=1)
                ), row=1, col=1)
            
            # Add Bollinger Bands
            if all(k in indicators for k in ['BB_upper', 'BB_lower']):
                if len(indicators['BB_upper']) > 0:
                    ind_ts = indicators.get('timestamps', timestamps[-len(indicators['BB_upper']):])
                    fig.add_trace(go.Scatter(
                        x=ind_ts,
                        y=indicators['BB_upper'],
                        name='BB Upper',
                        line=dict(color='#ab47bc', width=1, dash='dash'),
                        showlegend=False
                    ), row=1, col=1)
                    fig.add_trace(go.Scatter(
                        x=ind_ts,
                        y=indicators['BB_lower'],
                        name='BB Lower',
                        line=dict(color='#ab47bc', width=1, dash='dash'),
                        fill='tonexty',
                        fillcolor='rgba(171, 71, 188, 0.1)'
                    ), row=1, col=1)
            
            # Add corporate action markers
            for action in actions:
                if action.ex_date:
                    try:
                        action_date = datetime.fromisoformat(action.ex_date)
                        fig.add_vline(
                            x=action_date.timestamp() * 1000,
                            line_dash="dash",
                            line_color="yellow" if action.action_type == 'dividend' else "orange",
                            annotation_text=action.get_label(),
                            row=1, col=1
                        )
                    except:
                        pass
            
            # RSI
            if 'RSI_14' in indicators and len(indicators['RSI_14']) > 0:
                ind_ts = indicators.get('timestamps', timestamps[-len(indicators['RSI_14']):])
                fig.add_trace(go.Scatter(
                    x=ind_ts,
                    y=indicators['RSI_14'],
                    name='RSI',
                    line=dict(color='#ffa726', width=2)
                ), row=2, col=1)
                fig.add_hline(y=70, line_dash="dash", line_color="red", row=2, col=1)
                fig.add_hline(y=30, line_dash="dash", line_color="green", row=2, col=1)
            
            # MACD
            if all(k in indicators for k in ['MACD', 'MACD_signal', 'MACD_hist']):
                if len(indicators['MACD']) > 0:
                    ind_ts = indicators.get('timestamps', timestamps[-len(indicators['MACD']):])
                    fig.add_trace(go.Scatter(
                        x=ind_ts,
                        y=indicators['MACD'],
                        name='MACD',
                        line=dict(color='#42a5f5', width=2)
                    ), row=3, col=1)
                    fig.add_trace(go.Scatter(
                        x=ind_ts,
                        y=indicators['MACD_signal'],
                        name='Signal',
                        line=dict(color='#ef5350', width=2)
                    ), row=3, col=1)
            
            # Layout
            fig.update_layout(
                template='plotly_dark',
                height=900,
                showlegend=True,
                hovermode='x unified',
                title=dict(
                    text=f"{ticker} - {ref.name if ref else ticker}<br>" +
                         f"<sub>{ref.primary_exchange if ref else ''} | " +
                         f"{len(backfill_candles)} backfill + {len(ws_candles)} live candles</sub>",
                    x=0.5,
                    xanchor='center'
                )
            )
            
            # Save
            output_file = self.output_dir / f"{ticker}_chart.html"
            fig.write_html(str(output_file))
            logger.debug(f"Visualizer: Rendered chart for {ticker}")
        
        except Exception as e:
            logger.error(f"Visualizer: Error rendering chart for {ticker}: {e}")
    
    async def render_summary_page(self):
        """Render summary page with metrics and links"""
        try:
            # Get metrics
            metrics = self.backfill_metrics
            cpu = psutil.cpu_percent()
            memory = psutil.virtual_memory().percent
            
            # Build HTML
            html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>ChronoX Backfill Visualizer</title>
    <meta http-equiv="refresh" content="{int(CHART_UPDATE_INTERVAL)}">
    <style>
        body {{
            background-color: #1e1e1e;
            color: #d4d4d4;
            font-family: 'Segoe UI', monospace;
            margin: 20px;
        }}
        h1 {{ color: #007acc; }}
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }}
        .card {{
            background: #252526;
            border-left: 4px solid #007acc;
            padding: 15px;
            border-radius: 8px;
        }}
        .card h3 {{ margin: 0 0 10px 0; font-size: 14px; color: #858585; }}
        .card .value {{ font-size: 28px; font-weight: bold; }}
        .tickers {{ margin: 20px 0; }}
        .ticker-link {{
            display: inline-block;
            background: #007acc;
            color: #fff;
            padding: 10px 20px;
            margin: 5px;
            border-radius: 4px;
            text-decoration: none;
        }}
        .ticker-link:hover {{ background: #0098ff; }}
        .news {{ margin: 20px 0; }}
        .news-item {{
            background: #2d2d30;
            padding: 12px;
            margin: 8px 0;
            border-left: 3px solid #4ec9b0;
            border-radius: 6px;
        }}
        .news-item h4 {{ margin: 0 0 5px 0; font-size: 14px; }}
        .news-item p {{ margin: 5px 0; font-size: 12px; color: #a0a0a0; }}
        .timestamp {{ color: #858585; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>ChronoX Backfill Visualizer</h1>
    <p class="timestamp">Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    
    <div class="metrics">
        <div class="card">
            <h3>CPU Usage</h3>
            <div class="value" style="color: {'#4ec9b0' if cpu < 70 else '#ffa726' if cpu < 90 else '#ef5350'}">
                {cpu:.1f}%
            </div>
        </div>
        <div class="card">
            <h3>Memory Usage</h3>
            <div class="value" style="color: {'#4ec9b0' if memory < 70 else '#ffa726' if memory < 90 else '#ef5350'}">
                {memory:.1f}%
            </div>
        </div>
        <div class="card">
            <h3>Download Queue</h3>
            <div class="value" style="color: #007acc">
                {metrics.get('download_qsize', 0)}/{metrics.get('download_maxsize', 0)}
            </div>
        </div>
        <div class="card">
            <h3>Process Queue</h3>
            <div class="value" style="color: #007acc">
                {metrics.get('process_qsize', 0)}/{metrics.get('process_maxsize', 0)}
            </div>
        </div>
        <div class="card">
            <h3>Files Processed</h3>
            <div class="value" style="color: #4ec9b0">
                {metrics.get('processed', 0)}
            </div>
            <p style="font-size: 12px; color: #858585; margin: 5px 0 0 0;">
                Downloaded: {metrics.get('downloaded', 0)} | Failed: {metrics.get('failed', 0)}
            </p>
        </div>
        <div class="card">
            <h3>Throughput</h3>
            <div class="value" style="color: #4ec9b0">
                {metrics.get('throughput', 0.0):.2f}
            </div>
            <p style="font-size: 12px; color: #858585; margin: 5px 0 0 0;">files/sec</p>
        </div>
        <div class="card">
            <h3>Active Tasks</h3>
            <div class="value" style="color: #42a5f5">
                {metrics.get('active_futures', 0)}
            </div>
        </div>
        <div class="card">
            <h3>WebSocket Status</h3>
            <div class="value" style="font-size: 18px; color: {'#4ec9b0' if self.ws_connected else '#ef5350'}">
                {'Connected' if self.ws_connected else 'Disconnected'}
            </div>
        </div>
    </div>
    
    <h2>Ticker Charts</h2>
    <div class="tickers">
"""
            
            # Add ticker links
            for ticker in self.tickers:
                with self.reference_lock:
                    ref = self.reference_data.get(ticker)
                
                name = ref.name if ref else ticker
                html += f'        <a href="{ticker}_chart.html" class="ticker-link">{ticker} - {name}</a>\n'
            
            html += """
    </div>
    
    <h2>Latest News</h2>
    <div class="news">
"""
            
            # Add news items
            with self.news_lock:
                news_items = sorted(list(self.news_feed), key=lambda x: x.timestamp, reverse=True)[:10]
            
            for item in news_items:
                html += f"""
        <div class="news-item">
            <h4>{item.headline}</h4>
            <p class="timestamp">{item.ticker} | {item.source} | {item.timestamp.strftime('%Y-%m-%d %H:%M')}</p>
            <p>{item.summary[:200] + '...' if item.summary and len(item.summary) > 200 else item.summary or ''}</p>
            <a href="{item.url}" target="_blank" style="color: #4ec9b0; text-decoration: none;">Read more →</a>
        </div>
"""
            
            html += """
    </div>
    
    <p style="text-align: center; color: #858585; margin-top: 40px;">
        Auto-refreshing every {} seconds
    </p>
</body>
</html>
""".format(int(CHART_UPDATE_INTERVAL))
            
            # Save
            output_file = self.output_dir / "index.html"
            with open(output_file, 'w') as f:
                f.write(html)
            
            logger.debug("Visualizer: Rendered summary page")
        
        except Exception as e:
            logger.error(f"Visualizer: Error rendering summary: {e}")


# ============================================================================
# Integration Helper
# ============================================================================

def attach_visualizer(
    backfill_metrics: Dict,
    tickers: List[str],
    api_key: str,
    output_dir: Path = OUTPUT_DIR
) -> IntegratedBackfillVisualizer:
    """
    Attach visualizer to backfill process.
    
    Call this from backfill_historical_data.py after starting workers.
    
    Args:
        backfill_metrics: Shared dict with queue/throughput metrics
        tickers: List of tickers being processed
        api_key: Polygon API key
        output_dir: Directory for HTML output
    
    Returns:
        IntegratedBackfillVisualizer instance (already started)
    
    Usage:
        if args.visualize:
            visualizer = attach_visualizer(
                backfill_metrics=shared_metrics,
                tickers=tickers,
                api_key=polygon_api_key
            )
            
            # In processing callback:
            visualizer.add_candle_from_backfill(ticker, candle_data)
            
            # On shutdown:
            visualizer.stop()
    """
    visualizer = IntegratedBackfillVisualizer(
        api_key=api_key,
        tickers=tickers,
        backfill_metrics=backfill_metrics,
        output_dir=output_dir
    )
    
    visualizer.start()
    
    logger.info(f"Visualizer attached. View at: {output_dir}/index.html")
    
    return visualizer
