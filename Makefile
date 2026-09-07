.PHONY: sync lint fmt test verify

sync:
	uv sync --frozen

lint:
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts

fmt:
	uv run ruff check --fix src tests scripts
	uv run ruff format src tests scripts

test:
	uv run pytest

verify: lint test
	python3 -m compileall -q src

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
	uv run python -m unittest discover -s tests/e2e -p 'test_*.py'
