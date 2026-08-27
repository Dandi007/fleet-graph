.PHONY: help sync lint fmt test verify verify-core acceptance clean

help:
	@echo "sync    - install deps into .venv (uv)"
	@echo "lint    - ruff check + format check"
	@echo "fmt     - ruff format + autofix"
	@echo "test    - pytest"
	@echo "verify  - lint + test (the gate CI runs)"
	@echo "acceptance - capture user-session manager and verification evidence"

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

verify: acceptance

verify-core: lint test

acceptance:
	bash deploy/verify-user-session-bus.sh

clean:
	rm -rf .pytest_cache .ruff_cache dist build
