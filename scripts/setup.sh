#!/bin/bash
# ChronoX First-Time Setup Script

set -e

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║          ChronoX Trading Bot - First-Time Setup          ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check prerequisites
echo "Checking prerequisites..."

# Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ Python 3 not found${NC}"
    echo "  Please install Python 3.9 or higher"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo -e "${GREEN}✓${NC} Python ${PYTHON_VERSION} found"

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}✗ Docker not found${NC}"
    echo "  Please install Docker: https://docs.docker.com/get-docker/"
    exit 1
fi
echo -e "${GREEN}✓${NC} Docker found"

# Check Docker Compose
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null 2>&1; then
    echo -e "${YELLOW}⚠${NC} Docker Compose not found (optional)"
else
    echo -e "${GREEN}✓${NC} Docker Compose found"
fi

echo ""

# Create directory structure
echo "Creating directory structure..."
mkdir -p data/ingestion/polygon data/preprocessing data/storage
mkdir -p models/transformers models/rl_agents models/ensembles
mkdir -p training inference backtesting trading
mkdir -p monitoring/exporters pipelines/airflow_dags docker
mkdir -p tests/unit tests/integration configs notebooks scripts logs
mkdir -p data/flat_files docs
echo -e "${GREEN}✓${NC} Directories created"

# Create virtual environment
echo ""
echo "Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo -e "${GREEN}✓${NC} Virtual environment created"
else
    echo -e "${YELLOW}⚠${NC} Virtual environment already exists"
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo ""
echo "Installing Python dependencies..."
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt > /dev/null 2>&1
echo -e "${GREEN}✓${NC} Dependencies installed"

# Create .env file
echo ""
if [ ! -f ".env" ]; then
    echo "Creating .env file..."
    cp .env.example .env
    
    # Generate random tokens
    INFLUX_TOKEN=$(openssl rand -base64 32)
    INFLUX_PASSWORD=$(openssl rand -base64 16)
    AIRFLOW_SECRET=$(openssl rand -base64 32)
    
    # Update .env with generated values
    sed -i "s/your-super-secret-token/$INFLUX_TOKEN/" .env
    sed -i "s/your-secure-password/$INFLUX_PASSWORD/" .env
    
    echo -e "${GREEN}✓${NC} .env file created"
    echo ""
    echo -e "${YELLOW}⚠ IMPORTANT:${NC} Edit .env and add your POLYGON_API_KEY"
    echo "  Get your API key from: https://polygon.io"
else
    echo -e "${YELLOW}⚠${NC} .env file already exists (not overwriting)"
fi

# Start InfluxDB
echo ""
read -p "Start InfluxDB container now? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Starting InfluxDB..."
    
    # Stop existing container if running
    docker stop chronox-influxdb 2>/dev/null || true
    docker rm chronox-influxdb 2>/dev/null || true
    
    # Start new container
    docker run -d \
        --name chronox-influxdb \
        -p 8086:8086 \
        -v chronox-influxdb-data:/var/lib/influxdb2 \
        -v chronox-influxdb-config:/etc/influxdb2 \
        -e DOCKER_INFLUXDB_INIT_MODE=setup \
        -e DOCKER_INFLUXDB_INIT_USERNAME=admin \
        -e DOCKER_INFLUXDB_INIT_PASSWORD=$INFLUX_PASSWORD \
        -e DOCKER_INFLUXDB_INIT_ORG=chronox \
        -e DOCKER_INFLUXDB_INIT_BUCKET=market_data_5y \
        -e DOCKER_INFLUXDB_INIT_RETENTION=1825d \
        -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=$INFLUX_TOKEN \
        influxdb:2.7 > /dev/null 2>&1
    
    echo -e "${GREEN}✓${NC} InfluxDB started on http://localhost:8086"
    echo "  Username: admin"
    echo "  Password: (saved in .env)"
    echo "  Token: (saved in .env)"
    
    # Wait for InfluxDB to be ready
    echo ""
    echo "Waiting for InfluxDB to be ready..."
    for i in {1..30}; do
        if curl -s http://localhost:8086/health > /dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} InfluxDB is ready"
            break
        fi
        echo -n "."
        sleep 1
    done
    echo ""
fi

# Run component tests
echo ""
read -p "Run Phase 0 component tests? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Running tests..."
    python scripts/test_phase0.py
fi

# Summary
echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║                    Setup Complete!                        ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "Next Steps:"
echo ""
echo "1. ${YELLOW}Add your Polygon.io API key to .env:${NC}"
echo "   Edit .env and set POLYGON_API_KEY=your_key_here"
echo ""
echo "2. ${YELLOW}Activate the virtual environment:${NC}"
echo "   source venv/bin/activate"
echo ""
echo "3. ${YELLOW}Test the setup:${NC}"
echo "   python scripts/test_phase0.py"
echo ""
echo "4. ${YELLOW}Start data backfill:${NC}"
echo "   python scripts/backfill_historical_data.py --tickers AAPL,TSLA,NVDA"
echo ""
echo "5. ${YELLOW}Verify data quality:${NC}"
echo "   python scripts/verify_data.py"
echo ""
echo "6. ${YELLOW}Access InfluxDB UI:${NC}"
echo "   Open http://localhost:8086 in your browser"
echo ""
echo "For more commands, run: ${GREEN}make help${NC}"
echo ""
