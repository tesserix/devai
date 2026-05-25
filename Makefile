.PHONY: install dev lint type-check test run serve clean registry-seeds registry-seeds-check

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

format:
	ruff check --fix src/ tests/
	ruff format src/ tests/

type-check:
	mypy src/devai/

test:
	pytest tests/ -v --tb=short

test-cov:
	pytest tests/ -v --cov=devai --cov-report=term-missing

run:
	python -m devai run

serve:
	python -m devai serve

clean:
	rm -rf dist/ build/ *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +

# Regenerate architecture/registry-seeds/{skills,agents,prompts}/*.yaml
# from every specialization YAML. Idempotent.
registry-seeds:
	python3 scripts/generate_registry_seeds.py

# CI guard — exits 1 if any seed file is out of date relative to the
# source specializations. Catches drift before it reaches main.
registry-seeds-check:
	python3 scripts/generate_registry_seeds.py --check
