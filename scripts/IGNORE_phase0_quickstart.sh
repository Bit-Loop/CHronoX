# DO NOT LOOK AT FILE CONTENTS
#!/bin/bash
# ChronoX Phase 0 Quick Start
# This script sets up the initial environment and starts the historical data backfill

set -e  # Exit on error

echo "🚀 ChronoX Phase 0 - Historical Data Acquisition & Database Setup"
echo "=================================================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Check if running from project root
if [ ! -f "FLOORPLAN_CHECKLIST.md" ]; then
    echo -e "${RED}Error: Please run this script from the ChronoX project root${NC}"
    exit 1
fi

echo -e "${YELLOW}Step 1: Checking prerequisites...${NC}"

# Check for Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is not installed. Please install Docker first.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker found${NC}"

# Check for Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed.${NC}"
    exit 1
fi
PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo -e "${GREEN}✓ Python ${PYTHON_VERSION} found${NC}"

echo ""
echo -e "${YELLOW}Step 2: Creating project structure...${NC}"

# Create directory structure
mkdir -p data/ingestion/polygon
mkdir -p data/preprocessing
mkdir -p data/storage
mkdir -p models/transformers
mkdir -p models/rl_agents
mkdir -p models/ensembles
mkdir -p training
mkdir -p inference
mkdir -p backtesting
mkdir -p trading
mkdir -p monitoring/exporters
mkdir -p pipelines/airflow_dags
mkdir -p docker
mkdir -p tests/unit
mkdir -p tests/integration
mkdir -p configs
mkdir -p notebooks
mkdir -p scripts
mkdir -p logs
mkdir -p data/flat_files

echo -e "${GREEN}✓ Directory structure created${NC}"

# Create __init__.py files for Python packages
touch data/__init__.py
touch data/ingestion/__init__.py
touch data/ingestion/polygon/__init__.py
touch data/preprocessing/__init__.py
touch data/storage/__init__.py
touch models/__init__.py
touch models/transformers/__init__.py
touch models/rl_agents/__init__.py
touch models/ensembles/__init__.py
touch tests/__init__.py
touch tests/unit/__init__.py
touch tests/integration/__init__.py

echo ""
echo -e "${YELLOW}Step 3: Setting up Python virtual environment...${NC}"

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo -e "${GREEN}✓ Virtual environment created${NC}"
else
    echo -e "${GREEN}✓ Virtual environment already exists${NC}"
fi

# Activate virtual environment
source venv/bin/activate

echo ""
echo -e "${YELLOW}Step 4: Installing Python dependencies...${NC}"

# Create minimal requirements.txt for Phase 0
cat > requirements.txt << 'EOF'
# Data acquisition & processing
requests>=2.31.0
aiohttp>=3.9.0
websocket-client>=1.6.0
tenacity>=8.2.0
ratelimit>=2.2.1

# Database
influxdb-client>=1.38.0

# Data manipulation
pandas>=2.1.0
numpy>=1.24.0

# Utilities
python-dotenv>=1.0.0
tqdm>=4.66.0

# Development & Testing
pytest>=7.4.0
pytest-cov>=4.1.0
black>=23.7.0
pylint>=2.17.0
flake8>=6.1.0
EOF

pip install --upgrade pip
pip install -r requirements.txt

echo -e "${GREEN}✓ Dependencies installed${NC}"

echo ""
echo -e "${YELLOW}Step 5: Starting InfluxDB with Docker...${NC}"

# Check if InfluxDB container already exists
if docker ps -a | grep -q chronox-influxdb; then
    echo "InfluxDB container already exists"
    if docker ps | grep -q chronox-influxdb; then
        echo -e "${GREEN}✓ InfluxDB is already running${NC}"
    else
        echo "Starting existing InfluxDB container..."
        docker start chronox-influxdb
        echo -e "${GREEN}✓ InfluxDB started${NC}"
    fi
else
    echo "Creating new InfluxDB container..."
    docker run -d \
        --name chronox-influxdb \
        -p 8086:8086 \
        -v chronox-influxdb-data:/var/lib/influxdb2 \
        -v chronox-influxdb-config:/etc/influxdb2 \
        -e DOCKER_INFLUXDB_INIT_MODE=setup \
        -e DOCKER_INFLUXDB_INIT_USERNAME=admin \
        -e DOCKER_INFLUXDB_INIT_PASSWORD=chronox-admin-2025 \
        -e DOCKER_INFLUXDB_INIT_ORG=chronox \
        -e DOCKER_INFLUXDB_INIT_BUCKET=market_data_5y \
        -e DOCKER_INFLUXDB_INIT_RETENTION=1825d \
        -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=chronox-super-secret-token-change-this \
        influxdb:2.7
    
    echo "Waiting for InfluxDB to be ready..."
    sleep 10
    echo -e "${GREEN}✓ InfluxDB container created and started${NC}"
fi

echo ""
echo -e "${YELLOW}Step 6: Creating .env file...${NC}"

if [ -f ".env" ]; then
    echo -e "${YELLOW}⚠ .env file already exists. Skipping creation.${NC}"
else
    cat > .env << 'EOF'
# Polygon.io API
POLYGON_API_KEY=your_polygon_api_key_here

# InfluxDB Configuration
INFLUX_URL=http://localhost:8086
INFLUX_TOKEN=chronox-super-secret-token-change-this
INFLUX_ORG=chronox
INFLUX_BUCKET_MARKET=market_data_5y
INFLUX_BUCKET_INDICATORS=indicators_1y
INFLUX_BUCKET_CORP=corporate_actions
INFLUX_BUCKET_REF=reference_data
INFLUX_BUCKET_NEWS=news_6mo
INFLUX_BUCKET_SNAPSHOTS=snapshots_30d

# Alpaca Trading API (for later phases)
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Application Settings
LOG_LEVEL=INFO
ENV=development
EOF
    echo -e "${GREEN}✓ .env file created${NC}"
    echo -e "${YELLOW}⚠ IMPORTANT: Edit .env and add your Polygon.io API key!${NC}"
fi

echo ""
echo -e "${YELLOW}Step 7: Creating .gitignore...${NC}"

cat > .gitignore << 'EOF'
# Environment variables
.env
.env.local

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
venv/
env/
ENV/
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# IDEs
.vscode/
.idea/
*.swp
*.swo
*~

# Data
data/flat_files/*.gz
data/flat_files/*.csv
data/flat_files/*.json
*.db
*.sqlite

# Logs
logs/
*.log

# Models
models/saved_models/
models/checkpoints/
*.h5
*.pt
*.pth
*.ckpt

# Jupyter
.ipynb_checkpoints/
*.ipynb

# OS
.DS_Store
Thumbs.db

# Testing
.pytest_cache/
.coverage
htmlcov/
*.cover

# Docker
docker-compose.override.yml
EOF

echo -e "${GREEN}✓ .gitignore created${NC}"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Phase 0 Quick Start Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo ""
echo "1. Edit .env file and add your Polygon.io API key:"
echo "   nano .env"
echo ""
echo "2. Access InfluxDB UI to create additional buckets:"
echo "   Open: http://localhost:8086"
echo "   Username: admin"
echo "   Password: chronox-admin-2025"
echo ""
echo "3. Create additional buckets in InfluxDB UI:"
echo "   - indicators_1y (retention: 365 days)"
echo "   - corporate_actions (retention: infinite)"
echo "   - reference_data (retention: infinite)"
echo "   - news_6mo (retention: 180 days)"
echo "   - snapshots_30d (retention: 30 days)"
echo ""
echo "4. Verify InfluxDB is working:"
echo "   docker logs chronox-influxdb"
echo ""
echo "5. Continue with Phase 0 implementation:"
echo "   - Implement Polygon.io API client (see FLOORPLAN_CHECKLIST.md)"
echo "   - Run historical data backfill"
echo ""
echo -e "${GREEN}Happy coding! 🚀${NC}"
echo ""
