#!/bin/bash
# ChronoX Database Startup Script
# Uses sudo for Docker commands if needed

set -e

echo "🚀 Starting ChronoX TimescaleDB..."
echo ""

# Check if docker command works without sudo
if docker ps &> /dev/null; then
    echo "✅ Docker permissions OK"
    USE_SUDO=""
else
    echo "⚠️  Docker requires sudo (you're not in docker group yet)"
    echo "   To fix permanently: sudo usermod -aG docker $USER && newgrp docker"
    USE_SUDO="sudo"
fi

echo ""
echo "📦 Starting TimescaleDB container on port 5433..."
$USE_SUDO docker-compose up -d timescaledb

echo ""
echo "⏳ Waiting for database to be ready..."
sleep 5

# Check if container is running
if $USE_SUDO docker ps | grep -q chronox-timescaledb; then
    echo "✅ Container is running"
    
    # Wait for PostgreSQL to accept connections
    echo "⏳ Waiting for PostgreSQL to initialize..."
    for i in {1..30}; do
        if $USE_SUDO docker exec chronox-timescaledb pg_isready -U postgres -d chronox &> /dev/null; then
            echo "✅ Database is ready!"
            echo ""
            echo "📊 Connection details:"
            echo "   Host: localhost"
            echo "   Port: 5433"
            echo "   Database: chronox"
            echo "   User: postgres"
            echo "   Password: chronox_db_password"
            echo ""
            echo "🎯 Next steps:"
            echo "   1. Test connection: .venv/bin/python scripts/verify_implementation.py"
            echo "   2. Run backfill: .venv/bin/python main.py --action backfill --tickers AAPL,TSLA,NVDA --skip-minute"
            exit 0
        fi
        echo -n "."
        sleep 1
    done
    
    echo ""
    echo "⚠️  Database took too long to start. Check logs:"
    echo "   $USE_SUDO docker-compose logs timescaledb"
    exit 1
else
    echo "❌ Container failed to start. Check logs:"
    echo "   $USE_SUDO docker-compose logs timescaledb"
    exit 1
fi
