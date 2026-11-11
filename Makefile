.PHONY: setup test demo serve eval lint
setup:
	uv sync --locked --extra dev --extra mcp
test:
	uv run pytest -q
lint:
	uv run ruff check src tests
demo:
	uv run agent-lab demo
serve:
	uv run agent-lab serve
eval:
	uv run agent-lab eval --report reports/eval.json
