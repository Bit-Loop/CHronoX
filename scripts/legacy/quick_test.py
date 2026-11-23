#!/usr/bin/env python3
"""Quick test of pattern detection"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

print("1. Testing imports...")
try:
    from scripts.backfill_historical_data import get_aggregated_bars
    print("✓ Import successful")
except Exception as e:
    print(f"✗ Import failed: {e}")
    sys.exit(1)

print("\n2. Testing data fetch...")
try:
    df = get_aggregated_bars('AMD', '1d', limit=500)
    print(f"✓ Fetched {len(df)} bars for AMD")
except Exception as e:
    print(f"✗ Data fetch failed: {e}")
    sys.exit(1)

print("\n3. Testing pattern detection...")
try:
    if hasattr(df, 'attrs') and 'chart_patterns' in df.attrs:
        patterns = df.attrs['chart_patterns']
        pattern_list = patterns.get('patterns', [])
        print(f"✓ Detected {len(pattern_list)} patterns")
        
        for i, p in enumerate(pattern_list[:3], 1):
            print(f"   {i}. {p.get('type')} ({p.get('subtype')}) - {p.get('confidence', 0):.1%}")
    else:
        print("⚠️  No patterns in attrs")
except Exception as e:
    print(f"✗ Pattern check failed: {e}")
    import traceback
    traceback.print_exc()

print("\n✓ All tests completed!")
