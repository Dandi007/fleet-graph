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

verify: lint test conformance

clean:
	rm -rf .pytest_cache .ruff_cache dist build
