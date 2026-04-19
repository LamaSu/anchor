.PHONY: install dev test lint run clean

install:
	pip install -e ".[dev]"

dev:
	pip install -e ".[all]"

test:
	pytest

lint:
	ruff check anchord tests

run:
	python -m anchord

run-dev:
	ANCHOR_LOG_LEVEL=DEBUG python -m anchord

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache dist build *.egg-info

probe:
	@echo "GET /healthz"
	@curl -fsS http://localhost:3458/healthz && echo ""
	@echo "GET /missions"
	@curl -fsS http://localhost:3458/missions | head -20 && echo ""
