.PHONY: help install test lint format clean docker-up docker-down backfill verify

# Default target
help:
	@echo "ChronoX Trading Bot - Available Commands"
	@echo "========================================"
	@echo "install          Install dependencies"
	@echo "test             Run all tests"
	@echo "test-phase0      Run Phase 0 component tests"
	@echo "lint             Run code linters"
	@echo "format           Format code with black"
	@echo "clean            Clean temporary files"
	@echo "docker-up        Start InfluxDB container"
	@echo "docker-down      Stop InfluxDB container"
	@echo "docker-logs      View InfluxDB logs"
	@echo "backfill-test    Backfill test tickers (AAPL,TSLA,NVDA)"
	@echo "backfill-top50   Backfill top 50 tickers"
	@echo "verify           Verify data quality"
	@echo "influx-ui        Open InfluxDB UI in browser"

# Installation
install:
	pip install -r requirements.txt
	@echo "✓ Dependencies installed"

install-dev:
	pip install -r requirements.txt
	pip install pytest pytest-cov black pylint flake8 mypy
	@echo "✓ Dev dependencies installed"

# Testing
test:
	pytest tests/ -v

test-phase0:
	python scripts/test_phase0.py

test-coverage:
	pytest tests/ --cov=. --cov-report=html
	@echo "✓ Coverage report generated in htmlcov/"

# Code Quality
lint:
	pylint data/ scripts/ --ignore=tests
	flake8 data/ scripts/ --max-line-length=120

format:
	black data/ scripts/ tests/ --line-length=100
	isort data/ scripts/ tests/

type-check:
	mypy data/ scripts/ --ignore-missing-imports

# Cleaning
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
	rm -rf htmlcov
	rm -rf .coverage
	@echo "✓ Cleaned temporary files"

# Docker Management
docker-up:
	docker run -d \
		--name chronox-influxdb \
		-p 8086:8086 \
		-v chronox-influxdb-data:/var/lib/influxdb2 \
		-v chronox-influxdb-config:/etc/influxdb2 \
		-e DOCKER_INFLUXDB_INIT_MODE=setup \
		-e DOCKER_INFLUXDB_INIT_USERNAME=admin \
		-e DOCKER_INFLUXDB_INIT_PASSWORD=$$(openssl rand -base64 32) \
		-e DOCKER_INFLUXDB_INIT_ORG=chronox \
		-e DOCKER_INFLUXDB_INIT_BUCKET=market_data_5y \
		-e DOCKER_INFLUXDB_INIT_RETENTION=1825d \
		-e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=$$(openssl rand -base64 32) \
		influxdb:2.7
	@echo "✓ InfluxDB started on http://localhost:8086"
	@echo "⚠ Save the admin token from container logs!"
	@docker logs chronox-influxdb 2>&1 | grep -A 5 "token"

docker-down:
	docker stop chronox-influxdb
	docker rm chronox-influxdb
	@echo "✓ InfluxDB stopped"

docker-restart:
	$(MAKE) docker-down
	$(MAKE) docker-up

docker-logs:
	docker logs -f chronox-influxdb

docker-clean:
	docker stop chronox-influxdb 2>/dev/null || true
	docker rm chronox-influxdb 2>/dev/null || true
	docker volume rm chronox-influxdb-data 2>/dev/null || true
	docker volume rm chronox-influxdb-config 2>/dev/null || true
	@echo "✓ InfluxDB and volumes removed"

# Data Operations
backfill-test:
	python scripts/backfill_historical_data.py --tickers AAPL,TSLA,NVDA
	@echo "✓ Test backfill complete"

backfill-top50:
	python scripts/backfill_historical_data.py --all --limit 50
	@echo "✓ Top 50 tickers backfilled"

backfill-full:
	@echo "⚠ WARNING: This will take several hours and use significant bandwidth"
	@read -p "Continue? [y/N] " -n 1 -r; \
	echo; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		python scripts/backfill_historical_data.py --all --limit 500; \
	fi

verify:
	python scripts/verify_data.py

# Utilities
influx-ui:
	@echo "Opening InfluxDB UI in browser..."
	@python -m webbrowser http://localhost:8086

env-check:
	@echo "Checking environment variables..."
	@python -c "import os; from dotenv import load_dotenv; load_dotenv(); \
		print('POLYGON_API_KEY:', '✓' if os.getenv('POLYGON_API_KEY') else '✗ Missing'); \
		print('INFLUX_URL:', os.getenv('INFLUX_URL', 'http://localhost:8086')); \
		print('INFLUX_TOKEN:', '✓' if os.getenv('INFLUX_TOKEN') else '✗ Missing'); \
		print('INFLUX_ORG:', os.getenv('INFLUX_ORG', 'chronox'));"

logs:
	tail -f logs/*.log

# Development
dev-setup:
	$(MAKE) install-dev
	cp .env.example .env
	@echo "✓ Dev environment set up"
	@echo "⚠ Edit .env and add your API keys"

quick-start:
	@echo "ChronoX Quick Start"
	@echo "==================="
	@echo "1. Installing dependencies..."
	$(MAKE) install
	@echo "2. Starting InfluxDB..."
	$(MAKE) docker-up
	@echo "3. Testing components..."
	sleep 5
	$(MAKE) test-phase0
	@echo ""
	@echo "✓ Quick start complete!"
	@echo "Next steps:"
	@echo "  1. Edit .env and add your POLYGON_API_KEY"
	@echo "  2. Run: make backfill-test"
	@echo "  3. Run: make verify"
