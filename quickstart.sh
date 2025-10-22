#!/bin/bash
# ChronoX Quick Start Script

set -e

echo "🚀 ChronoX Quick Start"
echo "======================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}❌ Docker is not running. Please start Docker first.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Docker is running${NC}"

# Check if docker-compose exists
if ! command -v docker-compose &> /dev/null; then
    echo -e "${RED}❌ docker-compose not found. Please install docker-compose.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ docker-compose found${NC}"
echo ""

# Function to show progress
show_progress() {
    echo -e "${YELLOW}▶️  $1${NC}"
}

# Step 1: Build Docker images
show_progress "Step 1/5: Building Docker images (this may take 10-15 minutes)..."
echo ""

# Build base image
echo "Building base image..."
docker build -t chronox-base:latest -f docker/Dockerfile.base . || {
    echo -e "${RED}❌ Failed to build base image${NC}"
    exit 1
}

echo -e "${GREEN}✅ Base image built${NC}"
echo ""

# Build training image
echo "Building training image..."
docker build -t chronox-training:latest -f docker/Dockerfile.training . || {
    echo -e "${RED}❌ Failed to build training image${NC}"
    exit 1
}

echo -e "${GREEN}✅ Training image built${NC}"
echo ""

# Build inference image
echo "Building inference image..."
docker build -t chronox-inference:latest -f docker/Dockerfile.inference . || {
    echo -e "${RED}❌ Failed to build inference image${NC}"
    exit 1
}

echo -e "${GREEN}✅ Inference image built${NC}"
echo ""

# Step 2: Start core services
show_progress "Step 2/5: Starting core services (TimescaleDB, PostgreSQL, Redis)..."
docker-compose up -d timescaledb postgres redis

echo "Waiting for databases to be ready..."
sleep 15

echo -e "${GREEN}✅ Core services started${NC}"
echo ""

# Step 3: Initialize TimescaleDB
show_progress "Step 3/5: Initializing TimescaleDB..."

# Check if init script ran
docker exec chronox-timescaledb psql -U chronox -d chronox_timeseries -c "\dt market_data.*" || {
    echo "Running initialization script..."
    docker exec chronox-timescaledb psql -U chronox -d chronox_timeseries -f /docker-entrypoint-initdb.d/01_init.sql
}

echo -e "${GREEN}✅ TimescaleDB initialized${NC}"
echo ""

# Step 4: Start monitoring stack
show_progress "Step 4/5: Starting monitoring stack (Prometheus, Grafana)..."
docker-compose up -d prometheus grafana

echo "Waiting for monitoring services..."
sleep 10

echo -e "${GREEN}✅ Monitoring stack started${NC}"
echo ""

# Step 5: Start Airflow
show_progress "Step 5/5: Starting Airflow services..."
docker-compose up -d airflow-webserver airflow-scheduler airflow-worker

echo "Waiting for Airflow to initialize..."
sleep 15

echo -e "${GREEN}✅ Airflow started${NC}"
echo ""

# Check service health
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Service Status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

docker-compose ps

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 ChronoX is ready!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📍 Access Points:"
echo "  • Airflow:     http://localhost:8080 (admin/admin)"
echo "  • Grafana:     http://localhost:3000 (admin/chronox_grafana_2024)"
echo "  • Prometheus:  http://localhost:9090"
echo "  • TimescaleDB: localhost:5432 (chronox/chronox_db_pass_2024)"
echo ""
echo "🔧 Useful Commands:"
echo "  • View logs:        docker-compose logs -f [service]"
echo "  • Stop services:    docker-compose down"
echo "  • Restart:          docker-compose restart [service]"
echo "  • Run tests:        python build.py test"
echo ""
echo "📖 Next Steps:"
echo "  1. Open Airflow UI and enable the DAGs"
echo "  2. Check Grafana dashboards for system metrics"
echo "  3. View TimescaleDB data: psql -h localhost -U chronox -d chronox_timeseries"
echo ""
echo -e "${GREEN}Happy trading! 🚀${NC}"
echo ""
