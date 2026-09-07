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
