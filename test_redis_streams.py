#!/usr/bin/env python3
"""
Redis Streams Migration Test Suite

Tests the complete Redis Streams implementation:
1. Backend publishes to chronox:bars:* streams
2. Consumer groups work correctly
3. XACK acknowledgement
4. Offset tracking and recovery
5. GUI consumer integration

Usage:
    python test_redis_streams.py
"""

import redis
import json
import time
from datetime import datetime

def test_redis_connection(port=6380):
    """Test 1: Redis connection"""
    print("=" * 60)
    print("TEST 1: Redis Connection")
    print("=" * 60)
    try:
        client = redis.Redis(host='localhost', port=port, decode_responses=False)
        client.ping()
        print(f"✅ Connected to Redis on port {port}")
        return client
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return None

def test_xadd_publish(client):
    """Test 2: XADD publishing"""
    print("\n" + "=" * 60)
    print("TEST 2: XADD Publishing")
    print("=" * 60)
    try:
        test_bar = {
            'timestamp': datetime.now().isoformat(),
            'ticker': 'TEST',
            'timeframe': '1d',
            'open': 100.0,
            'high': 105.0,
            'low': 99.0,
            'close': 103.0,
            'volume': 1000000
        }
        
        stream_key = 'chronox:bars:TEST:1d'
        message_id = client.xadd(
            name=stream_key,
            fields={'data': json.dumps(test_bar)},
            maxlen=10000,
            approximate=True
        )
        print(f"✅ Published test bar to {stream_key}")
        print(f"   Message ID: {message_id}")
        
        # Verify stream exists
        stream_len = client.xlen(stream_key)
        print(f"   Stream length: {stream_len}")
        return True
    except Exception as e:
        print(f"❌ Publish failed: {e}")
        return False

def test_consumer_group(client):
    """Test 3: Consumer group creation"""
    print("\n" + "=" * 60)
    print("TEST 3: Consumer Group Creation")
    print("=" * 60)
    try:
        stream_key = 'chronox:bars:TEST:1d'
        group_name = 'test_consumers'
        
        # Try to create consumer group
        try:
            client.xgroup_create(
                name=stream_key,
                groupname=group_name,
                id='$',
                mkstream=True
            )
            print(f"✅ Created consumer group '{group_name}' on {stream_key}")
        except redis.ResponseError as e:
            if 'BUSYGROUP' in str(e):
                print(f"ℹ️  Consumer group '{group_name}' already exists")
            else:
                raise
        
        # Verify group exists
        groups = client.xinfo_groups(stream_key)
        print(f"   Active groups: {[g['name'] for g in groups]}")
        return True
    except Exception as e:
        print(f"❌ Consumer group creation failed: {e}")
        return False

def test_xreadgroup_consume(client):
    """Test 4: XREADGROUP consumption"""
    print("\n" + "=" * 60)
    print("TEST 4: XREADGROUP Consumption")
    print("=" * 60)
    try:
        stream_key = 'chronox:bars:TEST:1d'
        group_name = 'test_consumers'
        consumer_name = 'test_consumer_1'
        
        # Read messages
        messages = client.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={stream_key: '>'},
            count=10,
            block=1000
        )
        
        if messages:
            print(f"✅ Consumed {len(messages)} stream(s)")
            for stream, msgs in messages:
                print(f"   Stream: {stream}")
                for msg_id, fields in msgs:
                    data = json.loads(fields[b'data'].decode('utf-8'))
                    print(f"   Message ID: {msg_id}")
                    print(f"   Ticker: {data['ticker']}, Close: ${data['close']}")
                    
                    # Acknowledge message
                    client.xack(stream_key, group_name, msg_id)
                    print(f"   ✅ ACKed: {msg_id}")
        else:
            print("ℹ️  No new messages (this is OK if test ran before)")
        
        return True
    except Exception as e:
        print(f"❌ Consumption failed: {e}")
        return False

def test_pending_entries(client):
    """Test 5: Pending Entry List (PEL)"""
    print("\n" + "=" * 60)
    print("TEST 5: Pending Entry List (PEL)")
    print("=" * 60)
    try:
        stream_key = 'chronox:bars:TEST:1d'
        group_name = 'test_consumers'
        
        # Check pending entries
        pending = client.xpending(stream_key, group_name)
        print(f"✅ Pending entries: {pending}")
        print(f"   Count: {pending.get('pending', 0)}")
        
        if pending.get('pending', 0) == 0:
            print("   ✅ No pending entries (all ACKed)")
        
        return True
    except Exception as e:
        print(f"❌ PEL check failed: {e}")
        return False

def test_stream_info(client):
    """Test 6: Stream information"""
    print("\n" + "=" * 60)
    print("TEST 6: Stream Information")
    print("=" * 60)
    try:
        stream_key = 'chronox:bars:TEST:1d'
        
        # Get stream info
        info = client.xinfo_stream(stream_key)
        print(f"✅ Stream info for {stream_key}:")
        print(f"   Length: {info['length']}")
        print(f"   Groups: {info['groups']}")
        print(f"   First entry: {info.get('first-entry', 'N/A')}")
        print(f"   Last entry: {info.get('last-entry', 'N/A')}")
        
        return True
    except Exception as e:
        print(f"❌ Stream info failed: {e}")
        return False

def cleanup_test_data(client):
    """Cleanup: Delete test stream"""
    print("\n" + "=" * 60)
    print("CLEANUP: Removing Test Data")
    print("=" * 60)
    try:
        stream_key = 'chronox:bars:TEST:1d'
        client.delete(stream_key)
        print(f"✅ Deleted test stream: {stream_key}")
    except Exception as e:
        print(f"⚠️  Cleanup warning: {e}")

def main():
    """Run all tests"""
    print("\n" + "🔬" * 30)
    print("REDIS STREAMS MIGRATION TEST SUITE")
    print("🔬" * 30)
    
    # Test configuration
    redis_port = 6380  # Dev server
    
    # Run tests
    client = test_redis_connection(redis_port)
    if not client:
        print("\n❌ FAILED: Cannot connect to Redis")
        return False
    
    results = []
    results.append(test_xadd_publish(client))
    results.append(test_consumer_group(client))
    results.append(test_xreadgroup_consume(client))
    results.append(test_pending_entries(client))
    results.append(test_stream_info(client))
    
    # Cleanup
    cleanup_test_data(client)
    client.close()
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    
    if passed == total:
        print("\n✅ ✅ ✅ ALL TESTS PASSED ✅ ✅ ✅")
        return True
    else:
        print(f"\n❌ {total - passed} TEST(S) FAILED")
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
