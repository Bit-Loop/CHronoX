import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import psycopg2
import logging

from data.ingestion.polygon.client import PolygonClient
from data.ingestion.polygon.aggregates import AggregatesClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# Initialize API client
polygon_client = PolygonClient(api_key=os.getenv('POLYGON_API_KEY'))
api_client = AggregatesClient(polygon_client=polygon_client)

# Test with 1 month of NVDA 1-minute data
end_date = datetime.now()
start_date = end_date - timedelta(days=30)

logger.info(f"Testing 1-month fetch: {start_date.date()} to {end_date.date()}")

# Fetch 1-minute bars
bars = api_client.get_minute_bars(
    ticker="NVDA",
    start_date=start_date.strftime('%Y-%m-%d'),
    end_date=end_date.strftime('%Y-%m-%d'),
    adjusted=True
)

logger.info(f"✅ Got {len(bars)} 1-minute bars for NVDA")

# Insert into database
if bars:
    conn = psycopg2.connect(
        host='127.0.0.1',
        port=5433,
        database='chronox',
        user='postgres',
        password='chronox_db_password'
    )
    
    cursor = conn.cursor()
    
    values = []
    for bar in bars:
        timestamp = datetime.fromtimestamp(bar['t'] / 1000)
        values.append((
            timestamp, 'NVDA', '1min',
            bar['o'], bar['h'], bar['l'], bar['c'],
            bar['v'], bar.get('vw'), bar.get('n')
        ))
    
    cursor.executemany("""
        INSERT INTO market_data (time, ticker, timeframe, open, high, low, close, volume, vwap, transactions)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (time, ticker, timeframe) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            vwap = EXCLUDED.vwap,
            transactions = EXCLUDED.transactions
    """, values)
    
    conn.commit()
    logger.info(f"✅ Inserted {len(values)} bars into database")
    
    cursor.close()
    conn.close()

logger.info("Test complete!")
