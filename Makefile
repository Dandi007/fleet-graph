.PHONY: help sync lint fmt test verify verify-minimal conformance clean

help:
	@echo "sync    - install deps into .venv (uv)"
	@echo "lint    - ruff check + format check"
	@echo "fmt     - ruff format + autofix"
	@echo "test    - pytest"
	@echo "verify  - lint + test (the gate CI runs)"
	@echo "verify-minimal - ruff check (minimal paths) + pytest -k minimal"

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

# minimal 套件的快速门：ruff 只查 minimal 相关路径 + 只跑 -k minimal 的测试；
# 不替代 `verify`（全量 lint/test/conformance 仍是 CI 门槛）。
verify-minimal:
	uv run ruff check src/fleet_graph/minimal tests/test_minimal_*.py
	uv run pytest tests -q -k minimal

clean:
	rm -rf .pytest_cache .ruff_cache dist build
