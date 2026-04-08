.PHONY: check lint typecheck test format serve dashboard

check: lint typecheck test

lint:
	uv run ruff check src tests

typecheck:
	uv run pyright

test:
	uv run pytest tests/ -v

format:
	uv run ruff format src tests

serve:
	uv run context-analyzer-tool serve

dashboard:
	uv run context-analyzer-tool dashboard
