import os
import sys
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data.ingestion.polygon.client import PolygonClient  
from data.ingestion.polygon.aggregates import AggregatesClient

load_dotenv()

# Get just 1 week of daily data for AAPL
api_key = os.getenv('POLYGON_API_KEY')
polygon_client = PolygonClient(api_key=api_key)
agg_client = AggregatesClient(polygon_client=polygon_client)

end_date = datetime.now().strftime('%Y-%m-%d')
start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

print(f"Fetching AAPL daily bars from {start_date} to {end_date}...")
bars = agg_client.get_daily_bars(
    ticker="AAPL",
    start_date=start_date,
    end_date=end_date
)

if bars:
    print(f"Got {len(bars)} bars")
    
    # Insert into database
    conn = psycopg2.connect(
        host='127.0.0.1',
        port=5433,
        database='chronox',
        user='postgres',
        password='chronox_db_password'
    )
    
    values = []
    for bar in bars:
        timestamp = datetime.fromtimestamp(bar['t'] / 1000)
        values.append((
            timestamp,
            'AAPL',
            'daily',
            bar['o'],
            bar['h'],
            bar['l'],
            bar['c'],
            bar['v'],
            bar.get('vw'),
            bar.get('n')
        ))
    
    cursor = conn.cursor()
    execute_values(
        cursor,
        """
        INSERT INTO market_data 
        (time, ticker, timeframe, open, high, low, close, volume, vwap, transactions)
        VALUES %s
        ON CONFLICT (time, ticker, timeframe) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            vwap = EXCLUDED.vwap,
            transactions = EXCLUDED.transactions
        """,
        values
    )
    conn.commit()
    print(f"✅ Inserted {len(values)} bars")
    
    # Query back
    cursor.execute("""
        SELECT COUNT(*) FROM market_data WHERE ticker = 'AAPL'
    """)
    result = cursor.fetchone()
    count = result[0] if result else 0
    print(f"Total AAPL bars in database: {count}")
    
    cursor.close()
    conn.close()

print("Done!")
