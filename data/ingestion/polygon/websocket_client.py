"""
Polygon.io WebSocket Client

Handles real-time streaming data via WebSocket connection.
Note: Starter plan has ~15 minute delay on data.
"""

import json
import websocket
import threading
from typing import Callable, Dict, List, Optional
import time
import logging

logger = logging.getLogger(__name__)


class PolygonWebSocketClient:
    """
    WebSocket client for real-time (delayed) streaming data from Polygon.io.
    
    Supports:
        - Delayed aggregates (A.*)
        - Trades (T.*)
        - Quotes (Q.*)
    """
    
    WS_URL = "wss://socket.polygon.io/stocks"
    
    def __init__(self, api_key: str):
        """
        Initialize WebSocket client.
        
        Args:
            api_key: Polygon.io API key
        """
        if not api_key:
            raise ValueError("API key is required")
        
        self.api_key = api_key
        self.ws = None
        self.is_connected = False
        self.is_authenticated = False
        self.subscriptions = []
        self.callbacks = {}
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.should_reconnect = True
        
        logger.info("WebSocket client initialized")
    
    def on_open(self, ws):
        """Called when WebSocket connection opens"""
        logger.info("WebSocket connection opened")
        self.is_connected = True
        self.reconnect_attempts = 0
        
        # Authenticate
        auth_message = json.dumps({
            "action": "auth",
            "params": self.api_key
        })
        ws.send(auth_message)
        logger.debug("Authentication message sent")
    
    def on_message(self, ws, message):
        """Called when WebSocket receives a message"""
        try:
            data = json.loads(message)
            
            # Handle array of events
            if isinstance(data, list):
                for item in data:
                    self._process_event(item)
            
            # Handle status/control messages
            elif isinstance(data, dict):
                status = data.get('status')
                
                if status == 'auth_success':
                    logger.info("✓ WebSocket authenticated successfully")
                    self.is_authenticated = True
                    
                    # Resubscribe to previous subscriptions
                    if self.subscriptions:
                        logger.info(f"Resubscribing to {len(self.subscriptions)} channels")
                        self.subscribe(self.subscriptions)
                
                elif status == 'auth_failed':
                    logger.error("✗ WebSocket authentication failed")
                    self.is_authenticated = False
                
                elif status == 'success':
                    message = data.get('message', '')
                    logger.info(f"Subscription successful: {message}")
                
                elif status == 'error':
                    error = data.get('message', 'Unknown error')
                    logger.error(f"WebSocket error: {error}")
                
                else:
                    # Unknown message type
                    logger.debug(f"Received message: {data}")
        
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON message: {e}")
        except Exception as e:
            logger.error(f"Error processing message: {str(e)}")
    
    def _process_event(self, event: Dict):
        """Process individual event from stream"""
        event_type = event.get('ev')
        
        if not event_type:
            logger.warning(f"Event without type: {event}")
            return
        
        # Call registered callback if exists
        if event_type in self.callbacks:
            try:
                self.callbacks[event_type](event)
            except Exception as e:
                logger.error(f"Error in callback for {event_type}: {str(e)}")
        else:
            # Log unhandled events
            logger.debug(f"Unhandled event type '{event_type}': {event}")
    
    def on_error(self, ws, error):
        """Called when WebSocket encounters an error"""
        logger.error(f"WebSocket error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        """Called when WebSocket connection closes"""
        logger.warning(f"WebSocket connection closed: {close_status_code} - {close_msg}")
        self.is_connected = False
        self.is_authenticated = False
        
        # Attempt reconnection if enabled
        if self.should_reconnect and self.reconnect_attempts < self.max_reconnect_attempts:
            self.reconnect_attempts += 1
            wait_time = min(2 ** self.reconnect_attempts, 60)  # Exponential backoff, max 60s
            logger.info(f"Reconnecting in {wait_time} seconds... (Attempt {self.reconnect_attempts}/{self.max_reconnect_attempts})")
            time.sleep(wait_time)
            self.connect()
        elif self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error("Max reconnection attempts reached. Manual reconnection required.")
    
    def connect(self):
        """Establish WebSocket connection"""
        logger.info(f"Connecting to {self.WS_URL}")
        
        self.ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        
        # Run in separate daemon thread
        wst = threading.Thread(target=self.ws.run_forever)
        wst.daemon = True
        wst.start()
        
        # Wait for connection
        timeout = 10
        start_time = time.time()
        while not self.is_authenticated and (time.time() - start_time) < timeout:
            time.sleep(0.1)
        
        if self.is_authenticated:
            logger.info("✓ WebSocket connected and authenticated")
        else:
            logger.warning("✗ WebSocket connection timeout or authentication failed")
    
    def subscribe(self, channels: List[str]):
        """
        Subscribe to WebSocket channels.
        
        Args:
            channels: List of channels to subscribe to
                     Examples:
                     - 'A.AAPL' - Delayed aggregates for AAPL
                     - 'T.AAPL' - Trades for AAPL
                     - 'Q.AAPL' - Quotes for AAPL
                     - 'A.*' - All aggregates (use with caution)
        """
        if not isinstance(channels, list):
            channels = [channels]
        
        # Add to subscription list (avoid duplicates)
        for channel in channels:
            if channel not in self.subscriptions:
                self.subscriptions.append(channel)
        
        # Only subscribe if connected and authenticated
        if self.is_connected and self.is_authenticated and self.ws:
            subscribe_message = json.dumps({
                "action": "subscribe",
                "params": ",".join(channels)
            })
            self.ws.send(subscribe_message)
            logger.info(f"Subscribed to: {', '.join(channels)}")
        else:
            logger.warning("Not connected/authenticated. Subscriptions will be applied after connection.")
    
    def unsubscribe(self, channels: List[str]):
        """
        Unsubscribe from WebSocket channels.
        
        Args:
            channels: List of channels to unsubscribe from
        """
        if not isinstance(channels, list):
            channels = [channels]
        
        # Remove from subscription list
        for channel in channels:
            if channel in self.subscriptions:
                self.subscriptions.remove(channel)
        
        # Only unsubscribe if connected
        if self.is_connected and self.ws:
            unsubscribe_message = json.dumps({
                "action": "unsubscribe",
                "params": ",".join(channels)
            })
            self.ws.send(unsubscribe_message)
            logger.info(f"Unsubscribed from: {', '.join(channels)}")
    
    def register_callback(self, event_type: str, callback: Callable[[Dict], None]):
        """
        Register callback function for specific event types.
        
        Args:
            event_type: Event type code:
                       - 'A' for aggregates
                       - 'T' for trades
                       - 'Q' for quotes
                       - 'AM' for aggregate minute
            callback: Function to call when event is received.
                     Must accept one argument (event dict).
        
        Example:
            def on_aggregate(event):
                print(f"Ticker: {event['sym']}, Close: {event['c']}")
            
            ws_client.register_callback('A', on_aggregate)
        """
        self.callbacks[event_type] = callback
        logger.info(f"Registered callback for event type '{event_type}'")
    
    def unregister_callback(self, event_type: str):
        """
        Remove callback for event type.
        
        Args:
            event_type: Event type code to unregister
        """
        if event_type in self.callbacks:
            del self.callbacks[event_type]
            logger.info(f"Unregistered callback for event type '{event_type}'")
    
    def close(self):
        """Close WebSocket connection"""
        logger.info("Closing WebSocket connection")
        self.should_reconnect = False  # Disable auto-reconnect
        
        if self.ws:
            self.ws.close()
        
        self.is_connected = False
        self.is_authenticated = False
    
    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()


if __name__ == "__main__":
    # Test the WebSocket client
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    api_key = os.getenv("POLYGON_API_KEY")
    
    if not api_key:
        print("ERROR: POLYGON_API_KEY not found in .env file")
        exit(1)
    
    # Configure logging to see what's happening
    logging.basicConfig(level=logging.INFO)
    
    # Create WebSocket client
    ws_client = PolygonWebSocketClient(api_key)
    
    # Define callback for aggregate data
    def on_aggregate(event):
        ticker = event.get('sym', 'N/A')
        open_price = event.get('o', 0)
        close_price = event.get('c', 0)
        volume = event.get('v', 0)
        timestamp = event.get('e', 0)  # Epoch milliseconds
        
        print(f"[AGGREGATE] {ticker}: O={open_price:.2f}, C={close_price:.2f}, V={volume:,}, T={timestamp}")
    
    # Define callback for trades
    def on_trade(event):
        ticker = event.get('sym', 'N/A')
        price = event.get('p', 0)
        size = event.get('s', 0)
        print(f"[TRADE] {ticker}: Price={price:.2f}, Size={size}")
    
    # Register callbacks
    ws_client.register_callback('A', on_aggregate)
    ws_client.register_callback('AM', on_aggregate)  # Minute aggregates
    ws_client.register_callback('T', on_trade)
    
    # Connect and subscribe
    print("\n=== Connecting to Polygon.io WebSocket ===")
    ws_client.connect()
    
    # Wait a moment for authentication
    time.sleep(2)
    
    if ws_client.is_authenticated:
        print("\n=== Subscribing to tickers ===")
        ws_client.subscribe(['A.AAPL', 'A.TSLA', 'A.NVDA'])
        
        print("\n=== Streaming data (press Ctrl+C to stop) ===")
        try:
            # Keep the script running
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\nStopping stream...")
    else:
        print("\n✗ Failed to authenticate. Check your API key.")
    
    # Cleanup
    ws_client.close()
    print("✓ WebSocket client test complete!")
