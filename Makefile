.PHONY: help sync lint fmt test verify clean

help:
	@echo "sync    - install deps into .venv (uv)"
	@echo "lint    - ruff check + format check"
	@echo "fmt     - ruff format + autofix"
	@echo "test    - pytest in an isolated user-systemd session"
	@echo "verify  - lint + test in an isolated user-systemd session"

sync:
	uv sync --frozen || uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

test:
	bash deploy/verify-user-systemd.sh uv run pytest

verify: lint test

clean:
	rm -rf .pytest_cache .ruff_cache dist build
