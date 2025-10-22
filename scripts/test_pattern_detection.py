#!/usr/bin/env python3
"""
Test script for chart pattern detection integration.

Tests:
1. Pattern detection on historical data
2. Pattern storage in database
3. Pattern retrieval and validation

Usage:
    python scripts/test_pattern_detection.py --ticker AAPL --timeframe 1d
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from datetime import datetime, timedelta
from scripts.backfill_historical_data import (
    get_aggregated_bars,
    detect_chart_patterns,
    detect_and_store_patterns
)
from data.storage.timescale_writer import TimescaleWriter


def test_pattern_detection(ticker: str, timeframe: str):
    """Test pattern detection on fetched data"""
    print(f"\n{'='*60}")
    print(f"TEST 1: Pattern Detection for {ticker} at {timeframe}")
    print(f"{'='*60}")
    
    # Fetch data
    print(f"Fetching data for {ticker}...")
    df = get_aggregated_bars(
        ticker=ticker,
        interval=timeframe,
        with_indicators=True,
        limit=1000
    )
    
    if df.empty:
        print(f"❌ No data available for {ticker}")
        return False
    
    print(f"✓ Fetched {len(df)} bars")
    
    # Check for patterns in attrs
    if not hasattr(df, 'attrs') or 'chart_patterns' not in df.attrs:
        print(f"⚠️  No patterns detected (need ≥50 bars, have {len(df)})")
        return False
    
    patterns = df.attrs['chart_patterns']
    pattern_list = patterns.get('patterns', [])
    
    print(f"✓ Detected {len(pattern_list)} patterns")
    
    # Display patterns
    if pattern_list:
        print(f"\nDetected Patterns:")
        for i, pattern in enumerate(pattern_list, 1):
            ptype = pattern.get('type', 'Unknown')
            subtype = pattern.get('subtype', '')
            confidence = pattern.get('confidence', 0)
            start = pattern.get('start_index', 0)
            end = pattern.get('end_index', 0)
            
            print(f"  {i}. {ptype} ({subtype})")
            print(f"     Confidence: {confidence:.1%}")
            print(f"     Range: bars {start} - {end}")
    
    # Check support/resistance
    sr = patterns.get('support_resistance', {})
    if sr:
        print(f"\nSupport/Resistance Levels:")
        if 'support' in sr:
            print(f"  Support: {sr['support']}")
        if 'resistance' in sr:
            print(f"  Resistance: {sr['resistance']}")
    
    return True


def test_pattern_storage(ticker: str, timeframe: str):
    """Test pattern storage in database"""
    print(f"\n{'='*60}")
    print(f"TEST 2: Pattern Storage for {ticker} at {timeframe}")
    print(f"{'='*60}")
    
    # Detect and store patterns
    print(f"Detecting and storing patterns...")
    count = detect_and_store_patterns(
        ticker=ticker,
        interval=timeframe,
        start_time=datetime.now() - timedelta(days=365),
        end_time=datetime.now()
    )
    
    if count == 0:
        print(f"⚠️  No patterns stored")
        return False
    
    print(f"✓ Stored {count} patterns in database")
    return True


def test_pattern_retrieval(ticker: str, timeframe: str):
    """Test pattern retrieval from database"""
    print(f"\n{'='*60}")
    print(f"TEST 3: Pattern Retrieval for {ticker} at {timeframe}")
    print(f"{'='*60}")
    
    db = TimescaleWriter()
    
    try:
        # Query patterns
        query = """
            SELECT pattern_type, pattern_subtype, confidence, 
                   start_index, end_index, detected_at
            FROM chart_patterns
            WHERE ticker = %s AND timeframe = %s
            ORDER BY detected_at DESC, confidence DESC
            LIMIT 10
        """
        
        print(f"Querying stored patterns...")
        result = db.execute_query(query, (ticker, timeframe))
        
        if not result:
            print(f"⚠️  No patterns found in database")
            return False
        
        print(f"✓ Retrieved {len(result)} patterns")
        print(f"\nStored Patterns:")
        
        for i, row in enumerate(result, 1):
            ptype, subtype, confidence, start, end, detected = row
            print(f"  {i}. {ptype} ({subtype})")
            print(f"     Confidence: {confidence:.1%}")
            print(f"     Range: bars {start} - {end}")
            print(f"     Detected: {detected.strftime('%Y-%m-%d %H:%M:%S')}")
        
        return True
    
    except Exception as e:
        print(f"❌ Error querying patterns: {e}")
        return False
    
    finally:
        db.close()


def test_table_exists():
    """Test if chart_patterns table exists"""
    print(f"\n{'='*60}")
    print(f"TEST 0: Database Schema")
    print(f"{'='*60}")
    
    db = TimescaleWriter()
    
    try:
        query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'chart_patterns'
            )
        """
        
        result = db.execute_query(query)
        exists = result[0][0] if result else False
        
        if exists:
            print("✓ chart_patterns table exists")
            
            # Get table schema
            query = """
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = 'chart_patterns'
                ORDER BY ordinal_position
            """
            
            columns = db.execute_query(query)
            print(f"\nTable Schema:")
            for col_name, col_type in columns:
                print(f"  - {col_name}: {col_type}")
            
            return True
        else:
            print("⚠️  chart_patterns table does not exist")
            print("   (will be created on first pattern write)")
            return True
    
    except Exception as e:
        print(f"❌ Error checking schema: {e}")
        return False
    
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description='Test pattern detection integration')
    parser.add_argument('--ticker', type=str, default='AAPL', help='Ticker symbol (default: AAPL)')
    parser.add_argument('--timeframe', type=str, default='1d', help='Timeframe (default: 1d)')
    parser.add_argument('--skip-db', action='store_true', help='Skip database tests')
    
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"CHART PATTERN DETECTION - INTEGRATION TEST")
    print(f"{'='*60}")
    print(f"Ticker: {args.ticker}")
    print(f"Timeframe: {args.timeframe}")
    print(f"{'='*60}")
    
    results = []
    
    # Test 0: Check schema
    if not args.skip_db:
        results.append(('Database Schema', test_table_exists()))
    
    # Test 1: Pattern detection
    results.append(('Pattern Detection', test_pattern_detection(args.ticker, args.timeframe)))
    
    # Test 2: Pattern storage
    if not args.skip_db:
        results.append(('Pattern Storage', test_pattern_storage(args.ticker, args.timeframe)))
    
    # Test 3: Pattern retrieval
    if not args.skip_db:
        results.append(('Pattern Retrieval', test_pattern_retrieval(args.ticker, args.timeframe)))
    
    # Summary
    print(f"\n{'='*60}")
    print(f"TEST SUMMARY")
    print(f"{'='*60}")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")
    
    print(f"\n{passed}/{total} tests passed")
    
    if passed == total:
        print(f"✓ All tests passed!")
        return 0
    else:
        print(f"⚠️  Some tests failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
