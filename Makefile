# axion-wizard development tasks.
#
# Everything goes through `scripts/bootstrap.sh`, which is what knows how to
# build the environment (uv if present, venv+pip otherwise). That way there is
# a single definition of "how this gets installed" and the Makefile cannot
# drift away from it.
#
# On Windows without make, the equivalent is `.\scripts\bootstrap.ps1` with the
# same flags (-Check, -NoRun).

PYTHON := .venv/bin/python
BOOTSTRAP := ./scripts/bootstrap.sh

.DEFAULT_GOAL := help
.PHONY: help setup check test lint typecheck fmt run doctor build clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Build the environment and dependencies (idempotent)
	@$(BOOTSTRAP) --no-run

check: ## Build the environment and run lint + types + tests
	@$(BOOTSTRAP) --check --no-run

test: setup ## Tests with coverage
	@$(PYTHON) -m pytest -q --cov --cov-report=term-missing

lint: setup ## Lint (ruff)
	@$(PYTHON) -m ruff check .

typecheck: setup ## Type checking (mypy)
	@$(PYTHON) -m mypy src

fmt: setup ## Fix whatever ruff can fix on its own
	@$(PYTHON) -m ruff check --fix .
	@$(PYTHON) -m ruff format .

run: setup ## Start the wizard (make run ARGS="doctor")
	@$(PYTHON) -m axion_wizard $(ARGS)

doctor: setup ## Re-validate an already deployed stack
	@$(PYTHON) -m axion_wizard doctor

build: setup ## Package the binary with PyInstaller
	@./build/build.sh

clean: ## Remove the environment, caches and build outputs
	@rm -rf .venv build/work dist .pytest_cache .mypy_cache .ruff_cache .coverage
	@find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@echo "Clean."
