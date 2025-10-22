#!/usr/bin/env python3
"""
ChronoX System Test - Verify Everything is Working

This script tests all major components:
1. Database connectivity
2. Data availability
3. Main.py initialization
4. Configuration loading
"""

import os
import sys
from datetime import datetime

def print_header(title):
    print("\n" + "="*70)
    print(f"  {title}")
    print("="*70)

def test_environment():
    """Test environment variables"""
    print_header("1. Testing Environment Variables")
    
    required_vars = [
        "POLYGON_API_KEY",
        "TIMESCALE_HOST",
        "TIMESCALE_PORT",
        "TIMESCALE_USER",
        "TIMESCALE_PASSWORD"
    ]
    
    all_set = True
    for var in required_vars:
        value = os.getenv(var)
        if value:
            # Mask sensitive values
            display = value if var not in ["POLYGON_API_KEY", "TIMESCALE_PASSWORD"] else f"{value[:8]}..."
            print(f"✅ {var:25} = {display}")
        else:
            print(f"❌ {var:25} = NOT SET")
            all_set = False
    
    return all_set

def test_database():
    """Test database connection"""
    print_header("2. Testing Database Connection")
    
    try:
        import psycopg2
        
        # TimescaleDB connection
        host = os.getenv("TIMESCALE_HOST", "localhost")
        port = os.getenv("TIMESCALE_PORT", "5433")
        user = os.getenv("TIMESCALE_USER", "postgres")
        password = os.getenv("TIMESCALE_PASSWORD", "chronox_db_password")
        database = os.getenv("TIMESCALE_DB", "chronox")
        
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database
        )
        
        cursor = conn.cursor()
        
        # Get database version
        cursor.execute("SELECT version();")
        version_result = cursor.fetchone()
        if version_result:
            version = version_result[0]
            print(f"✅ Database Connected")
            print(f"   Version: {version.split(',')[0]}")
        
        # Get TimescaleDB version
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname='timescaledb';")
        ts_result = cursor.fetchone()
        if ts_result:
            ts_version = ts_result[0]
            print(f"   TimescaleDB: {ts_version}")
        
        # Get table info
        cursor.execute("""
            SELECT 
                COUNT(*) as total_bars,
                COUNT(DISTINCT ticker) as unique_tickers,
                MIN(time) as earliest,
                MAX(time) as latest
            FROM market_data;
        """)
        
        data_result = cursor.fetchone()
        if data_result:
            total, tickers, earliest, latest = data_result
            print(f"\n✅ Market Data Table")
            print(f"   Total bars: {total:,}")
            print(f"   Unique tickers: {tickers}")
            print(f"   Date range: {earliest.date()} to {latest.date()}")
        
        cursor.close()
        conn.close()
        
        return True
        
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return False

def test_data_availability():
    """Test available market data"""
    print_header("3. Testing Data Availability")
    
    try:
        import psycopg2
        
        host = os.getenv("TIMESCALE_HOST", "localhost")
        port = os.getenv("TIMESCALE_PORT", "5433")
        user = os.getenv("TIMESCALE_USER", "postgres")
        password = os.getenv("TIMESCALE_PASSWORD", "chronox_db_password")
        database = os.getenv("TIMESCALE_DB", "chronox")
        
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database
        )
        
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                ticker,
                timeframe,
                COUNT(*) as bars,
                MIN(time)::date as start_date,
                MAX(time)::date as end_date
            FROM market_data
            GROUP BY ticker, timeframe
            ORDER BY ticker, timeframe;
        """)
        
        rows = cursor.fetchall()
        
        print(f"\n{'Ticker':<10} {'Timeframe':<10} {'Bars':>10} {'Start':>12} {'End':>12}")
        print("-" * 70)
        
        for ticker, timeframe, bars, start, end in rows:
            print(f"{ticker:<10} {timeframe:<10} {bars:>10,} {str(start):>12} {str(end):>12}")
        
        cursor.close()
        conn.close()
        
        return len(rows) > 0
        
    except Exception as e:
        print(f"❌ Data check failed: {e}")
        return False

def test_main_py():
    """Test main.py initialization"""
    print_header("4. Testing main.py Initialization")
    
    try:
        # Import main components
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        
        from config.config_manager import ConfigManager
        from data.ingestion.polygon.client import PolygonClient
        from data.storage.timescale_writer import TimescaleWriter
        
        print("✅ Config Manager imported")
        
        # Test config loading
        config = ConfigManager()
        print("✅ Configuration loaded")
        
        # Test Polygon client
        api_key = os.getenv("POLYGON_API_KEY")
        if api_key:
            polygon_client = PolygonClient(api_key)
            print("✅ Polygon.io client initialized")
        else:
            print("⚠️  Polygon.io client skipped (no API key)")
        
        # Test database writer
        db_writer = TimescaleWriter()
        if db_writer.test_connection():
            print("✅ TimescaleDB writer initialized")
        else:
            print("❌ TimescaleDB writer failed")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ main.py initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_gpu():
    """Test GPU availability"""
    print_header("5. Testing GPU/CUDA")
    
    try:
        import torch
        
        print(f"✅ PyTorch version: {torch.__version__}")
        print(f"✅ CUDA available: {torch.cuda.is_available()}")
        
        if torch.cuda.is_available():
            print(f"✅ CUDA version: {torch.version.cuda}")
            print(f"✅ GPU count: {torch.cuda.device_count()}")
            print(f"✅ GPU name: {torch.cuda.get_device_name(0)}")
            print(f"✅ GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")
        
        return True
        
    except Exception as e:
        print(f"⚠️  GPU test failed: {e}")
        return False

def main():
    """Run all tests"""
    print("\n" + "="*70)
    print("  ChronoX System Test Suite")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("="*70)
    
    results = {
        "Environment": test_environment(),
        "Database": test_database(),
        "Data": test_data_availability(),
        "Main.py": test_main_py(),
        "GPU": test_gpu()
    }
    
    print_header("Test Results Summary")
    
    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {test_name}")
    
    all_passed = all(results.values())
    
    print("\n" + "="*70)
    if all_passed:
        print("  🎉 All tests passed! ChronoX is ready to use.")
    else:
        print("  ⚠️  Some tests failed. Please check the output above.")
    print("="*70 + "\n")
    
    return all_passed

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
