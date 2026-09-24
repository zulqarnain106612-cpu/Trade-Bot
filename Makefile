.PHONY: help install run-api docker-build docker-up docker-down test lint

help:
	@echo "Targets:"
	@echo "  install       Install runtime deps into current Python env"
	@echo "  run-api       Run FastAPI server locally (python -m src.api)"
	@echo "  run-worker    Run headless trader loop locally (python -m src.workers)"
	@echo "  docker-build  Build the runtime image"
	@echo "  docker-up     Start timescaledb + api via docker compose"
	@echo "  docker-down   Stop compose stack"
	@echo "  test          Run pytest"
	@echo "  lint          Run ruff"

install:
	pip install -r requirements.txt
	pip install -e .

run-api:
	python -m src.api

run-worker:
	python -m src.workers

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

test:
	pytest -q

lint:
	ruff check src tests
