.PHONY: help install dev finance test lint clean docker-finance docker-trading docker-down \
	desktop-install desktop desktop-icons desktop-backend \
	desktop-deb desktop-pacman desktop-appimage desktop-linux desktop-win desktop-mac

PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest
RUFF ?= .venv/bin/ruff
NPM ?= npm

help:
	@echo "ChronoX — Available Commands"
	@echo "========================================================"
	@echo "Web & Core:"
	@echo "  make dev              Create .venv and install app + dev deps"
	@echo "  make finance          Run ChronoX Finance Web App (port 8742)"
	@echo "  make test             Run full automated pytest suite"
	@echo "  make lint             Run ruff security & code quality checks"
	@echo "  make docker-finance   Start ChronoX Finance container (127.0.0.1:8742)"
	@echo "  make docker-trading   Start TimescaleDB + Redis trading stack"
	@echo "  make docker-down      Stop all Docker Compose containers"
	@echo ""
	@echo "Desktop App (Electron — Windows, Linux & macOS):"
	@echo "  make desktop-install  Install desktop/ Electron dependencies & icons"
	@echo "  make desktop          Launch ChronoX Finance Desktop App"
	@echo "  make desktop-icons    Regenerate cross-platform PNG, ICO & ICNS icons"
	@echo "  make desktop-backend  Bundle standalone zero-Python PyInstaller sidecar"
	@echo "  make desktop-deb      Build Debian/Ubuntu .deb package in desktop/dist/"
	@echo "  make desktop-pacman   Build Arch/CachyOS .pacman package in desktop/dist/"
	@echo "  make desktop-appimage Build universal Linux .AppImage in desktop/dist/"
	@echo "  make desktop-linux    Build .deb, .pacman & .AppImage packages"
	@echo "  make desktop-win      Build Windows NSIS + Portable .exe installers"
	@echo "  make desktop-mac      Build macOS .dmg installer"
	@echo ""
	@echo "Maintenance:"
	@echo "  make clean            Remove caches and build artifacts"

dev:
	uv venv .venv --python 3.12
	uv pip install --python $(PYTHON) -e ".[dev]"
	@echo "Ready! Copy .env.example to .env and configure your vault and providers."

finance:
	$(PYTHON) main.py --action finance

test:
	$(PYTEST) tests/ -v

lint:
	$(RUFF) check chronox_finance config main.py data/ingestion/polygon/client.py data/ingestion/polygon/__init__.py tests

docker-finance:
	docker compose up -d --build chronox-finance

docker-trading:
	docker compose --profile trading up -d

docker-down:
	docker compose down

desktop-install:
	cd desktop && $(NPM) install && $(NPM) run icons

desktop:
	cd desktop && $(NPM) start

desktop-icons:
	cd desktop && $(NPM) run icons

desktop-backend:
	cd desktop && $(NPM) run build:backend

desktop-deb:
	cd desktop && $(NPM) run dist:deb

desktop-pacman:
	cd desktop && $(NPM) run dist:pacman

desktop-appimage:
	cd desktop && $(NPM) run dist:appimage

desktop-linux:
	cd desktop && $(NPM) run dist:linux

desktop-win:
	cd desktop && $(NPM) run dist:win

desktop-mac:
	cd desktop && $(NPM) run dist:mac

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage dist build *.egg-info \
		desktop/dist desktop/build/backend desktop/build/pyinstaller-work
