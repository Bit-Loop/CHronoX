# ChronoX Architecture Refactor - Implementation Plan

## Phase 1: Core Abstractions (backfill_historical_data.py)

### 1.1 MarketDataSource Interface
- Abstract base class for data sources
- HistoricalDataSource (REST API, CSV)
- LiveDataSource (WebSocket streaming)
- Common iterator interface: `__iter__` and `__next__`

### 1.2 BarProcessor Chain
- IncrementalIndicatorProcessor (O(1) updates using deque)
- PatternDetectionProcessor
- FeatureEngineeringProcessor
- Each processor: `process(bar) -> enriched_bar`

### 1.3 MessageBus Abstraction
- MessageBus interface (publish/subscribe)
- RedisMessageBus (Streams + Pub/Sub)
- InMemoryMessageBus (testing/fallback)
- Kafka-ready interface design

### 1.4 DataIngestionEngine
- Single ingestion path for historical + live
- Idempotent upserts (INSERT ... ON CONFLICT DO UPDATE)
- Gap detection and auto-healing
- Retry logic with exponential backoff

### 1.5 Configuration
- TIMEFRAME_CONFIGS: seconds-based intervals
- SYMBOLS list (dynamic loading from DB)
- Redis connection params
- Incremental computation windows

## Phase 2: Event-Driven GUI (backfill_visualizer.py)

### 2.1 RedisSubscriberThread
- QThread that subscribes to Redis channels
- Emits Qt signals with bar data
- Pattern: `bars:{symbol}:{timeframe}`
- Thread-safe signal/slot connections

### 2.2 GUI Refactor
- Remove all polling mechanisms
- Remove ChartDataLoader (replaced by Redis subscriber)
- Initial load: TimescaleDB query
- Live updates: Redis Pub/Sub only
- Zero calculations in GUI

### 2.3 Chart Updates
- Append-only rendering (O(1))
- Throttling under high frequency
- Update only visible region
- Precomputed data only

## Phase 3: Integration

### 3.1 CLI Modes
- `--backfill`: Historical replay through unified pipeline
- `--live`: Real-time streaming mode
- `--mode=unified`: Backfill then seamless transition to live

### 3.2 Database Schema
- Wide hypertables: market_data_{timeframe}
- Columns: OHLCV + all indicators + pattern flags
- Indexes: (symbol, timestamp), symbol
- ON CONFLICT (symbol, timestamp) DO UPDATE

### 3.3 Performance
- Batch inserts during backfill (1000 rows/batch)
- Single inserts during live streaming
- Connection pooling
- Incremental indicator updates

## Implementation Checklist

- [ ] MarketDataSource abstraction
- [ ] IncrementalIndicatorProcessor with deque
- [ ] MessageBus interface + RedisMessageBus
- [ ] DataIngestionEngine unified pipeline
- [ ] Idempotent upsert SQL
- [ ] RedisSubscriberThread in GUI
- [ ] Remove polling from GUI
- [ ] Chart append-only updates
- [ ] Gap detection + healing
- [ ] Retry logic with backoff
- [ ] Config-driven timeframes
- [ ] CLI --backfill / --live modes
- [ ] Batch vs streaming writes
- [ ] Integration testing

## File Structure

All code inline in two files:
1. `backfill_historical_data.py`: Ingestion engine + processors + message bus
2. `backfill_visualizer.py`: Event-driven GUI with Redis subscriber
