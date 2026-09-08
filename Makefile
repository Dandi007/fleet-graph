.PHONY: help sync lint fmt test verify conformance clean

help:
	@echo "sync    - install deps into .venv (uv)"
	@echo "lint    - ruff check + format check"
	@echo "fmt     - ruff format + autofix"
	@echo "test    - pytest"
	@echo "verify  - lint + test (the gate CI runs)"

sync:
	uv sync --frozen || uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

test:
	uv run pytest

conformance:
	uv run python scripts/check_supervisor_conformance.py
	uv run python scripts/check_work_report_conformance.py
	uv run python scripts/check_research_role_contracts.py

verify: lint test conformance

clean:
	rm -rf .pytest_cache .ruff_cache dist build

# Docker 测试使用独立 named volumes；只有网关与 GitHub 可以出网。
CANDIDATE ?= codex
CASE ?= single-repo
.PHONY: docker-build test-docker test-end-to-end test-docker-contracts
docker-build:
	python3 tests/e2e/run.py build --candidate "$(CANDIDATE)"

test-docker:
	python3 tests/e2e/run.py smoke --candidate "$(CANDIDATE)"

test-end-to-end:
	python3 tests/e2e/run.py e2e --candidate "$(CANDIDATE)" --case "$(CASE)"

test-docker-contracts:
	uv run python -m unittest discover -s tests/e2e/contract -p 'test_*.py'
	uv run python -m unittest discover -s tests/e2e/runner -p 'test_*.py'
	uv run python -m unittest discover -s tests/e2e/candidate -p 'test_*.py'
	uv run python -m unittest discover -s tests/e2e -p 'test_boundary.py'
